"""Подключение движка голоса к ядру: сборка из настроек, маршруты HTTP-сервера, инструменты, остановка.

Тяжёлые части (numpy, aiohttp-клиенты, VAD) импортируются, только когда движок включён: выключенный движок не добавляет ядру
ни зависимостей, ни времени запуска.

Ядро зовёт `VoiceService.build(...)` один раз при старте. Без адресов распознавания и синтеза (или с `GITS_VOICE_ENGINE=0`) движок
не создаётся: `/voice/ws` не регистрируется, инструментов нет, в `/api/voice` написано, почему он выключен.
"""
import logging
from typing import Any, Callable, Iterable, Mapping

from aiohttp import web

from .config import EngineConfig

logger = logging.getLogger("voice_engine")

STATE_PATH = "/api/voice"
PRIVACY_CACHE_S = 10.0                  # личные слова перечитываются не чаще раза в 10 с


class VoiceService:
    """Движок и его вход в ядре (или выключенная заглушка с причиной)."""

    def __init__(self, config: EngineConfig, engine: Any = None, server: Any = None):
        self.config, self.engine, self.server = config, engine, server

    @property
    def enabled(self) -> bool:
        return self.engine is not None

    @classmethod
    def build(cls, environ: Mapping[str, str], *, submit: Callable[[str, dict], Any], token: Callable[[], str],
              allow: Callable[[str | None], bool], names: Callable[[], Iterable[str]],
              journal: Callable[[str, str, dict], None],
              ref_resolver: Callable[[], Any] | None = None, leaks: Callable[[str], Any] | None = None,
              memory_dir: Any = None, emit_spoken: bool = False) -> "VoiceService":
        """`submit(peer_id, event)` это `attention.submit`, `token` и `allow` берутся у шины модулей, `journal(kind, text, data)`
        это запись в журнал событий ядра, `ref_resolver()` отдаёт (WAV, текст) референса голоса активного персонажа; `leaks(text)` (можно async) говорит, есть ли в
        тексте личные слова, которые нельзя отправлять в облако; `memory_dir` это папка памяти ядра (суточный счётчик облака); `emit_spoken` включает событие итога речи `voicechat.spoken` (внимание v2)."""
        config = EngineConfig.from_env(environ)
        if not config.enabled:
            logger.info("Движок голоса выключен: %s", config.disabled_reason)
            return cls(config)
        from . import events
        from .engine import VoiceEngine
        from .server import VoiceServer
        from .stt import HttpSttClient
        from .tts import HttpTtsClient, TtsSettings
        from .vad import load_silero_factory

        async def emit(event: dict) -> None:
            await submit(events.PEER_ID, event)

        stt = HttpSttClient(config.stt_url, timeout_s=config.stt_timeout_s, lang=config.lang)
        local_speed = min(1.5, max(0.5, config.tts_speed * config.tts_tempo))      # «чуть быстрее»: ESpeech берёт темп в speed
        local = HttpTtsClient(TtsSettings(url=config.tts_url, ref_audio=config.tts_ref_audio, ref_text=config.tts_ref_text,
                                          speed=local_speed, voice=config.tts_voice, emotion=config.tts_emotion,
                                          timeout_s=config.tts_timeout_s, ref_resolver=ref_resolver))
        tts = _with_cloud(local, config, environ, ref_resolver, leaks, memory_dir)
        vad_factory = load_silero_factory(config.silero_model, config.silero_threshold) if config.vad == "silero" else None
        engine = VoiceEngine(stt=stt, tts=tts, config=config, emit_event=emit, journal=journal, names=names,
                             vad_factory=vad_factory, emit_spoken=emit_spoken)
        logger.info("Движок голоса включён: распознавание %s, синтез %s", config.stt_url, config.tts_url)
        return cls(config, engine, VoiceServer(engine, token, allow))

    def attach(self, app: web.Application) -> None:
        """Маршруты в приложение дашборда: `/api/voice` всегда, `/voice/ws` только у включённого движка."""
        async def state_handler(request: web.Request) -> web.Response:
            return web.json_response(self.state())

        app.router.add_get(STATE_PATH, state_handler)
        if self.server is not None:
            self.server.attach(app)

    def register_tools(self, registry) -> None:
        if self.engine is not None:
            from .tools import register_voice_tools
            register_voice_tools(registry, self.engine)

    def state(self) -> dict:
        if self.engine is None:
            return {"enabled": False, "reason": self.config.disabled_reason}
        snap = self.engine.snapshot()
        if self.server is not None:
            snap["connections"], snap["rejected"] = self.server.connections, self.server.rejected
        return snap

    async def stop(self) -> None:
        if self.engine is not None:
            await self.engine.aclose()


def _with_cloud(local, config: EngineConfig, environ: Mapping[str, str], ref_resolver, leaks, memory_dir):
    """Облако (OpenRouter) поверх локального синтеза или сам локальный: без ключа и при `GITS_TTS_PROVIDER=local` никаких облачных запросов."""
    if config.tts_provider == "local":
        return local
    key = str(environ.get("OPENROUTER_API_KEY", "") or "").strip()
    if not key:
        if config.tts_provider == "openrouter":
            logger.warning("GITS_TTS_PROVIDER=openrouter, но OPENROUTER_API_KEY не задан: синтез только локальный")
        return local
    from pathlib import Path

    from .cloud_tts import USAGE_FILE, CloudBudget, FallbackTtsClient, OpenRouterTtsClient
    from .tts import TtsSettings

    cloud = OpenRouterTtsClient(key, TtsSettings(ref_audio=config.tts_ref_audio, ref_text=config.tts_ref_text,
                                                 ref_resolver=ref_resolver),
                                model=config.openrouter_model, tempo=config.tts_tempo,
                                connect_timeout_s=config.cloud_connect_timeout_s, timeout_s=config.cloud_timeout_s)
    budget = CloudBudget(Path(memory_dir) / USAGE_FILE if memory_dir else None, config.cloud_daily_chars, config.cloud_usd_per_char)
    logger.info("Облачный синтез включён: модель %s, лимит %d символов в сутки (локальный запасной)", config.openrouter_model,
                config.cloud_daily_chars)
    return FallbackTtsClient(cloud, local, budget=budget, leaks=leaks, provider=config.tts_provider, model=config.openrouter_model)


def wire_voice(layer, registry, environ: Mapping[str, str] | None = None) -> VoiceService:
    """Собрать движок для ядра: `layer` это результат `wire_modules` (внимание и шина модулей), `registry` это реестр окружений.

    Ставит инструменты `voice.*` в реестр и возвращает сервис: ядро отдаёт его HTTP-серверу (`http_api.set_voice`), а при остановке
    зовёт `await service.stop()`. Выключенный движок ничего не регистрирует. Токен и разрешённые адреса те же, что у шины `/ws`."""
    import asyncio
    import os
    import time

    from services.character import character
    from services.peers.hub import remote_allowed
    from services.tracing import events_log

    def names() -> list[str]:
        card = character.active()
        return [card.name, *card.aliases]

    def character_reference():
        """Референс ESpeech активного персонажа: espeech_ref.wav и espeech_ref.txt рядом с основным референсом (как у голоса ПК)."""
        ref = character.active().voice_reference()
        if ref is None:
            return None
        audio, text = ref.parent / "espeech_ref.wav", ref.parent / "espeech_ref.txt"
        if audio.is_file() and text.is_file():
            return audio, text.read_text(encoding="utf-8").strip()
        return None

    privacy_cache: dict = {"at": 0.0, "terms": []}

    async def leaks(text: str) -> bool:
        """Есть ли в тексте личные слова (те же `core.privacy_terms`, что проверяют публикации модулей). Нет источника слов или он
        упал: считаем, что есть (в облако не уходит)."""
        getter = getattr(getattr(layer, "requests", None), "privacy_terms", None)
        if getter is None:
            return True
        if time.monotonic() - privacy_cache["at"] > PRIVACY_CACHE_S:
            privacy_cache["terms"] = [t.lower() for t in await asyncio.to_thread(getter)]
            privacy_cache["at"] = time.monotonic()
        low = text.lower().replace("ё", "е")
        return any(term.replace("ё", "е") in low for term in privacy_cache["terms"])

    from host import settings

    # при GITS_ATTENTION=v2 у слоя есть `submit_event`: часть видов идёт в внимание v2, иначе всё как раньше в `attention.submit`
    submit = getattr(layer, "submit_event", None) or layer.attention.submit
    service = VoiceService.build(os.environ if environ is None else environ, submit=submit,
                                 token=lambda: layer.hub.token, allow=lambda remote: remote_allowed(remote, layer.hub.allow),
                                 names=names, journal=lambda kind, text, data: events_log.log_nowait(kind, text, **data),
                                 ref_resolver=character_reference, leaks=leaks, memory_dir=settings.memory_dir(),
                                 emit_spoken=getattr(layer, "submit_event", None) is not None)
    service.register_tools(registry)
    return service
