"""Иконка в системном трее: управление ассистентом без окон.

Запуск:  pythonw tray.py   (или python tray.py)

Трей сам поднимает ядро (main.py) и питомца (desktop_pet.py, до ТЗ 13), если они не
запущены. Модули (ТЗ 02) трей не запускает: их запускает и перезапускает супервизор ядра (agent/modules.yaml),
включаются они в дашборде; процессы модулей дети ядра, поэтому остановка и перезапуск ядра (_kill_tree) их тоже
останавливают. Меню:
  - открыть дашборд;
  - показать/скрыть питомца (голос при этом продолжает работать);
  - озвучка вкл/выкл и громкость;
  - зрение, разговорчивость;
  - запуск вместе с Windows (по твоему клику, сама ничего не включает);
  - перезапуск ядра, остановка ассистента.

Настройки лежат в memory/settings.json — тот же файл читают ядро, дашборд
и питомец, поэтому всё синхронно с чатом и кнопками.
"""
import ctypes
import os
import socket
import subprocess
import sys
import threading
import webbrowser
import winreg
from pathlib import Path

import psutil
import pystray
from PIL import Image

import settings

HERE = Path(__file__).resolve().parent
DASHBOARD_URL = "http://localhost:8766/"
CORE_PORT = 8766
HOTKEY_PRESETS = ("Ctrl+Shift+Space", "Alt+Shift+Space", "Ctrl+Alt+A", "Ctrl+Alt+Q")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Assistant"
NO_WINDOW = subprocess.CREATE_NO_WINDOW

MEMORY_DIR = os.getenv("MEMORY_DIR", str(HERE / "memory"))
settings.init(MEMORY_DIR)


# ── процессы ─────────────────────────────────────────────────────────────────

def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def core_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", CORE_PORT)) == 0


def _find(script: str, cwd: Path | None = None) -> list[psutil.Process]:
    """Процессы со скриптом в командной строке и рабочей папкой cwd (по умолчанию: папка ассистента).
    Без проверки папки main.py ассистента путался бы с main.py агента RimWorld."""
    cwd = Path(cwd or HERE)
    found = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(str(a).endswith(script) for a in (p.info["cmdline"] or [])) and Path(p.cwd()) == cwd:
                found.append(p)
        except (psutil.Error, OSError):
            pass
    return found


# ── агент RimWorld: отдельный репозиторий (rimworld-agent), отдельный процесс ──
# устаревший запуск агента игры: удалить в ТЗ 11 (дальше он модуль rimworld, его запускает супервизор ядра)

GAME_AGENT_DIR = Path(os.getenv("RIMWORLD_AGENT_DIR", r"C:\MyProjects\rimworld-agent")) / "agent"


def game_agent_available() -> bool:
    return (GAME_AGENT_DIR / "main.py").exists()


def start_game_agent(icon=None, item=None):
    if not game_agent_available() or _find("main.py", GAME_AGENT_DIR):
        return
    mem = GAME_AGENT_DIR / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    log = open(mem / "core.log", "ab")
    exe = Path(sys.executable).with_name("python.exe")
    subprocess.Popen([str(exe if exe.exists() else sys.executable), "-u", "main.py"], cwd=GAME_AGENT_DIR,
                     stdout=log, stderr=log, env={**os.environ, "MEMORY_DIR": str(mem)}, creationflags=NO_WINDOW)


def restart_game_agent(icon=None, item=None):
    for p in _find("main.py", GAME_AGENT_DIR):
        _kill_tree(p)
    threading.Timer(2.0, start_game_agent).start()


def start_core():
    if core_running():
        return
    Path(MEMORY_DIR).mkdir(parents=True, exist_ok=True)
    log = open(Path(MEMORY_DIR) / "core.log", "ab")
    # python.exe (консольный) со скрытой консолью, а не pythonw: тогда дочерние консольные программы
    # (claude.exe) подключаются к этой скрытой консоли и не открывают свои окна терминала
    exe = Path(sys.executable).with_name("python.exe")
    subprocess.Popen([str(exe if exe.exists() else sys.executable), "-u", "main.py"], cwd=HERE,
                     stdout=log, stderr=log, env={**os.environ, "MEMORY_DIR": MEMORY_DIR},
                     creationflags=NO_WINDOW)
    start_game_agent()          # агент игры идёт вместе с ассистентом (если репозиторий rimworld-agent на месте)


def start_pet():
    if not _find("desktop_pet.py"):
        subprocess.Popen([_pythonw(), "desktop_pet.py"], cwd=HERE, creationflags=NO_WINDOW,
                         env={**os.environ, "MEMORY_DIR": MEMORY_DIR})


def _kill_tree(p: psutil.Process):
    """Остановить процесс вместе с потомками: иначе claude.exe от помощника остаётся жить сиротой."""
    try:
        for child in p.children(recursive=True):
            try:
                child.terminate()
            except psutil.Error:
                pass
        p.terminate()
    except psutil.Error:
        pass


def stop_all():
    for script in ("main.py", "desktop_pet.py"):
        for p in _find(script):
            _kill_tree(p)
    for p in _find("main.py", GAME_AGENT_DIR):
        _kill_tree(p)


def restart_core(icon=None, item=None):
    for p in _find("main.py"):
        _kill_tree(p)
    threading.Timer(2.0, start_core).start()


# ── автозапуск (реестр текущего пользователя, только по клику) ──────────────────

def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def toggle_autostart(icon, item):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if autostart_enabled():
            winreg.DeleteValue(k, RUN_NAME)
        else:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ,
                              f'"{_pythonw()}" "{HERE / "tray.py"}"')


# ── меню ─────────────────────────────────────────────────────────────────────

def _toggle(key):
    def action(icon, item):
        settings.update(**{key: not settings.load()[key]})
    return action


def _toggle_quiet(icon, item):
    """«Не отвлекать»: включить до выключения или выключить (в том числе таймер из чата)."""
    if settings.quiet_active():
        settings.update(dnd=False, quiet_until=0.0)
    else:
        settings.update(dnd=True)


def _toggle_pet(icon, item):
    """Показать/спрятать питомца; если процесс был закрыт, поднимаем его заново."""
    show = not settings.load()["pet_visible"]
    settings.update(pet_visible=show)
    if show:
        start_pet()


def _set(key, value):
    def action(icon, item):
        settings.update(**{key: value})
    return action


def _is(key, value):
    return lambda item: settings.load()[key] == value


def _is_volume(pct):
    # ближайшая ступень к текущей громкости считается выбранной
    def check(item):
        cur = settings.load()["volume"]
        return min((25, 50, 75, 100), key=lambda v: abs(v - cur)) == pct
    return check


def build_menu() -> pystray.Menu:
    M, Menu = pystray.MenuItem, pystray.Menu
    return Menu(
        M("Открыть дашборд", lambda i, it: webbrowser.open(DASHBOARD_URL), default=True),
        Menu.SEPARATOR,
        M("Показывать питомца", _toggle_pet,
          checked=lambda it: settings.load()["pet_visible"]),
        M("Закрепить питомца (не двигается)", _toggle("pet_pinned"),
          checked=lambda it: settings.load()["pet_pinned"]),
        M("Строка ввода (горячая клавиша)", Menu(*[
            M(spec, _set("hotkey_input", spec), checked=_is("hotkey_input", spec), radio=True)
            for spec in HOTKEY_PRESETS])),
        M("Не отвлекать", _toggle_quiet, checked=lambda it: settings.quiet_active()),
        M("Озвучка", lambda i, it: settings.update(voice_muted=not settings.load()["voice_muted"]),
          checked=lambda it: not settings.load()["voice_muted"]),
        M("Громкость", Menu(*[
            M(f"{p}%", _set("volume", p), checked=_is_volume(p), radio=True)
            for p in (25, 50, 75, 100)])),
        Menu.SEPARATOR,
        M("Слушать системный звук", _toggle("hearing"), checked=lambda it: settings.load()["hearing"]),
        M("Режим стримера (комментировать игру)", _toggle("streamer"), checked=lambda it: settings.load()["streamer"]),
        M("Микрофон: разговор голосом", _toggle("mic"), checked=lambda it: settings.load()["mic"]),
        M("Публиковать мысли в Telegram", _toggle("telegram"), checked=lambda it: settings.load()["telegram"]),
        M("Спрашивать подтверждение перед постом", _toggle("telegram_confirm"),
          checked=lambda it: settings.load()["telegram_confirm"]),
        M("Смотреть на экран", _toggle("vision"), checked=lambda it: settings.load()["vision"]),
        M("Разговорчивость", Menu(
            M("Редко", _set("chattiness", "quiet"), checked=_is("chattiness", "quiet"), radio=True),
            M("Обычно", _set("chattiness", "normal"), checked=_is("chattiness", "normal"), radio=True),
            M("Часто", _set("chattiness", "chatty"), checked=_is("chattiness", "chatty"), radio=True))),
        Menu.SEPARATOR,
        M("Авто-разрешать все действия (осторожно)", _toggle("auto_accept"),
          checked=lambda it: settings.load()["auto_accept"]),
        M("Запускать с Windows", toggle_autostart, checked=lambda it: autostart_enabled()),
        M("Перезапустить ядро", restart_core),
        M("Перезапустить агента RimWorld", restart_game_agent, visible=lambda it: game_agent_available()),
        M("Остановить ассистента", lambda i, it: (stop_all(), i.stop())),
        M("Закрыть трей (ассистент продолжит работать)", lambda i, it: i.stop()),
    )


def _icon_image() -> Image.Image:
    path = HERE / "static" / "stickers" / "idle.png"
    try:
        img = Image.open(path).convert("RGBA")
        img.thumbnail((64, 64))
        canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        canvas.paste(img, ((64 - img.width) // 2, (64 - img.height) // 2), img)
        return canvas
    except OSError:
        return Image.new("RGBA", (64, 64), (90, 90, 90, 255))


def _single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AssistantTray")
    return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS


def main():
    if not _single_instance():
        return
    icon = pystray.Icon("assistant", _icon_image(), "Ассистент", build_menu())

    def setup(ic):
        ic.visible = True
        start_core()
        start_pet()

    icon.run(setup)


if __name__ == "__main__":
    main()
