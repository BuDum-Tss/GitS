"""Хостовый помощник RimWorld: запуск игры, окно, UI, запись забега и прокси к моду для модуля в контейнере.

Запуск на хосте Windows из этой папки: `python main.py` (токен в GITS_RIMWORLD_HOST_TOKEN обязателен, порт GITS_RIMWORLD_HOST_PORT,
по умолчанию 8796; слушает только 127.0.0.1). Контракт: docs/RIMWORLD_CONTAINER.md, раздел «Контракт помощника».
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent / "agent"
# Данные помощника (протокол событий, видео) лежат в host/memory (или GITS_RIMWORLD_HOST_MEMORY): до импорта остального.
os.environ["MEMORY_DIR"] = os.getenv("GITS_RIMWORLD_HOST_MEMORY") or str(HERE / "memory")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(AGENT))                      # LocalHostBridge оборачивает модули из agent/


def _ensure_sdk() -> None:
    """gits_sdk.envcompat нужен мосту; обычно SDK установлен, иначе ищем в GITS_SDK_DIR или в sdk/ репозитория."""
    try:
        import gits_sdk  # noqa: F401
        return
    except ImportError:
        pass
    for candidate in (os.getenv("GITS_SDK_DIR"), HERE.parents[1] / "sdk"):
        if candidate and (Path(candidate) / "gits_sdk").is_dir():
            sys.path.append(str(candidate))
            return


_ensure_sdk()

import nowindow  # noqa: E402,F401  (скрывает окна консоли у дочерних процессов, как в модуле)
from aiohttp import web  # noqa: E402

import events_log  # noqa: E402
from host_bridge import LocalHostBridge  # noqa: E402
from server import build_app  # noqa: E402

logger = logging.getLogger("rimworld_host")
DEFAULT_PORT = 8796
BIND = "127.0.0.1"          # только петля: контейнер достаёт помощника по host.docker.internal


async def serve(token: str, port: int) -> None:
    mem = os.environ["MEMORY_DIR"]
    events_log.init(mem)
    bridge = LocalHostBridge()
    bridge.make_recorder(mem, lambda: {"record_runs": True}, max_hours=lambda: 6)     # решение «писать ли» принимает модуль
    app = build_app(bridge, token, rimworld_api=os.getenv("RIMWORLD_API") or None, wire=True)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, BIND, port).start()
    logger.info(f"Хостовый помощник RimWorld: http://{BIND}:{port} (прокси к моду: /game-api)")
    try:
        await asyncio.Event().wait()
    finally:
        rec = bridge._recorder.close("helper_stopped") if bridge._recorder else None
        if rec is not None:
            await asyncio.wait_for(asyncio.to_thread(bridge._recorder.stop_blocking, rec), 20)   # файл фрагментированный: записанное цело
        await runner.cleanup()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    token = os.getenv("GITS_RIMWORLD_HOST_TOKEN", "").strip()
    if not token:
        print("GITS_RIMWORLD_HOST_TOKEN не задан: без токена помощник не стартует (придумай длинную случайную строку).",
              file=sys.stderr)
        sys.exit(2)
    try:
        port = int(os.getenv("GITS_RIMWORLD_HOST_PORT") or DEFAULT_PORT)
    except ValueError:
        print("GITS_RIMWORLD_HOST_PORT: нужно целое число", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(serve(token, port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
