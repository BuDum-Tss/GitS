"""AgentRuntime поверх pi (`@earendil-works/pi-agent-core`) через мост Node `services/agent-runtime/bridge.mjs`.

Мост это процесс Node, который говорит со мной строками JSON (протокол в `services/agent-runtime/README.md`): я шлю `init`,
`prompt`, `steer`, `abort`, `tool_result`, он отвечает `text`, `tool_call`, `end`. Инструменты исполняет ядро: на `tool_call`
зову обработчик из `ToolSpec` и возвращаю `tool_result`. История хранится мостом в `resident_pi_session.json` в папке памяти.
Процесс умер или нет Node: ход кончается `EndEvent` со статусом error, следующий ход поднимает мост заново.
Вход по подписке (`auth.json`) создаётся вручную, содержимое я не читаю и не логирую.
"""
import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from contracts import EndEvent, RuntimeEvent, TextEvent, ToolEvent, ToolSpec

logger = logging.getLogger("resident")

BRIDGE_DIR = Path(__file__).resolve().parents[2] / "services" / "agent-runtime"
READY_TIMEOUT_S = 40.0
SHUTDOWN_TIMEOUT_S = 6.0
STEER_ACK_S = 0.2           # мост на удачный steer молчит, на «нет хода» отвечает ошибкой idle: ждём её столько
READ_LIMIT = 4 * 1024 * 1024
MAX_CONTEXT_CHARS = 120000
SESSION_ID = "gits-resident"


def _session_file() -> Path:
    import settings
    return settings.memory_dir() / "resident_pi_session.json"


class PiRuntime:
    can_steer = True

    def __init__(self, *, command: list[str] | None = None, provider: str = "openai", model: str | None = None,
                 thinking: str | None = None, auth_file: str | None = None, session_file: Path | None = None,
                 faux_responses: list | None = None, cwd: str | None = None):
        self._command = command             # тесты подставляют поддельный мост; иначе node bridge.mjs
        self._provider, self._model, self._thinking = provider, model, thinking
        self._auth_file, self._session_file_override = auth_file, session_file
        self._faux = faux_responses
        self._cwd = cwd
        self._system, self._sent_system = "", None
        self._tools: dict[str, ToolSpec] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._handlers: dict[str, asyncio.Task] = {}
        self._events: asyncio.Queue | None = None
        self._ready: asyncio.Future | None = None
        self._reset_fut: asyncio.Future | None = None
        self._idle_fut: asyncio.Future | None = None
        self._write_lock: asyncio.Lock | None = None
        self.restored = 0
        self._closing = False               # мост закрываем сами: его выход не сбой

    # ── настройка ──────────────────────────────────────────────────

    async def start(self, system: str, tools: list[ToolSpec]) -> None:
        self._system = system
        self._tools = {t.name: t for t in tools}

    def _alive(self) -> bool:
        ready = self._ready
        return (self._proc is not None and self._proc.returncode is None and ready is not None and ready.done()
                and not ready.cancelled() and ready.exception() is None)

    def _session_path(self) -> Path:
        return self._session_file_override or _session_file()

    def _resolve_command(self) -> tuple[list[str] | None, str]:
        if self._command:
            return self._command, ""
        node = shutil.which("node")
        if not node:
            return None, "не найден node: мост среды pi не запустить (установи Node.js)"
        bridge = BRIDGE_DIR / "bridge.mjs"
        if not bridge.is_file():
            return None, f"нет файла моста {bridge}"
        if not (BRIDGE_DIR / "node_modules").is_dir():
            return None, f"в {BRIDGE_DIR} не выполнен npm install"
        return [node, str(bridge)], ""

    def _init_message(self) -> dict:
        import settings
        cfg = settings.load()
        auth = self._auth_file or os.environ.get("GITS_PI_AUTH") or str(BRIDGE_DIR / "auth.json")
        msg = {"type": "init", "provider": self._provider, "model": self._model or cfg.get("pi_model") or "gpt-6-luna",
               "thinking": self._thinking or cfg.get("pi_thinking") or "minimal", "system": self._system,
               "tools": [{"name": t.name, "description": t.description, "parameters": t.parameters}
                         for t in self._tools.values()],
               "auth_file": auth, "session_file": str(self._session_path()), "session_id": SESSION_ID,
               "max_context_chars": MAX_CONTEXT_CHARS}
        if self._faux is not None:
            msg["faux_responses"] = self._faux
        return msg

    # ── процесс моста ──────────────────────────────────────────────

    async def _spawn(self) -> str | None:
        """Поднять мост и дождаться `ready`. Возвращает текст ошибки или None."""
        await self._kill()
        self._closing = False
        command, problem = self._resolve_command()
        if command is None:
            return problem
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=READ_LIMIT, cwd=self._cwd or str(BRIDGE_DIR if BRIDGE_DIR.is_dir() else Path.cwd()), **kwargs)
        except Exception as e:
            self._proc = None
            return f"мост среды pi не запустился: {e}"
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._write_lock = asyncio.Lock()
        proc = self._proc
        self._tasks = [loop.create_task(self._read_stdout(proc)), loop.create_task(self._read_stderr(proc))]
        await self._send(self._init_message())
        try:
            self.restored = int(await asyncio.wait_for(asyncio.shield(self._ready), READY_TIMEOUT_S) or 0)
        except asyncio.TimeoutError:
            await self._kill()
            return "мост среды pi не ответил на init вовремя"
        except Exception as e:
            await self._kill()
            return str(e) or "мост среды pi не запустился"
        self._sent_system = self._system
        return None

    async def _kill(self) -> None:
        for h in list(self._handlers.values()):
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
            logger.warning(f"мост среды pi: запись не удалась ({e})")
            return False

    async def _read_stderr(self, proc) -> None:
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                logger.info("[pi-мост] " + line.decode("utf-8", "replace").rstrip()[:500])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"stderr моста: {e}")

    async def _read_stdout(self, proc) -> None:
        reason = "мост среды pi завершился"
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    logger.debug("мост среды pi: строка не JSON")
                    continue
                if isinstance(msg, dict):
                    self._on_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = f"мост среды pi: сбой чтения ({e})"
        if self._proc is proc and not self._closing:
            self._on_death(reason)

    def _put(self, event: RuntimeEvent) -> None:
        if self._events is not None:
            self._events.put_nowait(event)

    def _on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(msg.get("restored") or 0)
        elif kind == "reset_done":
            if self._reset_fut is not None and not self._reset_fut.done():
                self._reset_fut.set_result(True)
        elif kind == "text":
            text = str(msg.get("text") or "").strip()
            if text:
                self._put(TextEvent(text))
        elif kind == "tool_call":
            call_id, name = str(msg.get("call_id") or ""), str(msg.get("name") or "")
            args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
            self._put(ToolEvent(name, args))
            self._handlers[call_id] = asyncio.get_running_loop().create_task(self._exec_tool(call_id, name, args))
        elif kind == "end":
            usage = msg.get("usage")
            self._put(EndEvent(str(msg.get("status") or "ok"), msg.get("error") or None,
                               dict(usage) if isinstance(usage, dict) else {}))
        elif kind == "error":
            self._on_error(msg)

    def _on_error(self, msg: dict) -> None:
        text = str(msg.get("message") or "ошибка моста")
        if msg.get("fatal"):
            logger.error(f"мост среды pi: {text}")
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(RuntimeError(text))
            else:
                self._put(EndEvent("error", text))
                self._fail_waiters()
        elif text == "idle":
            if self._idle_fut is not None and not self._idle_fut.done():
                self._idle_fut.set_result(True)
        elif text == "busy":
            self._put(EndEvent("error", "busy"))
        else:
            logger.warning(f"мост среды pi: {text}")

    def _fail_waiters(self) -> None:
        for fut in (self._reset_fut, self._idle_fut):
            if fut is not None and not fut.done():
                fut.set_result(False)

    def _on_death(self, reason: str) -> None:
        logger.warning(reason)
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(RuntimeError(reason))
        self._put(EndEvent("error", reason))
        self._fail_waiters()
        for h in list(self._handlers.values()):
            h.cancel()
        self._handlers.clear()
        self._ready = None                  # процесса нет: следующий ход поднимет мост заново

    async def _exec_tool(self, call_id: str, name: str, args: dict) -> None:
        spec = self._tools.get(name)
        try:
            if spec is None:
                ok, text = False, f"неизвестный инструмент {name}"
            else:
                ok, text = await spec.handler(args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"инструмент {name}: {e}", exc_info=True)
            ok, text = False, f"сбой инструмента: {e}"
        finally:
            self._handlers.pop(call_id, None)
        await self._send({"type": "tool_result", "call_id": call_id, "ok": bool(ok), "text": str(text)})

    # ── ход ────────────────────────────────────────────────────────

    async def run(self, prompt: str) -> AsyncIterator[RuntimeEvent]:
        if not self._alive():
            problem = await self._spawn()
            if problem:
                logger.error(f"среда pi недоступна: {problem}")
                yield EndEvent("error", problem)
                return
        elif self._sent_system != self._system:
            if await self._send({"type": "set_system", "system": self._system}):
                self._sent_system = self._system
        queue: asyncio.Queue = asyncio.Queue()
        self._events = queue
        started = ended = False
        try:
            if not await self._send({"type": "prompt", "id": uuid.uuid4().hex, "text": prompt}):
                ended = True
                yield EndEvent("error", "мост среды pi не принимает запись")
                return
            started = True
            while True:
                event = await queue.get()
                if isinstance(event, EndEvent):
                    ended = True
                yield event
                if ended:
                    return
        finally:
            if self._events is queue:
                self._events = None
            for h in list(self._handlers.values()):
                h.cancel()
            if started and not ended:           # ход бросили (тайм-аут, отмена): мост не должен доигрывать его в одиночку
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._send({"type": "abort"}))

    async def steer(self, text: str) -> bool:
        if self._events is None or not self._alive():
            return False
        fut = asyncio.get_running_loop().create_future()
        self._idle_fut = fut
        try:
            if not await self._send({"type": "steer", "text": text}):
                return False
            return not await asyncio.wait_for(asyncio.shield(fut), STEER_ACK_S)     # ответ idle: хода уже нет
        except asyncio.TimeoutError:
            return True
        finally:
            if self._idle_fut is fut:
                self._idle_fut = None

    async def abort(self) -> None:
        if self._alive():
            await self._send({"type": "abort"})
        for h in list(self._handlers.values()):
            h.cancel()                      # ждущие инструменты отклоняются сразу

    async def reset(self, forget: bool = True) -> None:
        """`forget`: забыть историю (мост удаляет файл сессии), процесс остаётся; без него: закрыть мост, историю хранит файл."""
        if not forget:
            await self.close()
            return
        if self._alive():
            fut = asyncio.get_running_loop().create_future()
            self._reset_fut = fut
            try:
                if await self._send({"type": "reset"}):
                    await asyncio.wait_for(asyncio.shield(fut), 6)
                    return
            except asyncio.TimeoutError:
                logger.warning("мост среды pi не подтвердил сброс")
            finally:
                if self._reset_fut is fut:
                    self._reset_fut = None
            await self._kill()
        with contextlib.suppress(Exception):
            self._session_path().unlink(missing_ok=True)

    async def close(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None and self._alive():
            self._closing = True
            await self._send({"type": "shutdown"})
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), SHUTDOWN_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning("мост среды pi не закрылся сам, завершаю")
        await self._kill()
