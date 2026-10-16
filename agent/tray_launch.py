"""Трей всегда запускается вместе с ядром (`main.py`).

Ядро при старте запускает `tray.py` отдельным процессом. Повторный запуск безопасен: трей держит мьютекс `Local\\AssistantTray`
и сам тихо выходит, если уже работает. Отключается переменной `GITS_NO_TRAY=1` (тесты, прогон на устойчивость, ядро без экрана) и
не нужен вне Windows. Решение не зависит от того, кто запустил ядро: трей, ярлык или консоль. Чистые функции, процесс создаёт
переданная `spawn` (в тестах заглушка)."""
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

HERE = Path(__file__).resolve().parent
NO_TRAY_ENV = "GITS_NO_TRAY"
_DETACHED = 0x00000008 | 0x00000200 | 0x08000000     # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def wanted(env: Mapping[str, str], platform: str) -> bool:
    """Нужен ли трей: Windows и не выключен переменной окружения."""
    return platform == "win32" and str(env.get(NO_TRAY_ENV, "")).strip().lower() not in ("1", "true", "yes", "on")


def command(executable: str, here: Path = HERE) -> list[str]:
    """Команда запуска: `pythonw tray.py` (без консоли), если pythonw рядом с интерпретатором."""
    exe = Path(executable)
    w = exe.with_name("pythonw.exe")
    return [str(w if w.exists() else exe), str(here / "tray.py")]


def ensure_tray(*, env: Mapping[str, str] | None = None, platform: str = sys.platform, executable: str = sys.executable,
                here: Path = HERE, spawn: Callable[..., object] = subprocess.Popen) -> bool:
    """Запустить трей. True, если процесс создан; сбой запуска никогда не роняет ядро."""
    if not wanted(os.environ if env is None else env, platform):
        return False
    if not (here / "tray.py").is_file():
        return False
    try:
        spawn(command(executable, here), cwd=str(here), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, creationflags=_DETACHED, close_fds=True)
    except OSError:
        return False
    return True
