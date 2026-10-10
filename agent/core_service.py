"""Ядро ГЛаДОС: всегда включённый сервис ассистента.

Что работает всегда:
  - зрение (perception): следит за активным окном, локально, OCR + Gemma;
  - окружения (environments/): реестр инструментов, маршрутизатор «могу или нет», манифесты;
  - слух (hearing) и микрофон: речь в текст, узнавание голосов, локально;
  - компаньон (companion): разговор, память, проактивность, граф памяти;
  - дашборд (порт 8766, только localhost): состояние для питомца, чат, настройки;
  - шина модулей (ТЗ 02): модули подключаются сами по WebSocket (/module/ws в том же приложении), супервизор запускает и
    перезапускает их процессы из agent/modules.yaml, профили в agent/profiles/ решают, какие инструменты видны. Telegram
    (канал, бот, её аккаунт) это модуль modules/telegram (ТЗ 10): в процессе ядра его кода нет. Игра RimWorld это модуль rimworld
    из отдельного репозитория rimworld-module (ТЗ 11): приказы идут его инструментом order, состояние и события приходят по шине.
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
from hearing import Hearing
from mic_listener import MicListener
import perception
from perception import ScreenWatcher
from relationships import Relationships
from router import Router
from stt_client import STTClient
from user_memory import UserMemory
from voice_id import VoiceIdentity

logger = logging.getLogger("core")

DASHBOARD_PORT = 8766
ORDER_TIMEOUT_S = 15           # RW-11: сколько ждём, пока модуль игры примет приказ
SUBGRAPH_TIMEOUT_S = 1.5       # CORE-22: подграф профиля от модуля для дашборда


def wire_modules(companion, registry, *, foreground_app=lambda: None, battery=claude_battery.status, specs=None,
                 port: int = DASHBOARD_PORT) -> SimpleNamespace:
    """Слой модулей (ТЗ 02): каналы разговора, заглушка внимания, реестр и шина модулей, профили, супервизор.

    companion - Companion (или заменитель с теми же методами), registry - реестр окружений планировщика.
    foreground_app() - имя процесса активного окна (до ТЗ 14, когда это знает модуль desktop).
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
        data = modules.state_data("desktop") or {}
        return data.get("foreground_app") or foreground_app()

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
                            supervisor=sup, presence=presence, game_order=game_order, tasks=[])

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
    watcher = ScreenWatcher(UserMemory(mem_dir))
    hearing = Hearing(UserMemory(mem_dir))
    presence = speech_out.PresenceTracker()
    companion = Companion(mem_dir, rel, watcher, hearing, speech=speech_out.LegacyPetSpeech(mem_dir, presence),
                          presence=presence)

    # профили: активное окно берётся из кэша, который обновляет фоновый опрос (без блокировки цикла); игра RimWorld
    # (game_running) приходит состоянием модуля rimworld, порт мода ядро больше не опрашивает (ТЗ 11)
    sample = {"app": None}

    async def sampler():
        while True:
            try:
                fg = await asyncio.to_thread(perception.foreground)
                sample["app"] = (fg or {}).get("app")
            except Exception as e:
                logger.debug(f"Опрос окна: {e}")
            await asyncio.sleep(2)

    registry = Registry()
    registry.embedder = tool_search.ToolEmbeddings(Path(mem_dir) / "tool_embeddings.json")     # отбор инструментов по смыслу
    layer = wire_modules(companion, registry, foreground_app=lambda: sample["app"])
    companion.game_order = layer.game_order              # RW-11: приказ игроку-агенту (инструмент order модуля игры)
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
