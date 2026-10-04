"""GLaDOS Desktop Pet — поверх всех окон, верхний правый угол."""
import sys
import ctypes
import threading
import textwrap
import time
import re
import queue
from pathlib import Path

import nowindow  # noqa: F401  (побочный эффект: скрывает окна консоли)
import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageTk
import httpx
import events_log
import quick_input
import tts_engine
import settings

# ── настройки ─────────────────────────────────────────────────────────────────

STICKER_H   = 160          # высота стикера
W           = 300          # ширина окна
CHROMA      = (255, 0, 255)
CHROMA_HEX  = "#FF00FF"
POLL_SECS   = 2.0
DASHBOARD   = "http://localhost:8766/api/state"
STICKERS    = Path(__file__).parent / "static" / "stickers"

STICKER_MAP = {
    "idle":      "idle",
    "thinking":  "thinking",
    "angry":     "angry",
    "question":  "question",
    "action":    "action",
    "looking":   "looking",
    "resting":   "resting",
    "threat":    "threat",
    "happy":     "happy",
    "sitting":   "sitting",
    "sad":       "sad",
    "scared":    "scared",
    "proud":     "proud",
    "surprised": "surprised",
}

# ── загрузка стикеров ─────────────────────────────────────────────────────────

def load_sprites() -> tuple[dict, int, int]:
    sprites = {}
    sw_s = sh_s = 0
    for mood, fname in STICKER_MAP.items():
        p = STICKERS / f"{fname}.png"
        if not p.exists():
            print(f"[pet] Нет файла: {p}")
            continue
        img = Image.open(p).convert("RGBA")
        # Масштабируем по высоте
        scale = STICKER_H / img.height
        nw = int(img.width * scale)
        img = img.resize((nw, STICKER_H), Image.LANCZOS)
        sw_s, sh_s = nw, STICKER_H
        sprites[mood] = img
    print(f"[pet] Загружено {len(sprites)} стикеров  ({sw_s}×{sh_s}px)")
    return sprites, sw_s, sh_s

# ── отрисовка кадра ───────────────────────────────────────────────────────────

def _font(size: int):
    for path in ["C:/Windows/Fonts/arial.ttf",
                 "C:/Windows/Fonts/cour.ttf",
                 "C:/Windows/Fonts/tahoma.ttf"]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()

FNT_MAIN = None   # инициализируется при первом render_frame


def render_frame(sprites, sw_s, sh_s, mood: str,
                 text: str, is_thinking: bool,
                 bubble_visible: bool = True) -> Image.Image:
    global FNT_MAIN
    if FNT_MAIN is None:
        FNT_MAIN = _font(12)

    sprite = sprites.get(mood, sprites.get("idle"))

    # Размеры — облачко растёт под текст, без обрезки строк
    lines  = textwrap.wrap(text, width=34) if (bubble_visible and text) else []
    line_h = FNT_MAIN.size + 3  # type: ignore[union-attr]
    text_h = len(lines) * line_h
    dots_h = 20 if is_thinking else 0
    bubble_h = max(50, 20 + text_h + dots_h) if bubble_visible else 0
    tail_h   = 18 if bubble_visible else 0
    pad      = 8
    H = sh_s + tail_h + bubble_h + pad

    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw  = ImageDraw.Draw(frame)

    # ── стикер — по центру сверху ────────────────────────────────────────────
    sx = (W - sw_s) // 2
    frame.paste(sprite, (sx, 0), sprite)

    if bubble_visible:
        bx   = pad
        by   = sh_s + tail_h
        bw   = W - pad * 2
        cx   = W // 2

        BUBBLE_BG   = (255, 255, 255, 245)
        BUBBLE_BRD  = (200, 200, 200, 255)
        TEXT_COLOR  = (20, 20, 20, 255)
        MUTED       = (120, 120, 120, 255)

        # хвостик
        draw.polygon(
            [(cx - 14, by), (cx + 14, by), (cx, sh_s + 4)],
            fill=BUBBLE_BG, outline=BUBBLE_BRD
        )
        # облачко
        draw.rounded_rectangle(
            (bx, by, bx + bw, by + bubble_h),
            radius=14, fill=BUBBLE_BG, outline=BUBBLE_BRD, width=1,
        )
        # текст
        inner_x = bx + 12
        inner_y = by + 10
        for line in lines:
            draw.text((inner_x, inner_y), line, font=FNT_MAIN, fill=TEXT_COLOR)
            inner_y += line_h

        if is_thinking:
            dot_fnt = _font(10)
            draw.text((bx + bw // 2 - 14, inner_y + 2),
                      "● ● ●", font=dot_fnt, fill=MUTED)

    # ── конвертируем: прозрачные → chroma ────────────────────────────────────
    alpha = frame.split()[3].point(lambda v: 255 if v > 50 else 0)
    bg = Image.new("RGB", (W, H), CHROMA)
    bg.paste(frame.convert("RGB"), mask=alpha)
    return bg, H

# ── поллинг ───────────────────────────────────────────────────────────────────

EMOTION_TTL_SECS = 30   # сколько эмоция от fast_brain считается свежей
BUBBLE_HIDE_SECS = 25   # скрывать облачко после N секунд тишины

_state = {"mood": "idle",
          "text": "",
          "is_thinking": False,
          "bubble_visible": False,
          "muted": False,
          "pet_visible": True,
          "pinned": False}
_lock            = threading.Lock()
_last_spoken     = [""]          # текст последней озвучки
_first_poll      = [True]        # первый опрос после запуска не озвучиваем
_last_comment_ts = [time.time()] # когда последний раз менялся комментарий
_pending_first_segment = [""]    # первый сегмент новой реплики, пока воркер TTS не проснулся


def _mood_from(data: dict) -> str:
    emo = data.get("emotion") or {}
    if emo.get("emotion") in STICKER_MAP and time.time() - emo.get("ts", 0) < EMOTION_TTL_SECS:
        return emo["emotion"]
    if not data.get("connected"):
        return "idle"   # игры нет — компаньон просто ждёт; «resting» только если ядро недоступно
    last    = data.get("game_decision") or {}
    actions = last.get("actions", [])
    fates   = data.get("pawn_fates", {})
    if any("attack" in a or "draft" in a for a in actions):
        return "threat"
    if any("build" in a or "research" in a for a in actions):
        return "action"
    if any(f.get("fate") == "dead" for f in fates.values()):
        return "angry"
    return "idle"


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p for p in parts if p]


SETTINGS_URL = "http://localhost:8766/api/settings"
HOTKEY_MUTE  = "Ctrl+Alt+M"   # глобальная глушилка голоса


def _apply_muted(val: bool):
    with _lock:
        _state["muted"] = val
    tts_engine.set_muted(val)


def toggle_mute():
    """Глушилка: переключить озвучку и сохранить в общих настройках (переживёт перезапуск)."""
    with _lock:
        new_muted = not _state["muted"]
    _apply_muted(new_muted)
    try:
        httpx.post(SETTINGS_URL, json={"voice_muted": new_muted}, timeout=2)
    except Exception:
        pass
    print(f"[pet] Озвучка: {'выкл' if new_muted else 'вкл'}")


_input_requests = queue.Queue()   # сюда хоткей кладёт просьбу открыть строку ввода


def _hotkey_loop():
    """Глобальные клавиши: Ctrl+Alt+M — глушилка; клавиша из настроек (выбирается в трее) —
    строка быстрого ввода. Смена клавиши подхватывается без перезапуска."""
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    MOD_NOREPEAT, ID_MUTE, ID_INPUT = 0x4000, 1, 2
    if not user32.RegisterHotKey(None, ID_MUTE, 0x2 | 0x1 | MOD_NOREPEAT, ord("M")):
        print(f"[pet] Не удалось занять {HOTKEY_MUTE} (возможно, уже занято другой программой).")
    current = [None]

    def apply_input_hotkey():
        spec = settings.load().get("hotkey_input")
        if spec == current[0]:
            return
        if current[0] is not None:
            user32.UnregisterHotKey(None, ID_INPUT)
        current[0] = spec
        try:
            mods, vk = quick_input.parse_hotkey(spec)
        except ValueError as e:
            events_log.log("error", f"клавиша ввода {spec!r}: {e}")
            return
        if not user32.RegisterHotKey(None, ID_INPUT, mods | MOD_NOREPEAT, vk):
            events_log.log("error", f"клавиша ввода {spec} занята другой программой: выбери другую в трее")

    apply_input_hotkey()
    msg, last = wt.MSG(), time.time()
    while True:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            if msg.message == 0x0312:   # WM_HOTKEY
                if msg.wParam == ID_MUTE:
                    toggle_mute()
                elif msg.wParam == ID_INPUT:
                    _input_requests.put(1)
        if time.time() - last > 2:
            last = time.time()
            apply_input_hotkey()
        time.sleep(0.05)


def _poll():
    while True:
        try:
            r = httpx.get(DASHBOARD, timeout=2.0)
            d = r.json()
            cfg = d.get("settings") or {}
            muted_cfg = cfg.get("voice_muted")
            if muted_cfg is not None and muted_cfg != _state["muted"]:
                _apply_muted(bool(muted_cfg))
            if "volume" in cfg:
                tts_engine.set_volume(cfg["volume"])
            with _lock:
                _state["pet_visible"] = bool(cfg.get("pet_visible", True))
                _state["pinned"] = bool(cfg.get("pet_pinned", False))
            say = d.get("say") or {}
            mood = _mood_from(d)
            text = (say.get("text") or "").strip()
            say_key = say.get("ts")   # метка времени реплики: меняется только когда сказана новая
        except Exception:
            mood = "resting"
            text = ""
            say_key = None

        now = time.time()
        if _first_poll[0]:
            # При запуске в ядре уже лежит старая реплика прошлой сессии: это не новость,
            # её не показываем и не озвучиваем
            _first_poll[0] = False
            _last_spoken[0] = say_key
        # Сказана новая реплика (даже дословно такая же, как прошлая): облачко и озвучка по предложениям
        if text and say_key is not None and say_key != _last_spoken[0]:
            _last_spoken[0] = say_key
            _last_comment_ts[0] = now
            segments = _split_sentences(text)
            # Воркер озвучки подхватит очередь не мгновенно — запоминаем первый
            # сегмент сразу же, иначе на первый цикл опроса мелькает полный текст
            _pending_first_segment[0] = segments[0] if segments else text
            tts_engine.speak_sequence(segments)

        bubble_visible = bool(text) and (now - _last_comment_ts[0] < BUBBLE_HIDE_SECS)
        # Пока идёт озвучка — показываем именно тот сегмент, что звучит сейчас;
        # пока воркер не проснулся — локально запомненный первый сегмент, а не весь текст
        current_seg = tts_engine.get_current_segment()
        if current_seg:
            _pending_first_segment[0] = ""  # воркер догнал — больше не нужен fallback
        display_text = current_seg or _pending_first_segment[0] or text

        with _lock:
            _state["mood"]           = mood
            _state["text"]           = display_text
            _state["bubble_visible"] = bubble_visible

        time.sleep(POLL_SECS)


# ── главный цикл ──────────────────────────────────────────────────────────────

def _already_running() -> bool:
    """Один питомец на компьютер: именованный мьютекс Windows."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AssistantPet")
    return ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS


def main():
    if _already_running():
        print("[pet] Питомец уже запущен.")
        sys.exit(0)
    sprites, sw_s, sh_s = load_sprites()
    if not sprites:
        print("[pet] Нет стикеров в static/stickers/. Запусти нарезку сначала.")
        sys.exit(1)

    # Пробный кадр чтобы узнать H
    sample, H = render_frame(sprites, sw_s, sh_s, "idle", "…", False)

    root = tk.Tk()
    root.overrideredirect(True)
    root.wm_attributes("-topmost", True)
    root.wm_attributes("-transparentcolor", CHROMA_HEX)
    root.configure(bg=CHROMA_HEX)

    # Позиция: верхний правый угол
    sw_screen, sh_screen = root.winfo_screenwidth(), root.winfo_screenheight()
    cfg = settings.load()
    x, y = cfg.get("pet_x"), cfg.get("pet_y")
    if x is None or y is None:
        x, y = sw_screen - W - 20, 20
    # запомненная позиция могла остаться от другого монитора — держим окно на экране
    x = max(0, min(int(x), sw_screen - W))
    y = max(0, min(int(y), sh_screen - H))
    root.geometry(f"{W}x{H}+{x}+{y}")
    _state["pinned"] = bool(cfg.get("pet_pinned", False))

    canvas = tk.Canvas(root, width=W, height=H,
                       bg=CHROMA_HEX, highlightthickness=0)
    canvas.pack()

    # ── перетаскивание + клики ─────────────────────────────────────────────────
    _drag  = {"x": 0, "y": 0, "moved": False}

    def on_press(e):
        _drag["x"]     = e.x_root - root.winfo_x()
        _drag["y"]     = e.y_root - root.winfo_y()
        _drag["moved"] = False

    def on_drag(e):
        if _state["pinned"]:
            return   # закреплён: не двигаем
        _drag["moved"] = True
        root.geometry(f"+{e.x_root - _drag['x']}+{e.y_root - _drag['y']}")

    canvas.bind("<Button-1>",   on_press)
    canvas.bind("<B1-Motion>",  on_drag)

    def on_release(_e):
        if _drag["moved"]:   # запоминаем, куда поставили (в потоке, чтобы окно не зависало)
            pos = {"pet_x": root.winfo_x(), "pet_y": root.winfo_y()}

            def save():
                try:
                    httpx.post(SETTINGS_URL, json=pos, timeout=2)
                except Exception:
                    pass
            threading.Thread(target=save, daemon=True).start()

    canvas.bind("<ButtonRelease-1>", on_release)

    def hide_pet(_e=None):
        """ПКМ прячет питомца (процесс и голос продолжают работать)."""
        with _lock:
            _state["pet_visible"] = False
        try:
            httpx.post(SETTINGS_URL, json={"pet_visible": False}, timeout=2)
        except Exception:
            pass

    canvas.bind("<Button-3>", hide_pet)  # ПКМ = спрятать; вернуть: трей, «Показывать питомца»

    _ref  = [None]
    _item = canvas.create_image(0, 0, anchor="nw")

    qi = quick_input.QuickInput(root)

    def check_input_requests():
        try:
            while True:
                _input_requests.get_nowait()
                qi.toggle()
        except queue.Empty:
            pass
        root.after(80, check_input_requests)

    root.after(300, check_input_requests)

    _shown = [True]

    def refresh():
        with _lock:
            want_visible = _state["pet_visible"]
        if want_visible != _shown[0]:
            _shown[0] = want_visible
            (root.deiconify if want_visible else root.withdraw)()
        with _lock:
            mood    = _state["mood"]
            text    = _state["text"]
            thinking = _state["is_thinking"]
            bubble  = _state["bubble_visible"]
        img, new_h = render_frame(sprites, sw_s, sh_s, mood, text, thinking, bubble)
        photo = ImageTk.PhotoImage(img)
        _ref[0] = photo
        if canvas.winfo_height() != new_h:
            canvas.config(height=new_h)
            root.geometry(f"{W}x{new_h}")
        canvas.itemconfig(_item, image=photo)
        root.after(1000, refresh)

    tts_engine.start()
    threading.Thread(target=_poll, daemon=True).start()
    threading.Thread(target=_hotkey_loop, daemon=True).start()
    root.after(200, refresh)

    print("[pet] GLaDOS запущена — верхний правый угол. ПКМ спрячет, вернуть можно из трея.")
    root.mainloop()


if __name__ == "__main__":
    main()
