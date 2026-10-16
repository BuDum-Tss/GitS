"""Event: единый конверт события для размышления и импульсов (ТЗ 30, IMP-1). Чистые данные, без ввода-вывода.

Внешние события приходят от модулей (`origin = external`), внутренние создаёт сам агент (`origin = self`): напоминание,
которое он назначил себе (`self.reminder`), мысль (`self.thought`). Внутри внимания событие по-прежнему `Item`; в размышление
оно передаётся как `Event` (`app/reflection.py`).
"""
from dataclasses import dataclass, field

ORIGIN_EXTERNAL = "external"
ORIGIN_SELF = "self"
SELF_MODULE = "self"                 # id источника внутренних событий в конвейере внимания
KIND_REMINDER = "self.reminder"
KIND_THOUGHT = "self.thought"
KIND_IDLE = "self.idle"                                   # свободное время: осмыслить недавние события (запись в память, без речи)
KIND_AFTER_CONVERSATION = "self.after_conversation"      # разговор затих: пора подвести итог (ТЗ 30, IMP-10)


@dataclass(frozen=True)
class Event:
    kind: str
    text: str
    origin: str = ORIGIN_EXTERNAL
    module: str = ""
    ts: float = 0.0
    sender: dict | None = None
    urgency: int | None = None
    priority: float = 0.0
    memorable: bool = False
    needs_reply: bool = False
    data: dict = field(default_factory=dict)
    item_id: str = ""

    @property
    def is_self(self) -> bool:
        return self.origin == ORIGIN_SELF

    @property
    def depth(self) -> int:
        """Глубина цепочки импульсов: напоминание, рождённое размышлением над напоминанием, глубже на 1."""
        try:
            return max(0, int(self.data.get("depth", 0)))
        except (TypeError, ValueError):
            return 0

    @property
    def trusted(self) -> bool:
        """Можно ли исполнять действия по этому событию: внутреннее без пометки untrusted или от владельца."""
        if self.is_self:
            return not self.data.get("untrusted")
        return bool(self.sender and self.sender.get("is_owner"))
