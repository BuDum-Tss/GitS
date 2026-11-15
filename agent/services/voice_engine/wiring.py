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
              journal: Callable[[str, str, dict], None]) -> "VoiceService":
        """`submit(peer_id, event)` это `attention.submit`, `token` и `allow` берутся у шины модулей, `journal(kind, text, data)`
        это запись в журнал событий ядра."""
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
        tts = HttpTtsClient(TtsSettings(url=config.tts_url, ref_audio=config.tts_ref_audio, ref_text=config.tts_ref_text,
                                        speed=config.tts_speed, voice=config.tts_voice, emotion=config.tts_emotion,
                                        timeout_s=config.tts_timeout_s))
        vad_factory = load_silero_factory(config.silero_model, config.silero_threshold) if config.vad == "silero" else None
        engine = VoiceEngine(stt=stt, tts=tts, config=config, emit_event=emit, journal=journal, names=names,
                             vad_factory=vad_factory)
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


def wire_voice(layer, registry, environ: Mapping[str, str] | None = None) -> VoiceService:
    """Собрать движок для ядра: `layer` это результат `wire_modules` (внимание и шина модулей), `registry` это реестр окружений.

    Ставит инструменты `voice.*` в реестр и возвращает сервис: ядро отдаёт его HTTP-серверу (`http_api.set_voice`), а при остановке
    зовёт `await service.stop()`. Выключенный движок ничего не регистрирует. Токен и разрешённые адреса те же, что у шины `/ws`."""
    import os

    from services.character import character
    from services.peers.hub import remote_allowed
    from services.tracing import events_log

    def names() -> list[str]:
        card = character.active()
        return [card.name, *card.aliases]

    service = VoiceService.build(os.environ if environ is None else environ, submit=layer.attention.submit,
                                 token=lambda: layer.hub.token, allow=lambda remote: remote_allowed(remote, layer.hub.allow),
                                 names=names, journal=lambda kind, text, data: events_log.log_nowait(kind, text, **data))
    service.register_tools(registry)
    return service
