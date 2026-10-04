"""Скриншоты: окно игры или весь экран. Файлы лежат в memory/screenshots/.

Игру снимает без вопросов (там нет личного). Весь экран может захватить чужие окна, поэтому
инструмент «экран» всегда спрашивает разрешение, и сам снимок не делается, если активно окно
с личным содержимым (пароли, банк, приватный режим).
Снимок игры берётся с экрана: окно RimWorld должно быть видно и на переднем плане.
"""
import os
import time
from pathlib import Path

from PIL import ImageGrab

import perception

KEEP = 40            # сколько последних снимков хранить
GAME_TITLE = "rimworld"


class ShotError(Exception):
    pass


def _dir() -> Path:
    base = os.getenv("MEMORY_DIR") or str(Path(__file__).resolve().parent / "memory")
    d = Path(base) / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def shots_dir() -> Path:
    return _dir()


def resolve(name: str) -> Path | None:
    """Имя файла → путь внутри папки снимков (только имя, без путей)."""
    p = _dir() / os.path.basename(name)
    return p if p.is_file() else None


def last() -> Path | None:
    files = sorted(_dir().glob("shot_*.*"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _game_window(focus: bool):
    import pygetwindow as gw
    for w in gw.getAllWindows():
        t = (w.title or "").strip().lower()
        if t.startswith(GAME_TITLE) and w.visible:
            return w
    return None


def _save(img, kind: str) -> Path:
    d = _dir()
    path = d / f"shot_{time.strftime('%Y%m%d_%H%M%S')}_{kind}.png"
    img.save(path, "PNG", optimize=True)
    if path.stat().st_size > 8 * 1024 * 1024:       # Bot API: фото не больше 10 МБ
        path.unlink()
        path = path.with_suffix(".jpg")
        img.convert("RGB").save(path, "JPEG", quality=88)
    for old in sorted(d.glob("shot_*.*"), key=lambda p: p.stat().st_mtime)[:-KEEP]:
        old.unlink(missing_ok=True)
    return path


def grab_game(focus: bool = False) -> Path:
    w = _game_window(focus)
    if w is None:
        raise ShotError("окна RimWorld нет: игра не запущена")
    fg = perception.foreground()
    on_top = fg is not None and fg["title"].strip().lower().startswith(GAME_TITLE) and not w.isMinimized
    if not on_top:
        if not focus:
            raise ShotError(("RimWorld свёрнута" if w.isMinimized else "RimWorld сейчас не на переднем плане")
                            + ", снимок не получится или выйдет с чужими окнами "
                            "(можно повторить с focus=true: она сама переключится на игру)")
        if w.isMinimized:
            w.restore()
        w.activate()
        time.sleep(0.9)
    l, t, r, b = w.left, w.top, w.right, w.bottom
    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True).convert("RGB")
    return _save(img, "game")


def grab_screen() -> Path:
    fg = perception.foreground()
    if fg is not None and perception.is_sensitive(fg):
        raise ShotError("сейчас активно окно с личным содержимым (пароли, банк, приватный режим): снимать не буду")
    return _save(ImageGrab.grab(all_screens=False).convert("RGB"), "screen")
