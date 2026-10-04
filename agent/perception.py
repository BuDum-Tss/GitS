"""Зрение ГЛаДОС: что происходит на экране. Всё обрабатывается ЛОКАЛЬНО.

Схема: заголовок активного окна (дёшево, часто) → при смене окна или по
таймеру скриншот этого окна → OCR (RapidOCR, точный текст) + локальная
Gemma (смысл). Наружу уходит только короткий вывод вроде «пишет код»;
сырой OCR-текст нигде не сохраняется и в облако не отправляется.

Защита приватности:
  - окна с «чувствительными» словами в заголовке (пароли, банк, приватный
    режим и т.п.) не снимаются вообще;
  - выключатель settings.vision (чат: «не смотри на экран»);
  - при простое пользователя (нет ввода) экран не анализируется.
"""
import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
from PIL import ImageGrab

import events_log
import fast_brain
import settings
from user_memory import UserMemory

logger = logging.getLogger("perception")

CHECK_EVERY_SECS   = 8            # как часто смотрим заголовок окна
REANALYZE_SECS     = 180          # перепроверка того же окна
MIN_GAP_SECS       = 25           # не чаще одного анализа за это время
IDLE_SKIP_SECS     = 300          # нет ввода дольше — пользователь отошёл
OCR_MAX_CHARS      = 1200
IMG_MAX_W          = 1280

_DEFAULT_BLOCK = (
    "пароль", "password", "keepass", "bitwarden", "1password", "lastpass",
    "банк", "bank", "сбер", "тинькофф", "т-банк", "alfa", "альфа",
    "приватн", "private browsing", "incognito", "inprivate", "инкогнито",
    "кошел", "wallet", "seed phrase", "credit card", "банковск",
)
_extra = os.getenv("GLADOS_VISION_BLOCK", "")
BLOCK_WORDS = _DEFAULT_BLOCK + tuple(w.strip().lower() for w in _extra.split(",") if w.strip())

def _set_dpi_aware():
    """Работать в физических пикселях. Иначе при масштабе экрана 125% координаты окон (логические)
    расходятся со снимками и координатами мыши (физические): вырезка окна и нажатия «мимо»."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # на каждый монитор
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_set_dpi_aware()
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


class _LASTINPUT(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Сколько секунд нет ввода с клавиатуры и мыши."""
    info = _LASTINPUT()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return max(0.0, (_kernel32.GetTickCount() - info.dwTime) / 1000.0)


def foreground() -> dict | None:
    """Активное окно: {title, app, rect} или None."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = "?"
    h = _kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
    if h:
        size = wt.DWORD(512)
        pbuf = ctypes.create_unicode_buffer(512)
        if _kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
            app = os.path.basename(pbuf.value).removesuffix(".exe")
        _kernel32.CloseHandle(h)
    rect = wt.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {"title": buf.value, "app": app,
            "rect": (rect.left, rect.top, rect.right, rect.bottom)}


def is_sensitive(win: dict) -> bool:
    low = (win["title"] + " " + win["app"]).lower()
    return any(w in low for w in BLOCK_WORDS)


_ocr_engine = None
_OCR_DIR = Path(__file__).resolve().parent / "models" / "ocr"


def _engine():
    """RapidOCR с русской моделью распознавания (agent/models/ocr), если она скачана.
    Встроенная модель китайско-английская и читает кириллицу как похожую латиницу."""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        rec, keys = _OCR_DIR / "eslav_rec.onnx", _OCR_DIR / "eslav_dict.txt"
        if rec.exists() and keys.exists():
            # В этой версии RapidOCR словарь для чужой модели можно задать только внутренним
            # ключом keys_path, которого нет среди параметров конструктора: подставляем его сами
            # RapidOCR подгружает распознаватель по верхнеуровневому имени ch_ppocr_v3_rec (путь добавляется
            # им самим при импорте), поэтому подменять нужно именно этот модуль
            from ch_ppocr_v3_rec import text_recognize as _tr
            if not getattr(_tr.TextRecognizer, "_dict_patched", False):
                _orig_init = _tr.TextRecognizer.__init__

                def _init(self, config):
                    if config.get("rec_keys_path"):
                        config = {**config, "keys_path": config["rec_keys_path"]}
                    _orig_init(self, config)
                _tr.TextRecognizer.__init__ = _init
                _tr.TextRecognizer._dict_patched = True
            _ocr_engine = RapidOCR(rec_model_path=str(rec), rec_keys_path=str(keys))
        else:
            logger.warning("Русская модель OCR не найдена (agent/models/ocr): кириллица читается плохо")
            _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_text(img) -> str:
    result, _ = _engine()(np.array(img))
    if not result:
        return ""
    lines = sorted(result, key=lambda r: (round(r[0][0][1] / 20), r[0][0][0]))
    return " ".join(r[1] for r in lines)[:OCR_MAX_CHARS]


def ocr_boxes(img) -> list[tuple[str, int, int]]:
    """Надписи на картинке с центрами: [(текст, x, y)] в порядке чтения (для нажатия «на текст»)."""
    result, _ = _engine()(np.array(img))
    out = []
    for box, text, _score in (result or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((str(text), int(sum(xs) / 4), int(sum(ys) / 4)))
    out.sort(key=lambda b: (round(b[2] / 20), b[1]))
    return out


def _capture(win: dict):
    l, t, r, b = win["rect"]
    if r - l < 80 or b - t < 80:
        return None
    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True).convert("RGB")
    if img.width > IMG_MAX_W:
        img = img.resize((IMG_MAX_W, round(img.height * IMG_MAX_W / img.width)))
    return img


def _b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


_CATEGORIES = ["coding", "browsing", "chat", "media", "game", "docs", "other"]
_SYSTEM = (
    "Ты зрение ассистента. По скриншоту окна и распознанному тексту определи, "
    "чем занят пользователь. Ответь ТОЛЬКО JSON: "
    '{"summary": "<одна фраза по-русски до 20 слов, что делает пользователь>", '
    '"category": <одно из ' + json.dumps(_CATEGORIES) + '>}. '
    "Не включай пароли, номера, адреса, имена людей и цитаты из переписки."
)


async def analyze(win: dict) -> dict | None:
    """Скриншот окна → OCR + Gemma → {"summary","category"}. None при неудаче."""
    img = await asyncio.to_thread(_capture, win)
    if img is None:
        return None
    text = await asyncio.to_thread(_ocr_text, img)
    user = (f"Приложение: {win['app']}\nТекст на экране (OCR): {text or '(нет текста)'}\n"
            "Опиши, чем занят пользователь.")
    raw = await fast_brain._ask(_SYSTEM, user, timeout=60, max_tokens=120, background=True,
                                image_b64=_b64(img), temperature=0.2)
    data = fast_brain._json_of(raw)
    if not data or not data.get("summary"):
        return None
    cat = data.get("category") if data.get("category") in _CATEGORIES else "other"
    return {"summary": str(data["summary"])[:200], "category": cat}


_DESCRIBE_SYSTEM = (
    "Ты зрение ассистента. Пользователь сам попросил рассказать, что у него на экране. "
    "По скриншоту и распознанному тексту опиши по-русски в 2-3 предложениях: какие окна "
    "и приложения видны и чем занят пользователь. Называй только то, что реально видно, "
    "ничего не выдумывай. Пароли, номера карт и тексты личной переписки не пересказывай."
)


async def describe_now() -> dict:
    """Разовый осмотр ВСЕГО экрана по прямой просьбе пользователя.
    Возвращает {"status": "ok"|"off"|"sensitive"|"fail", "text": ...}."""
    if not settings.load()["vision"]:
        return {"status": "off"}
    win = await asyncio.to_thread(foreground)
    if win and is_sensitive(win):
        return {"status": "sensitive"}

    def grab():
        img = ImageGrab.grab().convert("RGB")
        if img.width > IMG_MAX_W:
            img = img.resize((IMG_MAX_W, round(img.height * IMG_MAX_W / img.width)))
        return img

    img = await asyncio.to_thread(grab)
    text = await asyncio.to_thread(_ocr_text, img)
    user = (f"Активное окно: {win['app'] if win else '?'}\n"
            f"Текст на экране (OCR): {text or '(нет текста)'}\n"
            "Опиши, что на экране.")
    try:
        out = await fast_brain._ask(_DESCRIBE_SYSTEM, user, timeout=90, max_tokens=200,
                                    image_b64=_b64(img), temperature=0.2)
    except Exception as e:
        logger.warning(f"Не удалось описать экран: {e}")
        return {"status": "fail"}
    return {"status": "ok", "text": out.strip()} if out.strip() else {"status": "fail"}


class ScreenWatcher:
    """Фоновый цикл: следит за активным окном и копит краткие выводы."""

    def __init__(self, memory: UserMemory):
        self._mem = memory
        self._last_key = None
        self._last_run = 0.0
        self.current: dict | None = None     # {"app","category","summary","since","ts"}

    async def run(self):
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Сбой зрения: {e}")
                events_log.log("error", f"зрение: {e}")
            await asyncio.sleep(CHECK_EVERY_SECS)

    async def _step(self):
        if not settings.load()["vision"]:
            return
        if idle_seconds() > IDLE_SKIP_SECS:
            return
        win = await asyncio.to_thread(foreground)
        if not win or is_sensitive(win):
            if win:
                if self._last_key is not None:
                    events_log.log("vision", "окно пропущено: чувствительное содержимое")
                self._last_key = None   # после чувствительного окна анализируем заново
            return
        key = (win["app"], win["title"])
        now = time.time()
        if key == self._last_key and now - self._last_run < REANALYZE_SECS:
            return
        if now - self._last_run < MIN_GAP_SECS:
            return
        self._last_key, self._last_run = key, now
        res = await analyze(win)
        if not res:
            return
        prev = self.current
        same = prev and prev["app"] == win["app"] and prev["category"] == res["category"]
        self.current = {"app": win["app"], "category": res["category"],
                        "summary": res["summary"], "ts": now,
                        "since": prev["since"] if same else now}
        self._mem.add_activity(win["app"], res["category"], res["summary"])
        events_log.log("vision", res["summary"], app=win["app"], category=res["category"])
        logger.info(f"[зрение] {win['app']}: {res['summary']} ({res['category']})")
