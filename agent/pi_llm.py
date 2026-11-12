"""Быстрая модель через pi (подписка ChatGPT): тот же интерфейс, что у `claude_llm` (`ask`, `ask_stream`, `image_ok`).

Один общий долгоживущий процесс Node `services/agent-runtime/llm.mjs` на ядро (протокол в README папки): «один вопрос, один
ответ», без агента и инструментов, запросы идут параллельно. Поднимается при первом вызове и заново после гибели. Тайм-аут,
отмена задачи и закрытие генератора шлют процессу `cancel`. Ошибки как у `claude_llm`: RuntimeError (пустой ответ, нет входа,
сбой поставщика), TimeoutError. Вход по подписке (`auth.json`) создаётся вручную, содержимое я не читаю и не логирую.
"""
import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid

from adapters.runtime_pi import BRIDGE_DIR

logger = logging.getLogger("pi_llm")

READY_TIMEOUT_S = 40.0
SHUTDOWN_TIMEOUT_S = 6.0
READ_LIMIT = 4 * 1024 * 1024


def image_ok(image_b64: str | None) -> bool:
    """Проверка, что картинка это base64 (как в `claude_llm`)."""
    try:
        return bool(image_b64) and bool(base64.b64decode(image_b64[:64] + "=" * (-len(image_b64[:64]) % 4)))
    except Exception:
        return False


def _settings_cfg() -> dict:
    import settings
    return settings.load()


class PiLlmClient:
    def __init__(self, command: list[str] | None = None, provider: str = "openai", auth_file: str | None = None,
                 faux_responses: list | None = None, faux_tps: int | None = None):
        self._command = command             # тесты подставляют поддельный процесс; иначе node llm.mjs
        self._provider, self._auth_file = provider, auth_file
        self._faux, self._faux_tps = faux_responses, faux_tps
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._ready: asyncio.Future | None = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._spawn_lock: asyncio.Lock | None = None
        self._write_lock: asyncio.Lock | None = None
        self._closing = False

    # ── процесс ────────────────────────────────────────────────────

    def _alive(self) -> bool:
        ready = self._ready
        return (self._proc is not None and self._proc.returncode is None and ready is not None and ready.done()
                and not ready.cancelled() and ready.exception() is None)

    def _resolve_command(self) -> tuple[list[str] | None, str]:
        if self._command:
            return self._command, ""
        node = shutil.which("node")
        if not node:
            return None, "не найден node: быстрая модель pi не запустится (установи Node.js)"
        script = BRIDGE_DIR / "llm.mjs"
        if not script.is_file():
            return None, f"нет файла {script}"
        if not (BRIDGE_DIR / "node_modules").is_dir():
            return None, f"в {BRIDGE_DIR} не выполнен npm install"
        return [node, str(script)], ""

    def _init_message(self) -> dict:
        auth = self._auth_file or os.environ.get("GITS_PI_AUTH") or str(BRIDGE_DIR / "auth.json")
        msg: dict = {"type": "init", "provider": self._provider, "auth_file": auth}
        if self._faux is not None:
            msg["faux_responses"] = self._faux
        if self._faux_tps:
            msg["faux_tps"] = self._faux_tps
        return msg

    async def _ensure(self) -> None:
        if self._alive():
            return
        if self._spawn_lock is None:
            self._spawn_lock = asyncio.Lock()
        async with self._spawn_lock:
            if self._alive():
                return
            problem = await self._spawn()
            if problem:
                raise RuntimeError(problem)

    async def _spawn(self) -> str | None:
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
                limit=READ_LIMIT, cwd=str(BRIDGE_DIR if BRIDGE_DIR.is_dir() else os.getcwd()), **kwargs)
        except Exception as e:
            self._proc = None
            return f"быстрая модель pi не запустилась: {e}"
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._write_lock = asyncio.Lock()
        proc = self._proc
        self._tasks = [loop.create_task(self._read_stdout(proc)), loop.create_task(self._read_stderr(proc))]
        await self._send(self._init_message())
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self._kill()
            return "быстрая модель pi не ответила на init вовремя"
        except Exception as e:
            await self._kill()
            return str(e) or "быстрая модель pi не запустилась"
        return None

    async def _kill(self) -> None:
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
        self._fail_all("быстрая модель pi завершилась")

    def _fail_all(self, reason: str) -> None:
        for q in list(self._queues.values()):
            q.put_nowait({"type": "failed", "error": reason})

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
            logger.warning(f"быстрая модель pi: запись не удалась ({e})")
            return False

    async def _read_stderr(self, proc) -> None:
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                logger.info("[pi-llm] " + line.decode("utf-8", "replace").rstrip()[:500])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"stderr pi-llm: {e}")

    async def _read_stdout(self, proc) -> None:
        reason = "быстрая модель pi завершилась"
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
            reason = f"быстрая модель pi: сбой чтения ({e})"
        if self._proc is proc and not self._closing:
            logger.warning(reason)
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(RuntimeError(reason))
            self._ready = None              # процесса нет: следующий вызов поднимет его заново
            self._fail_all(reason)

    def _on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(True)
        elif kind == "error":
            text = str(msg.get("message") or "ошибка быстрой модели pi")
            if msg.get("fatal"):
                logger.error(f"быстрая модель pi: {text}")
                if self._ready is not None and not self._ready.done():
                    self._ready.set_exception(RuntimeError(text))
                else:
                    self._fail_all(text)
            else:
                logger.warning(f"быстрая модель pi: {text}")
        elif kind in ("delta", "done", "failed"):
            q = self._queues.get(str(msg.get("id") or ""))
            if q is not None:
                q.put_nowait(msg)

    # ── запросы ────────────────────────────────────────────────────

    async def _events(self, system: str, user: str, image_b64: str | None, stream: bool):
        """Сообщения `delta` и итог (`done` / `failed`) одного запроса; на выходе из генератора запрос обрывается."""
        await self._ensure()
        cfg = _settings_cfg()
        rid = uuid.uuid4().hex
        q: asyncio.Queue = asyncio.Queue()
        self._queues[rid] = q
        finished = False
        try:
            sent = await self._send({
                "type": "complete", "id": rid, "model": cfg.get("pi_model") or "gpt-6-luna",
                "thinking": cfg.get("pi_thinking") or "minimal", "system": system, "user": user,
                "image_b64": image_b64 if image_b64 and image_ok(image_b64) else None, "image_mime": "image/jpeg",
                "stream": stream})
            if not sent:
                finished = True
                raise RuntimeError("быстрая модель pi не принимает запись")
            while True:
                msg = await q.get()
                if msg.get("type") in ("done", "failed"):
                    finished = True
                yield msg
                if finished:
                    return
        finally:
            self._queues.pop(rid, None)
            if not finished:                # тайм-аут, отмена задачи, закрытый генератор
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._send({"type": "cancel", "id": rid}))

    async def ask(self, system: str, user: str, timeout: float, image_b64: str | None = None) -> str:
        async def go() -> str:
            async for msg in self._events(system, user, image_b64, False):
                if msg["type"] == "failed":
                    raise RuntimeError(f"pi: {msg.get('error') or 'сбой поставщика'}")
                if msg["type"] == "done":
                    text = str(msg.get("text") or "").strip()
                    if not text:
                        raise RuntimeError("pi вернул пустой ответ")
                    return text
            raise RuntimeError("pi вернул пустой ответ")
        return await asyncio.wait_for(go(), timeout)

    async def ask_stream(self, system: str, user: str, timeout: float):
        got = False
        async with asyncio.timeout(timeout):
            events = self._events(system, user, None, True)
            try:
                async for msg in events:
                    if msg["type"] == "delta" and msg.get("text"):
                        got = True
                        yield msg["text"]
                    elif msg["type"] == "failed":
                        raise RuntimeError(f"pi: {msg.get('error') or 'сбой поставщика'}")
                    elif msg["type"] == "done" and not got and str(msg.get("text") or "").strip():
                        got = True
                        yield str(msg["text"])
            finally:
                await events.aclose()
        if not got:
            raise RuntimeError("pi вернул пустой ответ")

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
                logger.warning("быстрая модель pi не закрылась сама, завершаю")
        await self._kill()


_client: PiLlmClient | None = None


def client() -> PiLlmClient:
    global _client
    if _client is None:
        _client = PiLlmClient()
    return _client


def set_client(c: PiLlmClient | None) -> None:
    """Подмена общего клиента (тесты)."""
    global _client
    _client = c


async def ask(system: str, user: str, timeout: float, image_b64: str | None = None, contour: str = "fast") -> str:
    """Ответ pi целиком. RuntimeError: нет входа, сбой поставщика, пустой ответ; TimeoutError: не уложился в `timeout`."""
    return await client().ask(system, user, timeout, image_b64=image_b64)


async def ask_stream(system: str, user: str, timeout: float):
    """Ответ pi потоком кусков текста. Закрытие генератора обрывает запрос."""
    gen = client().ask_stream(system, user, timeout)
    try:
        async for piece in gen:
            yield piece
    finally:
        await gen.aclose()


async def close() -> None:
    if _client is not None:
        await _client.close()
