"""Окно быстрого ввода по глобальной горячей клавише (как строка PowerToys Run).

Горячая клавиша → по центру сверху появляется строка; пишешь сообщение, Enter —
оно уходит ассистенту (как в чате дашборда), и ответ показывается тут же.
Esc или клик в другое окно закрывают строку. Клавишу выбирают в трее
(настройка hotkey_input), например "Ctrl+Alt+Space" или "Win+Shift+A".
"""
import ctypes
import queue
import threading
import time
import tkinter as tk

import httpx

CHAT_URL = "http://127.0.0.1:8766/api/chat"
CORE_URL = "http://127.0.0.1:8766/api/core"
REPLY_TIMEOUT_SECS = 90

_MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """"Alt+Space" → (модификаторы, виртуальный код клавиши). ValueError, если не разобрать."""
    mods, key = 0, None
    for part in (p.strip().lower() for p in spec.split("+")):
        if part in _MODS:
            mods |= _MODS[part]
        elif part == "space":
            key = 0x20
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 12:
            key = 0x6F + int(part[1:])
        else:
            raise ValueError(f"неизвестная клавиша: {part!r}")
    if not mods or key is None:
        raise ValueError("нужны модификатор и клавиша, например Alt+Space")
    return mods, key


BG, FG, MUTED, BORDER = "#1c1f24", "#e5e7e9", "#9aa0a6", "#3a3f47"
WIDTH = 640


class QuickInput:
    def __init__(self, root: tk.Tk, chat_url: str = CHAT_URL, core_url: str = CORE_URL):
        self.root = root
        self.chat_url, self.core_url = chat_url, core_url
        self.win: tk.Toplevel | None = None
        self._had_focus = False
        self._results: queue.Queue = queue.Queue()

    def toggle(self):
        if self.win is not None and self.win.winfo_exists():
            self.close()
        else:
            self.show()

    def close(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.destroy()
        self.win = None

    def show(self):
        win = tk.Toplevel(self.root)
        self.win = win
        self._had_focus = False
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BORDER)
        frame = tk.Frame(win, bg=BG)
        frame.pack(fill="both", expand=True, padx=1, pady=1)

        self.entry = tk.Entry(frame, font=("Segoe UI", 16), bg=BG, fg=FG, insertbackground=FG,
                              relief="flat", highlightthickness=0)
        self.entry.pack(fill="x", padx=18, pady=(16, 6))
        self.answer = tk.Label(frame, text="", font=("Segoe UI", 11), bg=BG, fg=FG, justify="left",
                               anchor="w", wraplength=WIDTH - 40)
        self.hint = tk.Label(frame, text="Enter — отправить    Esc — закрыть", font=("Segoe UI", 9),
                             bg=BG, fg=MUTED, anchor="w")
        self.hint.pack(fill="x", padx=18, pady=(0, 10))

        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{WIDTH}x{win.winfo_reqheight()}+{(sw - WIDTH) // 2}+{int(sh * 0.22)}")
        self.entry.bind("<Return>", self._submit)
        win.bind("<Escape>", lambda e: self.close())
        win.bind("<FocusIn>", lambda e: setattr(self, "_had_focus", True))
        win.bind("<FocusOut>", lambda e: win.after(150, self._maybe_close))
        win.after(20, self._focus)
        win.after(200, self._pump)
        win.after(6000, self._close_if_never_focused)   # не зависать на экране без фокуса

    def _focus(self):
        """Windows не отдаёт фокус чужому окну просто так: подключаемся к потоку
        активного окна и переводим своё на передний план."""
        if self.win is None or not self.win.winfo_exists():
            return
        try:
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            hwnd = user32.GetAncestor(self.win.winfo_id(), 2)   # GA_ROOT
            fg_tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            cur_tid = kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(fg_tid, cur_tid, True)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.AttachThreadInput(fg_tid, cur_tid, False)
        except Exception:
            pass
        self.win.focus_force()
        self.entry.focus_force()

    def _maybe_close(self):
        # закрываем по клику мимо только если фокус у окна уже был
        if (self._had_focus and self.win is not None and self.win.winfo_exists()
                and self.win.focus_displayof() is None):
            self.close()

    def _close_if_never_focused(self):
        if not self._had_focus:
            self.close()

    def _set_answer(self, text: str):
        self.answer.config(text=text)
        if not self.answer.winfo_ismapped():
            self.answer.pack(fill="x", padx=18, pady=(0, 6), before=self.hint)
        self.win.update_idletasks()
        self.win.geometry(f"{WIDTH}x{self.win.winfo_reqheight()}")

    def _submit(self, _event=None):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._set_answer("…")
        threading.Thread(target=self._send_and_wait, args=(text,), daemon=True).start()

    def _send_and_wait(self, text: str):
        sent = time.time()
        try:
            httpx.post(self.chat_url, json={"message": text}, timeout=5).raise_for_status()
        except Exception as e:
            self._results.put(f"Не удалось отправить: {e}")
            return
        deadline = sent + REPLY_TIMEOUT_SECS
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                dialogue = httpx.get(self.core_url, timeout=3).json().get("dialogue", [])
            except Exception:
                continue
            fresh = [m["text"] for m in dialogue if m["role"] != "user" and m["ts"] >= sent]
            if fresh:
                time.sleep(2.5)   # ответ может прийти несколькими репликами подряд
                try:
                    dialogue = httpx.get(self.core_url, timeout=3).json().get("dialogue", [])
                    fresh = [m["text"] for m in dialogue if m["role"] != "user" and m["ts"] >= sent]
                except Exception:
                    pass
                self._results.put("\n".join(fresh))
                return
        self._results.put("Ответа пока нет. Он может появиться в чате дашборда.")

    def _pump(self):
        """Забрать результаты из потока в потоке Tk."""
        if self.win is None or not self.win.winfo_exists():
            return
        try:
            while True:
                self._set_answer(self._results.get_nowait())
        except queue.Empty:
            pass
        self.win.after(200, self._pump)
