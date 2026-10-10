"""Ядро ГЛаДОС: всегда включённый сервис ассистента.

Что работает всегда:
  - зрение (perception): следит за активным окном, локально, OCR + Gemma;
  - окружения (environments/): реестр инструментов, маршрутизатор «могу или нет», манифесты;
  - слух (hearing) и микрофон: речь в текст, узнавание голосов, локально;
  - компаньон (companion): разговор, память, проактивность, граф памяти;
  - дашборд (порт 8766, только localhost): состояние для питомца, чат, настройки;
  - шина модулей (ТЗ 02): модули подключаются сами по WebSocket (/module/ws в том же приложении), супервизор запускает и
    перезапускает их процессы из agent/modules.yaml, профили в agent/profiles/ решают, какие инструменты видны. Telegram
    (канал, бот, её аккаунт) это модуль modules/telegram (ТЗ 10): в процессе ядра его кода нет.

Агент RimWorld до ТЗ 11 подключается по-старому: приказы идут через game_bridge, а он сам говорит через /api/say дашборда.
"""
import asyncio
import logging
import time as _time
from pathlib import Path
from types import SimpleNamespace

import claude_battery
import dashboard
import events_log
import gamenet
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
from module_hub import ModuleHub
from module_registry import ModuleRegistry
from graph_memory import GraphMemory
from hearing import Hearing
from mic_listener import MicListener
import perception
from perception import ScreenWatcher
from relationships import Relationships
from router import Router
from stt_client import STTClient
from user_memory import UserMemory
from voice_id import VoiceIdentity

import game_bridge

logger = logging.getLogger("core")

DASHBOARD_PORT = 8766
# настройки для старого агента игры (GET /api/speech_status); новые модули получают shared_settings по манифесту (CORE-41)
_LINKED_SETTINGS = ("streamer", "dnd", "quiet_until", "game_model", "claude_usage", "voice_muted", "telegram_after_run")


def wire_modules(companion, registry, *, foreground_app=lambda: None, legacy_module_state=lambda module_id: None,
                 battery=claude_battery.status, specs=None, port: int = DASHBOARD_PORT) -> SimpleNamespace:
    """Слой модулей (ТЗ 02): каналы разговора, заглушка внимания, реестр и шина модулей, профили, супервизор.

    companion - Companion (или заменитель с теми же методами), registry - реестр окружений планировщика.
    foreground_app() - имя процесса активного окна (до ТЗ 14, когда это знает модуль desktop); legacy_module_state(id) -
    state.data для того, что ещё не стало модулем (игра RimWorld определяется по порту мода до ТЗ 11).
    Ничего не запускает: старт и остановка - layer.start() и layer.stop()."""
    presence = companion.presence
    io = ConversationIO(companion)
    companion.io = io
    attention = Attention(companion, io)
    companion.attention = attention                      # речь идёт через очередь внимания (ТЗ 03, ATT-13)
    requests = CoreRequests(attention=attention)
    holder: dict = {}                                    # profiles появится ниже, а реестру и шине он нужен уже при создании
    modules = ModuleRegistry(registry, on_state=lambda: holder["profiles"].poke())
    hub = ModuleHub(modules, attention=attention, requests=requests, battery_source=battery, presence=presence,
                    profile_source=lambda: holder["profiles"].info())
    io.hub = hub
    attention.hub, attention.modules = hub, modules
    presence.listener = hub.broadcast_presence

    def module_state(module_id: str):
        data = modules.state_data(module_id)
        return data if data is not None else legacy_module_state(module_id)

    def foreground():
        data = modules.state_data("desktop") or {}
        return data.get("foreground_app") or foreground_app()

    def on_profile_change(old: str, new: str):
        events_log.log("profile", f"{old} -> {new}", old=old, new=new)                # CORE-20
        hub.notify("profile", holder["profiles"].info())

    profiles = profile_manager.ProfileManager(battery=battery, foreground_app=foreground, module_state=module_state,
                                              on_change=on_profile_change)
    holder["profiles"] = profiles
    attention.profiles = profiles
    profiles.evaluate()
    registry.focus = profiles.focus_modules
    companion.profile_hint = profiles.hint
    companion.module_notes = modules.notes                # что умеют подключённые модули и в каком они состоянии

    specs = supervisor_mod.load_specs() if specs is None else specs
    for spec in specs:
        modules.declare(spec.id, title=spec.title, default_enabled=spec.enabled, kind=spec.kind)

    async def send_shutdown(module_id: str) -> bool:
        conn = hub.connection(module_id)
        if conn is None:
            return False
        await conn.send("shutdown", {"reason": "модуль выключен или ядро останавливается"})
        return True

    sup = supervisor_mod.Supervisor(specs, token=hub.token, core_url=f"ws://127.0.0.1:{port}/module/ws",
                                    enabled=modules.is_enabled, is_connected=hub.is_connected,
                                    send_shutdown=send_shutdown, memory_dir=settings.memory_dir())
    modules.supervisor = sup
    builtin.register_core_tools(registry, modules, attention)

    layer = SimpleNamespace(io=io, attention=attention, requests=requests, modules=modules, hub=hub, profiles=profiles,
                            supervisor=sup, presence=presence, tasks=[])

    async def start():
        await hub.start()                                # токен в файл и фоновая сверка настроек
        await attention.start()                          # планировщик речи, сроки фокуса, чистка правил
        layer.tasks.append(asyncio.get_running_loop().create_task(profiles.run()))
        await sup.start()

    async def stop():
        for task in layer.tasks:
            task.cancel()
        await attention.stop()
        await sup.stop_all()                             # CORE-17: shutdown всем, через 10 с убить оставшихся
        await hub.stop()

    layer.start, layer.stop = start, stop
    return layer


async def post_run_summary(router, prompt: str) -> bool:
    """TG-15: пост-итог после забега. Публикует инструмент telegram.post модуля Telegram (через маршрутизатор: обычная проверка
    параметров и доступности); тема берётся из data.prompt события run_ended. До ТЗ 11 событие приходит старым каналом."""
    ok, text = await router.run_tool("telegram.post", {"topic": prompt})
    if not ok:
        events_log.log_nowait("telegram", f"пост-итог после забега не опубликован: {text}")
    return ok


async def _run(config: dict):
    # httpx пишет в журнал полный URL запроса (в запросах к API бывают токены): журнал не должен его хранить
    for noisy in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    mem_dir = config["memory_dir"]
    settings.init(mem_dir)
    fast_brain.init(mem_dir)
    rel = Relationships(mem_dir)
    watcher = ScreenWatcher(UserMemory(mem_dir))
    hearing = Hearing(UserMemory(mem_dir))
    presence = speech_out.PresenceTracker()
    companion = Companion(mem_dir, rel, watcher, hearing, speech=speech_out.LegacyPetSpeech(mem_dir, presence),
                          presence=presence)
    companion.game_order = game_bridge.order

    # профили: активное окно и состояние игры берутся из кэша, который обновляет фоновый опрос (без блокировки цикла)
    sample = {"app": None, "game_running": False}

    async def sampler():
        while True:
            try:
                fg = await asyncio.to_thread(perception.foreground)
                sample["app"] = (fg or {}).get("app")
                sample["game_running"] = await asyncio.to_thread(gamenet.port_open, gamenet.PORT)
            except Exception as e:
                logger.debug(f"Опрос окна и игры: {e}")
            await asyncio.sleep(2)

    def legacy_module_state(module_id: str):
        if module_id == "rimworld":      # устаревший канал агента игры: удалить в ТЗ 11 (до этого игра - порт мода)
            return {"game_running": sample["game_running"]}
        return None

    registry = Registry()
    registry.embedder = tool_search.ToolEmbeddings(Path(mem_dir) / "tool_embeddings.json")     # отбор инструментов по смыслу
    layer = wire_modules(companion, registry, foreground_app=lambda: sample["app"], legacy_module_state=legacy_module_state)
    dashboard.set_modules(layer.modules, layer.hub, layer.supervisor)
    dashboard.set_attention(layer.attention)
    dashboard.set_presence(presence)
    loop = asyncio.get_running_loop()
    await dashboard.start_dashboard(
        memory_dir=mem_dir, chat_callback=lambda m: loop.create_task(layer.io.from_dashboard(m)))
    await layer.start()                  # токен модулей в файл, сверка настроек, профили, процессы модулей
    asyncio.create_task(sampler())
    asyncio.create_task(fast_brain.warmup())
    asyncio.create_task(watcher.run())
    asyncio.create_task(hearing.run())

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
    voices = VoiceIdentity(mem_dir)
    companion.voice_id = voices

    async def own_voice_sample():
        """Образец её голоса для записи в voice_id: синтез и перевод в PCM 16 кГц."""
        import asyncio as _a
        import os as _os
        import opus_encoder
        import tts_engine
        path = await _a.to_thread(tts_engine.synth_file, "Проверка голоса. Меня зовут ГЛаДОС. Это тест.")
        if not path:
            return b""
        try:
            return await opus_encoder.audio_file_to_pcm16k(path)
        finally:
            _os.unlink(path)

    mic = MicListener(STTClient(), voices, companion.on_voice, companion.is_speaking, own_voice_sample,
                      on_speaking=layer.attention.set_user_speaking)
    asyncio.create_task(mic.run())

    builtin.register(registry, companion, hearing, memory, profiles=layer.profiles)

    def recent_dialogue() -> str:
        lines = [f"{'Пользователь' if m['role'] == 'user' else 'Ассистентка'}: {m['text']}"
                 for m in memory.recent_dialogue(4)]
        return "Последние реплики:\n" + "\n".join(lines) if lines else ""

    companion.router = Router(registry, companion.say, recent_dialogue)
    layer.requests.router = companion.router            # core.tool.call от модулей идёт тем же путём, что просьбы
    dashboard.set_router(companion.router)
    dashboard.set_registry(registry)
    dashboard.set_modes(layer.profiles)
    asyncio.create_task(registry.load_manifests(Path(__file__).parent / "environments" / "manifests"))
    asyncio.create_task(companion.proactive_loop())

    # ── связь с агентами игр ──
    def link_status() -> dict:
        recent = [m["text"] for m in memory.recent_dialogue(10) if m["role"] != "user"][-6:]
        cfg = settings.load()
        return {"user_ts": companion._last_user_ts, "speaking": companion.is_speaking(), "recent": recent,
                "settings": {k: cfg.get(k) for k in _LINKED_SETTINGS}}

    async def link_say(text: str, emotion=None):
        # устаревший канал агента игры: удалить в ТЗ 11 (дальше игра говорит как модуль: say verbatim)
        await layer.attention.submit_speech("rimworld", {"text": text, "mode": "verbatim", "emotion": emotion})

    async def link_event(kind: str, data: dict):
        if kind == "colonists":
            pawns, colony = data.get("pawns") or [], data.get("colony") or "колония"
            rel.sync_colonists(pawns)
            await graph.sync_colonists(pawns, colony)
        elif kind == "run_ended" and settings.load().get("telegram_after_run"):
            # TG-15: пост-итог делает модуль Telegram (до ТЗ 11 событие приходит старым каналом); в фоне, пост может ждать ответа
            task = asyncio.create_task(post_run_summary(
                companion.router, str(data.get("prompt") or "Забег закончен. Напиши короткий пост-итог.")))
            background.add(task)
            task.add_done_callback(background.discard)

    background: set[asyncio.Task] = set()

    dashboard.set_game_link(say=link_say, status=link_status, event=link_event)
    logger.info("Ядро запущено: зрение, компаньон, быстрый слой, шина модулей. Игра и модули подключатся, когда появятся.")
    try:
        await asyncio.Event().wait()
    finally:
        await layer.stop()


def run(config: dict):
    asyncio.run(_run(config))
