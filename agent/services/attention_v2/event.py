"""Единая запись `Event` (RUNTIME_SPEC раздел 3.1), состояния и уровни приоритета (3.2)."""
import itertools
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

BACKGROUND = 10
NORMAL = 30
NOTABLE = 50
IMPORTANT = 70
USER = 100

LEVELS = {"BACKGROUND": BACKGROUND, "NORMAL": NORMAL, "NOTABLE": NOTABLE, "IMPORTANT": IMPORTANT, "USER": USER}
LEVEL_NAMES = [(USER, "USER"), (IMPORTANT, "IMPORTANT"), (NOTABLE, "NOTABLE"), (NORMAL, "NORMAL"), (0, "BACKGROUND")]

MAX_TEXT = 1000                       # длина `text`, раздел 3.1
_counter = itertools.count(1)
_run = uuid.uuid4().hex[:6]


def new_event_id() -> str:
    """Уникальный id: метка запуска плюс счётчик (читаемо в журнале)."""
    return f"ev-{_run}-{next(_counter)}"


def level_name(priority: float) -> str:
    """Ближайший нижний именованный уровень (для метрик): 20 это BACKGROUND, 55 это NOTABLE."""
    for value, name in LEVEL_NAMES:
        if priority >= value:
            return name
    return "BACKGROUND"


def parse_priority(value) -> float | None:
    """Число или имя уровня (`IMPORTANT`); None остаётся None. Неверное значение это ValueError."""
    if value is None:
        return None
    if isinstance(value, str):
        if value.upper() in LEVELS:
            return LEVELS[value.upper()]
        raise ValueError(f"неизвестный уровень приоритета: {value!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"приоритет должен быть числом или именем уровня: {value!r}")
    return max(0, min(100, value))


class EventState(str, Enum):
    NEW = "new"
    QUEUED = "queued"
    DELIVERED = "delivered"
    INTERRUPTED = "interrupted"
    EXPIRED = "expired"
    EVICTED = "evicted"
    DROPPED = "dropped"


@dataclass
class Sender:
    id: str = ""
    name: str = ""
    is_owner: bool = False
    is_bot: bool = False

    @classmethod
    def from_obj(cls, obj) -> "Sender | None":
        """Из словаря шины, строки (только имя) или готового `Sender`."""
        if obj is None or isinstance(obj, Sender):
            return obj
        if isinstance(obj, str):
            return cls(id=obj, name=obj)
        if isinstance(obj, dict):
            return cls(id=str(obj.get("id", "") or ""), name=str(obj.get("name", "") or ""),
                       is_owner=bool(obj.get("is_owner", False)), is_bot=bool(obj.get("is_bot", False)))
        return None


@dataclass
class Reply:
    """Чем отвечать: инструмент (`discord.say`, `discord.text`) и адрес."""
    tool: str
    target: str | None = None


@dataclass(kw_only=True)
class Event:
    kind: str
    workspace: str
    text: str = ""
    id: str = field(default_factory=new_event_id)
    ts: float = field(default_factory=time.time)
    sender: Sender | None = None
    data: dict = field(default_factory=dict)
    priority: float | None = None             # None: возьмётся `default_priority` вида при приёме
    expires_at: float | None = None           # None: `ts + ttl_s` вида при приёме
    reply: Reply | None = None
    group_key: str | None = None
    state: EventState = EventState.NEW
    interrupted: bool = False

    def __post_init__(self):
        self.sender = Sender.from_obj(self.sender)
        if isinstance(self.priority, str):
            self.priority = parse_priority(self.priority)

    @property
    def prio(self) -> float:
        return 0 if self.priority is None else self.priority

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and self.expires_at <= now
