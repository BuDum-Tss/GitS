"""Запуск RimWorld: найти игру, не запускать вторую копию, дождаться, пока мод ответит.

Путь берётся из GITS_RIMWORLD_EXE, иначе типовые места. Игра стартует отдельным процессом (закрытие бота её не
закрывает). «Готова» значит: процесс жив и мод AgentBridge отвечает на порту 8765 (это бывает уже в главном меню).
"""
import glob
import subprocess
import time
from pathlib import Path

import psutil

import gamenet
from envcompat import env

PROCESS = "RimWorldWin64.exe"
_CANDIDATES = (r"C:\GOG Games\RimWorld\RimWorldWin64.exe",
               r"C:\Program Files (x86)\Steam\steamapps\common\RimWorld\RimWorldWin64.exe",
               r"C:\Program Files\Steam\steamapps\common\RimWorld\RimWorldWin64.exe",
               r"D:\SteamLibrary\steamapps\common\RimWorld\RimWorldWin64.exe")


def find_exe() -> str | None:
    hint = env("GITS_RIMWORLD_EXE")
    for path in ([hint] if hint else []) + list(_CANDIDATES) + glob.glob(r"C:\*\RimWorld\RimWorldWin64.exe"):
        if path and Path(path).is_file():
            return path
    return None


def is_running() -> bool:
    return any((p.info["name"] or "").lower() == PROCESS.lower() for p in psutil.process_iter(["name"]))


def mod_ready() -> bool:
    return gamenet.port_open(8765)


def spawn_detached(exe: str, cwd: str):
    """Запустить программу так, чтобы она НЕ была потомком бота: перезапуск бота (taskkill /T убивает дерево потомков)
    её не закрывает. Родителем становится промежуточный cmd, который сразу завершается."""
    subprocess.Popen(["cmd.exe", "/c", "start", "", "/D", cwd, exe], creationflags=0x08000000, close_fds=True)


def launch(wait_seconds: float = 120.0) -> str:
    """Запустить игру и дождаться мода. Возвращает человеческое описание итога; бросает RuntimeError, если не вышло."""
    if is_running():
        return ("игра уже запущена, мод отвечает" if mod_ready() else
                "игра уже запущена, мод ещё загружается (подожди минуту-две)")
    exe = find_exe()
    if exe is None:
        raise RuntimeError("не нашла RimWorldWin64.exe: укажи путь в переменной GITS_RIMWORLD_EXE")
    spawn_detached(exe, str(Path(exe).parent))
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(3)
        if mod_ready():
            return "игра запущена, мод ответил (сейчас главное меню, дальше загрузка колонии)"
        if not is_running():
            raise RuntimeError("игра запустилась и сразу закрылась: смотри журнал RimWorld")
    return "игра запущена, но мод пока не отвечает (загрузка может идти ещё; проверь через минуту)"
