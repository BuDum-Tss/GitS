"""Активное окно для слуха: что запрещает слушать системный звук (звонки и мессенджеры, чувствительные окна).

Это короткая копия `foreground()` и `is_sensitive()` из `agent/perception.py` (модуль не импортирует зрение ядра: оно тянет
настройки и память). Когда экран станет модулем desktop (ТЗ 14), активное окно слуху будет давать он, и эта копия уйдёт.
Список чувствительных слов тот же, что у зрения (`GLADOS_VISION_BLOCK` дополняет его).
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

_DEFAULT_BLOCK = (
    "пароль", "password", "keepass", "bitwarden", "1password", "lastpass",
    "банк", "bank", "сбер", "тинькофф", "т-банк", "alfa", "альфа",
    "приватн", "private browsing", "incognito", "inprivate", "инкогнито",
    "кошел", "wallet", "seed phrase", "credit card", "банковск",
)
_extra = os.getenv("GLADOS_VISION_BLOCK", "")
BLOCK_WORDS = _DEFAULT_BLOCK + tuple(w.strip().lower() for w in _extra.split(",") if w.strip())


def foreground() -> dict | None:
    """Активное окно: {title, app} или None (не Windows или окна нет)."""
    if sys.platform != "win32":
        return None
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = "?"
    h = kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
    if h:
        size = wt.DWORD(512)
        pbuf = ctypes.create_unicode_buffer(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
            app = os.path.basename(pbuf.value).removesuffix(".exe")
        kernel32.CloseHandle(h)
    return {"title": buf.value, "app": app}


def is_sensitive(win: dict) -> bool:
    low = (win["title"] + " " + win["app"]).lower()
    return any(w in low for w in BLOCK_WORDS)
