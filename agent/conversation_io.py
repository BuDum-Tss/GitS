"""Каналы ввода и ответа разговора (ТЗ 02, раздел 9.1-9.2).

Любое обращение пользователя к ней - один вид входа `UserInput(text, channel, sender, ts)`, где канал:
  voice      - микрофон (`MicListener` -> `Companion.on_voice`);
  dashboard  - чат дашборда и строка быстрого ввода питомца (`POST /api/chat`);
  module     - модуль-канал (Telegram и т.п.): обращение владельца, которое менеджер внимания признал обращением к ней.
Ответ уходит в тот канал, откуда пришло обращение. На время обработки обращения канал лежит в контекстной переменной
`current_channel`, поэтому `Companion.say` находит его без передачи параметров через весь путь (маршрутизатор,
планировщик, оговорки «сейчас посмотрю»). Для канала `module` ответ идёт запросом `deliver` модулю и вслух не
произносится; при ошибке или офлайне модуля ответ остаётся в чате дашборда, а в журнале появляется пометка.
"""
import contextvars
import logging
import time
from dataclasses import dataclass, field

import events_log

logger = logging.getLogger("conversation")

DELIVER_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class Channel:
    """Канал обращения: kind - voice | dashboard | module; для module заданы модуль и адресат (reply_channel события)."""

    kind: str
    module: str | None = None
    target: str | None = None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "module": self.module, "target": self.target}

    def label(self) -> str:
        return f"{self.module}:{self.target}" if self.kind == "module" else self.kind


VOICE = Channel("voice")
DASHBOARD = Channel("dashboard")


@dataclass
class UserInput:
    """Обращение пользователя: текст, откуда пришло, кто (для модулей: sender события) и когда."""

    text: str
    channel: Channel = DASHBOARD
    sender: dict | None = None
    ts: float = field(default_factory=time.time)


# канал обработки текущего обращения (None вне обращения: самостоятельные реплики идут в речь и чат, CORE-26)
current_channel: contextvars.ContextVar[Channel | None] = contextvars.ContextVar("current_channel", default=None)


class ConversationIO:
    """Вход обращений из любых каналов и доставка ответов в каналы модулей."""

    def __init__(self, companion, hub=None):
        self.companion = companion
        self.hub = hub                       # ModuleHub: для deliver; ставится ядром после создания шины

    async def submit(self, ui: UserInput) -> None:
        """Обращение пользователя: обычный путь компаньона, ответ уйдёт в канал обращения."""
        await self.companion.on_input(ui)

    async def from_dashboard(self, text: str) -> None:
        await self.submit(UserInput(text, DASHBOARD))

    async def from_module(self, text: str, reply_channel: dict, sender: dict | None = None) -> None:
        """Обращение владельца из модуля-канала: reply_channel события {"module", "target"}."""
        channel = Channel("module", str(reply_channel.get("module") or ""), str(reply_channel.get("target") or ""))
        await self.submit(UserInput(text, channel, sender))

    async def deliver(self, channel: Channel, text: str) -> bool:
        """Доставить ответ в канал модуля (запрос `deliver`). False, если модуль офлайн, не принимает доставку или
        вернул ошибку: тогда ответ остаётся только в чате дашборда (CORE-25), причина пишется в журнал."""
        try:
            conn = self.hub.connection(channel.module) if self.hub is not None and channel.module else None
            if conn is None:
                raise RuntimeError("модуль не подключён")
            if "chat.channel" not in (conn.manifest or {}).get("capabilities", []):
                raise RuntimeError("модуль не принимает доставку (нет возможности chat.channel)")
            value = await conn.request("deliver", {"target": channel.target or "", "text": text},
                                       timeout=DELIVER_TIMEOUT_S)
            if not (isinstance(value, dict) and value.get("delivered")):
                raise RuntimeError("модуль не доставил ответ")
            return True
        except Exception as e:
            events_log.log("module", f"ответ в канал {channel.label()} не доставлен: {e}", module=channel.module,
                           deliver_failed=True)
            logger.warning(f"deliver в {channel.label()}: {e}")
            return False
