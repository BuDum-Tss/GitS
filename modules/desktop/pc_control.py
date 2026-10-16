"""Управление компьютером: мышь и клавиатура на ВЕСЬ экран (не только окно игры), модуль desktop (ТЗ 14).

Реализация инструментов (синхронные функции, их вызывают в потоке). Описания и параметры инструментов лежат в
манифесте модуля (manifest.py), обработчики - в app.py. Ввод в окна с личным содержимым (пароли, банк, приватный
режим) запрещён совсем. Опасные сочетания клавиш и разрушительный текст спрашиваются всегда (`danger_check`).

Как она находит, куда нажимать: click_text ищет надпись на экране локальным OCR (RapidOCR) и нажимает в её центр,
поэтому координаты угадывать не нужно.
"""
import ctypes
import difflib
import re
import time
from pathlib import Path

import perception
from gits_sdk import safety
from gits_sdk import ToolError

_DANGEROUS_KEYS = {"alt+f4", "win+r", "win+x", "ctrl+alt+del", "ctrl+alt+delete", "ctrl+shift+esc"}
_MAX_TYPED = 500

_user32 = ctypes.windll.user32
_PUL = ctypes.POINTER(ctypes.c_ulong)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _INPUTUNION)]


def _pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = True    # мышь в левый верхний угол прерывает управление
    pyautogui.PAUSE = 0.05
    return pyautogui


def _guard():
    """Нельзя вводить в окно с личным содержимым."""
    win = perception.foreground()
    if win and perception.is_sensitive(win):
        raise ToolError("активное окно содержит личное (пароли, банк, приватный режим): ввод в него запрещён")


def _type_unicode(text: str):
    """Набор любых символов (включая русские) через SendInput, а не через раскладку клавиатуры."""
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        code = int.from_bytes(units[i:i + 2], "little")
        for flags in (0x0004, 0x0004 | 0x0002):     # UNICODE, затем UNICODE|KEYUP
            inp = _INPUT(type=1, ii=_INPUTUNION(ki=_KEYBDINPUT(0, code, flags, 0, None)))
            _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        time.sleep(0.01)


def _screen_size() -> tuple[int, int]:
    return _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


def _check_xy(x: int, y: int):
    w, h = _screen_size()
    if not (0 <= x < w and 0 <= y < h):
        raise ToolError(f"точка ({x}, {y}) вне экрана {w}x{h}")


# ── реализации (синхронные, вызываются в потоке) ──────────────────────────────────

def _list_windows() -> str:
    import pygetwindow as gw
    seen, out = set(), []
    for w in gw.getAllWindows():
        title = (w.title or "").strip()
        if not title or title in seen or not w.visible or w.width < 100:
            continue
        low = title.lower()
        if any(b in low for b in perception.BLOCK_WORDS):
            continue    # личное в списке не показываем
        seen.add(title)
        out.append(title[:70])
    if not out:
        raise ToolError("открытых окон не нашла")
    return "; ".join(out[:15])


def _focus_window(title: str) -> str:
    import pygetwindow as gw
    needle = title.lower()
    for w in gw.getAllWindows():
        t = (w.title or "").strip()
        if t and needle in t.lower() and w.visible:
            if any(b in t.lower() for b in perception.BLOCK_WORDS):
                raise ToolError("это окно с личным содержимым, переключаться на него не буду")
            if w.isMinimized:
                w.restore()
            w.activate()
            return f"переключилась на окно «{t[:60]}»"
    raise ToolError(f"окно «{title}» не нашла")


def _norm(t: str) -> str:
    return re.sub(r"[^\w ]+", "", re.sub(r"\s+", " ", t.lower())).strip()


def _find_text(boxes: list, needle: str) -> list:
    """Где на экране надпись: целиком в одном блоке OCR, а если фраза разбита на слова по блокам,
    то склеиваются соседние блоки одной строки. Возвращает [(текст, x, y)]."""
    n = _norm(needle)
    if not n:
        return []
    exact = [b for b in boxes if _norm(b[0]) == n]
    if exact:
        return exact
    near = [b for b in boxes if n in _norm(b[0])]
    if near:
        return near
    rows: dict[int, list] = {}
    for b in boxes:
        rows.setdefault(round(b[2] / 24), []).append(b)
    for row in rows.values():
        row.sort(key=lambda b: b[1])
        for i in range(len(row)):
            acc = ""
            for j in range(i, min(i + 6, len(row))):
                acc = (acc + " " + row[j][0]).strip()
                if n in _norm(acc):
                    span = row[i:j + 1]
                    return [(acc, int(sum(b[1] for b in span) / len(span)), int(sum(b[2] for b in span) / len(span)))]
    # последняя ступень: нечёткое сравнение (OCR мог добавить или потерять букву на стыке блоков)
    flat = n.replace(" ", "")
    best, best_ratio = None, 0.8
    for row in rows.values():
        row.sort(key=lambda b: b[1])
        for i in range(len(row)):
            acc = ""
            for j in range(i, min(i + 6, len(row))):
                acc = (acc + " " + row[j][0]).strip()
                ratio = difflib.SequenceMatcher(None, _norm(acc).replace(" ", ""), flat).ratio()
                if ratio > best_ratio:
                    span = row[i:j + 1]
                    best_ratio = ratio
                    best = (acc, int(sum(b[1] for b in span) / len(span)), int(sum(b[2] for b in span) / len(span)))
    return [best] if best else []


def _click_text(text: str, button: str, double: bool) -> str:
    from PIL import ImageGrab
    _guard()
    img = ImageGrab.grab().convert("RGB")
    hits = []
    win = perception.foreground()
    if win:   # сначала только активное окно: OCR всего экрана на процессоре занимает ~10 секунд
        l, t, r, b = win["rect"]
        l, t, r, b = max(0, l), max(0, t), min(img.width, r), min(img.height, b)
        if r - l > 100 and b - t > 100 and (r - l) * (b - t) < 0.8 * img.width * img.height:
            found = perception.ocr_boxes(img.crop((l, t, r, b)))
            hits = _find_text([(tx, x + l, y + t) for tx, x, y in found], text)
    if not hits:
        hits = _find_text(perception.ocr_boxes(img), text)
    if not hits:
        raise ToolError(f"надпись «{text}» на экране не нашла")
    _, x, y = hits[0]
    pg = _pyautogui()
    pg.click(x, y, button=button, clicks=2 if double else 1)
    extra = f" (подходящих надписей было {len(hits)}, выбрала первую)" if len(hits) > 1 else ""
    return f"нажала на «{text}» ({x}, {y}){extra}"


def _click_xy(x: int, y: int, button: str, double: bool) -> str:
    _guard()
    _check_xy(x, y)
    _pyautogui().click(x, y, button=button, clicks=2 if double else 1)
    return f"нажала в точку ({x}, {y})"


def _type(text: str) -> str:
    _guard()
    if len(text) > _MAX_TYPED:
        raise ToolError(f"слишком длинный текст (больше {_MAX_TYPED} символов)")
    pg = _pyautogui()
    for i, line in enumerate(text.split("\n")):
        if i:
            pg.press("enter")
        if line:
            _type_unicode(line)
    return f"набрала текст ({len(text)} символов)"


def _press(keys: str) -> str:
    _guard()
    parts = [k.strip().lower() for k in re.split(r"[+\s]+", keys) if k.strip()]
    if not parts:
        raise ToolError("не указаны клавиши")
    pg = _pyautogui()
    bad = [k for k in parts if k not in pg.KEYBOARD_KEYS]
    if bad:
        raise ToolError(f"неизвестные клавиши: {', '.join(bad)}")
    pg.hotkey(*parts) if len(parts) > 1 else pg.press(parts[0])
    return "нажала " + "+".join(parts)


def _scroll(amount: int, x: int | None, y: int | None) -> str:
    _guard()
    pg = _pyautogui()
    if x is not None and y is not None:
        _check_xy(x, y)
        pg.moveTo(x, y)
    pg.scroll(amount)
    return f"прокрутила на {amount}"


# ── опасное: спрашивается всегда ────────────────────────────────────────────────────

def _danger_key(a: dict) -> str | None:
    combo = "+".join(k for k in re.split(r"[+\s]+", str(a.get("keys", "")).lower()) if k)
    return "это сочетание закрывает окна или вызывает системные окна" if combo in _DANGEROUS_KEYS else None


def _danger_type(a: dict) -> str | None:
    return safety.danger_of("Bash", {"command": str(a.get("text", ""))}, Path.cwd())


