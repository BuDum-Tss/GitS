"""Точка входа модуля RimWorld (ТЗ 11): игровые контуры, API оверлея на порту 8767, связь с ядром по контракту модулей.

Запускает супервизор ядра (`agent/peers.yaml` в GitS): `python -u main.py` из папки agent/. Модуль сам подключается к ядру
(`ws://127.0.0.1:8766/ws`, токен из GITS_TOKEN), присылает манифест (рабочее место RimWorld: узлы, действия, запросы, `launch_game`),
состояние и события, говорит и показывает эмоции через ядро. Без игры не падает: контуры простаивают, `game_running: false`.
Вручную (ядро должно работать): `python main.py`; токен тогда берётся из файла ядра.
"""
import asyncio
import logging
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Данные модуля (цели, задачи, журнал, настройки, видео) лежат в agent/memory рядом с кодом. Супервизор ядра передаёт MEMORY_DIR
# своей, ядровой папки: она модулю не подходит, поэтому заменяется до импорта остального (settings читает её при импорте).
os.environ["MEMORY_DIR"] = os.getenv("RIMWORLD_MEMORY_DIR") or str(HERE / "memory")
sys.path.insert(0, str(HERE))

from gits_sdk.envcompat import env  # noqa: E402


def _ensure_sdk() -> None:
    """Пакет gits-sdk обычно установлен или лежит в PYTHONPATH (супервизор добавляет GitS/sdk). Ручной запуск без того и
    другого: ищем SDK в GITS_SDK_DIR или в sdk/ этого репозитория."""
    try:
        import gits_sdk  # noqa: F401
        return
    except ImportError:
        pass
    for candidate in (env("GITS_SDK_DIR"), HERE.parents[2] / "agent" / "interfaces" / "workspace"):
        if candidate and (Path(candidate) / "gits_sdk").is_dir():
            sys.path.append(str(candidate))
            return


_ensure_sdk()

import nowindow  # noqa: E402,F401  (побочный эффект: скрывает окна консоли)
from dotenv import load_dotenv  # noqa: E402

# Загружаем .env до импорта controller (на случай если задан ANTHROPIC_API_KEY)
load_dotenv()

import gamenet  # noqa: E402
import host_bridge  # noqa: E402
import game_api  # noqa: E402
import game_panel  # noqa: E402
import module_manifest  # noqa: E402
import workspace_module  # noqa: E402
import settings  # noqa: E402
from camera_director import CameraDirector  # noqa: E402
from controller import Controller  # noqa: E402
from core_link import CoreLink  # noqa: E402
from gits_sdk import WorkspaceClient  # noqa: E402
import core_tasks  # noqa: E402
from state_reporter import StateReporter  # noqa: E402

logger = logging.getLogger("rimworld")

STOP_RECORDING_S = 8        # на остановку записи при выходе: ядро ждёт завершения модуля 10 секунд


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error(f"Фоновая задача модуля упала: {task.exception()!r}", exc_info=task.exception())


def _config() -> dict:
    """Настройки процесса из окружения: данные модуля и адрес мода (RIMWORLD_API, в контейнере прокси помощника)."""
    return {
        "rimworld_api": gamenet.base_url(),       # RIMWORLD_API, в контейнере прокси помощника, иначе localhost:8765
        "memory_dir":   os.environ["MEMORY_DIR"],
    }


async def build(url: str | None = None, token: str | None = None, config: dict | None = None):
    """Собрать модуль: клиент шины и всё вокруг него. Возвращает `(client, ctx)`. `url` и `token` (ядро хозяина) приходят из `acquire`
    при аренде, без них клиент берёт их из окружения, как раньше. Фоновые задачи здесь не запускаются: это делает `run`."""
    config = config or _config()
    for noisy in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    mem_dir = config["memory_dir"]
    settings.init(mem_dir)
    loop = asyncio.get_running_loop()

    manifest = await module_manifest.build()
    tools = [t for t in manifest["tools"] if t["name"] not in ("launch_game", "restart_game")]
    client = WorkspaceClient(manifest, url=url, token=token)
    link = CoreLink(client)

    def _announce_launch(text: str) -> bool:
        """Объявление о запуске игры идёт через ядро (`say`, факт); вызывается из любого потока, ответа не ждёт."""
        if not link.online:
            return False
        asyncio.run_coroutine_threadsafe(link.announce(text), loop)
        return True
    host = host_bridge.make_host_bridge()       # Local: хостовые части в этом процессе; Remote: хостовый помощник (контейнер)
    host.set_notifier(_announce_launch)

    ctrl = Controller({**config, "host_bridge": host})
    ctrl.link = link
    ctrl.recorder = host.make_recorder(mem_dir, settings.load,
                                       max_hours=lambda: link.module_settings.get("max_record_hours", 6))
    ctrl.director = CameraDirector()
    node_state = workspace_module.NodeState()
    reporter = StateReporter(client, recording=lambda: game_panel.recording_info(ctrl.recorder.current),
                             policy=ctrl.policy_state, node=lambda: node_state.path)
    node_state.on_change = reporter.poke                 # смена узла уходит ядру сразу (ТЗ 33)
    ctrl.on_phase_change = reporter.poke
    ctrl.on_nodes_reset = node_state.forget_seen          # «виденные» узлы живут до выхода из среды, новой аренды или новой игры

    module_manifest.attach(client, tools, graph=game_api.graph_data,
                           launch=ctrl.launch_game, focus=ctrl, node_state=node_state, restart=ctrl.restart_game)   # ТЗ 11e, ТЗ 33
    ctx = types.SimpleNamespace(config=config, mem_dir=mem_dir, manifest=manifest, link=link, host=host, ctrl=ctrl,
                                reporter=reporter)
    return client, ctx


async def run(client, ctx) -> int:
    """Работа модуля до shutdown ядра, остановки клиента (`client.stop()`, отпускание аренды) или падения игровых контуров. Возвращает
    код выхода (0, или 2 если ядро отклонило манифест) и в конце закрывает всё: запись, события помощника, API оверлея."""
    loop = asyncio.get_running_loop()
    ctrl, host, reporter, link = ctx.ctrl, ctx.host, ctx.reporter, ctx.link
    api = await game_api.start(ctx.mem_dir, rimworld_url=ctx.config["rimworld_api"], core=client)
    logger.info(f"Манифест: {len(ctx.manifest['tools'])} инструментов, ядро {client.url}")

    work = [loop.create_task(coro) for coro in (reporter.run(), core_tasks.run(client))]
    game = loop.create_task(ctrl._run_async())            # игровые контуры; падение контроллера останавливает модуль
    core = loop.create_task(link.run())
    host.start_events()                                   # Remote: события помощника (объявления, протокол записи) идут в те же обработчики
    for task in (*work, game):
        task.add_done_callback(_log_failure)
    try:
        done, _ = await asyncio.wait({core, game}, return_when=asyncio.FIRST_COMPLETED)
        if core in done:
            return core.result()                          # shutdown ядра, вытеснение (0) или отказ ядра (2)
        logger.error("Игровые контуры остановились: модуль завершается, супервизор перезапустит его")
        await client.stop()
        await asyncio.wait({core}, timeout=5)
        return 1
    finally:
        for task in (*work, game):
            task.cancel()
        if not core.done():
            await client.stop()
            await asyncio.wait({core}, timeout=5)
        await _stop_recording(ctrl)
        await host.close()
        await api.cleanup()


async def amain(config: dict) -> int:
    """Обычный запуск (без аренды): `build` из окружения и `run` до shutdown ядра."""
    client, ctx = await build(config=config)
    return await run(client, ctx)


async def _stop_recording(ctrl: Controller) -> None:
    """Остановить запись забега, если она идёт: файл фрагментированный, записанное не теряется."""
    rec = ctrl.recorder.close("module_stopped") if ctrl.recorder is not None else None
    if rec is None:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(ctrl.recorder.stop_blocking, rec), STOP_RECORDING_S)
    except asyncio.TimeoutError:
        logger.warning("Запись забега не остановилась за отведённое время")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    config = _config()
    import lease_host
    if lease_host.enabled():                      # контейнер: сервис ждёт аренды, к ядру хозяина подключается по acquire
        try:
            sys.exit(asyncio.run(lease_host.serve(build, run)))     # build(url=, token=) -> (client, ctx); run(client, ctx)
        except KeyboardInterrupt:
            sys.exit(0)
    try:
        code = asyncio.run(amain(config))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
