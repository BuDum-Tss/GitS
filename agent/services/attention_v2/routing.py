"""Маршрут событий в внимание v2 под флагом `GITS_ATTENTION=v2` (docs/RUNTIME_PLAN.md этап 3).

Какие виды событий идут в v2, задают префиксы `GITS_ATTENTION_V2_KINDS` (через запятую; по умолчанию `discord.`, `telegram.`,
`voicechat.`, `dashboard.`, `core.timer`). Остальное, а также речь, вопросы и ход задач идут в старое внимание как раньше.
Виды событий окружений берутся из манифестов подключённых окружений (ключ `event_kinds`), для необъявленных работает `from_legacy`.
"""
import os
from typing import Callable

from .attention import AttentionV2
from .event import BACKGROUND, IMPORTANT, NOTABLE, USER, Event, Reply, Sender
from .kinds import TTL_CHAT, TTL_SPOKEN, TTL_TIMER, TTL_VOICE, KindRegistry, KindSpec, from_legacy

ENV_FLAG = "GITS_ATTENTION"
ENV_KINDS = "GITS_ATTENTION_V2_KINDS"
DEFAULT_PREFIXES = ("discord.", "telegram.", "voicechat.", "dashboard.", "core.timer")

DASHBOARD_WORKSPACE = "dashboard"
DASHBOARD_KIND = "dashboard.message"
DASHBOARD_REPLY = "dashboard.text"
DASHBOARD_GROUP = "dashboard:owner"

VOICE_WORKSPACE = "voicechat"
VOICE_UTTERANCE_KIND = "voicechat.utterance"
VOICE_SPOKEN_KIND = "voicechat.spoken"
TIMER_KIND = "core.timer"
TIMER_TEXT_PREFIX = "напоминание: "


def v2_enabled(environ=None) -> bool:
    """Флаг `GITS_ATTENTION=v2` (по умолчанию выключено: ядро работает по-старому)."""
    env = os.environ if environ is None else environ
    return str(env.get(ENV_FLAG, "")).strip().lower() == "v2"


def kind_prefixes(environ=None) -> tuple[str, ...]:
    env = os.environ if environ is None else environ
    raw = env.get(ENV_KINDS)
    if raw is None or not str(raw).strip():
        return DEFAULT_PREFIXES
    return tuple(p.strip() for p in str(raw).split(",") if p.strip())


def register_core_kinds(registry: KindRegistry) -> None:
    """Виды самого ядра: сообщение владельца из чата дашборда, таймер, фраза из голосового канала и итог её речи.
    Итог речи (`voicechat.spoken`) это фон: потолок BACKGROUND, а `passive` не даёт запускать ход даже таймеру `ON_TURN_S`: агента не будит, а в ближайшую пачку попадает."""
    registry.register(KindSpec(kind=DASHBOARD_KIND, default_priority=USER, max_priority=USER, ttl_s=TTL_CHAT, coalesce=True,
                               reply_tool=DASHBOARD_REPLY), DASHBOARD_WORKSPACE)
    registry.register(KindSpec(kind=TIMER_KIND, default_priority=NOTABLE, max_priority=IMPORTANT, ttl_s=TTL_TIMER), "core")
    registry.register(KindSpec(kind=VOICE_UTTERANCE_KIND, default_priority=BACKGROUND, max_priority=USER, ttl_s=TTL_VOICE,
                               coalesce=True), VOICE_WORKSPACE)
    registry.register(KindSpec(kind=VOICE_SPOKEN_KIND, default_priority=BACKGROUND, max_priority=BACKGROUND, ttl_s=TTL_SPOKEN,
                               passive=True),
                      VOICE_WORKSPACE)


def dashboard_event(text: str, *, ts: float | None = None) -> Event:
    """Сообщение владельца из чата дашборда: приоритет USER, срок 15 минут, `group_key` один (серия сообщений сливается)."""
    kw = {} if ts is None else {"ts": ts}
    return Event(workspace=DASHBOARD_WORKSPACE, kind=DASHBOARD_KIND, text=text,
                 sender=Sender(id="owner", name="пользователь", is_owner=True), priority=USER, group_key=DASHBOARD_GROUP,
                 reply=Reply(tool=DASHBOARD_REPLY), **kw)


class V2Router:
    """Приёмник событий шины для внимания v2: выбирает по префиксу вида, синхронизирует виды из манифеста, переводит событие."""

    def __init__(self, attention: AttentionV2, registry: KindRegistry, prefixes: tuple[str, ...] = DEFAULT_PREFIXES,
                 manifest_of: Callable[[str], dict | None] | None = None):
        self.attention = attention
        self.registry = registry
        self.prefixes = tuple(prefixes)
        self.manifest_of = manifest_of
        self._synced: dict[str, object] = {}              # окружение -> объявленные виды, которые уже внесены в реестр

    def wants(self, event: dict) -> bool:
        kind = str((event or {}).get("kind") or "")
        return bool(kind) and any(kind.startswith(p) for p in self.prefixes)

    def sync_manifest(self, peer_id: str) -> None:
        """Внести в реестр виды из манифеста окружения (при смене манифеста заменяются)."""
        manifest = self.manifest_of(peer_id) if self.manifest_of is not None else None
        kinds = (manifest or {}).get("event_kinds")
        if not kinds or self._synced.get(peer_id) == kinds:
            return
        try:
            self.registry.register_manifest(peer_id, {"event_kinds": kinds})
            self._synced[peer_id] = kinds
        except ValueError:
            self._synced[peer_id] = kinds                  # неверное объявление не должно ронять приём событий: вид станет legacy

    async def submit(self, peer_id: str, event: dict) -> None:
        self.sync_manifest(peer_id)
        await self.attention.process_event(from_legacy(peer_id, event, self.registry))


def timer_event(text: str, *, priority=None, ts: float | None = None) -> Event:
    """Напоминание или таймер агента: событие `core.timer` окружения `core` (по умолчанию NOTABLE, потолок IMPORTANT, срок 10 минут)."""
    kw = {} if ts is None else {"ts": ts}
    return Event(workspace="core", kind=TIMER_KIND, text=TIMER_TEXT_PREFIX + text, priority=priority, **kw)
