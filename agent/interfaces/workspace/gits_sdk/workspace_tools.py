"""Помощники окружений (docs/WORKSPACE_CONTRACT.md, разделы 3 и 6): песочница путей и стандартные инструменты workspace.

`Sandbox` проверяет пути рабочей области (нормализация, `..`, симлинки наружу, скрытые каталоги, выход за `area`).
`WorkspaceTools` даёт обработчики инструментов `skills`, `files`, `run` и их описания для манифеста (`declare_tools`).
Сам запуск процессов сюда не входит: окружение передаёт функцию-исполнитель, оболочка не запускается.

Обработчики имеют вид `async (args: dict) -> {"ok": True, ...}`; отказы идут через `ToolError` с кодами
`denied` (вне area или commands), `unknown` (нет такого), `busy`, `unavailable`, `invalid`.
Подключение к клиенту: `WorkspaceTools(...).attach(client)`; риск инструментов `auto`, а необратимое (`delete`, команда с
`irreversible`) отдаётся ядру через `danger_check` и `tool.check` (ядро спрашивает согласие).
"""
from __future__ import annotations

import difflib
import fnmatch
import inspect
import logging
import mimetypes
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .protocol import ToolError, WorkspaceArea, WorkspaceCommand, WorkspaceSkill

READ_LIMIT = 32 * 1024            # сколько байт файла возвращает read за раз
WRITE_LIMIT = 4 * 1024 * 1024     # самая большая запись одним вызовом
LIST_LIMIT = 500
RUN_ARGS_MAX, RUN_ARG_LEN_MAX = 50, 2000
FILE_OPS = ("list", "read", "write", "replace", "delete", "move")

log = logging.getLogger("gits_sdk.workspace_tools")
ACTION_TEXT_MAX = 300
ACTION_TARGET_MAX = 200

Executor = Callable[[WorkspaceCommand, list, bool, Path], Any]   # (команда, аргументы, фон, рабочий каталог) -> результат или job_id


def _denied(message: str) -> ToolError:
    return ToolError(message, code="denied")


def _match(parts: tuple[str, ...], pattern: str, *, anywhere: bool = False) -> bool:
    """Путь (по сегментам) подпадает под шаблон: шаблон совпадает с началом пути (`jobs/*/plan` покрывает всё внутри).
    Шаблон из одного сегмента без `/` с `anywhere` совпадает с сегментом на любой глубине (`node_modules`)."""
    pp = tuple(p for p in pattern.replace("\\", "/").split("/") if p and p != ".")
    if not pp:
        return True                                   # шаблон «.» покрывает весь корень
    if anywhere and len(pp) == 1:
        return any(fnmatch.fnmatchcase(p.casefold(), pp[0].casefold()) for p in parts)
    return len(parts) >= len(pp) and all(fnmatch.fnmatchcase(parts[i].casefold(), pp[i].casefold()) for i in range(len(pp)))


def _is_ancestor(parts: tuple[str, ...], pattern: str) -> bool:
    """Путь лежит выше шаблона (по нему можно дойти до разрешённого): `jobs` для `jobs/*/plan`."""
    pp = tuple(p for p in pattern.replace("\\", "/").split("/") if p and p != ".")
    return len(parts) < len(pp) and all(fnmatch.fnmatchcase(parts[i].casefold(), pp[i].casefold()) for i in range(len(parts)))


class Sandbox:
    """Песочница путей рабочей области.

    `root` корень (все пути относительны ему); `writable` и `readonly` шаблоны путей (`*` внутри сегмента, совпадение с
    началом пути); `hidden` скрытые имена каталогов и файлов (одно имя действует на любой глубине, путь со `/` от корня).
    Читать можно writable и readonly, писать только в writable. Если ни writable, ни readonly не заданы, весь корень
    только для чтения. Всё вне этого и скрытое недоступно (`denied`)."""

    def __init__(self, root: str | Path, writable: Iterable[str] = (), readonly: Iterable[str] = (),
                 hidden: Iterable[str] = ()):
        self.root = Path(root).resolve()
        self.writable = [str(x) for x in writable]
        self.readonly = [str(x) for x in readonly]
        self.hidden = [str(x) for x in hidden]
        if not self.writable and not self.readonly:
            self.readonly = ["."]

    @classmethod
    def from_area(cls, base: str | Path, area: WorkspaceArea | None) -> Sandbox:
        """Песочница по полю `workspace.area` манифеста: корень области = `base / area.root`."""
        if area is None:
            return cls(base)
        root = Path(base) / area.root if area.root else Path(base)
        return cls(root, area.writable, area.readonly, area.hidden)

    # -- разбор пути

    def parts(self, rel: Any) -> tuple[str, ...]:
        """Нормализовать относительный путь в сегменты. Абсолютные пути, диск, `..` и NUL отклоняются."""
        if rel is None:
            rel = "."
        if not isinstance(rel, str):
            raise ToolError("путь должен быть строкой", code="invalid")
        if "\0" in rel:
            raise _denied("недопустимый символ в пути")
        norm = rel.replace("\\", "/")
        if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":") or os.path.isabs(rel):
            raise _denied(f"путь «{rel}» должен быть относительным")
        parts = tuple(p for p in norm.split("/") if p and p != ".")
        if ".." in parts:
            raise _denied(f"«..» в пути «{rel}» запрещено")
        if self._hidden(parts):
            raise _denied(f"путь «{rel}» недоступен")
        return parts

    def _hidden(self, parts: tuple[str, ...]) -> bool:
        return any(_match(parts, h, anywhere=True) for h in self.hidden)

    def can_read(self, parts: tuple[str, ...]) -> bool:
        return any(_match(parts, p) for p in (*self.writable, *self.readonly))

    def can_write(self, parts: tuple[str, ...]) -> bool:
        return any(_match(parts, p) for p in self.writable)

    def is_waypoint(self, parts: tuple[str, ...]) -> bool:
        """Каталог выше разрешённых путей: его можно перечислить, чтобы дойти до области."""
        return any(_is_ancestor(parts, p) for p in (*self.writable, *self.readonly))

    def resolve(self, rel: Any, *, write: bool = False, listing: bool = False) -> Path:
        """Путь на диске по относительному. Проверяет область, скрытое и симлинки наружу; файл не обязан существовать.
        `write`: нужен writable. `listing`: допустим и каталог-ступень к разрешённым путям."""
        parts = self.parts(rel)
        allowed = self.can_write(parts) if write else (self.can_read(parts) or (listing and self.is_waypoint(parts)))
        if not allowed:
            raise _denied(f"путь «{rel}» вне {'записываемой' if write else 'доступной'} области")
        return self._real(parts, rel, write=write, listing=listing)

    def _real(self, parts: tuple[str, ...], rel: Any, *, write: bool, listing: bool) -> Path:
        path = self.root.joinpath(*parts)
        try:
            resolved = path.resolve()
            inside = resolved.relative_to(self.root).parts
        except (OSError, ValueError, RuntimeError):
            raise _denied(f"путь «{rel}» выходит за область") from None
        if self._hidden(inside):
            raise _denied(f"путь «{rel}» недоступен")
        if inside != parts:                                # симлинк внутри области: цель тоже должна быть разрешена
            ok = self.can_write(inside) if write else (self.can_read(inside) or (listing and self.is_waypoint(inside)))
            if not ok:
                raise _denied(f"путь «{rel}» ведёт за пределы области")
        return path

    def rel(self, path: Path) -> str:
        return "/".join(path.relative_to(self.root).parts)


# ---------------------------------------------------------------------------------------------------- журнал действий (раздел 7а)

def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Слово после числа: 1 строка, 2 строки, 5 строк."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def human_size(n: Any) -> str:
    """Размер файла для человека: 512 Б, 3 КБ, 1.2 МБ."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{round(n / 1024)} КБ"
    return f"{n / 1024 / 1024:.1f} МБ"


def count_lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def action_record(tool: str, op: str = "", target: str = "", ok: bool = True, error: str | None = None, text: str = "") -> dict:
    """Запись журнала действий `{ts, tool, op, target, ok, error, text}` (раздел 7а контракта). Содержимого файлов в ней нет:
    только путь, размер и краткий итог. Окружение для своих инструментов (`work`, `site`, `resources`) строит запись тем же
    помощником, чтобы формат был общий."""
    return {"ts": round(time.time(), 3), "tool": str(tool), "op": str(op or ""), "target": str(target or "")[:ACTION_TARGET_MAX],
            "ok": bool(ok), "error": (str(error)[:ACTION_TEXT_MAX] if error else None), "text": str(text or "")[:ACTION_TEXT_MAX]}


def error_text(e: BaseException) -> str:
    code = getattr(e, "code", None)
    message = getattr(e, "message", None) or str(e)
    return f"{code}: {message}" if code else f"{type(e).__name__}: {message}"


FILE_VERBS = {"list": "посмотреть каталог", "read": "прочитать", "write": "записать", "replace": "заменить текст в",
              "delete": "удалить", "move": "перенести"}


def failure_text(e: BaseException, what: str) -> str:
    """Человеческий текст отказа или ошибки. `what`: что она пыталась сделать («прочитать work/src/A.tsx»)."""
    message = getattr(e, "message", None) or str(e)
    if getattr(e, "code", None) == "denied":
        return f"Отказано: нельзя {what}" + (f" ({message})" if message else "")
    return f"Не вышло: {what}: {message}"


def similar_names(name: str, names: Iterable[str], limit: int = 8) -> list[str]:
    """Самые похожие на `name` имена: вхождение подстроки или общая часть имени, затем близкие по написанию."""
    low = name.casefold().strip()
    names = list(names)
    found = [n for n in names if low and (low in n.casefold() or n.casefold() in low)]
    parts = [p for p in low.replace("_", "-").split("-") if len(p) >= 3]
    found += [n for n in names if n not in found and any(p in n.casefold() for p in parts)]
    found += [n for n in difflib.get_close_matches(low, names, n=limit, cutoff=0.5) if n not in found]
    return found[:limit]


def _read_file(path: Path, shown: str, limit: int, offset: int = 0) -> dict:
    """Содержимое файла: текст с обрезкой (`truncated`, `size`) или метаданные для двоичного."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(8192)
            if b"\0" in head:
                kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                return {"ok": True, "path": shown, "binary": True, "size": size, "type": kind}
            fh.seek(max(offset, 0))
            data = fh.read(limit + 1)
    except OSError as e:
        raise ToolError(f"не удалось прочитать «{shown}»: {e.strerror or e}", code="unavailable") from None
    truncated = len(data) > limit
    text = data[:limit].decode("utf-8", errors="ignore" if truncated else "replace")
    out = {"ok": True, "path": shown, "content": text, "size": size, "truncated": truncated}
    if offset:
        out["offset"] = offset
    return out


def _str_arg(args: dict, key: str, *, required: bool = True) -> str | None:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"нужен параметр «{key}»", code="invalid")
        return None
    if not isinstance(value, str):
        raise ToolError(f"параметр «{key}» должен быть строкой", code="invalid")
    return value


class WorkspaceTools:
    """Обработчики стандартных инструментов окружения: `skills`, `files`, `run`.

    `on_action(запись)` (необязательный, обычная или async функция) зовётся после каждого вызова: успешного, отказа и ошибки;
    запись `{ts, tool, op, target, ok, error, text}` по-русски, без содержимого файлов (`action_record`). Ошибка в `on_action`
    вызов инструмента не ломает.

    `sandbox` область; `skills` навыки (`WorkspaceSkill`, путь относительно корня песочницы); `commands` разрешённые
    команды (`WorkspaceCommand`); `executor` функция запуска `(команда, args, background, cwd) -> результат` (синхронная
    или async); для фона она возвращает `job_id` (строка или словарь с `job_id`)."""

    def __init__(self, sandbox: Sandbox, skills: Iterable[WorkspaceSkill] = (), commands: Iterable[WorkspaceCommand] = (),
                 executor: Executor | None = None, read_limit: int = READ_LIMIT,
                 on_action: Callable[[dict], Any] | None = None):
        self.sandbox = sandbox
        self.skill_list = list(skills)
        self.commands = {c.id: c for c in commands}
        self.executor = executor
        self.read_limit = read_limit
        self.on_action = on_action

    # -- журнал действий

    async def emit_action(self, record: dict) -> None:
        """Отдать запись в `on_action`; сбой обработчика только в лог."""
        if self.on_action is None:
            return
        try:
            out = self.on_action(record)
            if inspect.isawaitable(out):
                await out
        except Exception as e:
            log.warning("on_action: %s", e)

    async def _audit(self, tool: str, args: Any, call: Callable[[Any], Any]) -> dict:
        try:
            out = await call(args)
        except Exception as e:
            await self.emit_action(self._safe_record(tool, args, None, e))
            raise
        await self.emit_action(self._safe_record(tool, args, out, None))
        return out

    def _safe_record(self, tool: str, args: Any, out: dict | None, err: BaseException | None) -> dict:
        try:
            return getattr(self, "_record_" + tool)(args if isinstance(args, dict) else {}, out, err)
        except Exception as e:                      # описание действия не должно ломать вызов
            return action_record(tool, "", "", err is None, error_text(err) if err else None, f"{tool}: (описание недоступно: {e})")

    def _record_skills(self, args: dict, out: dict | None, err: BaseException | None) -> dict:
        name = args.get("name") if isinstance(args.get("name"), str) else ""
        file = args.get("file") if isinstance(args.get("file"), str) else ""
        target = name + (f"/{file}" if file and name else "")
        op = "read" if name else "list"
        if err is not None:
            what = f"читать навык {name}" + (f", файл {file}" if file else "") if name else "посмотреть список навыков"
            return action_record("skills", op, target, False, error_text(err), failure_text(err, what))
        if not name:
            return action_record("skills", op, "", True, None, "Смотрит список навыков")
        size = human_size((out or {}).get("size"))
        text = f"Читает файл {file} навыка {name}" if file else f"Читает навык {name}"
        return action_record("skills", op, target, True, None, text + (f" ({size})" if size else ""))

    def _record_files(self, args: dict, out: dict | None, err: BaseException | None) -> dict:
        op = args.get("op") if isinstance(args.get("op"), str) else ""
        path = (out or {}).get("path") or (args.get("path") if isinstance(args.get("path"), str) else "") or "."
        dest = args.get("dest") if isinstance(args.get("dest"), str) else ""
        target = path + (f" -> {dest}" if op == "move" and dest else "")
        if err is not None:
            what = f"{FILE_VERBS.get(op, 'выполнить «' + op + '» над')} {target}"
            return action_record("files", op, target, False, error_text(err), failure_text(err, what))
        out = out or {}
        if op == "list":
            text = f"Смотрит каталог {path}"
        elif op == "read":
            size = human_size(out.get("size"))
            text = f"Читает {path}" + (f" ({size})" if size else "") + (", двоичный" if out.get("binary") else "")
        elif op == "write":
            n = count_lines(args.get("content") if isinstance(args.get("content"), str) else "")
            text = f"Записала {path} ({'+' if out.get('created') else ''}{n} {plural_ru(n, 'строка', 'строки', 'строк')})"
        elif op == "replace":
            n = int(out.get("replaced") or 1)
            text = f"Заменила текст в {path}" + (f" ({n} {plural_ru(n, 'место', 'места', 'мест')})" if n > 1 else "")
        elif op == "delete":
            text = f"Удалила {path}"
        elif op == "move":
            text = f"Перенесла {out.get('from') or args.get('path')} в {out.get('path') or dest}"
            target = f"{out.get('from') or args.get('path')} -> {out.get('path') or dest}"
        else:
            text = f"Файлы: {op} {path}"
        return action_record("files", op, target, True, None, text)

    def _record_run(self, args: dict, out: dict | None, err: BaseException | None) -> dict:
        cid = args.get("command") if isinstance(args.get("command"), str) else ""
        cargs = [str(a) for a in args.get("args") or []] if isinstance(args.get("args"), list) else []
        command = self.commands.get(cid)
        title = command.title if command is not None and command.title else cid
        shown = (" " + " ".join(cargs))[:120] if cargs else ""
        target = (cid + shown)[:ACTION_TARGET_MAX]
        background = bool((out or {}).get("background")) if out else bool(args.get("background"))
        op = "background" if background else "run"
        if err is not None:
            return action_record("run", op, target, False, error_text(err), failure_text(err, f"запустить команду «{title}»{shown}"))
        if background:
            text = f"Запустила в фоне: {title}{shown}, операция {(out or {}).get('job_id')}"
        else:
            result = (out or {}).get("result")
            note = result.get("text") if isinstance(result, dict) and isinstance(result.get("text"), str) else ""
            text = f"Выполнила: {title}{shown}" + (f": {note}" if note else "")
        return action_record("run", op, target, True, None, text)

    # -- skills

    async def skills(self, args: dict | None = None) -> dict:
        """Без параметров список навыков с описаниями; `name` текст SKILL.md; `name` и `file` файл внутри навыка. Неточное
        имя даёт ошибку `unknown` с точными и самыми похожими именами."""
        return await self._audit("skills", args or {}, self._skills)

    async def _skills(self, args: dict) -> dict:
        name = _str_arg(args, "name", required=False)
        if not name:
            return {"ok": True, "result": [{"name": s.name, "description": s.description} for s in self.skill_list]}
        skill = next((s for s in self.skill_list if s.name == name), None)
        if skill is None:
            raise self._unknown_skill(name)
        file = _str_arg(args, "file", required=False) or "SKILL.md"
        skill_root = Sandbox(self.sandbox.root, readonly=[skill.path], hidden=self.sandbox.hidden).resolve(skill.path).resolve()
        target = Sandbox(skill_root, readonly=["."], hidden=self.sandbox.hidden).resolve(file)
        if not target.is_file():
            raise ToolError(f"в навыке «{name}» нет файла «{file}»", code="unknown")
        out = _read_file(target, file, self.read_limit, _offset(args))
        out["skill"] = name
        return out

    def _unknown_skill(self, name: str) -> ToolError:
        names = [s.name for s in self.skill_list]
        similar = similar_names(name, names)
        message = f"навыка «{name}» нет. Точные имена: {', '.join(names) or 'навыков нет'}."
        if similar:
            message += f" Похожие: {', '.join(similar)}."
        return ToolError(message, code="unknown")

    # -- files

    async def files(self, args: dict) -> dict:
        """`op`: list, read, write, replace (точная замена строки), delete, move; `path` в области, запись только в writable."""
        return await self._audit("files", args, self._files)

    async def _files(self, args: dict) -> dict:
        op = _str_arg(args, "op")
        if op not in FILE_OPS:
            raise ToolError(f"op: одно из {list(FILE_OPS)}", code="invalid")
        return getattr(self, "_op_" + op)(args)

    def _op_list(self, args: dict) -> dict:
        path = self.sandbox.resolve(_str_arg(args, "path", required=False) or ".", listing=True)
        if not path.exists():
            raise ToolError("каталога нет", code="unknown")
        if not path.is_dir():
            raise ToolError("это файл, а не каталог", code="invalid")
        base = self.sandbox.parts(self.sandbox.rel(path)) if path != self.sandbox.root else ()
        entries = []
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            parts = (*base, child.name)
            if self.sandbox._hidden(parts):
                continue
            if not (self.sandbox.can_read(parts) or (child.is_dir() and self.sandbox.is_waypoint(parts))):
                continue
            try:
                self.sandbox._real(parts, child.name, write=False, listing=True)
            except ToolError:
                continue                                  # симлинк наружу в списке не показывается
            entry = {"name": child.name, "type": "dir" if child.is_dir() else "file",
                     "writable": self.sandbox.can_write(parts)}
            if child.is_file():
                entry["size"] = child.stat().st_size
            entries.append(entry)
        more = len(entries) > LIST_LIMIT
        return {"ok": True, "path": "/".join(base) or ".", "entries": entries[:LIST_LIMIT], "truncated": more}

    def _op_read(self, args: dict) -> dict:
        rel = _str_arg(args, "path")
        path = self.sandbox.resolve(rel)
        if not path.exists():
            raise ToolError(f"файла «{rel}» нет", code="unknown")
        if not path.is_file():
            raise ToolError(f"«{rel}» не файл", code="invalid")
        return _read_file(path, "/".join(self.sandbox.parts(rel)), self.read_limit, _offset(args))

    def _op_write(self, args: dict) -> dict:
        rel = _str_arg(args, "path")
        content = _str_arg(args, "content")
        if len(content.encode("utf-8")) > WRITE_LIMIT:
            raise ToolError(f"содержимое больше {WRITE_LIMIT} байт", code="invalid")
        path = self.sandbox.resolve(rel, write=True)
        if path == self.sandbox.root or path.is_dir():
            raise ToolError(f"«{rel}» каталог", code="invalid")
        existed = path.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.sandbox.resolve(rel, write=True)          # после создания каталогов путь ещё внутри области
            path.write_text(content, encoding="utf-8", newline="")
        except OSError as e:
            raise ToolError(f"не удалось записать «{rel}»: {e.strerror or e}", code="unavailable") from None
        return {"ok": True, "path": self.sandbox.rel(path), "size": path.stat().st_size, "created": not existed}

    def _op_replace(self, args: dict) -> dict:
        rel = _str_arg(args, "path")
        old, new = _str_arg(args, "old"), _str_arg(args, "new")
        if not old:
            raise ToolError("«old» не может быть пустым", code="invalid")
        path = self.sandbox.resolve(rel, write=True)
        if not path.is_file():
            raise ToolError(f"файла «{rel}» нет", code="unknown")
        try:
            text = path.read_text(encoding="utf-8", newline="")
        except (OSError, UnicodeDecodeError) as e:
            raise ToolError(f"«{rel}» не читается как текст: {e}", code="invalid") from None
        count = text.count(old)
        if count == 0:
            raise ToolError("строка для замены не найдена", code="invalid")
        if count > 1 and not args.get("all"):
            raise ToolError(f"строка встречается {count} раз: уточните её или укажите all=true", code="invalid")
        path.write_text(text.replace(old, new), encoding="utf-8", newline="")
        return {"ok": True, "path": self.sandbox.rel(path), "replaced": count}

    def _op_delete(self, args: dict) -> dict:
        rel = _str_arg(args, "path")
        parts = self.sandbox.parts(rel)
        path = self.sandbox.resolve(rel, write=True)
        if not parts:
            raise _denied("корень удалять нельзя")
        if not path.exists() and not path.is_symlink():
            raise ToolError(f"«{rel}» нет", code="unknown")
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif args.get("recursive"):
                shutil.rmtree(path)
            else:
                path.rmdir()
        except OSError as e:
            raise ToolError(f"не удалось удалить «{rel}» (для непустого каталога нужен recursive=true): {e.strerror or e}",
                            code="invalid") from None
        return {"ok": True, "path": "/".join(parts), "deleted": True}

    def _op_move(self, args: dict) -> dict:
        src_rel, dst_rel = _str_arg(args, "path"), _str_arg(args, "dest")
        src = self.sandbox.resolve(src_rel, write=True)
        dst = self.sandbox.resolve(dst_rel, write=True)
        if not self.sandbox.parts(src_rel):
            raise _denied("корень переносить нельзя")
        if not src.exists():
            raise ToolError(f"«{src_rel}» нет", code="unknown")
        if dst.exists():
            raise ToolError(f"«{dst_rel}» уже есть", code="invalid")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            self.sandbox.resolve(dst_rel, write=True)
            shutil.move(str(src), str(dst))
        except OSError as e:
            raise ToolError(f"не удалось переместить: {e.strerror or e}", code="unavailable") from None
        return {"ok": True, "path": self.sandbox.rel(dst), "from": "/".join(self.sandbox.parts(src_rel))}

    # -- run

    def _prepare_run(self, args: dict) -> tuple[WorkspaceCommand, list, bool]:
        cid = _str_arg(args, "command")
        command = self.commands.get(cid)
        if command is None:
            raise _denied(f"команды «{cid}» нет в списке разрешённых: {sorted(self.commands)}")
        cargs = args.get("args") or []
        if not isinstance(cargs, list) or not all(isinstance(a, str) and "\0" not in a for a in cargs) \
                or len(cargs) > RUN_ARGS_MAX or any(len(a) > RUN_ARG_LEN_MAX for a in cargs):
            raise ToolError(f"args: список строк, до {RUN_ARGS_MAX} штук", code="invalid")
        if "background" in args and not isinstance(args["background"], bool):
            raise ToolError("background: true или false", code="invalid")
        return command, list(cargs), command.background or bool(args.get("background"))

    async def run(self, args: dict) -> dict:
        """Запуск команды из списка разрешённых. Фон: сразу `job_id`; иначе результат исполнителя."""
        return await self._audit("run", args, self._run)

    async def _run(self, args: dict) -> dict:
        command, cargs, background = self._prepare_run(args)
        if self.executor is None:
            raise ToolError("исполнитель команд не подключён", code="unavailable")
        out = self.executor(command, cargs, background, self.sandbox.root)
        if inspect.isawaitable(out):
            out = await out
        if background:
            job_id = out.get("job_id") if isinstance(out, dict) else out
            if not isinstance(job_id, str) or not job_id:
                raise ToolError("исполнитель не вернул job_id для фоновой команды", code="unavailable")
            return {"ok": True, "command": command.id, "background": True, "job_id": job_id}
        return {"ok": True, "command": command.id, "background": False, "result": out}

    # -- опасность (tool.check)

    async def check_files(self, args: dict) -> str | None:
        """Причина, по которой ядро спросит согласие: удаление."""
        if isinstance(args, dict) and args.get("op") == "delete":
            return f"удаление «{args.get('path', '')}» нельзя отменить"
        return None

    async def check_run(self, args: dict) -> str | None:
        """Причина согласия: команда с пометкой `irreversible`."""
        command = self.commands.get(args.get("command")) if isinstance(args, dict) else None
        if command is not None and command.irreversible:
            return f"команда «{command.title}» необратима"
        return None

    # -- манифест и подключение

    def declare_tools(self) -> list[dict]:
        """Описания инструментов `skills`, `files`, `run` в формате `tools` манифеста (риск auto; удаление и необратимые
        команды ядро переспрашивает через `danger_check`)."""
        common = {"risk": "auto", "availability": "always", "informative": False}
        cmd_param: dict = {"type": "string", "description": "id команды из списка разрешённых", "required": True}
        if self.commands:
            cmd_param["enum"] = sorted(self.commands)
        foreground = [c.timeout_s for c in self.commands.values() if not c.background and c.timeout_s]
        run_timeout = min(max(foreground), 120) if foreground else 30
        return [
            {"name": "skills", "description": "Навыки окружения: без параметров список, name даёт текст навыка, name и file файл внутри навыка.",
             "params": {"name": {"type": "string", "description": "имя навыка"},
                        "file": {"type": "string", "description": "файл внутри навыка"},
                        "offset": {"type": "integer", "description": "с какого байта читать (для длинных файлов)"}},
             "keywords": ["навык", "справка", "регламент", "skill"], **common},
            {"name": "files", "description": "Файлы рабочей области: op list, read, write, replace (точная замена строки), delete, move. "
                                              "Запись и удаление только в разрешённых каталогах.",
             "params": {"op": {"type": "string", "description": "действие", "required": True, "enum": list(FILE_OPS)},
                        "path": {"type": "string", "description": "путь внутри области"},
                        "content": {"type": "string", "description": "текст для write"},
                        "old": {"type": "string", "description": "что заменить (replace)"},
                        "new": {"type": "string", "description": "на что заменить (replace)"},
                        "all": {"type": "boolean", "description": "заменить все вхождения"},
                        "dest": {"type": "string", "description": "новый путь (move)"},
                        "recursive": {"type": "boolean", "description": "удалить каталог с содержимым"},
                        "offset": {"type": "integer", "description": "с какого байта читать (read)"}},
             "keywords": ["файл", "прочитать", "записать", "правка", "папка"], "danger_check": True, **common},
            {"name": "run", "description": "Запустить разрешённую команду окружения. Долгая команда идёт в фоне и сразу возвращает job_id; "
                                            "итог приходит событием.",
             "params": {"command": cmd_param,
                        "args": {"type": "array", "description": "аргументы команды (строки)"},
                        "background": {"type": "boolean", "description": "запустить в фоне"}},
             "keywords": ["запустить", "команда", "рендер", "run"], "timeout_s": run_timeout,
             "danger_check": any(c.irreversible for c in self.commands.values()), **common},
        ]

    def attach(self, client: Any) -> None:
        """Повесить обработчики на клиента (инструменты должны быть в его манифесте, см. `declare_tools`)."""
        for name, handler, check in (("skills", self.skills, None), ("files", self.files, self.check_files),
                                     ("run", self.run, self.check_run)):
            client.tool(name)(handler)
            tool = next((t for t in client.manifest.get("tools", []) if t.get("name") == name), None)
            if check is not None and tool is not None and tool.get("danger_check"):
                client.check(name)(check)


def _offset(args: dict) -> int:
    value = args.get("offset", 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolError("offset: целое неотрицательное", code="invalid")
    return value
