"""Режим аренды модуля монтажа: контейнер-пространство, которое агент берёт и возвращает как семафор.

Без `GITS_CORE_URL` и с `GITS_CONTROL_TOKEN` (или `GITS_LEASE_MODE=1`) модуль при старте к ядру НЕ подключается: поднимает сервер
управления (`gits_sdk.control.ControlServer`, порт `GITS_CONTROL_PORT`, по умолчанию 8780) и ждёт аренды.
`acquire` приносит адрес и токен ядра хозяина: модуль строит клиента и подключается к нему; `release` или обрыв WebSocket
(у аренды нет срока и продления). `release` при идущих операциях (фоновые рендеры, озвучка, финал) НЕ отменяет их: аренда переходит
в `releasing` (`on_release` возвращает `False`), связь с ядром остаётся, операции доделываются, и когда активных не осталось, модуль
сам завершает освобождение (`finish_release`): клиент отключается, сайт показывает свободный модуль. Хозяин может вернуться
(`acquire` тем же персонажем). `workspace.afk` аренду не трогает вовсе: окружение остаётся за хозяином, операции идут. Рабочие
единицы (папки роликов) лежат на диске в папке хозяина и переживают возврат аренды: при повторном входе `recover` их возвращает.
Обрыв связи с ядром отменяет работающие операции и освобождает аренду сразу.
Ресурсы хозяина: при `GITS_CHARACTERS_ROOT` берутся из `<root>/<персонаж>/montage` (`Layout.for_owner`), других персонажей
модуль не видит. Без токена управления модуль работает как раньше: подключается к ядру при старте (хостовый запуск, тесты).
"""
import asyncio
import logging
import os
import signal
from pathlib import Path

from gits_sdk.control import ControlServer, Lease

from layout import DEFAULT, Layout
from manifest import ENV_OUT_DIR, MANIFEST
from montage_site import Site

log = logging.getLogger("montage.lease")

ENV_CONTROL_TOKEN = "GITS_CONTROL_TOKEN"
ENV_CONTROL_PORT = "GITS_CONTROL_PORT"
ENV_CHARACTERS_ROOT = "GITS_CHARACTERS_ROOT"
ENV_LEASE_MODE = "GITS_LEASE_MODE"
DEFAULT_PORT = 8780
CONNECT_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 10.0


def enabled(env=None) -> bool:
    """Режим аренды: есть токен управления и нет адреса ядра (или `GITS_LEASE_MODE=1`)."""
    env = os.environ if env is None else env
    return bool(env.get(ENV_CONTROL_TOKEN)) and (not env.get("GITS_CORE_URL") or env.get(ENV_LEASE_MODE) == "1")


class LeaseHost:
    def __init__(self, build_fn, run_fn, *, layout: Layout = DEFAULT, characters_root: Path | str | None = None,
                 connect_timeout: float = CONNECT_TIMEOUT_S):
        """build_fn(layout, url=, token=) -> (клиент, менеджер) (`main.build`); run_fn(клиент, менеджер) -> корутина работы (`main.run`)."""
        self.build_fn, self.run_fn, self.layout = build_fn, run_fn, layout
        self.characters_root = Path(characters_root) if characters_root else None
        self.connect_timeout = connect_timeout
        self.server: ControlServer | None = None
        self.site: Site | None = None
        self.client = self.manager = self.task = None
        self.lease: Lease | None = None
        self._out_dir_before: str | None = None
        self._closing = False

    # ── аренда ──────────────────────────────────────────────────────────────

    async def acquire(self, lease: Lease) -> None:
        layout = self.layout
        if self.characters_root is not None:
            layout = self.layout.for_owner(lease.character, self.characters_root)
            self._out_dir_before = os.environ.get(ENV_OUT_DIR)
            os.environ[ENV_OUT_DIR] = str(layout.character / "out")       # готовые ролики хозяина в его папке
        self._closing = False
        client, manager = self.build_fn(layout, url=lease.core_url, token=lease.token)
        self.client, self.manager, self.lease = client, manager, lease
        if self.site is not None:
            manager.site = self.site

        @client.on_disconnect
        async def lost():                          # WebSocket оборвался: аренда отпускается (отдельной задачей, не из потока клиента)
            self._spawn_lost(lease)

        self.task = asyncio.create_task(self.run_fn(client, manager))
        self.task.add_done_callback(lambda t: self._spawn_lost(lease) if not self._closing else None)
        try:
            await client.wait_connected(self.connect_timeout)
        except Exception as e:
            await self._teardown()
            raise RuntimeError("не удалось подключиться к ядру хозяина") from e
        if self.site is not None and self.lease is lease:
            self.site.manager = manager
        log.info("аренда: подключён к ядру хозяина %s", lease.character)

    def active_jobs(self) -> int:
        return self.manager.active_jobs() if self.manager is not None else 0

    async def release(self, lease: Lease) -> bool | None:
        """`release`: есть идущие операции: False (аренда ждёт). Конец операций (`finished`), обрыв связи и остановка: сразу
        (при обрыве и остановке операции отменяются)."""
        if lease.why in ("release", "finished") and self.active_jobs() > 0:
            self.manager.on_idle = self._on_idle
            log.info("аренда: хозяин %s вышел, операций в работе %d: жду конца", lease.character, self.active_jobs())
            return False
        await self._teardown(cancel=lease.why not in ("release", "finished"))
        log.info("аренда отпущена (хозяин %s)", lease.character)
        return None

    def _on_idle(self) -> None:
        lease = self.lease
        if self.server is not None and lease is not None and lease.releasing and not self._closing:
            asyncio.get_running_loop().create_task(self.server.finish_release(lease.lease_id))

    def _spawn_lost(self, lease: Lease) -> None:
        if self.server is not None and not self._closing:
            asyncio.get_running_loop().create_task(self.server.connection_lost(lease.lease_id))

    async def _teardown(self, cancel: bool = True) -> None:
        """Отменить идущие операции (если cancel), отключить клиента, забыть состояние хозяина."""
        self._closing = True
        if self.site is not None:
            self.site.manager = None
        client, manager, task = self.client, self.manager, self.task
        self.client = self.manager = self.task = self.lease = None
        if manager is not None:
            manager.on_idle = None
            for job_id in (manager.running_job_ids() if cancel else []):
                try:
                    await manager.cancel(job_id)
                except Exception:
                    log.exception("не удалось отменить операцию %s при возврате аренды", job_id)
        if client is not None:
            await client.stop()
        if task is not None:
            try:
                await asyncio.wait_for(task, STOP_TIMEOUT_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                log.exception("сбой остановки работы модуля")
        if self.characters_root is not None:
            if self._out_dir_before is None:
                os.environ.pop(ENV_OUT_DIR, None)
            else:
                os.environ[ENV_OUT_DIR] = self._out_dir_before


async def serve(build_fn, run_fn, *, env=None) -> int:
    """Сервер управления и ожидание аренды до остановки процесса."""
    env = os.environ if env is None else env
    host = LeaseHost(build_fn, run_fn, characters_root=env.get(ENV_CHARACTERS_ROOT) or None)
    server = ControlServer(id="montage", token=env[ENV_CONTROL_TOKEN], ownership="shared", manifest=MANIFEST,
                           on_acquire=host.acquire, on_release=host.release, jobs=host.active_jobs)
    host.server = server
    host.site = Site(None, persistent=True)
    await server.start("0.0.0.0", int(env.get(ENV_CONTROL_PORT) or DEFAULT_PORT))
    stop = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    except (NotImplementedError, RuntimeError, ValueError):               # Windows: остановка по Ctrl+C
        pass
    try:
        await host.site.start()
        await stop.wait()
    finally:
        await server.stop()
        await host.site.close()
    return 0
