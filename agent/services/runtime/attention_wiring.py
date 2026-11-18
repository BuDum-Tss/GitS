"""Сборка внимания v2 для ядра (флаг `GITS_ATTENTION=v2`, docs/RUNTIME_PLAN.md этап 3).

Создаёт реестр видов, журнал событий (записи уходят в общий журнал ядра видом `attention_v2`), Runtime поверх единого агента и само
внимание; маршрут событий шины (`attention_v2.routing.V2Router`) ставится в `AttentionEventSink`. Ничего не запускает.
"""
from types import SimpleNamespace

from services.attention_v2.attention import AttentionV2
from services.attention_v2.eventlog import EventLog
from services.attention_v2.kinds import KindRegistry
from services.attention_v2.routing import V2Router, kind_prefixes, register_core_kinds
from services.runtime.turns import ResidentRuntime
from services.tracing import events_log

JOURNAL_KIND = "attention_v2"


def journal_sink(entry: dict) -> None:
    """Запись внимания v2 в общий журнал ядра: события и переходы состояний. Поле `data` событий в запись не входит
    (приватные данные адаптера в журнал не идут), только текст и метаданные."""
    ev = f"{entry.get('workspace')}/{entry.get('kind')}"
    text = f"{entry.get('op')} {entry.get('state')} {ev}" + (f": {entry['reason']}" if entry.get("reason") else "")
    fields = {k: v for k, v in entry.items() if k not in ("text", "reason") and v not in (None, "", {})}
    if entry.get("op") == "new" and entry.get("text"):
        fields["text"] = str(entry["text"])[:300]
    events_log.log_nowait(JOURNAL_KIND, text, **fields)


def wire_attention_v2(companion, legacy_attention, modules, workspaces, sink, environ=None) -> SimpleNamespace:
    """Собрать внимание v2 и подключить его: `companion.attention_v2`, `sink.v2`. Старое внимание остаётся для речи, вопросов,
    задач и остальных видов событий; остановку речи при прерывании делает его очередь речи."""
    registry = KindRegistry()
    register_core_kinds(registry)
    log = EventLog(sink=journal_sink)
    stop = None
    speech = getattr(legacy_attention, "speech", None)
    if speech is not None and hasattr(speech, "stop_all"):
        async def stop(why: str) -> None:
            await speech.stop_all()
    runtime = ResidentRuntime(companion.resident, stop_speech=stop)
    v2 = AttentionV2(registry, log, runtime, focus_getter=lambda: workspaces.current)
    runtime.attention_v2 = v2
    router = V2Router(v2, registry, kind_prefixes(environ), manifest_of=modules.manifest_of)
    sink.v2 = router
    companion.attention_v2 = v2

    async def submit_event(peer_id: str, event: dict) -> None:
        """Приём события не от участника шины (движок голоса): по префиксу вида в v2, иначе в старое внимание."""
        if router.wants(event):
            await router.submit(peer_id, event)
        else:
            await legacy_attention.submit(peer_id, event)

    async def close() -> None:
        v2.close()
        await runtime.close()

    return SimpleNamespace(attention=v2, runtime=runtime, router=router, registry=registry, log=log,
                           submit_event=submit_event, close=close)
