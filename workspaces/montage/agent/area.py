"""Рабочая область монтажа для агента (docs/WORKSPACE_CONTRACT.md, разделы 2, 3, 6): песочница путей и навыки.

Корень области: папка единиц хозяина аренды (`jobs/<персонаж>`). Шаблон, ресурсы персонажа и навыки лежат вне неё, поэтому
песочница SDK дополнена точками монтирования: первый сегмент пути `template`, `resources`, `skills` ведёт в свою папку
(`layout.template`, `layout.character`, `layout.skills`), а проверки (`..`, симлинки наружу, скрытое, запись только в
writable) те же. Навыки отдаются только инструментом `skills`; через `files` они не видны.
"""
from __future__ import annotations

import re
from pathlib import Path

from gits_sdk import Sandbox, ToolError, WorkspaceTools
from gits_sdk.protocol import WorkspaceSkill
from gits_sdk.resources import resource_path
from gits_sdk.workspace_tools import _denied, _offset, _read_file, _str_arg

from layout import Layout

# где что лежит относительно корня области (папка единиц хозяина); те же значения идут в манифест (`workspace.area`)
AREA_ROOT = "jobs"
WRITABLE = ["*/plan", "*/work/src/custom", "*/work/public/design"]
PLAN_FILES = ("idea.md", "story.md", "plan.json")
PLAN_JSON = "plan.json"
READONLY = ["template/demo/plan.*.json", "resources", "character", "*/sources", "*/transcript", "*/renders", "*/qa", "*/work/public"]
HIDDEN = ["node_modules", ".git", ".claude"]
SKILLS_MOUNT = "skills"
SKILL_DESCRIPTION_MAX = 300


class MontageSandbox(Sandbox):
    """Песочница SDK с точками монтирования (имя первого сегмента -> настоящая папка вне корня)."""

    def __init__(self, root, mounts: dict[str, Path], **kw):
        super().__init__(root, **kw)
        self.mounts = {name: Path(path).resolve() for name, path in mounts.items()}

    def _mount(self, parts: tuple[str, ...]) -> Path | None:
        return self.mounts.get(parts[0]) if parts else None

    def can_read(self, parts: tuple[str, ...]) -> bool:
        if parts and parts[0] in ("character", "resources"):
            return resource_path("/".join(parts[1:]), home=self.mounts[parts[0]]) is not None
        return super().can_read(parts)

    def _real(self, parts: tuple[str, ...], rel, *, write: bool, listing: bool) -> Path:
        base = self._mount(parts)
        if base is None:
            return super()._real(parts, rel, write=write, listing=listing)
        path = base.joinpath(*parts[1:])
        try:
            inside = path.resolve().relative_to(base).parts
        except (OSError, ValueError, RuntimeError):
            raise _denied(f"путь «{rel}» выходит за область") from None
        if self._hidden(inside):
            raise _denied(f"путь «{rel}» недоступен")
        if inside != parts[1:]:                                # симлинк внутри точки монтирования: цель тоже должна быть разрешена
            full = (parts[0], *inside)
            ok = self.can_write(full) if write else (self.can_read(full) or (listing and self.is_waypoint(full)))
            if not ok:
                raise _denied(f"путь «{rel}» ведёт за пределы области")
        return path

    def rel(self, path: Path) -> str:
        for name, base in self.mounts.items():
            try:
                return "/".join((name, *Path(path).relative_to(base).parts))
            except ValueError:
                continue
        return super().rel(path)

    def can_write(self, parts: tuple[str, ...]) -> bool:
        """План и собственные компоненты и дизайн конкретной единицы."""
        plan = len(parts) == 3 and parts[1].casefold() == "plan" and parts[2] in PLAN_FILES
        custom = len(parts) >= 5 and parts[1:4] in (("work", "src", "custom"), ("work", "public", "design"))
        return (plan or custom) and super().can_write(parts)

    def mount_root(self, name: str) -> Path | None:
        return self.mounts.get(name)


class MontageTools(WorkspaceTools):
    """Стандартные инструменты окружения (`skills`, `files`, `run`) поверх песочницы с точками монтирования."""

    def __init__(self, *a, plan_validator=None, **kw):
        super().__init__(*a, **kw)
        self.plan_validator = plan_validator                   # (id единицы, текст plan.json) -> список ошибок

    def _plan_target(self, args: dict) -> tuple[str, Path] | None:
        rel = args.get("path")
        if not isinstance(rel, str) or self.plan_validator is None:
            return None
        parts = self.sandbox.parts(rel)
        if len(parts) == 3 and parts[1] == "plan" and parts[2] == PLAN_JSON and self.sandbox.can_write(parts):
            return parts[0], self.sandbox.resolve(rel, write=True)
        return None

    def _guarded_plan_write(self, args: dict, op):
        """Запись монтажного листа с проверкой схемы: неверный лист откатывается, ошибки возвращаются списком."""
        target = self._plan_target(args)
        if target is None:
            return op(args)
        jid, path = target
        old = path.read_bytes() if path.is_file() else None
        out = op(args)
        try:
            problems = self.plan_validator(jid, path.read_text(encoding="utf-8"))
        except Exception as e:
            problems = [f"проверка не выполнена: {e}"]
        if problems:
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old)
            shown = problems[:20]
            lines = "\n".join(f"- {p[:150]}" + ("…" if len(p) > 150 else "") for p in shown)
            remaining = len(problems) - len(shown)
            tail = f"\nЕщё ошибок: {remaining}. Исправь показанные поля и повтори запись." if remaining else ""
            raise ToolError(
                f"монтажный лист {jid}/plan/plan.json не принят (файл не записан), ошибок {len(problems)}:\n"
                f"{lines}{tail}\nПримеры: template/demo/plan.horizontal.json и template/demo/plan.vertical.json.",
                code="invalid",
            )
        return out

    def _op_write(self, args: dict) -> dict:
        return self._guarded_plan_write(args, super()._op_write)

    def _op_replace(self, args: dict) -> dict:
        return self._guarded_plan_write(args, super()._op_replace)

    async def _skills(self, args: dict) -> dict:
        name = _str_arg(args, "name", required=False)
        if not name:
            return {"ok": True, "result": [{"name": s.name, "description": s.description} for s in self.skill_list]}
        skill = next((s for s in self.skill_list if s.name == name), None)
        if skill is None:
            raise self._unknown_skill(name)
        file = _str_arg(args, "file", required=False) or "SKILL.md"
        parts = tuple(p for p in skill.path.replace("\\", "/").split("/") if p)
        mount = self.sandbox.mount_root(parts[0]) if parts else None
        skill_root = (mount.joinpath(*parts[1:]) if mount is not None else self.sandbox.root.joinpath(*parts)).resolve()
        if mount is not None:
            try:
                skill_root.relative_to(mount)
            except ValueError:
                raise _denied(f"навык «{name}» лежит вне области") from None
        target = Sandbox(skill_root, readonly=["."], hidden=self.sandbox.hidden).resolve(file)
        if not target.is_file():
            raise ToolError(f"в навыке «{name}» нет файла «{file}»", code="unknown")
        out = _read_file(target, file, self.read_limit, _offset(args))
        out["skill"] = name
        return out

    def _op_list(self, args: dict) -> dict:
        out = super()._op_list(args)
        if out["path"] == ".":                                  # точки монтирования видны в корне как обычные каталоги
            have = {e["name"] for e in out["entries"]}
            virtual = [{"name": n, "type": "dir", "writable": False} for n in self.sandbox.mounts
                       if n != SKILLS_MOUNT and n not in have
                       and (self.sandbox.can_read((n,)) or self.sandbox.is_waypoint((n,)))]
            out["entries"] = virtual + out["entries"]
        return out


# ---------------------------------------------------------------------------------------------------- навыки

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def skill_description(skill_md: Path) -> str:
    """Описание навыка из шапки `description:` его SKILL.md (одна строка; длинное обрезается)."""
    try:
        head = skill_md.read_text(encoding="utf-8")[:4000]
    except OSError:
        return ""
    m = _FRONT.match(head)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "description":
            text = " ".join(value.strip().strip("\"'").split())
            return text if len(text) <= SKILL_DESCRIPTION_MAX else text[:SKILL_DESCRIPTION_MAX - 1].rstrip() + "…"
    return ""


def discover_skills(skills_dir: Path) -> list[WorkspaceSkill]:
    """Навыки модуля: папки `.claude/skills/<имя>` с `SKILL.md` (имя, описание из шапки, путь `skills/<имя>` в области)."""
    out: list[WorkspaceSkill] = []
    if not Path(skills_dir).is_dir():
        return out
    for folder in sorted(Path(skills_dir).iterdir()):
        md = folder / "SKILL.md"
        if folder.is_dir() and md.is_file() and re.fullmatch(r"[a-z0-9_-]{1,64}", folder.name):
            out.append(WorkspaceSkill(folder.name, skill_description(md) or folder.name, f"{SKILLS_MOUNT}/{folder.name}"))
    return out


def make_sandbox(layout: Layout) -> MontageSandbox:
    """Песочница агента для хозяина аренды: корень это его папка единиц, рядом шаблон, ресурсы персонажа и навыки."""
    layout.jobs.mkdir(parents=True, exist_ok=True)
    return MontageSandbox(layout.jobs, {"template": layout.template, "resources": layout.character,
                                       "character": layout.character.parent, SKILLS_MOUNT: layout.skills},
                          writable=WRITABLE, readonly=READONLY, hidden=HIDDEN)
