"""Клиент рабочего агента pi (`services/agent-runtime/worker.mjs`) для ядра и модулей.

Замена Claude Code SDK там, где нужны встроенные инструменты (чтение, поиск, правка, команды) с проверкой каждого вызова:
`PiAgent.run(prompt)` отдаёт события, а каждый вызов встроенного инструмента до исполнения уходит в `can_use_tool`
(аналог `can_use_tool` Claude SDK). Без `can_use_tool` и при любой ошибке в нём инструмент отклоняется.
Имена и поля инструментов pi приводятся к виду Claude (`to_claude`), поэтому прежние проверки разрешений и песочницы
работают без переписывания. Только стандартная библиотека; `agent/` не импортируется.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("gits_sdk.pi_agent")

READ_LIMIT = 4 * 1024 * 1024
READY_TIMEOUT_S = 60.0
SHUTDOWN_TIMEOUT_S = 8.0
_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")


def default_runtime_dir() -> Path:
    env = os.environ.get("GITS_PI_RUNTIME_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "services" / "agent-runtime"


class PiAgentError(RuntimeError):
    """Понятная причина, по которой агент не запустился или упал (нет node, зависимостей, входа)."""


# ── события ────────────────────────────────────────────────────────

@dataclass
class TextDelta:
    text: str


@dataclass
class TextEvent:
    """Целое сообщение модели."""
    text: str


@dataclass
class ToolUse:
    """Разрешённый и исполняемый встроенный инструмент (для журнала). `claude_*`: вид Claude."""
    tool: str
    input: dict
    claude_tool: str = ""
    claude_input: dict = field(default_factory=dict)


@dataclass
class EndEvent:
    status: str                 # ok | aborted | error | max_turns
    error: str | None = None
    usage: dict = field(default_factory=dict)   # requests, input, output
    turns: int = 0


AgentEvent = TextDelta | TextEvent | ToolUse | EndEvent


@dataclass
class CustomTool:
    """Свой инструмент: исполняет вызывающая сторона. `handler(args) -> str`, исключение это ошибка инструмента."""
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[str]]


CanUseTool = Callable[[str, dict], Awaitable[tuple[bool, str]]]


# ── приведение к виду Claude ───────────────────────────────────────

CLAUDE_NAMES = {"read": "Read", "write": "Write", "edit": "Edit", "bash": "Bash", "grep": "Grep", "find": "Glob", "ls": "LS"}


def _abs(path: Any, cwd: str | None) -> str:
    p = os.path.expanduser(str(path or "").removeprefix("@"))
    if not p:
        return str(Path(cwd).resolve()) if cwd else ""
    if cwd and not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def to_claude(tool: str, tool_input: dict, cwd: str | None = None) -> dict:
    """`{"tool": "Read", "input": {"file_path": ...}}` для встроенного инструмента pi. Пути делаются абсолютными
    (относительно `cwd`), поэтому проверки «внутри папки» видят настоящее место. Неизвестный инструмент: имя как есть."""
    i = dict(tool_input or {})
    name = CLAUDE_NAMES.get(tool, tool)
    if tool == "read":
        out = {"file_path": _abs(i.get("path"), cwd)}
        for k in ("offset", "limit"):
            if k in i:
                out[k] = i[k]
    elif tool == "write":
        out = {"file_path": _abs(i.get("path"), cwd), "content": i.get("content", "")}
    elif tool == "edit":
        edits = i.get("edits") or []
        first = edits[0] if edits and isinstance(edits[0], dict) else {}
        out = {"file_path": _abs(i.get("path"), cwd), "old_string": first.get("oldText", ""),
               "new_string": first.get("newText", ""), "edits": edits}
    elif tool == "bash":
        out = {"command": i.get("command", "")}
        if "timeout" in i:
            out["timeout"] = i["timeout"]
    elif tool == "grep":
        out = {"pattern": i.get("pattern", ""), "path": _abs(i.get("path"), cwd)}
        for src, dst in (("glob", "glob"), ("ignoreCase", "-i"), ("context", "-C"), ("limit", "head_limit")):
            if src in i:
                out[dst] = i[src]
    elif tool == "find":
        out = {"pattern": i.get("pattern", ""), "path": _abs(i.get("path"), cwd)}
    elif tool == "ls":
        out = {"path": _abs(i.get("path"), cwd)}
    else:
        out = i
    return {"tool": name, "input": out}


def claude_tool_names(tools: list[str]) -> list[str]:
    """Имена Claude для списка встроенных инструментов pi."""
    return [CLAUDE_NAMES.get(t, t) for t in tools]


def pi_tool_names(tools: list[str]) -> list[str]:
    """Имена pi для списка имён Claude (`["Read", "Glob", "Grep"]` -> `["read", "find", "grep"]`)."""
    back = {v: k for k, v in CLAUDE_NAMES.items()}
    return [back.get(t, t.lower()) for t in tools]


# ── агент ──────────────────────────────────────────────────────────

class PiAgent:
    def __init__(self, *, cwd: str | os.PathLike, system: str | None = None, system_file: str | os.PathLike | None = None,
                 tools: list[str] | tuple[str, ...] = ("read", "grep", "find", "ls"),
                 custom_tools: list[CustomTool] | None = None, model: str = "gpt-6-luna", thinking: str = "minimal",
                 session_file: str | os.PathLike | None = None, skills_dir: str | os.PathLike | None = None,
                 max_turns: int = 0, can_use_tool: CanUseTool | None = None, auth_file: str | os.PathLike | None = None,
                 runtime_dir: str | os.PathLike | None = None, provider: str = "openai",
                 faux_responses: list | None = None, command: list[str] | None = None):
        self.cwd = str(cwd)
        self._system, self._system_file = system, str(system_file) if system_file else None
        self._tools = list(tools)
        self._custom = {t.name: t for t in (custom_tools or [])}
        self._model, self._thinking = model, thinking
        self._session_file = str(session_file) if session_file else None
        self._skills_dir = str(skills_dir) if skills_dir else None
        self._max_turns = int(max_turns or 0)
        self._can_use_tool = can_use_tool
        self._runtime_dir = Path(runtime_dir) if runtime_dir else default_runtime_dir()
        self._auth_file = str(auth_file) if auth_file else (os.environ.get("GITS_PI_AUTH") or str(self._runtime_dir / "auth.json"))
        self._provider, self._faux, self._command = provider, faux_responses, command   # command: подмена процесса в тестах
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._handlers: set[asyncio.Task] = set()
        self._inbox: asyncio.Queue | None = None
        self._ready: asyncio.Future | None = None
        self._write_lock: asyncio.Lock | None = None
        self._running = False
        self._closing = False

    # процесс ──────────────────────────────────────────────────────

    def _resolve_command(self) -> list[str]:
        if self._command:
            return self._command
        node = shutil.which("node")
        if not node:
            raise PiAgentError("не найден node: рабочий агент pi не запустить (установи Node.js)")
        worker = self._runtime_dir / "worker.mjs"
        if not worker.is_file():
            raise PiAgentError(f"нет файла {worker}")
        if not (self._runtime_dir / "node_modules").is_dir():
            raise PiAgentError(f"в {self._runtime_dir} не выполнен npm ci")
        return [node, str(worker)]

    @staticmethod
    def _clean_env() -> dict[str, str]:
        """Окружение процесса без секретов: встроенные инструменты (bash) его наследуют."""
        return {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in _SECRET_WORDS)}

    def _init_message(self) -> dict:
        msg: dict = {
            "type": "init", "provider": self._provider, "model": self._model, "thinking": self._thinking,
            "auth_file": self._auth_file, "cwd": self.cwd, "tools": self._tools,
            "custom_tools": [{"name": t.name, "description": t.description, "parameters": t.parameters}
                             for t in self._custom.values()],
            "skills_dir": self._skills_dir, "session_file": self._session_file, "max_turns": self._max_turns,
        }
        if self._system_file:
            msg["system_file"] = self._system_file
        else:
            msg["system"] = self._system or ""
        if self._faux is not None:
            msg["faux_responses"] = self._faux
        return msg

    async def _spawn(self) -> None:
        await self._kill()
        self._closing = False
        command = self._resolve_command()
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=READ_LIMIT, cwd=str(self._runtime_dir if self._runtime_dir.is_dir() else Path.cwd()),
                env=self._clean_env(), **kwargs)
        except Exception as e:
            self._proc = None
            raise PiAgentError(f"рабочий агент pi не запустился: {e}") from e
        loop = asyncio.get_running_loop()
        self._inbox = asyncio.Queue()
        self._ready = loop.create_future()
        self._write_lock = asyncio.Lock()
        proc = self._proc
        self._tasks = [loop.create_task(self._read_stdout(proc)), loop.create_task(self._read_stderr(proc))]
        await self._send(self._init_message())
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self._kill()
            raise PiAgentError("рабочий агент pi не ответил на init вовремя") from None
        except Exception as e:
            await self._kill()
            raise PiAgentError(str(e) or "рабочий агент pi не запустился") from e

    def _alive(self) -> bool:
        r = self._ready
        return (self._proc is not None and self._proc.returncode is None and r is not None and r.done()
                and not r.cancelled() and r.exception() is None)

    async def _kill(self) -> None:
        for h in list(self._handlers):
            h.cancel()
        self._handlers.clear()
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        for t in self._tasks:
            if t is not asyncio.current_task():
                t.cancel()
        self._tasks = []
        self._ready = None

    async def _send(self, msg: dict) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            return False
        try:
            async with self._write_lock:
                proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                await proc.stdin.drain()
            return True
        except Exception as e:
            log.warning("рабочий агент pi: запись не удалась (%s)", e)
            return False

    async def _read_stderr(self, proc) -> None:
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                log.info("[pi-агент] %s", line.decode("utf-8", "replace").rstrip()[:500])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("stderr агента: %s", e)

    async def _read_stdout(self, proc) -> None:
        reason = "рабочий агент pi завершился"
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._on_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = f"рабочий агент pi: сбой чтения ({e})"
        if self._proc is proc and not self._closing:
            self._on_death(reason)

    def _on_death(self, reason: str) -> None:
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(PiAgentError(reason))
        if self._inbox is not None:
            self._inbox.put_nowait({"type": "end", "status": "error", "error": reason, "usage": {}, "turns": 0, "_dead": True})

    def _on_message(self, m: dict) -> None:
        t = m.get("type")
        if t == "ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(True)
        elif t == "error" and m.get("fatal"):
            err = PiAgentError(str(m.get("message") or "ошибка рабочего агента pi"))
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(err)
            elif self._inbox is not None:
                self._inbox.put_nowait({"type": "end", "status": "error", "error": str(err), "usage": {}, "turns": 0})
        elif t == "permission":
            self._spawn_handler(self._answer_permission(m))
        elif t == "tool_call":
            self._spawn_handler(self._answer_custom(m))
        elif t == "error":
            log.warning("рабочий агент pi: %s", m.get("message"))
        elif self._inbox is not None and t in ("text_delta", "text", "tool_use", "end"):
            self._inbox.put_nowait(m)

    def _spawn_handler(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _answer_permission(self, m: dict) -> None:
        call_id = m.get("call_id")
        allow, reason = False, "разрешение не получено"
        try:
            if self._can_use_tool is not None:
                conv = to_claude(str(m.get("tool")), m.get("input") or {}, self.cwd)
                allow, reason = await self._can_use_tool(conv["tool"], conv["input"])
                allow = allow is True
        except asyncio.CancelledError:
            raise
        except Exception as e:                        # любой сбой проверки это отказ
            log.warning("проверка разрешения упала: %s", e)
            allow, reason = False, "проверка разрешения не удалась"
        await self._send({"type": "permission_result", "call_id": call_id, "allow": allow, "reason": str(reason or "")})

    async def _answer_custom(self, m: dict) -> None:
        tool = self._custom.get(str(m.get("name")))
        ok, text = False, f"нет инструмента {m.get('name')}"
        if tool is not None:
            try:
                ok, text = True, str(await tool.handler(m.get("args") or {}))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ok, text = False, str(e) or "ошибка инструмента"
        await self._send({"type": "tool_result", "call_id": m.get("call_id"), "ok": ok, "text": text})

    # публичный интерфейс ──────────────────────────────────────────

    async def run(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Один ход. Заканчивается ровно одним `EndEvent`. Закрытие генератора раньше конца обрывает ход."""
        if self._running:
            raise PiAgentError("ход уже идёт")
        self._running = True
        finished = False
        try:
            if not self._alive():
                await self._spawn()
            while not self._inbox.empty():             # хвост прошлого хода
                self._inbox.get_nowait()
            rid = uuid.uuid4().hex[:8]
            if not await self._send({"type": "prompt", "id": rid, "text": prompt}):
                raise PiAgentError("рабочий агент pi не принимает запросы")
            while True:
                m = await self._inbox.get()
                t = m["type"]
                if t == "text_delta":
                    yield TextDelta(str(m.get("delta") or ""))
                elif t == "text":
                    yield TextEvent(str(m.get("text") or ""))
                elif t == "tool_use":
                    conv = to_claude(str(m.get("tool")), m.get("input") or {}, self.cwd)
                    yield ToolUse(str(m.get("tool")), m.get("input") or {}, conv["tool"], conv["input"])
                else:
                    finished = True
                    yield EndEvent(str(m.get("status")), m.get("error"), m.get("usage") or {}, int(m.get("turns") or 0))
                    if m.get("_dead"):
                        await self._kill()
                    return
        finally:
            self._running = False
            if not finished and self._alive():
                await self._send({"type": "abort"})    # генератор закрыли раньше конца: ход обрываем

    async def abort(self) -> None:
        await self._send({"type": "abort"})

    async def close(self) -> None:
        self._closing = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            await self._send({"type": "shutdown"})
            try:
                await asyncio.wait_for(proc.wait(), SHUTDOWN_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("рабочий агент pi не закрылся сам, убиваю")
        await self._kill()

    async def ask(self, prompt: str) -> tuple[str, EndEvent]:
        """Удобный вызов: последний текст модели и итог хода."""
        last, end = "", EndEvent("error", "нет итога")
        async for ev in self.run(prompt):
            if isinstance(ev, TextEvent):
                last = ev.text
            elif isinstance(ev, EndEvent):
                end = ev
        return last, end
