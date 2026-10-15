"""Песочница агента-монтажёра: политика `can_use_tool` (ТЗ 20, раздел 8.3, MON-17, MON-18).

Решения автоматические, пользователю политика вопросов не задаёт: агент получает причину отказа и ищет другой путь. Политика
работает только потому, что инструменты не добавлены в `allowed_tools` (иначе SDK пропускает их мимо `can_use_tool`):
`claude_runner` перечисляет их в `tools`, а `allowed_tools` оставляет пустым.

Правила:
  - Read, Glob, Grep: путь внутри папки задачи, template/, assets/, docs/, .claude/skills/ модуля;
  - Write, Edit: внутри jobs/<id>/work/ (кроме node_modules) или jobs/<id>/plan/;
  - Bash: ffmpeg, ffprobe, `npx remotion render|still|compositions`, `npx tsc --noEmit`, `node <файл в work/scripts/>`;
    без `; && || | ` $( <`, перенаправление `>` только в файл внутри папки задачи; пути-аргументы только внутрь папки
    задачи, template/, assets/;
  - Skill, TodoWrite и свои инструменты (`mcp__montage__*`): разрешены; `ask_user` только на стадиях clarify и build;
  - всё остальное (WebFetch, WebSearch, Task, чужие MCP, ...): отказ.
Каждое решение пишется в журнал задачи (имя инструмента, разрешено ли, причина, команда или путь; без содержимого файлов).
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from layout import Layout

MCP_PREFIX = "mcp__montage__"
OWN_TOOLS = ("ask_user", "progress", "synthesize_voice", "kb_lookup", "transcript_words")
READ_TOOLS = ("Read", "Glob", "Grep")
WRITE_TOOLS = ("Write", "Edit")
PASS_TOOLS = ("Skill", "TodoWrite")
BASH_PROGRAMS = ("ffmpeg", "ffprobe", "npx", "node")
REMOTION_COMMANDS = ("render", "still", "compositions")
# флаги, через которые Remotion запустил бы произвольный исполняемый файл или чужую конфигурацию
FORBIDDEN_FLAGS = ("--browser-executable", "--ffmpeg-executable", "--ffprobe-executable", "--config", "--env-file",
                   "--package", "--yes", "-y", "--call", "-c")
NODE_ALLOWED_FLAGS = ("--disable-warning=",)
MAX_COMMAND = 2000
# в скриптах node, которые агент написал сам, это не допускается (шаблонные скрипты из template/scripts идут как есть)
DANGEROUS_JS = re.compile(r"child_process|\bspawn|\bexec(?:Sync|File)?\b|node:(?:http|https|net|dgram|dns|tls|cluster|"
                          r"worker_threads|vm|os)\b|\bfetch\s*\(|XMLHttpRequest|WebSocket|\beval\s*\(|new\s+Function|"
                          r"process\.(?:binding|env|kill|exit)|\brequire\s*\(|\brmSync\b|\bunlink|\brm\s*\(|\brmdir")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_DRIVE_ANYWHERE = re.compile(r"[A-Za-z]\\?:[\\/][^'\",;\s]*")
_GITBASH = re.compile(r"^/([A-Za-z])(/.*)?$")


class Denied(Exception):
    """Причина отказа (по-русски, её читает агент)."""


# ---------------------------------------------------------------------------------------------- разбор команды

def tokenize(cmd: str) -> list[str]:
    """Разбор строки команды на слова. Кавычки снимаются; обратная косая черта не экранирует (пути Windows). Всё, что
    оболочка поймёт как составную команду или подстановку, запрещено. Перенаправления вида `> путь` дают токены `>`
    или `>>` и путь следом; `2>&1` пропускается."""
    out: list[str] = []
    cur: list[str] = []
    have = False
    quote = ""
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and ch in "$`":
                raise Denied("подстановки $ и ` запрещены")
            else:
                cur.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote, have = ch, True
        elif ch.isspace():
            if "\n" in ch or "\r" in ch:
                raise Denied("несколько команд (перевод строки) запрещены")
            if have or cur:
                out.append("".join(cur))
                cur, have = [], False
        elif ch == ">":
            if cmd.startswith("2>&1", i - 1) and cur == ["2"]:
                cur, have = [], False
                i += 4 - 1
                continue
            if cur and "".join(cur) not in ("1", "2"):
                out.append("".join(cur))
            cur, have = [], False
            if cmd.startswith(">>", i):
                out.append(">>")
                i += 1
            else:
                out.append(">")
        elif ch in ";|&`$<(){}":
            raise Denied(f"символ «{ch}» запрещён: одна простая команда без конвейеров, подстановок и цепочек")
        else:
            cur.append(ch)
        i += 1
    if quote:
        raise Denied("незакрытая кавычка")
    if have or cur:
        out.append("".join(cur))
    return out


def _normalise(raw: str) -> str:
    """Путь в виде Windows: `/c/Users/x` (git bash) -> `C:/Users/x`."""
    m = _GITBASH.match(raw)
    if m and os.name == "nt":
        return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return raw


def _inside(path: Path, roots: list[Path]) -> bool:
    try:
        real = Path(os.path.realpath(path))
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            real.relative_to(Path(os.path.realpath(root)))
            return True
        except ValueError:
            continue
    return False


def looks_like_path(arg: str) -> bool:
    if arg in ("-", ""):
        return False
    if _DRIVE.match(arg) or arg.startswith(("/", "\\", "~", ".")):
        return True
    return ".." in re.split(r"[\\/]", arg)


@dataclass
class Sandbox:
    """Политика для одной задачи. `readonly=True`: проверяющий кадры (Read и Glob внутри задачи, больше ничего)."""
    job_dir: Path
    layout: Layout
    allow_ask: bool = False
    readonly: bool = False
    log: Callable[[dict], None] | None = None
    decisions: list[dict] = field(default_factory=list)

    # ---- корни

    @property
    def work(self) -> Path:
        return self.job_dir / "work"

    def read_roots(self) -> list[Path]:
        return [self.job_dir, self.layout.template, self.layout.character, self.layout.docs, self.layout.skills]

    def write_roots(self) -> list[Path]:
        return [self.work, self.job_dir / "plan"]

    def arg_roots(self) -> list[Path]:
        return [self.job_dir, self.layout.template, self.layout.assets]

    def _abs(self, raw: str, base: Path | None = None) -> Path:
        raw = _normalise(str(raw))
        p = Path(raw)
        return p if p.is_absolute() or _DRIVE.match(raw) else (base or self.work) / p

    # ---- решение

    def decide(self, name: str, tool_input: dict | None) -> tuple[bool, str]:
        tool_input = tool_input or {}
        try:
            self._decide(name, tool_input)
        except Denied as e:
            return False, str(e)
        return True, "разрешено"

    def _decide(self, name: str, inp: dict) -> None:
        if name.startswith(MCP_PREFIX):
            tool = name[len(MCP_PREFIX):]
            if self.readonly or tool not in OWN_TOOLS:
                raise Denied(f"инструмент {name} недоступен")
            if tool == "ask_user" and not self.allow_ask:
                raise Denied("вопрос пользователю можно задавать только на стадиях clarify и build")
            return
        if name in PASS_TOOLS:
            if self.readonly:
                raise Denied(f"{name} недоступен проверяющему")
            return
        if name in READ_TOOLS:
            self._read(name, inp)
            return
        if self.readonly:
            raise Denied("проверяющему разрешено только чтение кадров и плана (Read, Glob)")
        if name in WRITE_TOOLS:
            self._write(inp)
            return
        if name == "Bash":
            self._bash(str(inp.get("command") or ""))
            return
        raise Denied(f"инструмент {name} не разрешён политикой монтажа (сеть, браузер и сторонние сервисы закрыты)")

    # ---- Read / Glob / Grep

    def _read(self, name: str, inp: dict) -> None:
        keys = ("file_path", "notebook_path") if name == "Read" else ("path",)
        paths = [str(inp[k]) for k in keys if inp.get(k)]
        if name in ("Glob", "Grep"):
            for key in ("pattern", "glob"):
                pat = str(inp.get(key) or "")
                if name == "Grep" and key == "pattern":
                    continue                                      # у Grep pattern это регулярное выражение
                if pat and (".." in re.split(r"[\\/]", pat) or _DRIVE.match(pat) or pat.startswith(("/", "\\", "~"))):
                    raise Denied("шаблон поиска должен быть относительным и без «..»")
        if name == "Read" and not paths:
            raise Denied("не указан file_path")
        for raw in paths or [str(self.work)]:
            if not _inside(self._abs(raw), self.read_roots()):
                raise Denied(f"читать можно только внутри папки задачи, template/, assets/, docs/ и .claude/skills/: {raw}")

    # ---- Write / Edit

    def _write(self, inp: dict) -> None:
        raw = str(inp.get("file_path") or "")
        if not raw:
            raise Denied("не указан file_path")
        path = self._abs(raw)
        norm = Path(os.path.normpath(path))
        node_modules = self.work / "node_modules"
        if norm == node_modules or node_modules in norm.parents:
            raise Denied("work/node_modules менять нельзя")
        if not _inside(path, self.write_roots()):
            raise Denied(f"писать можно только в work/ и plan/ папки задачи: {raw}")

    # ---- Bash

    def _bash(self, command: str) -> None:
        if not command.strip():
            raise Denied("пустая команда")
        if len(command) > MAX_COMMAND:
            raise Denied("команда слишком длинная")
        tokens = tokenize(command)
        redirects: list[str] = []
        words: list[str] = []
        it = iter(tokens)
        for t in it:
            if t in (">", ">>"):
                target = next(it, None)
                if target is None:
                    raise Denied("после > нужен путь файла")
                redirects.append(target)
            else:
                words.append(t)
        if not words:
            raise Denied("нет команды")
        prog = words[0]
        if re.search(r"[\\/]", prog):
            raise Denied("программа вызывается по имени (ffmpeg, ffprobe, npx, node), не по пути")
        base = re.sub(r"\.(exe|cmd|bat)$", "", prog.lower())
        if base not in BASH_PROGRAMS:
            raise Denied(f"программа «{prog}» не разрешена: допустимы ffmpeg, ffprobe, npx remotion (render|still|compositions), "
                         "npx tsc --noEmit и node для скриптов из work/scripts/")
        args = words[1:]
        for a in args:
            if "://" in a or a.lower().startswith(("http:", "https:", "ftp:", "rtmp:", "file:")):
                raise Denied("сетевые адреса и file: запрещены")
        if base == "npx":
            args = self._npx(args)
        elif base == "node":
            args = self._node(args)
        for target in redirects:
            self._arg_path(target, "перенаправление")
        for a in args:
            self._arg(a)

    def _npx(self, args: list[str]) -> list[str]:
        if not args:
            raise Denied("npx без команды")
        if args[0] == "remotion":
            if len(args) < 2 or args[1] not in REMOTION_COMMANDS:
                raise Denied("разрешено только npx remotion render, still или compositions (studio, add, upgrade и прочее закрыты)")
        elif args[0] == "tsc":
            if "--noEmit" not in args:
                raise Denied("разрешено только npx tsc --noEmit")
        else:
            raise Denied(f"npx {args[0]} не разрешён: допустимы remotion (render|still|compositions) и tsc --noEmit")
        for a in args:
            flag = a.split("=", 1)[0]
            if flag in FORBIDDEN_FLAGS:
                raise Denied(f"флаг {flag} запрещён")
        return args[1:]

    def _node(self, args: list[str]) -> list[str]:
        rest = list(args)
        while rest and rest[0].startswith("-"):
            if not rest[0].startswith(NODE_ALLOWED_FLAGS):
                raise Denied(f"флаг node {rest[0]} запрещён")
            rest.pop(0)
        if not rest:
            raise Denied("node без файла скрипта")
        script = self._abs(rest[0])
        scripts = self.work / "scripts"
        if not _inside(script, [scripts]) or script.suffix not in (".mjs", ".js", ".cjs"):
            raise Denied("node запускает только скрипты .mjs/.js из work/scripts/")
        if not self._trusted_script(script):
            raise Denied("скрипт использует сеть, процессы или удаление файлов: так нельзя")
        return rest

    def _trusted_script(self, script: Path) -> bool:
        original = self.layout.template / "scripts" / script.name
        try:
            text = script.read_text(encoding="utf-8")
        except OSError:
            return True                                   # скрипта ещё нет: node сам скажет; запускать нечего
        if original.is_file():
            try:
                if original.read_text(encoding="utf-8") == text:
                    return True                           # шаблонный скрипт без правок
            except OSError:
                pass
        return not DANGEROUS_JS.search(text)

    def _arg(self, arg: str) -> None:
        if arg.startswith("-"):
            flag, eq, value = arg.partition("=")
            if eq:
                self._arg_path(value, flag)
            return
        self._arg_path(arg, "аргумент")

    def _arg_path(self, value: str, what: str) -> None:
        if value.lower() in ("nul", "nul:"):
            return
        for m in _DRIVE_ANYWHERE.finditer(value):                  # пути внутри строк фильтров ffmpeg
            candidate = m.group(0).replace("\\:", ":")
            if not _inside(Path(candidate), self.arg_roots()):
                raise Denied(f"путь {candidate} ({what}) вне папки задачи, template/ и assets/")
        v = _normalise(value)
        if v.startswith("~"):
            raise Denied(f"путь {value} ({what}) в домашней папке: нельзя")
        if looks_like_path(v) or "/" in v or "\\" in v:
            # путь в аргументе: абсолютный, с «..» или относительный (считается от work/); внутрь задачи, template/, assets/
            if not _inside(self._abs(v), self.arg_roots()):
                raise Denied(f"путь {value} ({what}) вне папки задачи, template/ и assets/")

    # ---- интерфейс SDK

    def _record(self, name: str, tool_input: dict, allowed: bool, reason: str) -> None:
        detail = ""
        if name == "Bash":
            detail = str(tool_input.get("command") or "")[:300]
        elif name in READ_TOOLS + WRITE_TOOLS:
            detail = str(tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or "")[:300]
        row = {"tool": name, "allowed": allowed, "reason": reason, "detail": detail}
        self.decisions.append(row)
        if self.log is not None:
            try:
                self.log(row)
            except Exception:
                pass

    async def can_use_tool(self, name, tool_input, _ctx=None):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        allowed, reason = self.decide(name, tool_input)
        self._record(name, tool_input or {}, allowed, reason)
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=reason)
