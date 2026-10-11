"""Точка входа модуля питомца (ТЗ 13). Запускает супервизор ядра (`pythonw main.py` из папки модуля, modules.yaml).

tkinter работает в главном потоке, клиент модуля (`ModuleClient.run()`) в отдельном потоке со своим циклом asyncio (PET-3);
между ними очередь `queue.Queue`. GITS_PET_HEADLESS=1 запускает то же без окна и без горячих клавиш (проверка запуска).
"""
import logging
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SDK до установки пакета glados-module; свои файлы модуля первыми
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "sdk")]

import os  # noqa: E402

from bridge import ClientThread  # noqa: E402
from manifest import build_manifest  # noqa: E402
from pet_logic import PetController  # noqa: E402

log = logging.getLogger("pet")
PUMP_S = 0.05


class HeadlessView:
    """Окна нет: только запоминает, что нарисовал бы (проверка запуска и тесты)."""

    def __init__(self):
        self.visible: bool | None = None
        self.frames: list[tuple] = []

    def set_visible(self, visible: bool) -> None:
        self.visible = visible

    def draw(self, sticker: str, text: str, bubble_visible: bool) -> None:
        self.frames.append((sticker, text, bubble_visible))


def run_headless(thread: ClientThread, messages: "queue.Queue") -> None:
    controller = PetController(HeadlessView(), messages)
    while thread.is_alive():
        controller.pump()
        time.sleep(PUMP_S)


def run_window(thread: ClientThread, messages: "queue.Queue") -> int:
    import pet_window
    sprites = pet_window.load_sprites()
    if not sprites[0]:
        log.error("Нет стикеров в modules/pet/static/stickers/")
        return 1
    holder: dict = {}
    win = pet_window.PetWindow(lambda: holder["controller"].state, sprites)
    holder["controller"] = controller = PetController(win, messages, on_settings=win.on_settings)
    win.pump = controller.pump
    pet_window.HotkeyThread(lambda: controller.state.hotkey_input, win.toggle_mute, win.request_input).start()
    log.info("GLaDOS запущена — верхний правый угол. ПКМ спрячет, вернуть можно из трея.")
    win.run(should_stop=lambda: not thread.is_alive())
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    headless = os.getenv("GITS_PET_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")
    if not headless:
        import pet_window
        if pet_window.already_running():
            log.info("Питомец уже запущен.")
            sys.exit(0)
    messages: queue.Queue = queue.Queue()
    thread = ClientThread(build_manifest(), messages)
    thread.start()
    try:
        if headless:
            run_headless(thread, messages)
            code = 0
        else:
            code = run_window(thread, messages)
    except KeyboardInterrupt:
        code = 0
    finally:
        thread.request_stop()
        thread.join(5)
    sys.exit(code or thread.exit_code or 0)


if __name__ == "__main__":
    threading.current_thread().name = "pet-main"
    main()
