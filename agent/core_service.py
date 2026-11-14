"""Ядро GitS: всегда включённый сервис ассистента.

Что работает всегда:
  - окружения (environments/): реестр инструментов, маршрутизатор «могу или нет», манифесты;
  - компаньон (companion): разговор, память, проактивность, граф памяти;
  - дашборд (порт 8766, только localhost): состояние, чат, настройки;
  - шина модулей (ТЗ 02): модули подключаются сами по WebSocket (/ws в том же приложении), супервизор запускает и
    перезапускает их процессы из agent/peers.yaml, тон, видимые инструменты и области памяти среды лежат в services/spaces/space_config.yaml. Telegram
    (канал, бот, её аккаунт) это модуль workspaces/telegram (ТЗ 10): в процессе ядра его кода нет. Игра RimWorld это модуль rimworld
    из отдельного репозитория rimworld-module (ТЗ 11): приказы идут его инструментом order, состояние и события приходят по шине.
    Весь звук (микрофон, распознавание, узнавание голосов, синтез, воспроизведение, слух системного звука) это модуль voice
    (services/voice, ТЗ 12): речь уходит ему сообщением speak, а что сказал пользователь и когда говорит она, приходит событиями
    voice.*. Показ состояния на экране идёт подписчиками темы presence (ядро их по имени не знает, ТЗ 13).
"""
import asyncio
import os
import logging
import time as _time
from pathlib import Path
from types import SimpleNamespace

from services.character import character
import http_api
from services.tracing import events_log
from services.tracing import tool_trace
from services.memory_service import episode_routes
from services.memory_service import episode_service
from services.memory_service import scope as graph_scope
from services.reflection import service as impulse_service
from services.memory_service import migration as memory_migration
from services.tasks import wiring as task_wiring
from services.spaces.focus import SpaceFocus
from host import settings
from gits_sdk import protocol as P
from services.conversation import speech_out
from host import supervisor as supervisor_mod
from services.tools import tool_search
from services.spaces.manager import WorkspaceManager
from services.attention import Attention, AttentionEventSink
from services.spaces.link import WorkspaceLink
from services.spaces.lease import LeaseAdapter
from services.character.character_switch import CharacterSwitch
from services.conversation.companion import Companion
from services.conversation.conversation_io import ConversationIO
from services.peers import requests as core_requests
from services.peers.requests import CoreRequests
from environments import builtin
from environments.registry import Registry
from services.event_handlers.desktop import desktop_state, make_activity_hook
from services.runtime import llm_gate
from kernel import scoring
from services.spaces.node_panel import NodePanel
from services.reflection.work_loop import WorkLoop
from services.event_handlers.game import make_colonists_handler, make_run_lost_hook
from services.event_handlers.voice import VOICE_SILENT_KINDS, make_heard_hook, make_voice_handlers
from services.peers.hub import PeerHub
from services.peers.registry import PeerRegistry
from services.memory_service import GraphMemory
from services.character.relationships import Relationships
from services.tools.router import Router

logger = logging.getLogger("core")

DASHBOARD_PORT = 8766
SUBGRAPH_TIMEOUT_S = 1.5       # CORE-22: подграф профиля от модуля для дашборда


def core_public_url(port: int = DASHBOARD_PORT) -> str:
    """Адрес шины ядра для арендованного пространства: `GITS_CORE_PUBLIC_URL` (в контейнере адрес контейнера ядра)
    или `ws://127.0.0.1:<порт>/ws`."""
    return os.environ.get("GITS_CORE_PUBLIC_URL", "").strip() or f"ws://127.0.0.1:{port}{P.WS_PATH}"


def _workspace_manifests(modules, leases) -> list:
    """Манифесты сред для менеджера: подключённых модулей, а у арендуемых пространств без подключения описание из `/info`."""
    items = [(rec.id, rec.manifest, rec.title) for rec in modules.records.values() if rec.manifest]
    seen = {i[0] for i in items}
    for ws in leases.spaces:
        manifest = None if ws in seen else leases.manifest_of(ws)
        if manifest is not None:
            items.append((ws, manifest, manifest["workspace"]["title"]))
    return items


def wire_workspace_nodes(companion, registry, attention, modules, workspaces) -> NodePanel:
    """ТЗ 33: рабочее место модуля. Узел, о котором сообщил модуль (`state.node`), определяет видимость инструментов узла,
    сдвиг срочности событий по теме (`nodes[].watch`) и панель узла в контексте агента (`nodes[].context`)."""
    registry.node_of = modules.node

    def node_watch(peer_id: str) -> dict:
        node, manifest = modules.node(peer_id), modules.manifest_of(peer_id) or {}
        for n in manifest.get("nodes") or []:
            if isinstance(n, dict) and n.get("path") == node:
                return dict(n.get("watch") or {})
        return {}
    attention.set_node_watch(node_watch)
    panel = NodePanel(env_of=lambda: workspaces.current, manifest_of=modules.manifest_of, node_of=modules.node,
                      run_tool=lambda tool_id, args: companion.router.run_tool_ex(tool_id, args))
    companion.node_panel = panel
    companion.node_panel_block = panel.block
    return panel


def wire_modules(companion, registry, *, specs=None,
                 port: int = DASHBOARD_PORT) -> SimpleNamespace:
    """Слой модулей (ТЗ 02): каналы разговора, заглушка внимания, реестр и шина модулей, профили, супервизор.

    companion - Companion (или заменитель с теми же методами), registry - реестр окружений планировщика.
    Ничего не запускает: старт и остановка - layer.start() и layer.stop()."""
    presence = companion.presence
    io = ConversationIO(companion)
    companion.io = io
    attention = Attention(companion, io, idle_source=lambda: float(desktop_state(modules).get("idle_s") or 0.0))
    companion.attention = attention                      # речь идёт через очередь внимания (ТЗ 03, ATT-13)
    requests = CoreRequests(attention=attention, memory_reader=getattr(companion, "memory_reader", None),
                            memory_writer=getattr(companion, "memory_writer", None))
    holder: dict = {}                                    # менеджер сред и фокус появятся ниже, а реестру и шине нужны уже при создании
    modules = PeerRegistry(registry, on_state=lambda: holder["workspaces"].check())
    hub = PeerHub(modules, sink=AttentionEventSink(attention, presence, recorder=_module_event_recorder(modules, attention)), requests=requests,
                    presence=presence, profile_source=lambda: holder["focus"].info())
    hub.attention = attention                            # только для диагностики и тестов: шина работает через sink (ARC-7)
    io.hub = hub
    attention.connect(hub=hub, modules=modules)
    # среды агента: среду модуля объявляет его манифест и включает только импульс (сообщение пользователя, самоимпульс);
    # обмен с модулем (вход, выход, возобновление) делает WorkspaceLink
    leases = LeaseAdapter(character=lambda: character.active().id, core_url=lambda: core_public_url(port),
                          core_token=lambda: hub.token, on_event=_log_lease)
    modules.leases = leases                              # аренда общих пространств: состояние хранит сам сервис пространства
    link = WorkspaceLink(connection_of=hub.connection, leases=leases)

    async def enter_module(ws: str, goal: str) -> dict:
        if not goal:                                     # вход без новой цели: вернуть приостановленную задачу, если она есть
            res = await link.resume(ws)
            if res.get("ok"):
                return res
        return await link.activate(ws, goal)

    def on_workspace_change(rec: dict) -> None:
        if link.holder is not None and rec.get("to") != link.holder["module"]:
            link.forget(link.holder["module"])             # среда модуля кончилась: слот свободен
        events_log.log("profile", f"{rec.get('from')} -> {rec.get('to')}", old=rec.get("from"), new=rec.get("to"))
        hub.notify("profile", holder["focus"].info())      # тон и видимые инструменты следуют за средой

    workspaces = WorkspaceManager(
        activate=enter_module,
        release=lambda ws, reason, force: link.release(reason=reason, force=force),
        interruptible=lambda ws: bool((_module_status(modules, ws) or {}).get("interruptible", True)),
        connected=hub.is_connected, status_of=lambda ws: _module_status(modules, ws),
        leased=leases.manages, lease_view=leases.view,
        modules_of=lambda: _workspace_manifests(modules, leases),
        on_change=on_workspace_change,
        log=lambda kind, **data: _log_workspace(kind, data))
    holder["workspaces"] = workspaces
    workspaces.refresh_leases = leases.refresh
    attention.set_env_of(lambda: workspaces.current)            # среда: вес записей очереди, подавление ассистентского шума в игре
    wire_workspace_nodes(companion, registry, attention, modules, workspaces)
    modules.is_noise = lambda module, kind: scoring.is_assistant_noise(workspaces.current, module, kind)     # отчёт о событиях без экрана пользователя
    presence.listener = hub.broadcast_presence
    speech = getattr(companion, "speech", None)
    if hasattr(speech, "attach"):
        speech.attach(hub, modules)                      # VOICE-15: речь идёт модулю voice, пока он подключён
    attention.add_silent_kinds(VOICE_SILENT_KINDS)
    attention.register_handlers(make_voice_handlers(companion, attention, speech))
    attention.register_hook("voice.heard", make_heard_hook(companion))
    attention.register_hook("desktop.activity", make_activity_hook(companion))
    attention.register_hook("rimworld.run_ended", make_run_lost_hook())

    focus = SpaceFocus(workspaces, streamer=lambda: bool(settings.load().get("streamer")))
    holder["focus"] = focus
    registry.focus = focus.focus_modules
    companion.profile_hint = focus.hint
    companion.workspace_of = lambda: workspaces.current

    def environment_note() -> str:
        ws = workspaces.current
        if ws in ("assistant", "sleep"):
            return ""
        spec = workspaces.specs.get(ws)
        rec = modules.records.get(ws)
        summary = ((rec.state or {}).get("summary") or "").strip() if rec is not None and rec.connected else ""
        goal = f" Цель: {workspaces.goal}." if workspaces.goal else ""
        return f"Ты сейчас в среде «{spec.title if spec else ws}».{goal}" + (f" Сводка: {summary}." if summary else "")
    companion.environment_note = environment_note
    companion.events_block = attention.context_events        # ждущие события очереди видны в контексте ответа
    companion.module_notes = modules.notes                # что умеют подключённые модули и в каком они состоянии
    companion.idle_seconds = lambda: float(desktop_state(modules).get("idle_s") or 0.0)     # простой (ТЗ 14, DESK-7)
    companion.screen_activity = lambda: desktop_state(modules).get("activity")              # активность экрана (DESK-7)
    companion.job_wait = modules.wait_job                                                  # итог задачи модуля (DESK-11)
    modules.context_for = lambda query: companion._context(include_hearing=False, query=query)   # core_fills: context (DESK-12)

    specs = supervisor_mod.load_specs() if specs is None else specs
    for spec in specs:
        modules.declare(spec.id, title=spec.title, default_enabled=spec.enabled, role=spec.role, ownership=spec.ownership,
                        external=spec.external, description=spec.description, dev=spec.dev, control_url=spec.control_url)
        if spec.control_url:                             # общее пространство в контейнере: аренда, а не подключение при старте
            leases.register(spec.id, spec.control_url, os.environ.get(spec.control_token_env, ""))
    for planned in supervisor_mod.load_planned():
        modules.declare_planned(planned.id, title=planned.title, tz=planned.tz)

    async def send_shutdown(peer_id: str) -> bool:
        conn = hub.connection(peer_id)
        if conn is None:
            return False
        await conn.send("shutdown", {"reason": "модуль выключен или ядро останавливается"})
        return True

    async def announce_launch(peer_id: str, text: str) -> None:
        """Запуск процесса модуля супервизором (правило 14): объявление идёт через очередь внимания, как у инструментов."""
        await attention.submit_announce(peer_id, text, data={"source": "supervisor"})

    sup = supervisor_mod.Supervisor(specs, token=hub.token, core_url=f"ws://127.0.0.1:{port}{P.WS_PATH}",
                                    enabled=modules.is_enabled, is_connected=hub.is_connected,
                                    send_shutdown=send_shutdown, memory_dir=settings.memory_dir(),
                                    announce=announce_launch,
                                    only_core=lambda: bool(settings.load().get("only_core")))
    modules.supervisor = sup
    builtin.register_core_tools(registry, modules, attention,
                                switch=lambda cid: layer.character_switch.tool(cid),   # ТЗ 05, CHR-7
                                workspaces=workspaces)                                  # ТЗ 32: workspace.switch, workspace.status

    layer = SimpleNamespace(io=io, attention=attention, requests=requests, modules=modules, hub=hub, focus=focus,
                            supervisor=sup, presence=presence, tasks=[], link=link,
                            workspaces=workspaces)

    async def start():
        await hub.start()                                # токен в файл и фоновая сверка настроек
        await leases.refresh()                           # описание и состояние арендуемых пространств (без таймеров: дальше по запросу)
        await attention.start()                          # планировщик речи, сроки фокуса, чистка правил
        layer.tasks.append(asyncio.get_running_loop().create_task(workspaces.run()))
        await sup.start()

    async def stop():
        for task in layer.tasks:
            task.cancel()
        await attention.stop()
        await leases.release_all()                       # пространства возвращаются хозяину-семафору
        await sup.stop_all()                             # CORE-17: shutdown всем, через 10 с убить оставшихся
        await hub.stop()

    layer.start, layer.stop = start, stop
    return layer


async def _migrate_memory(companion, graph, folder: Path) -> None:
    """Перенос старой `memories.json` в граф и задачи (`services/memory_service/migration.py`); сбой переноса не мешает запуску."""
    try:
        done = await memory_migration.migrate(folder, graph, getattr(companion, "tasks", None), companion._owner_name())
        if done:
            events_log.log("memory", f"старая память перенесена: владелец {done['owner']}, "
                                     f"планы {done['plans']}, отброшено мелочи {done['dropped']}")
    except Exception as e:
        logger.warning(f"Перенос старой памяти: {e}")


AUTO_SWITCH_REASONS = ("schedule", "done", "blocked", "critical", "disconnect")      # смены среды не по инструменту: в чате их иначе не видно


def _log_lease(kind: str, data: dict) -> None:
    """Журнал аренды пространств: факт без токенов (acquired, released, busy, unavailable)."""
    events_log.log_nowait("workspace", f"аренда {kind}: {data.get('workspace')}", type="lease", lease=kind,
                          **{k: v for k, v in data.items() if k in ("workspace", "reason", "holder")})


def _log_workspace(kind: str, data: dict) -> None:
    events_log.log_nowait("workspace", f"{data.get('from')} -> {data.get('to')}", type=kind, **data)
    if kind == "switch" and data.get("reason") in AUTO_SWITCH_REASONS:
        tool_trace.note("external", "workspace.switch", f"{data.get('from')} -> {data.get('to')} ({data.get('reason')})")


def _module_event_recorder(modules, attention):
    """Журнал событий модулей по запросу (`core.module_events`) и строки в чат о том, что сделал модуль сам (запуск игры)."""
    def record(peer_id: str, event: dict) -> None:
        kind = str(event.get("kind") or "")
        if attention.is_silent(kind):
            return
        modules.note_event(peer_id, event)
        if kind in TRACED_EVENT_KINDS:
            tool_trace.note("external", kind, str(event.get("text") or "")[:120])
    return record


TRACED_EVENT_KINDS = ("rimworld.game_started",)


def _module_status(modules, peer_id: str) -> dict | None:
    """Статус модуля (ТЗ 18, FOC-1) из его последнего `state`: data.status; None, если модуль его не прислал."""
    rec = modules.get(peer_id)
    data = (rec.state or {}).get("data") if rec is not None and rec.state else None
    status = (data or {}).get("status") if isinstance(data, dict) else None
    return status if isinstance(status, dict) else None


async def _run(config: dict):
    # httpx пишет в журнал полный URL запроса (в запросах к API бывают токены): журнал не должен его хранить
    for noisy in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    mem_dir = config["memory_dir"]
    settings.init(mem_dir)
    character.init()                    # ТЗ 05: персонаж из настроек (его папка, имя, память)
    char_dir = character.memory_dir(character.active().id)   # личная память активного персонажа (CHR-10)
    rel = Relationships(char_dir)
    presence = speech_out.PresenceTracker()
    # речь: модуль voice, пока он подключён и включён (VOICE-15), иначе только текст в чате и в presence
    speech = speech_out.SpeechSelector(speech_out.VoiceModuleSpeech(presence), speech_out.TextOnlySpeech(presence))
    companion = Companion(char_dir, rel, speech=speech, presence=presence)

    # профили: активное окно и простой приходят состоянием модуля desktop (ТЗ 14), игра RimWorld (game_running) приходит
    # состоянием модуля rimworld (ТЗ 11). Без модуля desktop данных нет, профиль по окну не выбирается
    registry = Registry()
    registry.embedder = tool_search.ToolEmbeddings(Path(mem_dir) / "tool_embeddings.json")     # отбор инструментов по смыслу
    layer = wire_modules(companion, registry)
    http_api.set_modules(layer.modules, layer.hub, layer.supervisor)
    http_api.set_attention(layer.attention)
    http_api.set_workspace(layer.workspaces)            # ТЗ 32: карточка «Сейчас», среда агента
    http_api.set_presence(presence)
    loop = asyncio.get_running_loop()
    await http_api.start_dashboard(
        memory_dir=mem_dir, chat_callback=lambda m: loop.create_task(layer.io.from_dashboard(m)))
    await layer.start()                  # токен модулей в файл, сверка настроек, профили, процессы модулей

    # Окружения: встроенные плагины сразу, манифесты (MCP, вебхуки, RimWorld) подгружаются в фоне
    memory = companion.memory_reader                     # одна память на компаньона, инструменты и разговор
    layer.requests.privacy_terms = lambda: core_requests.collect_privacy_terms(
        companion._owner_name(), rel.people_terms(), [f["text"] for f in memory.facts()])      # core.privacy_terms (TG-12)
    graph = GraphMemory(mem_dir, busy=lambda: _time.time() - companion._last_user_ts < 20 or companion.is_speaking())
    companion.graph = graph
    graph.scopes_of = layer.focus.memory_scopes        # ТЗ 04, KB-12/13: области графа по текущей среде
    graph.scope_of = graph_scope.make_scope_of(
        layer.attention.decider, graph.scopes_of,
        layer.focus.scope_title)
    http_api.set_graph(graph)
    http_api.set_tasks(lambda: getattr(companion, "tasks", None))
    layer.character_switch = CharacterSwitch(companion=companion, rel=rel, graph=graph, attention=layer.attention,
                                             hub=layer.hub)
    http_api.set_character(layer.character_switch)
    graph.set_character(character.active().id)           # новые факты пишутся в группу активного персонажа
    asyncio.create_task(graph.run())
    try:
        companion.memory_writer.loop = loop             # планы из рабочих потоков ставятся задачами в цикл ядра
    except AttributeError:
        pass
    builtin.register(registry, companion, memory)

    companion.router = Router(registry, companion.ask_permission)     # вопрос о разрешении формулирует модель по факту

    async def enter_after_launch(tool, args, ok, result) -> None:
        """Игру запустили по просьбе пользователя: заходим в среду её модуля (цели, камера, комментарии и ход идут только в
        фокусе). По самоимпульсу сюда не входим: там вход решает размышление (ТЗ 32)."""
        from services.reflection import origin as impulse_origin
        if not ok or impulse_origin.current() == impulse_origin.SELF:
            return
        layer.workspaces.sync()
        ws = None
        if tool.name == "launch_game":
            ws = str(tool.id).split(".", 1)[0]
        elif tool.id == "desktop.launch_app":           # «запусти римворлд» планировщик чаще понимает как запуск приложения
            wanted = str(args.get("name") or "").lower()
            ws = next((sid for sid, spec in layer.workspaces.specs.items()
                       if spec.kind == "module" and wanted and (sid.lower() in wanted or spec.title.lower() in wanted)), None)
        if ws is not None and ws in layer.workspaces.specs:
            res = await layer.workspaces.enter(ws, "user", str(args.get("reason") or "")[:200])
            tool_trace.note("external", "workspace.switch", f"{ws}: {'ok' if res.get('ok') else res.get('error')}")
    companion.router.after_tool = enter_after_launch
    layer.requests.router = companion.router            # core.tool.call от модулей идёт тем же путём, что просьбы
    http_api.set_router(companion.router)              # POST /api/menu_click идёт инструментом desktop.menu_click (DESK-14)
    http_api.set_registry(registry)
    http_api.set_focus(layer.focus)
    asyncio.create_task(registry.load_manifests(Path(__file__).parent / "environments" / "manifests"))
    layer.requests.tasks = companion.tasks = task_wiring.build(companion, layer.workspaces)    # ТЗ 32, WS-8: общие задачи, напоминания себе и планы
    await _migrate_memory(companion, graph, Path(companion._dir))      # старая memories.json -> граф и задачи (один раз)
    link_change = layer.workspaces.on_change                            # слот модуля и профиль среды
    task_change = companion.tasks.on_workspace_change                   # checkpoint задач при уходе из среды, возобновление при входе

    def on_change(rec):
        link_change(rec)
        task_change(rec)
    layer.workspaces.on_change = on_change
    impulses = impulse_service.build(companion, layer.attention, tasks=companion.tasks)             # ТЗ 30: импульсы и размышление
    await impulses.timer.start()                         # восстановить сроки напоминаний себе
    work_loop = WorkLoop(env_of=lambda: layer.workspaces.current, manifest_of=layer.modules.manifest_of, agent=companion.resident,
                         emit=layer.attention.submit_self, settings=layer.attention.settings_now,
                         quiet=layer.attention.quiet_now, blocked=llm_gate.blocked,
                         tasks=companion.tasks)
    work_loop.start()                                    # ТЗ 33: автономная работа агента в среде игры или мира
    companion.router.auto_ok = lambda: work_loop.spec() is not None      # пока она играет сама, обычные действия идут без вопроса
    http_api.set_impulses(impulses)
    episodes = episode_service.build(companion, layer.attention,       # ТЗ 27: эпизоды и сон (простой, расписание, догоняющий)
                                     busy=lambda: layer.attention.top_focus().kind in ("conversation", "question")
                                     or layer.link.holder is not None)
    await episodes.start()
    episodes.scheduler.on_state = layer.workspaces.on_sleep_state      # ТЗ 32: сон это среда sleep
    episode_routes.set_service(episodes)
    layer.requests.episodes = episodes                    # core.memory.episodes
    await impulses.plans.start()                         # планы пользователя -> напоминания себе (без опроса модели, ТЗ 30)

    # ── события игры (ТЗ 11, RW-9): список колонистов обновляет отношения и граф памяти, не озвучивается ──
    layer.attention.register_handler("rimworld.colonists", make_colonists_handler(rel, graph))
    logger.info("Ядро запущено: зрение, компаньон, быстрый слой, шина модулей. Игра и модули подключатся, когда появятся.")
    try:
        await asyncio.Event().wait()
    finally:
        await episodes.stop()
        await impulses.timer.stop()
        await work_loop.stop()
        if impulses.idle is not None:
            impulses.idle.stop()
        await layer.stop()


def run(config: dict):
    # голос по умолчанию быстрый (edge-tts): голос персонажа (ESpeech, Fish Speech) синтезируется секунды, а при занятом сервисе речь
    # падает на запасной голос, и голос «плавает». Выбрать другой: TTS_ENGINE=auto|espeech|fish в .env.
    os.environ.setdefault("TTS_ENGINE", "edge")
    asyncio.run(_run(config))
