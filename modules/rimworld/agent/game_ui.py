"""Прямое управление UI RimWorld через мышь/клавиатуру и скриншоты окна.

Резерв для того, чего нет в структурном API мода: торговля, переименование
колонии/пешек, произвольный микроменеджмент через контекстные меню —
всё, что обычный игрок делает мышью, а не через JSON-команды.

ВАЖНО: физически двигает курсор мыши пользователя по экрану. Работает
только пока окно RimWorld видно и не свёрнуто.
"""
import ctypes
import io

import httpx
import pyautogui
import win32con
import win32gui
from PIL import Image as PILImage

pyautogui.FAILSAFE = False  # окно игры может быть у самого края экрана
pyautogui.PAUSE = 0.05

WINDOW_TITLE = "RimWorld by Ludeon Studios"
_BASE = "http://localhost:8765"

_user32   = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def _find_window():
    hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
    if not hwnd:
        raise RuntimeError(f"Окно RimWorld не найдено ('{WINDOW_TITLE}'). Игра запущена и видна?")
    return hwnd


def _window_rect() -> tuple[int, int, int, int]:
    hwnd = _find_window()
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return left, top, right, bottom


def _focus():
    """SetForegroundWindow из фонового процесса Windows обычно молча
    игнорирует (защита от кражи фокуса) — обходим стандартным трюком
    AttachThreadInput: временно "приклеиваем" очередь ввода нашего потока
    к потоку текущего активного окна, тогда смена фокуса разрешается."""
    hwnd = _find_window()
    if _user32.GetForegroundWindow() == hwnd:
        return

    fg_hwnd = _user32.GetForegroundWindow()
    cur_thread = _kernel32.GetCurrentThreadId()
    fg_thread = _user32.GetWindowThreadProcessId(fg_hwnd, None)
    target_thread = _user32.GetWindowThreadProcessId(hwnd, None)

    _user32.AttachThreadInput(cur_thread, fg_thread, True)
    _user32.AttachThreadInput(cur_thread, target_thread, True)
    try:
        _user32.ShowWindow(hwnd, win32con.SW_RESTORE)
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
    finally:
        _user32.AttachThreadInput(cur_thread, fg_thread, False)
        _user32.AttachThreadInput(cur_thread, target_thread, False)


def _to_screen(x: int, y: int) -> tuple[int, int]:
    left, top, _, _ = _window_rect()
    return left + x, top + y


def window_size() -> tuple[int, int]:
    left, top, right, bottom = _window_rect()
    return right - left, bottom - top


def screenshot_png() -> bytes:
    left, top, right, bottom = _window_rect()
    img: PILImage.Image = pyautogui.screenshot(region=(left, top, right - left, bottom - top))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _show_ai_cursor(x: int, y: int):
    """Просим мод нарисовать цветной курсор ПОВЕРХ игры в точке (x,y) окна
    (аналогично тому, как Multiplayer-мод рисует курсоры других игроков) —
    чисто визуальная подсказка, не связана с реальным системным курсором."""
    try:
        httpx.post(f"{_BASE}/command",
                  json={"type": "set_ai_cursor", "x": x, "y": y}, timeout=2)
    except Exception:
        pass  # косметика — не критично, если не получилось


def hide_ai_cursor():
    try:
        httpx.post(f"{_BASE}/command", json={"type": "clear_ai_cursor"}, timeout=2)
    except Exception:
        pass


def click(x: int, y: int, button: str = "left", clicks: int = 1):
    _show_ai_cursor(x, y)
    _focus()
    sx, sy = _to_screen(x, y)
    pyautogui.click(sx, sy, button=button, clicks=clicks)


def move(x: int, y: int):
    _show_ai_cursor(x, y)
    sx, sy = _to_screen(x, y)
    pyautogui.moveTo(sx, sy)


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left"):
    _show_ai_cursor(x2, y2)
    _focus()
    sx1, sy1 = _to_screen(x1, y1)
    sx2, sy2 = _to_screen(x2, y2)
    pyautogui.moveTo(sx1, sy1)
    pyautogui.dragTo(sx2, sy2, button=button, duration=0.2)


def type_text(text: str):
    _focus()
    pyautogui.typewrite(text, interval=0.02)


def key_press(key: str):
    _focus()
    pyautogui.press(key)


def scroll(x: int, y: int, amount: int):
    _show_ai_cursor(x, y)
    _focus()
    sx, sy = _to_screen(x, y)
    pyautogui.moveTo(sx, sy)
    pyautogui.scroll(amount)
