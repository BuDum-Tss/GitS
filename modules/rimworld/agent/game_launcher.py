"""Запуск RimWorld: найти игру, не запускать вторую копию, дождаться, пока мод ответит.

Путь берётся из GLADOS_RIMWORLD_EXE, иначе типовые места. Игра стартует отдельным процессом (закрытие бота её не
закрывает). «Готова» значит: процесс жив и мод AgentBridge отвечает на порту 8765 (это бывает уже в главном меню).
"""
import glob
import json
import logging
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

import psutil

import events_log
import gamenet

logger = logging.getLogger("game_launcher")
NOTIFY_URL = "http://localhost:8766/api/notify"          # дашборд ядра: объявление пользователю вне процесса модуля
NOTIFY_SOURCE = "rimworld-tests"
NOTIFY_TIMEOUT_S = 2
# Уведомитель процесса модуля (REC-7): ставит main.py. Получает текст-факт, возвращает True, если передал его ядру.
notifier: Callable[[str], bool] | None = None

PROCESS = "RimWorldWin64.exe"
_CANDIDATES = (r"C:\GOG Games\RimWorld\RimWorldWin64.exe",
               r"C:\Program Files (x86)\Steam\steamapps\common\RimWorld\RimWorldWin64.exe",
               r"C:\Program Files\Steam\steamapps\common\RimWorld\RimWorldWin64.exe",
               r"D:\SteamLibrary\steamapps\common\RimWorld\RimWorldWin64.exe")


def find_exe() -> str | None:
    env = os.getenv("GLADOS_RIMWORLD_EXE")
    for path in ([env] if env else []) + list(_CANDIDATES) + glob.glob(r"C:\*\RimWorld\RimWorldWin64.exe"):
        if path and Path(path).is_file():
            return path
    return None


def is_running() -> bool:
    return any((p.info["name"] or "").lower() == PROCESS.lower() for p in psutil.process_iter(["name"]))


def mod_ready() -> bool:
    return gamenet.port_open(8765)


# Окно без рамки вместо полноэкранного: полноэкранная RimWorld сворачивается, как только другое окно берёт фокус,
# а свёрнутое окно не записать. Пользовательские настройки (Prefs.xml) при этом не трогаются.
def windowed_args() -> tuple:
    """Аргументы запуска: окно без рамки ровно по физическому размеру экрана (разрешение из Prefs.xml может не совпадать
    с экраном, и тогда карта рисуется не на весь кадр)."""
    try:
        import win32gui
        import win32print
        dc = win32gui.GetDC(0)
        w, h = win32print.GetDeviceCaps(dc, 118), win32print.GetDeviceCaps(dc, 117)    # DESKTOPHORZRES, DESKTOPVERTRES
        win32gui.ReleaseDC(0, dc)
        if w >= 800 and h >= 600:
            return ("-popupwindow", "-screen-width", str(w), "-screen-height", str(h))
    except Exception:
        pass
    return ("-popupwindow",)


def set_notifier(fn: Callable[[str], bool] | None):
    global notifier
    notifier = fn


def launch_forbidden() -> bool:
    """GLADOS_NO_LAUNCH=1: игру запускать нельзя (тесты, `tools/produce_run.py`, проверки без игры)."""
    return os.getenv("GLADOS_NO_LAUNCH") == "1"


def _post_notify(text: str) -> bool:
    """Объявление через дашборд ядра (вне процесса модуля: тесты, скрипты). Ядро недоступно: тихо ничего."""
    try:
        req = urllib.request.Request(os.getenv("GLADOS_NOTIFY_URL", NOTIFY_URL),
                                     data=json.dumps({"text": text, "source": NOTIFY_SOURCE}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=NOTIFY_TIMEOUT_S):
            return True
    except Exception as e:
        logger.info(f"Ядро не приняло объявление о запуске игры: {e}")
        return False


def _deliver(text: str, kind: str | None) -> str:
    """Объявить пользователю факт о действии с игрой (REC-7, GT-1, GT-2): запись в протокол (`kind`) и факт для ядра. В процессе
    модуля факт идёт через `core_link` (`notifier`), иначе HTTP на дашборд ядра (тихо, если ядра нет). Текст это факт:
    фразу из него делает персонаж. Возвращает текст."""
    if kind:
        events_log.log(kind, text)
    logger.info(text)
    sent = False
    if notifier is not None:
        try:
            sent = bool(notifier(text))
        except Exception as e:
            logger.info(f"Уведомитель не сработал: {e}")
    if not sent:
        _post_notify(text)
    return text


def announce_fact(text: str, kind: str | None = "game") -> str:
    """Объявить пользователю факт о действии с игрой (GT-1, GT-2): протокол (`kind`; None: не писать) и ядро, как у `announce`."""
    return _deliver(text, kind)


# DP-2: факт для модели, не готовая реплика. Прежняя форма «Запускаю RimWorld: …» читалась как действие пользователя
# («Наконец-то решил поиграть?»), поэтому в факте явно сказано, что игру запускает сама система/персонаж, а не человек.
SELF_LAUNCH_PREFIX = "Ты сама запускаешь RimWorld (не пользователь): "


def announce(reason: str) -> str:
    """Объявить пользователю, что игра сейчас запустится, кто её запускает и почему (REC-7, DP-2). Возвращает текст."""
    return _deliver(f"{SELF_LAUNCH_PREFIX}{reason}", "game")


# Правило 14 (объявления о долгих запусках): то же для других заметных действий на ПК модуля (запись забега, монтаж,
# ход по меню игры), не только для запуска игры. Факт про «не пользователь» тот же, что у DP-2.
SELF_ACTION_PREFIX = "Ты сама делаешь это на ПК (не пользователь): "


def announce_self(fact: str, kind: str = "game") -> str:
    """Объявить пользователю заметное действие модуля на ПК, `fact` - что делается и зачем (правило 14). Идёт через
    `announce_fact`: протокол и ядро (через уведомитель процесса модуля, а из скриптов и тестов - HTTP на дашборд)."""
    return announce_fact(f"{SELF_ACTION_PREFIX}{fact}", kind)


def launch_env_on() -> bool:
    """Модуль сам держит игру запущенной: GITS_LAUNCH_GAME=1 (новое имя) или GLADOS_LAUNCH_GAME=1 (прежнее)."""
    return "1" in (os.getenv("GITS_LAUNCH_GAME"), os.getenv("GLADOS_LAUNCH_GAME"))


def spawn_detached(exe: str, cwd: str, args: tuple = ()):
    """Запустить программу так, чтобы она НЕ была потомком бота: перезапуск бота (taskkill /T убивает дерево потомков)
    её не закрывает. Родителем становится промежуточный cmd, который сразу завершается.
    GLADOS_NO_LAUNCH=1 запрещает запуск совсем: единственное место, где игра запускается, поэтому защита стоит здесь."""
    if launch_forbidden():
        raise RuntimeError("запуск игры запрещён переменной GLADOS_NO_LAUNCH=1")
    subprocess.Popen(["cmd.exe", "/c", "start", "", "/D", cwd, exe, *args], creationflags=0x08000000, close_fds=True)


def launch(reason: str, wait_seconds: float = 120.0, args: tuple = ()) -> str:
    """Запустить игру и дождаться мода. `reason` обязателен: кто и зачем запускает (факт: «запись забега: игры не было»,
    «тест запуска ТЗ 11b»); пользователь слышит его до запуска (`announce`). Возвращает человеческое описание итога; бросает
    RuntimeError, если не вышло или запуск запрещён."""
    if not reason or not reason.strip():
        raise ValueError("launch: укажи reason, кто и зачем запускает игру")
    if is_running():
        return ("игра уже запущена, мод отвечает" if mod_ready() else
                "игра уже запущена, мод ещё загружается (подожди минуту-две)")
    exe = find_exe()
    if exe is None:
        raise RuntimeError("не нашла RimWorldWin64.exe: укажи путь в переменной GLADOS_RIMWORLD_EXE")
    if launch_forbidden():
        raise RuntimeError("запуск игры запрещён переменной GLADOS_NO_LAUNCH=1")
    announce(reason)
    spawn_detached(exe, str(Path(exe).parent), args)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(3)
        if mod_ready():
            return "игра запущена, мод ответил (сейчас главное меню, дальше загрузка колонии)"
        if not is_running():
            raise RuntimeError("игра запустилась и сразу закрылась: смотри журнал RimWorld")
    return "игра запущена, но мод пока не отвечает (загрузка может идти ещё; проверь через минуту)"
