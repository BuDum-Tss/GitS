"""Режим аренды модуля RimWorld в контейнере: общее окружение, которое агент берёт и возвращает как семафор (по образцу монтажа).

Контейнер `rimworld` при старте к ядру НЕ подключается: поднимает сервер управления (`gits_sdk.control.ControlServer`, порт
`GITS_CONTROL_PORT`, по умолчанию 8782, слушает 0.0.0.0 внутри контейнера, токен `GITS_CONTROL_TOKEN`) и ждёт аренды. `acquire`
приносит адрес и токен ядра хозяина: модуль строит клиента и подключается к нему; `release` или обрыв WebSocket отпускают аренду
(срока и продления нет). Токен ядра хозяина живёт только в `Lease` и в журнал не попадает; секретов `.env` в контейнере нет.

Режим включается ЯВНО: токен управления и `GITS_RIMWORLD_CONTAINER=1` (образ ставит её сам) либо `GITS_LEASE_MODE=1`. Одного токена
мало: на хосте тот же `GITS_CONTROL_TOKEN` лежит в общем `.env`, и ручной запуск модуля без аренды не должен менять режим.

Что передаёт `main.py`: `build(url=, token=)` (обычная или async функция) возвращает `(client, ctx)`: клиент шины (`WorkspaceClient`)
и всё остальное, что построено вокруг него (контроллер, обработчики); `run(client, ctx)` корутина работы модуля до остановки клиента
(`client.stop()`); её завершение само по себе тоже отпускает аренду. Игра и хостовые части (окно, ввод, запись) идут через
`HostBridge` по `GITS_RIMWORLD_HOST_URL`, к аренде это отношения не имеет.
"""
import asyncio
import inspect
import logging
import os
import signal

from gits_sdk.control import ControlServer, Lease

log = logging.getLogger("rimworld.lease")

ENV_CONTROL_TOKEN = "GITS_CONTROL_TOKEN"
ENV_CONTROL_PORT = "GITS_CONTROL_PORT"
ENV_CONTAINER = "GITS_RIMWORLD_CONTAINER"
ENV_LEASE_MODE = "GITS_LEASE_MODE"
ENV_CHARACTERS_ROOT = "GITS_CHARACTERS_ROOT"
ENV_CHARACTER_DIR = "GITS_RIMWORLD_CHARACTER_DIR"        # папка хозяина аренды (ресурсы персонажа), пока аренда идёт
DEFAULT_PORT = 8782
CONNECT_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 10.0


def enabled(env=None) -> bool:
    """Режим аренды: есть токен управления и явная пометка контейнера (`GITS_RIMWORLD_CONTAINER=1`) или `GITS_LEASE_MODE=1`."""
    env = os.environ if env is None else env
    return bool(env.get(ENV_CONTROL_TOKEN)) and (env.get(ENV_CONTAINER) == "1" or env.get(ENV_LEASE_MODE) == "1")


class LeaseHost:
    def __init__(self, build_fn, run_fn, *, characters_root: str | None = None, connect_timeout: float = CONNECT_TIMEOUT_S,
                 stop_timeout: float = STOP_TIMEOUT_S):
        self.build_fn, self.run_fn = build_fn, run_fn
        self.characters_root = characters_root
        self.connect_timeout, self.stop_timeout = connect_timeout, stop_timeout
        self.server: ControlServer | None = None
        self.client = self.ctx = self.task = None
        self.lease: Lease | None = None
        self._closing = False
        self._character_dir_before: str | None = None

    # ── аренда ──────────────────────────────────────────────────────────────

    async def acquire(self, lease: Lease) -> None:
        if self.characters_root:                                      # папка хозяина: /characters/<id>, других модуль не видит
            if lease.character in ("", ".", "..") or any(c in lease.character for c in "/\\"):
                raise ValueError("недопустимый id персонажа")
            self._character_dir_before = os.environ.get(ENV_CHARACTER_DIR)
            os.environ[ENV_CHARACTER_DIR] = os.path.join(self.characters_root, lease.character)
        self._closing = False
        try:
            built = self.build_fn(url=lease.core_url, token=lease.token)
            if inspect.isawaitable(built):
                built = await built
            client, ctx = built
        except Exception:
            self._restore_env()
            raise
        self.client, self.ctx, self.lease = client, ctx, lease

        @client.on_disconnect
        async def lost():                          # WebSocket оборвался: аренда отпускается (отдельной задачей, не из потока клиента)
            self._spawn_lost(lease)

        self.task = asyncio.create_task(self.run_fn(client, ctx))
        self.task.add_done_callback(lambda t: self._spawn_lost(lease) if not self._closing else None)
        try:
            await client.wait_connected(self.connect_timeout)
        except Exception as e:
            await self._teardown()
            raise RuntimeError("не удалось подключиться к ядру хозяина") from e
        log.info("аренда: подключён к ядру хозяина %s", lease.character)

    async def release(self, lease: Lease) -> bool | None:
        """`release`, обрыв связи, остановка сервера: отключиться от ядра и забыть состояние хозяина."""
        await self._teardown()
        log.info("аренда отпущена (хозяин %s)", lease.character)
        return None

    def _spawn_lost(self, lease: Lease) -> None:
        if self.server is not None and not self._closing:
            asyncio.get_running_loop().create_task(self.server.connection_lost(lease.lease_id))

    def _restore_env(self) -> None:
        if not self.characters_root:
            return
        if self._character_dir_before is None:
            os.environ.pop(ENV_CHARACTER_DIR, None)
        else:
            os.environ[ENV_CHARACTER_DIR] = self._character_dir_before

    async def _teardown(self) -> None:
        self._closing = True
        client, task = self.client, self.task
        self.client = self.ctx = self.task = self.lease = None
        if client is not None:
            await client.stop()
        if task is not None:
            try:
                await asyncio.wait_for(task, self.stop_timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                log.exception("сбой остановки работы модуля")
        self._restore_env()


async def serve(build_fn, run_fn, *, manifest: dict | None = None, env=None) -> int:
    """Сервер управления и ожидание аренды до остановки процесса. manifest: для `GET /info` (по умолчанию строится из модуля)."""
    env = os.environ if env is None else env
    if manifest is None:
        import module_manifest
        manifest = await module_manifest.build()
    host = LeaseHost(build_fn, run_fn, characters_root=env.get(ENV_CHARACTERS_ROOT) or None)
    server = ControlServer(id="rimworld", token=env[ENV_CONTROL_TOKEN], ownership="shared", manifest=manifest,
                           on_acquire=host.acquire, on_release=host.release)
    host.server = server
    await server.start("0.0.0.0", int(env.get(ENV_CONTROL_PORT) or DEFAULT_PORT))
    stop = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    except (NotImplementedError, RuntimeError, ValueError):               # Windows: остановка по Ctrl+C
        pass
    try:
        await stop.wait()
    finally:
        await host._teardown()
        await server.stop()
    return 0
