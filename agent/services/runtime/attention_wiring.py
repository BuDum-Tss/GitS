"""Сборка внимания v2 для ядра (флаг `GITS_ATTENTION=v2`, docs/RUNTIME_PLAN.md этап 3).

Создаёт реестр видов, журнал событий (записи уходят в общий журнал ядра видом `attention_v2`), Runtime поверх единого агента и само
внимание; маршрут событий шины (`attention_v2.routing.V2Router`) ставится в `AttentionEventSink`. Ничего не запускает.
"""
import time
from types import SimpleNamespace

from environments.base import Environment, Tool, ToolError

from services.attention_v2.attention import AttentionV2
from services.attention_v2.eventlog import EventLog
from services.attention_v2.kinds import KindRegistry
from services.attention_v2.event import parse_priority
from services.attention_v2.render import fmt_hms, one_line
from services.attention_v2.routing import (WORK_TICK_KIND, V2Router, kind_prefixes, register_core_kinds, timer_event,
                                           work_tick_event)
from services.reflection.timer import TEXT_PREFIX
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
    # служебные обработчики и хуки видов (`rimworld.colonists`, `rimworld.run_ended`) зовёт старое внимание через свой публичный метод до решения v2
    side_effects = getattr(legacy_attention, "run_kind_handlers", None)
    router = V2Router(v2, registry, kind_prefixes(environ), manifest_of=modules.manifest_of, side_effects=side_effects)
    sink.v2 = router
    companion.attention_v2 = v2

    async def submit_event(peer_id: str, event: dict) -> None:
        """Приём события не от участника шины (движок голоса): по префиксу вида в v2, иначе в старое внимание."""
        if router.wants(event):
            await router.submit(peer_id, event)
        else:
            await legacy_attention.submit(peer_id, event)

    async def timer_emit(body: dict) -> None:
        """Приёмник `ImpulseTimer`: напоминание агента приходит событием `core.timer` (приоритет из `data.priority`, иначе NOTABLE)."""
        data = body.get("data") or {}
        text = str(body.get("text") or "").removeprefix(TEXT_PREFIX)
        try:
            priority = parse_priority(data.get("priority"))
        except ValueError:
            priority = None
        await v2.process_event(timer_event(text, priority=priority))

    async def work_tick_emit(body: dict) -> None:
        """Приёмник `WorkLoop`: рабочий цикл приходит событием `core.work_tick` (NORMAL, срок 90 с, один тик в очереди)."""
        await v2.process_event(work_tick_event(str(body.get("text") or "")))

    def work_busy() -> bool:
        """Для `WorkLoop`: идёт ход по пачке событий или тик уже ждёт в очереди (новый не выдаётся)."""
        return bool(runtime.current_batch()) or any(x.kind == WORK_TICK_KIND for x in v2.queue.items())

    async def close() -> None:
        v2.close()
        await runtime.close()

    return SimpleNamespace(attention=v2, runtime=runtime, router=router, registry=registry, log=log,
                           submit_event=submit_event, timer_emit=timer_emit, work_tick_emit=work_tick_emit, work_busy=work_busy,
                           close=close)


EVENT_LOG_DEFAULT = 20
EVENT_LOG_MAX = 100
EVENT_LOG_TEXT = 160


def event_log_report(log, args: dict, now: float | None = None) -> str:
    """Ответ `core.event_log`: по строке на событие (время, последнее состояние, окружение/вид, текст до 160 символов), свежие внизу.
    `data` событий не показывается. Без журнала (v2 выключен) сообщает об этом."""
    if log is None:
        return "журнал событий v2 выключен (внимание v2 включается флагом GITS_ATTENTION=v2)"
    now = time.time() if now is None else now
    try:
        limit = int(args.get("limit") or EVENT_LOG_DEFAULT)
    except (TypeError, ValueError):
        raise ToolError("limit должен быть числом", "bad_request")
    limit = max(1, min(limit, EVENT_LOG_MAX))
    since = None
    if args.get("since") not in (None, ""):
        try:
            since = now - float(args["since"])
        except (TypeError, ValueError):
            raise ToolError("since это секунды назад (число)", "bad_request")
    query = str(args.get("query") or "").strip().lower()
    rows: dict[str, dict] = {}
    for x in log.query(workspace=args.get("workspace") or None, kind=args.get("kind") or None):
        if x.op not in ("new", "state"):
            continue
        row = rows.setdefault(x.event_id, {"ts": x.ts, "ws": x.workspace, "kind": x.kind, "text": "", "state": x.state})
        row["state"] = x.state
        if x.op == "new":
            row["text"] = x.text
    out = [r for r in rows.values() if (since is None or r["ts"] >= since) and (not query or query in r["text"].lower())]
    if not out:
        return "в журнале событий ничего подходящего нет"
    lines = [f"[{fmt_hms(r['ts'])}] {r['state']} {r['ws']}/{r['kind']}: {one_line(r['text'], EVENT_LOG_TEXT)}" for r in out[-limit:]]
    return f"События ({len(lines)} из {len(out)}):\n" + "\n".join(lines)


def register_v2_tools(registry, v2) -> Environment:
    """`core.event_log` (только чтение): журнал внимания v2 в памяти; при выключенном v2 инструмент отвечает, что журнал выключен."""
    log = v2.log if v2 is not None else None

    async def event_log(a: dict) -> str:
        return event_log_report(log, a)

    tool = Tool(id="core.event_log", env="core", name="event_log",
                description="Журнал событий внимания: что приходило и что с ним стало (время, состояние, окружение/вид, текст). "
                            "Для вопросов «что было недавно», «кто писал», «что я пропустила».",
                params={"query": {"type": "string", "required": False, "description": "слова из текста события"},
                        "workspace": {"type": "string", "required": False, "description": "окружение (discord, telegram, core)"},
                        "kind": {"type": "string", "required": False, "description": "вид события, например core.timer"},
                        "since": {"type": "number", "required": False, "description": "за сколько последних секунд"},
                        "limit": {"type": "integer", "required": False, "description": "сколько записей, по умолчанию 20, не больше 100"}},
                risk="auto", call=event_log, informative=True, read_only=True,
                keywords=("журнал", "события", "что было", "что пропустила", "недавно"), availability="always")
    env = Environment(id="attention_v2", title="Журнал событий внимания", description="Журнал событий внимания v2.", tools=[tool])
    registry.add(env)
    return env
