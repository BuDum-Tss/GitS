"""Запись автозапуска трея в реестре (HKCU\\...\\Run): разбор и проверка, что она не протухла.

Запись хранит команду `"<pythonw>" "<tray.py>"`. Если проект переехал в другую папку или Python обновился, путь указывает в никуда,
Windows при входе молча ничего не запускает, и трей пропадает. Чистые функции без реестра и побочных эффектов (их проверяют тесты)."""
import re
from pathlib import Path

_QUOTED = re.compile(r'"([^"]+)"')


def command(pythonw: str, script: Path | str) -> str:
    return f'"{pythonw}" "{script}"'


def parts(value: str | None) -> tuple[str, str] | None:
    """(интерпретатор, скрипт) из значения записи; None, если значение не нашей формы."""
    found = _QUOTED.findall(value or "")
    return (found[0], found[1]) if len(found) >= 2 else None


def is_stale(value: str | None) -> bool:
    """Запись есть, но интерпретатор или tray.py по её путям не существуют (или формат чужой)."""
    p = parts(value)
    return p is None or not Path(p[0]).is_file() or not Path(p[1]).is_file()
