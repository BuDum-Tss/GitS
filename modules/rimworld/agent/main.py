"""Точка входа модуля RimWorld (ТЗ 11): игровые контуры, API оверлея на порту 8767, связь с ядром по контракту модулей.

Запускает супервизор ядра (`agent/modules.yaml` в GitS): `python -u main.py` из папки agent/. Модуль сам подключается к ядру
(`ws://127.0.0.1:8766/module/ws`, токен из GLADOS_MODULE_TOKEN), присылает манифест (инструменты MCP-сервера игры и `order`),
состояние и события, говорит и показывает эмоции через ядро. Без игры не падает: контуры простаивают, `game_running: false`.
Вручную (ядро должно работать): `python main.py`; токен тогда берётся из файла ядра.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Данные модуля (цели, задачи, журнал, настройки, видео) лежат в agent/memory рядом с кодом. Супервизор ядра передаёт MEMORY_DIR
# своей, ядровой папки: она модулю не подходит, поэтому заменяется до импорта остального (settings читает её при импорте).
os.environ["MEMORY_DIR"] = os.getenv("RIMWORLD_MEMORY_DIR") or str(HERE / "memory")
sys.path.insert(0, str(HERE))


def _ensure_sdk() -> None:
    """Пакет glados-module обычно установлен или лежит в PYTHONPATH (супервизор добавляет GitS/sdk). Ручной запуск без того и
    другого: ищем SDK в GLADOS_SDK_DIR или в sdk/ этого репозитория."""
    try:
        import glados_module  # noqa: F401
        return
    except ImportError:
        pass
    for candidate in (os.getenv("GLADOS_SDK_DIR"), HERE.parents[2] / "sdk"):
        if candidate and (Path(candidate) / "glados_module").is_dir():
            sys.path.append(str(candidate))
            return


_ensure_sdk()

import nowindow  # noqa: E402,F401  (побочный эффект: скрывает окна консоли)
from dotenv import load_dotenv  # noqa: E402

# Загружаем .env до импорта controller (на случай если задан ANTHROPIC_API_KEY)
load_dotenv()

import fast_brain  # noqa: E402
import game_launcher  # noqa: E402
import game_api  # noqa: E402
import game_panel  # noqa: E402
import module_manifest  # noqa: E402
import settings  # noqa: E402
import usage_report  # noqa: E402
from camera_director import CameraDirector  # noqa: E402
from commentator import Commentator  # noqa: E402
from controller import Controller  # noqa: E402
from core_link import CoreLink  # noqa: E402
from glados_module import ModuleClient  # noqa: E402
from run_recorder import RunRecorder  # noqa: E402
from state_reporter import StateReporter  # noqa: E402

logger = logging.getLogger("rimworld")

STOP_RECORDING_S = 8        # на остановку записи при выходе: ядро ждёт завершения модуля 10 секунд


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error(f"Фоновая задача модуля упала: {task.exception()!r}", exc_info=task.exception())


async def amain(config: dict) -> int:
    """Работа модуля до shutdown ядра. Возвращает код выхода (0, или 2 если ядро отклонило манифест)."""
    for noisy in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    mem_dir = config["memory_dir"]
    settings.init(mem_dir)
    loop = asyncio.get_running_loop()

    manifest = await module_manifest.build()
    tools = [t for t in manifest["tools"] if t["name"] not in ("order", "launch_game")]
    client = ModuleClient(manifest)
    link = CoreLink(client)
    usage_report.attach(client, loop)

    def _announce_launch(text: str) -> bool:
        """Объявление о запуске игры идёт через ядро (`say`, факт); вызывается из любого потока, ответа не ждёт."""
        if not link.online:
            return False
        asyncio.run_coroutine_threadsafe(link.announce(text), loop)
        return True
    game_launcher.set_notifier(_announce_launch)

    ctrl = Controller(config)
    ctrl.link = link
    ctrl.recorder = RunRecorder(mem_dir, settings.load,
                                max_hours=lambda: link.module_settings.get("max_record_hours", 6))
    ctrl.director = CameraDirector()
    reporter = StateReporter(client, recording=lambda: game_panel.recording_info(ctrl.recorder.current),
                             policy=ctrl.policy_state)
    ctrl.on_phase_change = reporter.poke
    fast_brain.on_publish = lambda evt, res: loop.create_task(link.emotion(evt, res["emotion"]))
    ctrl.commentator = Commentator(mem_dir, link.say, link.last_user_ts, link.is_speaking, repeats=link.repeats)
    ctrl.commentator.enabled = lambda: ctrl.policies.in_focus                  # ТЗ 11e POL-11: вне фокуса молчит

    module_manifest.attach(client, tools, order=ctrl.handle_order, graph=game_api.graph_data,
                           launch=ctrl.launch_game, focus=ctrl)                # ТЗ 11e: launch_game, policy.activate, focus.release
    api = await game_api.start(mem_dir, rimworld_url=config["rimworld_api"], core=client)
    logger.info(f"Манифест: {len(manifest['tools'])} инструментов, ядро {client.url}")

    work = [loop.create_task(coro) for coro in (ctrl.commentator.run(), reporter.run())]
    game = loop.create_task(ctrl._run_async())            # игровые контуры; падение контроллера останавливает модуль
    core = loop.create_task(link.run())
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
        await api.cleanup()


async def _stop_recording(ctrl: Controller) -> None:
    """Остановить запись забега, если она идёт: файл фрагментированный, записанное не теряется."""
    rec = ctrl.recorder.close("module_stopped") if ctrl.recorder is not None else None
    if rec is None:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(RunRecorder._stop_blocking, rec), STOP_RECORDING_S)
    except asyncio.TimeoutError:
        logger.warning("Запись забега не остановилась за отведённое время")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Agent SDK использует либо ANTHROPIC_API_KEY либо OAuth из ~/.claude/.credentials.json
    config = {
        "rimworld_api": os.getenv("RIMWORLD_API", "http://localhost:8765"),
        "memory_dir":   os.environ["MEMORY_DIR"],
    }
    try:
        code = asyncio.run(amain(config))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
