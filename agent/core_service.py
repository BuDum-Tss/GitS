"""Ядро ГЛаДОС: всегда включённый сервис ассистента.

Что работает всегда:
  - зрение (perception): следит за активным окном, локально, OCR + Gemma;
  - окружения (environments/): реестр инструментов, маршрутизатор «могу или нет», манифесты;
  - слух (hearing) и микрофон: речь в текст, узнавание голосов, локально;
  - компаньон (companion): разговор, память, проактивность, граф памяти;
  - Telegram: канал-дневник и её собственный аккаунт;
  - дашборд (порт 8766, только localhost): состояние для питомца, чат, настройки.

Игры (RimWorld и другие) подключаются отдельными процессами: приказы идут через game_bridge, а агент игры говорит
через /api/say дашборда.
"""
import asyncio
import logging
import time as _time
from pathlib import Path

import dashboard
import fast_brain
import settings
from companion import Companion
from environments import builtin
from environments.registry import Registry
from graph_memory import GraphMemory
from hearing import Hearing
from mic_listener import MicListener
from perception import ScreenWatcher
from relationships import Relationships
from router import Router
from stt_client import STTClient
from telegram_channel import TelegramChannel
from telegram_user import TelegramUser
from user_memory import UserMemory
from voice_id import VoiceIdentity

import game_bridge

logger = logging.getLogger("core")

_LINKED_SETTINGS = ("streamer", "dnd", "quiet_until", "game_model", "claude_usage", "voice_muted", "telegram_after_run")


async def _run(config: dict):
    # httpx пишет в журнал полный URL запроса, а в нём токен Telegram-бота: журнал не должен его хранить
    for noisy in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    mem_dir = config["memory_dir"]
    settings.init(mem_dir)
    fast_brain.init(mem_dir)
    rel = Relationships(mem_dir)
    watcher = ScreenWatcher(UserMemory(mem_dir))
    hearing = Hearing(UserMemory(mem_dir))
    companion = Companion(mem_dir, rel, watcher, hearing)
    companion.game_order = game_bridge.order

    loop = asyncio.get_running_loop()
    asyncio.create_task(dashboard.start_dashboard(
        memory_dir=mem_dir, chat_callback=lambda m: loop.create_task(companion.on_user_message(m))))
    asyncio.create_task(fast_brain.warmup())
    asyncio.create_task(watcher.run())
    asyncio.create_task(hearing.run())

    telegram = TelegramChannel(mem_dir, companion.say, lambda: [f["text"] for f in UserMemory(mem_dir).facts()])
    companion.telegram = telegram
    telegram.rel = rel
    _um = UserMemory(mem_dir)

    def _contact(name, text, new):
        _um.add_fact(f"В Telegram-бот написал(а) {name}: «{text[:100]}»", kind="episode", importance=3)
        companion.graph.enqueue(name, text)
    telegram.on_contact = _contact
    asyncio.create_task(telegram.run())

    # Окружения: встроенные плагины сразу, манифесты (MCP, вебхуки, RimWorld) подгружаются в фоне
    memory = UserMemory(mem_dir)
    registry = Registry()
    tg_user = TelegramUser(mem_dir, lambda: [f["text"] for f in UserMemory(mem_dir).facts()])
    companion.tg_user = tg_user
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

    mic = MicListener(STTClient(), voices, companion.on_voice, companion.is_speaking, own_voice_sample)
    asyncio.create_task(mic.run())
    asyncio.create_task(tg_user.run())
    builtin.register(registry, companion, hearing, telegram, memory, tg_user)

    def recent_dialogue() -> str:
        lines = [f"{'Пользователь' if m['role'] == 'user' else 'Ассистентка'}: {m['text']}"
                 for m in memory.recent_dialogue(4)]
        return "Последние реплики:\n" + "\n".join(lines) if lines else ""

    companion.router = Router(registry, companion.say, recent_dialogue)
    dashboard.set_registry(registry)
    asyncio.create_task(registry.load_manifests(Path(__file__).parent / "environments" / "manifests"))
    asyncio.create_task(companion.proactive_loop())

    # ── связь с агентами игр ──
    def link_status() -> dict:
        recent = [m["text"] for m in memory.recent_dialogue(10) if m["role"] != "user"][-6:]
        cfg = settings.load()
        return {"user_ts": companion._last_user_ts, "speaking": companion.is_speaking(), "recent": recent,
                "settings": {k: cfg.get(k) for k in _LINKED_SETTINGS}}

    async def link_say(text: str, emotion=None):
        await companion.say(text, emotion)

    async def link_event(kind: str, data: dict):
        if kind == "colonists":
            pawns, colony = data.get("pawns") or [], data.get("colony") or "колония"
            rel.sync_colonists(pawns)
            await graph.sync_colonists(pawns, colony)
        elif kind == "run_ended" and settings.load().get("telegram_after_run"):
            await telegram.post_on_request(str(data.get("prompt") or "Забег закончен. Напиши короткий пост-итог."))

    dashboard.set_game_link(say=link_say, status=link_status, event=link_event)
    logger.info("Ядро запущено: зрение, компаньон, быстрый слой. Игра подключится отдельным агентом, когда появится.")
    await asyncio.Event().wait()


def run(config: dict):
    asyncio.run(_run(config))
