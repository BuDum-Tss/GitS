"""Пауза на время планирования постройки и разметка прямоугольной комнаты.

Пока она раскладывает стены и двери, пешки не должны бежать строить недоразмеченное: игра ставится на паузу
(если она на ходу, а не на паузе игрока). Маркер в памяти говорит: паузу поставила она, снять её тоже нужно ей.
Страховка в контроллере снимает такую паузу в конце хода, чтобы игра не осталась стоять."""
import json
import time
from pathlib import Path

_MARK = Path(__file__).parent / "memory" / "planner_paused.json"


def mark() -> None:
    try:
        _MARK.parent.mkdir(parents=True, exist_ok=True)
        _MARK.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    except OSError:
        pass


def marked() -> bool:
    return _MARK.exists()


def active(max_age_s: float = 1800) -> bool:
    """Паузу поставило планирование прямо сейчас (маркер свежий). Старый маркер это забытый след, он паузу не оправдывает."""
    try:
        return time.time() - _MARK.stat().st_mtime < max_age_s
    except OSError:
        return False


def clear() -> None:
    try:
        _MARK.unlink()
    except OSError:
        pass


def rect_perimeter(x1: int, z1: int, x2: int, z2: int) -> list[dict]:
    """Клетки внешнего контура прямоугольника (углы включены), x1..x2 и z1..z2 в любом порядке."""
    x1, x2, z1, z2 = min(x1, x2), max(x1, x2), min(z1, z2), max(z1, z2)
    return [{"x": x, "z": z} for x in range(x1, x2 + 1) for z in range(z1, z2 + 1)
            if x in (x1, x2) or z in (z1, z2)]


def rect_room(x1: int, z1: int, x2: int, z2: int, door_x: int, door_z: int):
    """Прямоугольная комната: стены по контуру, дверь в одной клетке контура (не в углу), внутренняя клетка.
    Возвращает (стены, двери, inside, ошибка)."""
    x1, x2, z1, z2 = min(x1, x2), max(x1, x2), min(z1, z2), max(z1, z2)
    if x2 - x1 < 2 or z2 - z1 < 2:
        return [], [], {}, "комната слишком мала: по каждой стороне нужно минимум 3 клетки (две стены и клетка внутри)"
    perimeter = rect_perimeter(x1, z1, x2, z2)
    door = {"x": door_x, "z": door_z}
    if door not in perimeter:
        return [], [], {}, f"дверь ({door_x},{door_z}) должна стоять на контуре комнаты (стена), а не внутри или снаружи"
    if door_x in (x1, x2) and door_z in (z1, z2):
        return [], [], {}, "дверь нельзя ставить в угол: выбери клетку между углами"
    walls = [c for c in perimeter if c != door]
    return walls, [door], {"x": (x1 + x2) // 2, "z": (z1 + z2) // 2}, None
