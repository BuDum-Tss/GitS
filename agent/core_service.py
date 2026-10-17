"""Ядро GitS: всегда включённый сервис ассистента.

Что работает всегда:
  - окружения (environments/): реестр инструментов, маршрутизатор «могу или нет», манифесты;
  - компаньон (companion): разговор, память, проактивность, граф памяти;
  - дашборд (порт 8766, только localhost): состояние, чат, настройки;
  - шина модулей (ТЗ 02): модули подключаются сами по WebSocket (/module/ws в том же приложении), супервизор запускает и
    перезапускает их процессы из agent/modules.yaml, профили в agent/profiles/ решают, какие инструменты видны. Telegram
    (канал, бот, её аккаунт) это модуль modules/telegram (ТЗ 10): в процессе ядра его кода нет. Игра RimWorld это модуль rimworld
    из отдельного репозитория rimworld-module (ТЗ 11): приказы идут его инструментом order, состояние и события приходят по шине.
    Весь звук (микрофон, распознавание, узнавание голосов, синтез, воспроизведение, слух системного звука) это модуль voice
    (modules/voice, ТЗ 12): речь уходит ему сообщением speak, а что сказал пользователь и когда говорит она, приходит событиями
    voice.*. Питомец на экране это модуль pet (modules/pet, ТЗ 13): он показывает присутствие (тему presence).
"""
import asyncio
import logging
import time as _time
from pathlib import Path
from types import SimpleNamespace

import character
import claude_battery
import dashboard
import events_log
import fast_brain
import episode_routes
import episode_service
import graph_scope
import impulse_service
import memory_migration
import task_wiring
import focus_dispatch
import profile_manager
import settings
import speech_out
import supervisor as supervisor_mod
import tool_search
from app.workspace_manager import WorkspaceManager
from attention import Attention
from focus import STATE_FILE, FocusManager
from character_switch import CharacterSwitch
from companion import Companion
from conversation_io import ConversationIO
import core_requests
from core_requests import CoreRequests
from environments import builtin
from environments.registry import Registry
from adapters.module_events import AttentionEventSink
from app.handlers.desktop import desktop_state, make_activity_hook
from app.handlers.game import make_colonists_handler, make_game_order, make_run_lost_hook
from app.handlers.voice import VOICE_SILENT_KINDS, make_heard_hook, make_voice_handlers
from module_hub import ModuleHub
from module_registry import ModuleRegistry
from graph_memory import GraphMemory
from relationships import Relationships
from router import Router

logger = logging.getLogger("core")

DASHBOARD_PORT = 8766
SUBGRAPH_TIMEOUT_S = 1.5       # CORE-22: подграф профиля от модуля для дашборда


def wire_modules(companion, registry, *, battery=claude_battery.status, specs=None,
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
    holder: dict = {}                                    # profiles появится ниже, а реестру и шине он нужен уже при создании
    modules = ModuleRegistry(registry, on_state=lambda: (holder["profiles"].poke(), holder["workspaces"].check()))
    hub = ModuleHub(modules, sink=AttentionEventSink(attention, presence), requests=requests, battery_source=battery,
                    presence=presence, profile_source=lambda: holder["profiles"].info())
    hub.attention = attention                            # только для диагностики и тестов: шина работает через sink (ARC-7)
    io.hub = hub
    attention.hub, attention.modules = hub, modules
    # менеджер фокуса (ТЗ 18): решения внимания попадают в его очередь; модули без policies в манифесте в фокусе не участвуют
    # два эшелона (ТЗ 18): GLiNER (внимание) даёт подсказки, Gemma (LM Studio) только для неоднозначных случаев
    second_echelon = focus_dispatch.enabled()
    focus_mgr = FocusManager(
        connection_of=hub.connection, status_of=lambda mid: _module_status(modules, mid),
        policies_of=lambda mid: _module_policies(modules, mid),
        busy_of=lambda: attention.top_focus().kind in ("conversation", "question"),
        state_path=settings.memory_dir() / STATE_FILE, title_of=lambda mid: _module_title(modules, mid),
        llm=focus_dispatch.make_llm() if second_echelon else None,
        recheck=focus_dispatch.make_recheck() if second_echelon else None,
        # одно состояние «не беспокоить» для всего агента: настройки ядра dnd / quiet_until (дашборд, голос, фокус)
        dnd_all_of=settings.quiet_active, set_dnd_all=lambda on: settings.update(dnd=on, quiet_until=0.0),
        profile_quiet_of=lambda: holder["profiles"].current().focus_quiet if "profiles" in holder else (),
        modules_of=lambda: [mid for mid in modules.records if hub.is_connected(mid)],
        auto_switch=False, workspace_names=True)      # ТЗ 32: слот не выбирает среду, имена запросов workspace.*
    # среды агента (ТЗ 32): фокус как текущая среда; среду модуля объявляет его манифест и включает только импульс (сообщение
    # пользователя, самоимпульс) или критический сигнал; обмен с модулем (вход, выход, checkpoint) делает менеджер фокуса
    async def enter_module(ws: str, goal: str) -> dict:
        if not goal:                                     # вход без новой цели: вернуть приостановленную задачу, если она есть
            res = await focus_mgr.resume_module(ws)
            if res.get("ok"):
                return res
        return await focus_mgr.request(ws, goal=goal, source="workspace")

    workspaces = WorkspaceManager(
        activate=enter_module,
        release=lambda ws, reason, force: focus_mgr.release_holder(reason=reason, force=force),
        interruptible=lambda ws: bool((_module_status(modules, ws) or {}).get("interruptible", True)),
        connected=hub.is_connected, status_of=lambda ws: _module_status(modules, ws),
        is_background=focus_mgr.is_background,
        modules_of=lambda: [(rec.id, rec.manifest, rec.title) for rec in modules.records.values() if rec.manifest],
        on_change=lambda rec: holder["profiles"].poke(),           # профиль среды (тон, инструменты) следует за средой
        log=lambda kind, **data: events_log.log_nowait("workspace", f"{data.get('from')} -> {data.get('to')}", type=kind, **data))
    holder["workspaces"] = workspaces
    attention.on_decided = workspaces.on_event          # ТЗ 32, WS-6: события чужих сред это сигналы, а не претенденты на слот
    attention.policy_options = focus_mgr.policy_options         # подсказка политики для GLiNER (первый эшелон)
    presence.listener = hub.broadcast_presence
    speech = getattr(companion, "speech", None)
    if hasattr(speech, "attach"):
        speech.attach(hub, modules)                      # VOICE-15: речь идёт модулю voice, пока он подключён
    emotion_listener = fast_brain.add_listener(          # PET-9: каждая эмоция уходит в presence (стикер питомца)
        lambda emotion, ts: presence.update(emotion=emotion, emotion_ts=ts))
    attention.silent_kinds |= VOICE_SILENT_KINDS
    attention.kind_handlers.update(make_voice_handlers(companion, attention, speech))
    attention.kind_hooks["voice.heard"] = make_heard_hook(companion)
    attention.kind_hooks["desktop.activity"] = make_activity_hook(companion)
    attention.kind_hooks["rimworld.run_ended"] = make_run_lost_hook()

    async def live_subgraph(profile):
        """Подграф профиля от первого модуля фокуса с graph: true (запрос graph.get, CORE-22, RW-19); None, если такого нет."""
        for module_id in profile.focus_modules:
            rec = modules.get(module_id)
            conn = hub.connection(module_id)
            if rec is None or conn is None or not (rec.manifest or {}).get("graph") or not modules.is_enabled(module_id):
                continue
            return await conn.request("graph.get", {}, timeout=SUBGRAPH_TIMEOUT_S)
        return None

    game_order = make_game_order(modules, hub, lambda: profiles)

    def foreground():
        return desktop_state(modules).get("foreground_app")

    def on_profile_change(old: str, new: str):
        events_log.log("profile", f"{old} -> {new}", old=old, new=new)                # CORE-20
        hub.notify("profile", holder["profiles"].info())

    profiles = profile_manager.ProfileManager(battery=battery, foreground_app=foreground, module_state=modules.state_data,
                                              workspace_of=lambda: workspaces.current,
                                              on_change=on_profile_change, subgraph=live_subgraph)
    holder["profiles"] = profiles
    attention.profiles = profiles
    profiles.evaluate()
    registry.focus = profiles.focus_modules
    companion.profile_hint = profiles.hint
    companion.module_notes = modules.notes                # что умеют подключённые модули и в каком они состоянии
    companion.idle_seconds = lambda: float(desktop_state(modules).get("idle_s") or 0.0)     # простой (ТЗ 14, DESK-7)
    companion.screen_activity = lambda: desktop_state(modules).get("activity")              # активность экрана (DESK-7)
    companion.job_wait = modules.wait_job                                                  # итог задачи модуля (DESK-11)
    modules.context_for = lambda query: companion._context(include_hearing=False, query=query)   # core_fills: context (DESK-12)

    specs = supervisor_mod.load_specs() if specs is None else specs
    for spec in specs:
        modules.declare(spec.id, title=spec.title, default_enabled=spec.enabled, kind=spec.kind,
                        description=spec.description, dev=spec.dev)
    for planned in supervisor_mod.load_planned():
        modules.declare_planned(planned.id, title=planned.title, tz=planned.tz)

    async def send_shutdown(module_id: str) -> bool:
        conn = hub.connection(module_id)
        if conn is None:
            return False
        await conn.send("shutdown", {"reason": "модуль выключен или ядро останавливается"})
        return True

    async def announce_launch(module_id: str, text: str) -> None:
        """Запуск процесса модуля супервизором (правило 14): объявление идёт через очередь внимания, как у инструментов."""
        await attention.submit_announce(module_id, text, data={"source": "supervisor"})

    sup = supervisor_mod.Supervisor(specs, token=hub.token, core_url=f"ws://127.0.0.1:{port}/module/ws",
                                    enabled=modules.is_enabled, is_connected=hub.is_connected,
                                    send_shutdown=send_shutdown, memory_dir=settings.memory_dir(),
                                    announce=announce_launch,
                                    only_core=lambda: bool(settings.load().get("only_core")))
    modules.supervisor = sup
    builtin.register_core_tools(registry, modules, attention, focus_mgr,
                                switch=lambda cid: layer.character_switch.tool(cid),   # ТЗ 05, CHR-7
                                workspaces=workspaces)                                  # ТЗ 32: workspace.switch, workspace.status

    layer = SimpleNamespace(io=io, attention=attention, requests=requests, modules=modules, hub=hub, profiles=profiles,
                            supervisor=sup, presence=presence, game_order=game_order, tasks=[], focus=focus_mgr,
                            workspaces=workspaces)

    async def start():
        await hub.start()                                # токен в файл и фоновая сверка настроек
        await attention.start()                          # планировщик речи, сроки фокуса, чистка правил
        await focus_mgr.start()                          # очередь и переходы фокуса (ТЗ 18), состояние с диска
        layer.tasks.append(asyncio.get_running_loop().create_task(profiles.run()))
        layer.tasks.append(asyncio.get_running_loop().create_task(workspaces.run()))
        await sup.start()

    async def stop():
        fast_brain.remove_listener(emotion_listener)
        for task in layer.tasks:
            task.cancel()
        await focus_mgr.stop()
        await attention.stop()
        await sup.stop_all()                             # CORE-17: shutdown всем, через 10 с убить оставшихся
        await hub.stop()

    layer.start, layer.stop = start, stop
    return layer


async def _migrate_memory(companion, graph, folder: Path) -> None:
    """Перенос старой `memories.json` в граф и задачи (`memory_migration`); сбой переноса не мешает запуску."""
    try:
        done = await memory_migration.migrate(folder, graph, getattr(companion, "tasks", None), companion._owner_name())
        if done:
            events_log.log("memory", f"старая память перенесена: владелец {done['owner']}, "
                                     f"планы {done['plans']}, отброшено мелочи {done['dropped']}")
    except Exception as e:
        logger.warning(f"Перенос старой памяти: {e}")


def _module_status(modules, module_id: str) -> dict | None:
    """Статус модуля (ТЗ 18, FOC-1) из его последнего `state`: data.status; None, если модуль его не прислал."""
    rec = modules.get(module_id)
    data = (rec.state or {}).get("data") if rec is not None and rec.state else None
    status = (data or {}).get("status") if isinstance(data, dict) else None
    return status if isinstance(status, dict) else None


def _module_policies(modules, module_id: str) -> dict | None:
    """Политики модуля из манифеста; None - модуль без policies (режим совместимости: без фокуса)."""
    rec = modules.get(module_id)
    manifest = (rec.manifest if rec is not None else None) or {}
    if "policies" not in manifest:
        return None
    return {"default_policy": manifest.get("default_policy"), "policies": manifest.get("policies") or []}


def _module_title(modules, module_id: str) -> str:
    rec = modules.get(module_id)
    return rec.title if rec is not None and rec.title else module_id


def wire_affect(companion, layer, presence, speech, mem_root: Path):
    """Состояние эмоций персонажа (ТЗ 26): оценка событий, настроение в контексте, питомец, голос, пороги внимания."""
    import fast_brain as _fb
    import journal as _journal
    from app.affect_service import AffectService
    from domain import affect as dom
    char = character.active()
    affect = AffectService(char.memory_dir(mem_root), dom.Params.from_card(char.emotions),
                           on_change=lambda fields: presence.update(**fields))
    _fb.set_affect(affect)
    _journal.mistake_hook = affect.on_mistake
    companion.context_builder.mood_of = affect.mood_block
    layer.attention.ctx.threshold_offset = affect.proactivity_offset
    voice = getattr(speech, "voice", None)
    if voice is not None:
        voice.tone_of = affect.tone
    presence.update(**affect.presence_fields())
    return affect


async def _run(config: dict):
    # httpx пишет в журнал полный URL запроса (в запросах к API бывают токены): журнал не должен его хранить
    for noisy in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    mem_dir = config["memory_dir"]
    settings.init(mem_dir)
    character.init()                    # ТЗ 05: персонаж из настроек (его папка, имя, память)
    fast_brain.init(mem_dir)
    char_dir = character.memory_dir(character.active().id, Path(mem_dir))   # личная память активного персонажа (CHR-10)
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
    affect = wire_affect(companion, layer, presence, speech, Path(mem_dir))      # ТЗ 26: состояние эмоций
    companion.game_order = layer.game_order              # RW-11: приказ игроку-агенту (инструмент order модуля игры)
    dashboard.set_modules(layer.modules, layer.hub, layer.supervisor)
    dashboard.set_attention(layer.attention)
    dashboard.set_focus(layer.focus)                     # ТЗ 18: вкладка «Состояние», блок «Фокус»
    dashboard.set_workspace(layer.workspaces)            # ТЗ 32: карточка «Сейчас», среда агента
    dashboard.set_presence(presence)
    dashboard.set_affect(affect)
    loop = asyncio.get_running_loop()
    await dashboard.start_dashboard(
        memory_dir=mem_dir, chat_callback=lambda m: loop.create_task(layer.io.from_dashboard(m)))
    await layer.start()                  # токен модулей в файл, сверка настроек, профили, процессы модулей
    asyncio.create_task(fast_brain.warmup())

    # Окружения: встроенные плагины сразу, манифесты (MCP, вебхуки, RimWorld) подгружаются в фоне
    memory = companion.memory_reader                     # одна память на компаньона, инструменты и разговор
    layer.requests.privacy_terms = lambda: core_requests.collect_privacy_terms(
        companion._owner_name(), rel.people_terms(), [f["text"] for f in memory.facts()])      # core.privacy_terms (TG-12)
    graph = GraphMemory(mem_dir, busy=lambda: _time.time() - companion._last_user_ts < 20 or companion.is_speaking())
    companion.graph = graph
    graph.scopes_of = lambda: list(layer.profiles.current().memory_scopes)        # ТЗ 04, KB-12/13: области графа по профилю
    graph.scope_of = graph_scope.make_scope_of(
        layer.attention.decider, graph.scopes_of,
        lambda s: next((p.description or p.title for p in layer.profiles.profiles.values() if s in p.memory_scopes), s))
    dashboard.set_graph(graph)
    dashboard.set_tasks(lambda: getattr(companion, "tasks", None))
    layer.character_switch = CharacterSwitch(companion=companion, rel=rel, graph=graph, attention=layer.attention,
                                             hub=layer.hub, mem_root=Path(mem_dir), affect=affect)
    dashboard.set_character(layer.character_switch)
    graph.set_character(character.active().id)           # новые факты пишутся в группу активного персонажа
    asyncio.create_task(graph.run())
    try:
        companion.memory_writer.loop = loop             # планы из рабочих потоков ставятся задачами в цикл ядра
    except AttributeError:
        pass
    builtin.register(registry, companion, memory, profiles=layer.profiles)

    def recent_dialogue() -> str:
        lines = [f"{'Пользователь' if m['role'] == 'user' else 'Ассистентка'}: {m['text']}"
                 for m in memory.recent_dialogue(4)]
        return "Последние реплики:\n" + "\n".join(lines) if lines else ""

    companion.router = Router(registry, companion.say, recent_dialogue)
    layer.requests.router = companion.router            # core.tool.call от модулей идёт тем же путём, что просьбы
    dashboard.set_router(companion.router)              # POST /api/menu_click идёт инструментом desktop.menu_click (DESK-14)
    dashboard.set_registry(registry)
    dashboard.set_modes(layer.profiles)
    asyncio.create_task(registry.load_manifests(Path(__file__).parent / "environments" / "manifests"))
    companion.tasks = task_wiring.build(companion, layer.workspaces)    # ТЗ 32, WS-8: общие задачи, напоминания себе и планы
    await _migrate_memory(companion, graph, Path(companion._dir))      # старая memories.json -> граф и задачи (один раз)
    layer.workspaces.on_change = companion.tasks.on_workspace_change    # checkpoint при уходе из среды, возобновление при входе
    impulses = impulse_service.build(companion, layer.attention, router=lambda: companion.router,
                                     tasks=companion.tasks)             # ТЗ 30: импульсы и размышление
    await impulses.timer.start()                         # восстановить сроки напоминаний себе
    dashboard.set_impulses(impulses)
    episodes = episode_service.build(companion, layer.attention,       # ТЗ 27: эпизоды и сон (простой, расписание, догоняющий)
                                     busy=lambda: layer.attention.top_focus().kind in ("conversation", "question")
                                     or layer.focus.slot is not None)
    await episodes.start()
    episodes.scheduler.on_state = layer.workspaces.on_sleep_state      # ТЗ 32: сон это среда sleep
    episode_routes.set_service(episodes)
    if episodes.scheduler is not None:                    # сон это состояние ассистента: профиль sleep, пока идёт разбор памяти
        layer.profiles.sleeping = lambda: episodes.scheduler.state == "sleeping"
        episodes.scheduler.on_change = layer.profiles.poke
    layer.requests.episodes = episodes                    # core.memory.episodes
    await impulses.plans.start()                         # планы пользователя -> напоминания себе (без опроса модели, ТЗ 30)

    def free_to_reflect() -> bool:
        """Размышление в простое: разговора и вопросов нет, слот фокуса пуст, она не спит, «не отвлекать» выключено, пользователь
        молчит не меньше пяти минут."""
        since_user = _time.time() - float(getattr(companion, "_last_user_ts", 0.0) or 0.0)
        return (layer.attention.top_focus().kind not in ("conversation", "question") and layer.focus.slot is None
                and episodes.scheduler.state != "sleeping" and not settings.quiet_active() and since_user >= 300)
    impulse_service.attach_idle(impulses, companion, is_free=free_to_reflect).start()      # осмысление событий в свободное время

    # ── события игры (ТЗ 11, RW-9): список колонистов обновляет отношения и граф памяти, не озвучивается ──
    layer.attention.kind_handlers["rimworld.colonists"] = make_colonists_handler(rel, graph)
    logger.info("Ядро запущено: зрение, компаньон, быстрый слой, шина модулей. Игра и модули подключатся, когда появятся.")
    try:
        await asyncio.Event().wait()
    finally:
        affect.stop()
        await episodes.stop()
        await impulses.timer.stop()
        if impulses.idle is not None:
            impulses.idle.stop()
        await layer.stop()


def run(config: dict):
    asyncio.run(_run(config))
