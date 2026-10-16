"""Логика питомца без окна (ТЗ 13, PET-4, PET-5, PET-12): выбор стикера и текста облачка по присутствию и времени.

Состояние питомца `PetState` собирается из сообщений потока клиента модуля (`presence`, `settings`, подключение, включённость);
`PetController` вынимает их из `queue.Queue` и отдаёт окну только то, что изменилось. Окно (tkinter) и поток клиента
сюда не входят: всё проверяется без экрана.
"""
import queue
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol
from envcompat import env

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

EMOTION_TTL_SECS = 30   # сколько эмоция считается свежей
BUBBLE_HIDE_SECS = 25   # сколько облачко держится после конца речи
SETTINGS_GRACE_S = 3.0  # сколько ждём первых настроек от ядра, прежде чем показать окно без них
HOTKEY_MUTE = "Ctrl+Alt+M"   # глобальная глушилка голоса


def dashboard_base() -> str:
    """Адрес дашборда ядра (пульт: /api/chat, /api/settings, /api/core). GITS_DASHBOARD_URL, иначе по GITS_CORE_URL."""
    explicit = env("GITS_DASHBOARD_URL")
    if explicit:
        return explicit.rstrip("/")
    core = env("GITS_CORE_URL") or "ws://127.0.0.1:8766/module/ws"
    host = core.split("://", 1)[-1].split("/", 1)[0]
    return f"http://{host}"


def sticker_for(presence: dict, now: float, connected: bool = True) -> str:
    """PET-5: эмоция из presence, пока она моложе EMOTION_TTL_SECS; иначе idle; ядро недоступно - resting."""
    if not connected:
        return "resting"
    emotion = presence.get("emotion")
    try:
        ts = float(presence.get("emotion_ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if emotion in STICKER_MAP and now - ts < EMOTION_TTL_SECS:
        return emotion
    mood_emotion = presence.get("mood_emotion")       # ТЗ 26: когда свежей реакции нет, стикер по настроению
    return mood_emotion if mood_emotion in STICKER_MAP and mood_emotion != "idle" else "idle"


def panel_blocks(settings: dict, presence: dict, now: float, connected: bool, enabled: bool) -> list[dict]:
    """Пространство «Питомец» в дашборде (TZ 15, TAB-9): окно показано или скрыто, стикер, закреплён ли, позиция, связь,
    кнопки «Показать» и «Скрыть» (действия show и hide, TZ 15b: они ставят pet_visible через core.set_setting)."""
    x, y = settings.get("pet_x"), settings.get("pet_y")
    position = f"{int(x)}, {int(y)}" if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
        and not isinstance(x, bool) and not isinstance(y, bool) else "угол по умолчанию"
    link = "подключён к ядру" if connected else "не подключён к ядру"
    return [
        {"type": "stats", "title": "Сейчас", "items": [
            {"label": "Окно", "value": "показано" if settings.get("pet_visible", True) else "скрыто"},
            {"label": "Стикер", "value": sticker_for(presence, now, connected)},
            {"label": "Закреплён", "value": "да" if settings.get("pet_pinned") else "нет"},
            {"label": "Позиция", "value": position}]},
        {"type": "text", "title": "Модуль", "text": link + ("" if enabled else "; выключен")},
        {"type": "buttons", "items": [{"label": "Показать", "action": "show"},
                                      {"label": "Скрыть", "action": "hide"}]},
    ]


@dataclass
class PetState:
    presence: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    connected: bool = False
    enabled: bool = True
    segment: str = ""             # последнее показанное предложение
    segment_ts: float = 0.0       # когда оно звучало в последний раз (во время речи обновляется, после неё: конец речи)
    _was_speaking: bool = False

    def apply(self, kind: str, data, now: float) -> None:
        """Сообщение потока клиента: presence | settings | connected | enabled."""
        if kind == "presence" and isinstance(data, dict):
            self.presence = dict(data)
            speaking = bool(data.get("speaking"))
            seg = str(data.get("segment") or "").strip()
            if speaking and seg:
                self.segment, self.segment_ts = seg, now
            elif self._was_speaking and not speaking:
                self.segment_ts = now                 # речь кончилась: облачко держится ещё BUBBLE_HIDE_SECS
            self._was_speaking = speaking
        elif kind == "settings" and isinstance(data, dict):
            self.settings = dict(data)
        elif kind == "connected":
            self.connected = bool(data)
        elif kind == "enabled":
            self.enabled = bool(data)

    # ── что показывать ───────────────────────────────────────────────────────

    def sticker(self, now: float) -> str:
        return sticker_for(self.presence, now, self.connected)

    def bubble(self, now: float) -> tuple[str, bool]:
        """PET-4: (текст, виден ли). Пока она говорит, показывается presence.segment; после конца речи последний сегмент
        держится BUBBLE_HIDE_SECS."""
        if not self.settings.get("pet_bubble", True):
            return "", False                          # облачко выключено в настройках: остаётся только стикер
        if self.presence.get("speaking") and self.segment:
            return self.segment, True
        if self.segment and now - self.segment_ts < self.bubble_secs:
            return self.segment, True
        return "", False

    @property
    def bubble_secs(self) -> float:
        """Сколько облачко держится после конца речи: настройка `pet_bubble_secs` (3-120 с), иначе BUBBLE_HIDE_SECS."""
        value = self.settings.get("pet_bubble_secs")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return BUBBLE_HIDE_SECS
        return min(120.0, max(3.0, float(value)))

    @property
    def visible(self) -> bool:
        """PET-8: pet_visible == false или модуль выключен - окно скрыто, клиент остаётся подключён."""
        return bool(self.settings.get("pet_visible", True)) and self.enabled

    @property
    def pinned(self) -> bool:
        return bool(self.settings.get("pet_pinned", False))

    @property
    def muted(self) -> bool:
        return bool(self.settings.get("voice_muted", False))

    @property
    def hotkey_input(self) -> str | None:
        return self.settings.get("hotkey_input")


class PetView(Protocol):
    """Что умеет окно: показать/скрыть и перерисовать. Тесты подставляют свою запись вместо tkinter."""

    def set_visible(self, visible: bool) -> None: ...
    def draw(self, sticker: str, text: str, bubble_visible: bool) -> None: ...


class PetController:
    """Вынимает сообщения из очереди потока клиента, ведёт PetState и перерисовывает окно при изменениях."""

    def __init__(self, view: PetView, messages: "queue.Queue", clock: Callable[[], float] = time.time,
                 on_settings: Callable[[dict], None] | None = None):
        self.view, self.q, self.clock = view, messages, clock
        self.state = PetState()
        self._on_settings = on_settings                 # настройки изменились (окно перечитывает закрепление и клавишу)
        self._shown: tuple | None = None                # что нарисовано: перерисовывать только при изменении
        self._visible: bool | None = None
        self._started = clock()

    def pump(self) -> int:
        """Разобрать очередь и перерисовать. Возвращает, сколько сообщений обработано. Вызывать из потока окна."""
        n = 0
        while True:
            try:
                kind, data = self.q.get_nowait()
            except queue.Empty:
                break
            self.state.apply(kind, data, self.clock())
            if kind == "settings" and self._on_settings is not None:
                self._on_settings(self.state.settings)
            n += 1
        self.render()
        return n

    def render(self) -> None:
        now = self.clock()
        # окно не показывается, пока не пришли настройки (позиция, pet_visible); если ядра нет, оно появится через SETTINGS_GRACE_S
        # со стикером resting (PET-5)
        waiting = not self.state.settings and now - self._started < SETTINGS_GRACE_S
        visible = self.state.visible and not waiting
        if visible != self._visible:
            self._visible = visible
            self.view.set_visible(visible)
        text, shown = self.state.bubble(now)
        frame = (self.state.sticker(now), text, shown)
        if frame != self._shown:
            self._shown = frame
            self.view.draw(*frame)
