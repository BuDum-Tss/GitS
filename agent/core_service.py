"""Ядро ГЛаДОС: всегда включённый сервис ассистента.

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

import claude_battery
import dashboard
import events_log
import fast_brain
import profile_manager
import settings
import speech_out
import supervisor as supervisor_mod
import tool_search
from attention import Attention
from companion import Companion
from conversation_io import ConversationIO
import core_requests
from core_requests import CoreRequests
from environments import builtin
from environments.registry import Registry
from module_hub import ModuleError, ModuleHub
from module_registry import ModuleRegistry
from graph_memory import GraphMemory
from relationships import Relationships
from router import Router
from user_memory import UserMemory

logger = logging.getLogger("core")

DASHBOARD_PORT = 8766
ORDER_TIMEOUT_S = 15           # RW-11: сколько ждём, пока модуль игры примет приказ
SUBGRAPH_TIMEOUT_S = 1.5       # CORE-22: подграф профиля от модуля для дашборда


def desktop_state(modules) -> dict:
    """state.data модуля desktop (ТЗ 14, DESK-7): активное приложение, простой, текущая активность экрана.
    Пусто, если модуль не подключён: тогда профиль по окну не выбирается, простоя нет, активности нет."""
    return modules.state_data("desktop") or {}


def make_activity_hook(companion):
    """desktop.activity (ТЗ 14, DESK-3): итог анализа экрана пишется в память занятий (activity.json); событие само
    идёт обычным путём внимания (urgency 1, как voice.heard)."""
    async def on_activity(item) -> None:
        data = item.data or {}
        mem = getattr(companion, "_mem", None)
        if mem is None or not data.get("app") or not data.get("summary"):
            return
        await asyncio.to_thread(mem.add_activity, str(data["app"]), str(data.get("category") or "other"), str(data["summary"]))
    return on_activity


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
    requests = CoreRequests(attention=attention)
    holder: dict = {}                                    # profiles появится ниже, а реестру и шине он нужен уже при создании
    modules = ModuleRegistry(registry, on_state=lambda: holder["profiles"].poke())
    hub = ModuleHub(modules, attention=attention, requests=requests, battery_source=battery, presence=presence,
                    profile_source=lambda: holder["profiles"].info())
    io.hub = hub
    attention.hub, attention.modules = hub, modules
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

    async def live_subgraph(profile):
        """Подграф профиля от первого модуля фокуса с graph: true (запрос graph.get, CORE-22, RW-19); None, если такого нет."""
        for module_id in profile.focus_modules:
            rec = modules.get(module_id)
            conn = hub.connection(module_id)
            if rec is None or conn is None or not (rec.manifest or {}).get("graph") or not modules.is_enabled(module_id):
                continue
            return await conn.request("graph.get", {}, timeout=SUBGRAPH_TIMEOUT_S)
        return None

    async def game_order(text: str) -> bool:
        """RW-11: приказ игроку-агенту. Каждому модулю из focus_modules активного профиля, у которого есть capabilities
        ["game"] и инструмент order, отправляется tool.call order (тайм-аут 15 с); accepted: true - обращение обработано игрой."""
        for module_id in profiles.current().focus_modules:
            rec = modules.get(module_id)
            manifest = (rec.manifest if rec is not None else None) or {}
            if "game" not in (manifest.get("capabilities") or []) or not modules.is_enabled(module_id):
                continue
            if not any(isinstance(t, dict) and t.get("name") == "order" for t in manifest.get("tools") or []):
                continue
            conn = hub.connection(module_id)
            if conn is None:
                continue
            try:
                value = await conn.request("tool.call", {"tool": "order", "args": {"text": text}}, timeout=ORDER_TIMEOUT_S)
            except ModuleError as e:
                logger.info(f"Приказ модулю «{module_id}» не принят: {e.message}")
                continue
            if isinstance(value, dict) and value.get("accepted"):
                return True
        return False

    def foreground():
        return desktop_state(modules).get("foreground_app")

    def on_profile_change(old: str, new: str):
        events_log.log("profile", f"{old} -> {new}", old=old, new=new)                # CORE-20
        hub.notify("profile", holder["profiles"].info())

    profiles = profile_manager.ProfileManager(battery=battery, foreground_app=foreground, module_state=modules.state_data,
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
                                    announce=announce_launch)
    modules.supervisor = sup
    builtin.register_core_tools(registry, modules, attention)

    layer = SimpleNamespace(io=io, attention=attention, requests=requests, modules=modules, hub=hub, profiles=profiles,
                            supervisor=sup, presence=presence, game_order=game_order, tasks=[])

    async def start():
        await hub.start()                                # токен в файл и фоновая сверка настроек
        await attention.start()                          # планировщик речи, сроки фокуса, чистка правил
        layer.tasks.append(asyncio.get_running_loop().create_task(profiles.run()))
        await sup.start()

    async def stop():
        fast_brain.remove_listener(emotion_listener)
        for task in layer.tasks:
            task.cancel()
        await attention.stop()
        await sup.stop_all()                             # CORE-17: shutdown всем, через 10 с убить оставшихся
        await hub.stop()

    layer.start, layer.stop = start, stop
    return layer


VOICE_SILENT_KINDS = {"voice.speech", "voice.user_speaking", "voice.utterance"}      # частые и служебные: в module_event не пишутся


def make_voice_handlers(companion, attention, speech) -> dict:
    """События модуля голоса, которые ядро обрабатывает само, мимо оценки внимания (ТЗ 12): voice.utterance - реплика
    пользователя в разговор с каналом voice (VOICE-6); voice.speech и voice.user_speaking - присутствие (VOICE-17, VOICE-8);
    voice.named - голосу назначено имя, человек заводится в памяти и отношениях (раньше это делал инструмент name_voice)."""

    async def on_utterance(item) -> None:
        speaker = (item.data or {}).get("speaker")
        if not isinstance(speaker, dict):
            sender = item.sender or {}
            speaker = {"id": str(sender.get("id", "")).removeprefix("voice:") or None, "name": sender.get("name"),
                       "new": False, "owner": bool(sender.get("is_owner")), "has_owner": bool(sender.get("is_owner"))}
        await companion.on_voice(item.text, speaker)

    async def on_speech(item) -> None:
        if hasattr(speech, "on_event"):
            speech.on_event(item.data or {})

    async def on_user_speaking(item) -> None:
        attention.set_user_speaking(bool((item.data or {}).get("speaking")))

    async def on_named(item) -> None:
        data = item.data or {}
        name, voice_id = str(data.get("name") or "").strip(), data.get("voice_id")
        if not name or not voice_id:
            return
        if data.get("is_owner"):
            mem = getattr(companion, "_mem", None)
            if mem is not None:
                await asyncio.to_thread(mem.add_fact, f"Имя: {name}", kind="user", importance=5)
        else:
            rel = getattr(companion, "_rel", None)
            if rel is not None:
                await asyncio.to_thread(rel.ensure_person, f"voice:{voice_id}", name, source="voice")

    return {"voice.utterance": on_utterance, "voice.speech": on_speech, "voice.user_speaking": on_user_speaking,
            "voice.named": on_named}


def make_heard_hook(companion):
    """VOICE-14: сводка услышанного в колонках дополнительно пишется в память занятий и в контекст разговора, а сам факт
    «В колонках: ...» идёт обычным путём внимания (voice.heard, срочность 1: чаще всего в сводку)."""
    async def on_heard(item) -> None:
        summary = str((item.data or {}).get("summary") or item.text.removeprefix("В колонках:")).strip()
        if not summary:
            return
        if hasattr(companion, "note_heard"):
            companion.note_heard(summary)
        mem = getattr(companion, "_mem", None)
        if mem is not None:
            await asyncio.to_thread(mem.add_activity, "звук", "audio", summary)
    return on_heard


def make_colonists_handler(rel, graph):
    """Обработчик события rimworld.colonists (RW-9): `data` = {"pawns": [...], "colony": "имя"}. Раньше это делал канал
    /api/event агента игры (link_event)."""
    async def on_colonists(item) -> None:
        data = item.data or {}
        pawns, colony = data.get("pawns") or [], data.get("colony") or "колония"
        rel.sync_colonists(pawns)
        await graph.sync_colonists(pawns, colony)
    return on_colonists


async def _run(config: dict):
    # httpx пишет в журнал полный URL запроса (в запросах к API бывают токены): журнал не должен его хранить
    for noisy in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    mem_dir = config["memory_dir"]
    settings.init(mem_dir)
    fast_brain.init(mem_dir)
    rel = Relationships(mem_dir)
    presence = speech_out.PresenceTracker()
    # речь: модуль voice, пока он подключён и включён (VOICE-15), иначе только текст в чате и в presence
    speech = speech_out.SpeechSelector(speech_out.VoiceModuleSpeech(presence), speech_out.TextOnlySpeech(presence))
    companion = Companion(mem_dir, rel, speech=speech, presence=presence)

    # профили: активное окно и простой приходят состоянием модуля desktop (ТЗ 14), игра RimWorld (game_running) приходит
    # состоянием модуля rimworld (ТЗ 11). Без модуля desktop данных нет, профиль по окну не выбирается
    registry = Registry()
    registry.embedder = tool_search.ToolEmbeddings(Path(mem_dir) / "tool_embeddings.json")     # отбор инструментов по смыслу
    layer = wire_modules(companion, registry)
    companion.game_order = layer.game_order              # RW-11: приказ игроку-агенту (инструмент order модуля игры)
    dashboard.set_modules(layer.modules, layer.hub, layer.supervisor)
    dashboard.set_attention(layer.attention)
    dashboard.set_presence(presence)
    loop = asyncio.get_running_loop()
    await dashboard.start_dashboard(
        memory_dir=mem_dir, chat_callback=lambda m: loop.create_task(layer.io.from_dashboard(m)))
    await layer.start()                  # токен модулей в файл, сверка настроек, профили, процессы модулей
    asyncio.create_task(fast_brain.warmup())

    # Окружения: встроенные плагины сразу, манифесты (MCP, вебхуки, RimWorld) подгружаются в фоне
    memory = UserMemory(mem_dir)
    layer.requests.privacy_terms = lambda: core_requests.collect_privacy_terms(
        companion._owner_name(), rel.people_terms(), [f["text"] for f in memory.facts()])      # core.privacy_terms (TG-12)
    graph = GraphMemory(mem_dir, busy=lambda: _time.time() - companion._last_user_ts < 20 or companion.is_speaking())
    companion.graph = graph
    dashboard.set_graph(graph)
    asyncio.create_task(graph.run())
    migrated = graph.migrate_from_store(UserMemory(mem_dir).store, companion._owner_name())
    if migrated:
        logger.info(f"Граф: в очередь на разбор поставлено {migrated} старых фактов о пользователе")
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
    asyncio.create_task(companion.proactive_loop())

    # ── события игры (ТЗ 11, RW-9): список колонистов обновляет отношения и граф памяти, не озвучивается ──
    layer.attention.kind_handlers["rimworld.colonists"] = make_colonists_handler(rel, graph)
    logger.info("Ядро запущено: зрение, компаньон, быстрый слой, шина модулей. Игра и модули подключатся, когда появятся.")
    try:
        await asyncio.Event().wait()
    finally:
        await layer.stop()


def run(config: dict):
    asyncio.run(_run(config))
