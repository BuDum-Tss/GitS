"""Управление компьютером: мышь и клавиатура на ВЕСЬ экран (не только окно игры).

Инструменты окружения «Компьютер пользователя». Все требуют разрешения (риск ask); поверх этого
отдельные действия спрашиваются всегда, даже при авто-разрешении (опасные сочетания клавиш,
разрушительные команды в вводимом тексте). Ввод в окна с личным содержимым (пароли, банк,
приватный режим) запрещён совсем.

Как она находит, куда нажимать: click_text ищет надпись на экране локальным OCR (RapidOCR) и
нажимает в её центр, поэтому координаты угадывать не нужно.
"""
import asyncio
import ctypes
import difflib
import re
import time
from pathlib import Path

import perception
import safety
from .base import ToolError

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


# ── сборка инструментов ─────────────────────────────────────────────────────────────

def make_tools(_tool) -> list:
    """_tool - фабрика Tool из builtin (одинаковое оформление со встроенными)."""
    async def run(fn, *a):
        return await asyncio.to_thread(fn, *a)

    async def list_windows(a):
        return await run(_list_windows)

    async def focus_window(a):
        return await run(_focus_window, a["title"])

    async def click_text(a):
        return await run(_click_text, a["text"], a.get("button") or "left", bool(a.get("double")))

    async def click(a):
        return await run(_click_xy, int(a["x"]), int(a["y"]), a.get("button") or "left", bool(a.get("double")))

    async def type_text(a):
        return await run(_type, a["text"])

    async def press_key(a):
        return await run(_press, a["keys"])

    async def scroll(a):
        return await run(_scroll, int(a["amount"]), a.get("x"), a.get("y"))

    btn = {"type": "string", "enum": ["left", "right"], "required": False, "description": "кнопка мыши, по умолчанию left"}
    dbl = {"type": "boolean", "required": False, "description": "двойной щелчок"}
    tools = [
        _tool("system", "list_windows", "Перечислить открытые окна на компьютере (заголовки).",
              {}, list_windows, risk="auto", informative=True,
              keywords=("окна", "окно", "открыто", "программы", "приложения")),
        _tool("system", "focus_window", "Запасной уровень (по экрану): используй, только если у программы нет прямого инструмента или адаптера. Переключиться на окно по части заголовка.",
              {"title": {"type": "string", "description": "часть заголовка окна"}}, focus_window, risk="ask",
              keywords=("окно", "переключись", "открой", "перейди", "свернуто")),
        _tool("system", "click_text", "Запасной уровень (по экрану): используй, только если у программы нет прямого инструмента или адаптера. Нажать мышью на надпись на экране (кнопку, ссылку, пункт меню): находит текст по экрану "
              "и кликает в его центр.",
              {"text": {"type": "string", "description": "надпись, на которую нажать"}, "button": btn, "double": dbl},
              click_text, risk="ask", keywords=("нажми", "кликни", "клик", "кнопка", "кнопку", "нажать", "ссылка", "пункт", "меню")),
        _tool("system", "click", "Запасной уровень (по экрану): используй, только если у программы нет прямого инструмента или адаптера. Нажать мышью в точку экрана по координатам (пиксели от левого верхнего угла).",
              {"x": {"type": "integer", "description": "x"}, "y": {"type": "integer", "description": "y"},
               "button": btn, "double": dbl},
              click, risk="ask", keywords=("нажми", "кликни", "координаты", "точка", "клик")),
        _tool("system", "type_text", "Запасной уровень (по экрану): используй, только если у программы нет прямого инструмента или адаптера. Набрать текст на клавиатуре в активном окне (в том числе русский). "
              "Перед этим нужно, чтобы курсор стоял в нужном поле.",
              {"text": {"type": "string", "description": "что набрать (до 500 символов)"}},
              type_text, risk="ask", keywords=("набери", "напечатай", "введи", "впиши", "текст", "клавиатура")),
        _tool("system", "press_key", "Запасной уровень (по экрану): используй, только если у программы нет прямого инструмента или адаптера. Нажать клавишу или сочетание клавиш, например enter, esc, f5, ctrl+s, alt+tab.",
              {"keys": {"type": "string", "description": "клавиши через плюс: ctrl+s"}},
              press_key, risk="ask", keywords=("нажми", "клавиша", "enter", "сохрани", "сочетание", "горячая")),
        _tool("system", "scroll", "Запасной уровень (по экрану): используй, только если у программы нет прямого инструмента или адаптера. Прокрутить колесо мыши (положительное число вверх, отрицательное вниз).",
              {"amount": {"type": "integer", "description": "на сколько щелчков"},
               "x": {"type": "integer", "required": False, "description": "x, где прокрутить"},
               "y": {"type": "integer", "required": False, "description": "y, где прокрутить"}},
              scroll, risk="ask", keywords=("прокрути", "скролл", "вниз", "вверх", "колесо")),
    ]
    # опасные сочетания и разрушительный текст спрашиваются всегда, даже при авто-разрешении
    for t in tools:
        if t.name == "press_key":
            t.danger = _danger_key
        elif t.name == "type_text":
            t.danger = _danger_type
    return tools
