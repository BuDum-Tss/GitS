"""Окно питомца (ТЗ 13): tkinter поверх всех окон, стикер эмоции и облачко с текущим предложением.

Окно работает только в главном потоке. Всё, что пришло от ядра, оно получает через `PetController` (очередь потока клиента,
`root.after(50, ...)`); свои изменения (глушилка, позиция, видимость) отправляет в пульт управления, HTTP-API дашборда
(`POST /api/settings`, PET-6), и ядро возвращает их обратно темой `settings`. Звука здесь нет: озвучивает модуль voice (PET-2).
"""
import ctypes
import ctypes.wintypes as wt
import logging
import queue
import textwrap
import threading
import time
from pathlib import Path

import httpx
import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageTk

import quick_input
from pet_logic import HOTKEY_MUTE, STICKER_MAP, dashboard_base

log = logging.getLogger("pet")

# ── настройки ─────────────────────────────────────────────────────────────────

STICKER_H   = 160          # высота стикера
W           = 300          # ширина окна
CHROMA      = (255, 0, 255)
CHROMA_HEX  = "#FF00FF"
STICKERS    = Path(__file__).parent / "static" / "stickers"
PUMP_MS     = 50           # PET-3: разбор очереди потока клиента
HOTKEY_RECHECK_S = 2.0     # как часто сверять клавишу быстрого ввода с настройкой (смена подхватывается без перезапуска)


# ── загрузка стикеров ─────────────────────────────────────────────────────────

def load_sprites() -> tuple[dict, int, int]:
    sprites = {}
    sw_s = sh_s = 0
    for mood, fname in STICKER_MAP.items():
        p = STICKERS / f"{fname}.png"
        if not p.exists():
            log.warning(f"Нет файла: {p}")
            continue
        img = Image.open(p).convert("RGBA")
        # Масштабируем по высоте
        scale = STICKER_H / img.height
        nw = int(img.width * scale)
        img = img.resize((nw, STICKER_H), Image.LANCZOS)
        sw_s, sh_s = nw, STICKER_H
        sprites[mood] = img
    log.info(f"Загружено {len(sprites)} стикеров  ({sw_s}×{sh_s}px)")
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


# ── пульт управления: HTTP-API дашборда (PET-6) ───────────────────────────────

def post_settings(payload: dict) -> None:
    """Изменить общие настройки (глушилка, позиция, видимость, закрепление) через дашборд. Не блокирует окно."""
    def work():
        try:
            httpx.post(dashboard_base() + "/api/settings", json=payload, timeout=2)
        except Exception as e:
            log.debug(f"Настройки не отправлены: {e}")
    threading.Thread(target=work, daemon=True).start()


def already_running() -> bool:
    """Один питомец на компьютер: именованный мьютекс Windows (то же имя, что у прежнего desktop_pet.py)."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AssistantPet")
    return ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS


# ── горячие клавиши ───────────────────────────────────────────────────────────

class HotkeyThread(threading.Thread):
    """Глобальные клавиши: Ctrl+Alt+M - глушилка; клавиша строки быстрого ввода берётся из настройки hotkey_input (PET-7),
    смена подхватывается без перезапуска. RegisterHotKey привязан к потоку, поэтому цикл сообщений живёт здесь."""

    def __init__(self, get_hotkey, on_mute, on_input):
        super().__init__(name="pet-hotkeys", daemon=True)
        self.get_hotkey, self.on_mute, self.on_input = get_hotkey, on_mute, on_input
        self.registered: str | None = None

    def run(self) -> None:
        user32 = ctypes.windll.user32
        MOD_NOREPEAT, ID_MUTE, ID_INPUT = 0x4000, 1, 2
        if not user32.RegisterHotKey(None, ID_MUTE, 0x2 | 0x1 | MOD_NOREPEAT, ord("M")):
            log.warning(f"Не удалось занять {HOTKEY_MUTE} (возможно, уже занято другой программой).")
        current: list = [None]

        def apply_input_hotkey():
            spec = self.get_hotkey()
            if spec == current[0] or spec is None:
                return
            if current[0] is not None:
                user32.UnregisterHotKey(None, ID_INPUT)
            current[0] = spec
            try:
                mods, vk = quick_input.parse_hotkey(spec)
            except ValueError as e:
                log.warning(f"клавиша ввода {spec!r}: {e}")
                return
            if not user32.RegisterHotKey(None, ID_INPUT, mods | MOD_NOREPEAT, vk):
                log.warning(f"клавиша ввода {spec} занята другой программой: выбери другую в трее")
            self.registered = spec

        apply_input_hotkey()
        msg, last = wt.MSG(), time.time()
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0312:   # WM_HOTKEY
                    if msg.wParam == ID_MUTE:
                        self.on_mute()
                    elif msg.wParam == ID_INPUT:
                        self.on_input()
            if time.time() - last > HOTKEY_RECHECK_S:
                last = time.time()
                apply_input_hotkey()
            time.sleep(0.05)


# ── окно ──────────────────────────────────────────────────────────────────────

class PetWindow:
    """Реализация PetView на tkinter. state() даёт PetState (настройки: закрепление, позиция, глушилка)."""

    def __init__(self, state, sprites: tuple, post=post_settings):
        self.state = state
        self.post = post
        self.sprites, self.sw_s, self.sh_s = sprites
        self._positioned = False
        self._shown = False
        self._ref = None
        self.input_requests: queue.Queue = queue.Queue()      # хоткей кладёт просьбу открыть строку ввода
        self.pump = None                                       # PetController.pump, ставит main

        # Пробный кадр чтобы узнать H
        _sample, self.H = render_frame(self.sprites, self.sw_s, self.sh_s, "idle", "…", False)

        root = self.root = tk.Tk()
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-transparentcolor", CHROMA_HEX)
        root.configure(bg=CHROMA_HEX)
        root.withdraw()                                        # пока не пришли настройки (позиция, видимость), окна нет
        sw_screen, sh_screen = root.winfo_screenwidth(), root.winfo_screenheight()
        self._screen = (sw_screen, sh_screen)
        root.geometry(f"{W}x{self.H}+{sw_screen - W - 20}+20")      # верхний правый угол по умолчанию

        self.canvas = tk.Canvas(root, width=W, height=self.H, bg=CHROMA_HEX, highlightthickness=0)
        self.canvas.pack()
        self._item = self.canvas.create_image(0, 0, anchor="nw")
        self._drag = {"x": 0, "y": 0, "moved": False}
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._hide_pet)         # ПКМ = спрятать; вернуть: трей, «Показывать питомца»
        self.qi = quick_input.QuickInput(root)

    # ── PetView ───────────────────────────────────────────────────────────

    def set_visible(self, visible: bool) -> None:
        if visible != self._shown:
            self._shown = visible
            (self.root.deiconify if visible else self.root.withdraw)()

    def draw(self, sticker: str, text: str, bubble_visible: bool) -> None:
        img, new_h = render_frame(self.sprites, self.sw_s, self.sh_s, sticker, text, False, bubble_visible)
        photo = ImageTk.PhotoImage(img)
        self._ref = photo
        if self.canvas.winfo_height() != new_h:
            self.canvas.config(height=new_h)
            self.root.geometry(f"{W}x{new_h}")
        self.canvas.itemconfig(self._item, image=photo)

    # ── настройки от ядра ───────────────────────────────────────────────────

    def on_settings(self, settings: dict) -> None:
        """Позиция из настройки pet_x/pet_y применяется один раз (запомненная могла остаться от другого монитора)."""
        if self._positioned:
            return
        self._positioned = True
        x, y = settings.get("pet_x"), settings.get("pet_y")
        if x is None or y is None:
            return
        sw_screen, sh_screen = self._screen
        x = max(0, min(int(x), sw_screen - W))
        y = max(0, min(int(y), sh_screen - self.H))
        self.root.geometry(f"{W}x{self.H}+{x}+{y}")

    # ── перетаскивание и клики ──────────────────────────────────────────────

    def _on_press(self, e):
        self._drag["x"] = e.x_root - self.root.winfo_x()
        self._drag["y"] = e.y_root - self.root.winfo_y()
        self._drag["moved"] = False

    def _on_drag(self, e):
        if self.state().pinned:
            return   # закреплён: не двигаем
        self._drag["moved"] = True
        self.root.geometry(f"+{e.x_root - self._drag['x']}+{e.y_root - self._drag['y']}")

    def _on_release(self, _e):
        if self._drag["moved"]:   # запоминаем, куда поставили
            self.post({"pet_x": self.root.winfo_x(), "pet_y": self.root.winfo_y()})

    def _hide_pet(self, _e=None):
        """ПКМ прячет питомца (процесс остаётся подключённым к ядру)."""
        self.post({"pet_visible": False})

    # ── хоткеи ──────────────────────────────────────────────────────────────

    def toggle_mute(self) -> None:
        """Глушилка: переключить озвучку в общих настройках (переживёт перезапуск; звук выключает модуль voice)."""
        new_muted = not self.state().muted
        self.post({"voice_muted": new_muted})
        log.info(f"Озвучка: {'выкл' if new_muted else 'вкл'}")

    def request_input(self) -> None:
        self.input_requests.put(1)

    # ── главный цикл ────────────────────────────────────────────────────────

    def _tick(self) -> None:
        try:
            while True:
                self.input_requests.get_nowait()
                self.qi.toggle()
        except queue.Empty:
            pass
        if self.pump is not None:
            self.pump()
        self.root.after(PUMP_MS, self._tick)

    def run(self, should_stop=lambda: False) -> None:
        def watch_stop():
            if should_stop():
                self.root.quit()
                return
            self.root.after(200, watch_stop)
        self.root.after(PUMP_MS, self._tick)
        self.root.after(200, watch_stop)
        self.root.mainloop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

