"""Приём звука из голосового канала Discord: общая часть пробы (voice_probe/probe.py) и адаптера (discord_voice.py).

Схема (docs/DISCORD_VOICE_PROBE.md): расширение discord-ext-voice-recv снимает транспортное шифрование (PyNaCl), разводит пакеты по
SSRC и отдаёт сырой Opus, но ДО сквозного шифрования DAVE (поддержки DAVE в расширении нет). DAVE снимаем сами: DaveSession из
discord.py 2.7 (`vc._connection.dave_session`).decrypt(user_id, MediaType.audio, кадр), затем штатный `discord.opus.Decoder`.

Используются ПРИВАТНЫЕ части discord.py и расширения: `vc._connection.dave_session`, `vc._connection.dave_protocol_version`,
`vc._get_id_from_ssrc`. При обновлении библиотек их надо перепроверить; всё, что на них опирается, собрано здесь, в одном месте.

Тяжёлые импорты (discord, nacl, davey, расширение) ленивые: модуль и его тесты работают без них и без сети. Звук нигде не пишется
и не логируется.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable

# кадр тишины Opus: Discord шлёт его без DAVE-шифрования, расшифровывать нельзя
OPUS_SILENCE = b"\xf8\xff\xfe"

# (модуль, сообщение при отсутствии)
REQUIRED = (
    ("discord", "нет discord.py (pip install discord.py==2.7.1)"),
    ("nacl", "нет PyNaCl: без него нет голоса (pip install 'PyNaCl>=1.5,<1.6')"),
    ("davey", "нет davey: без него нет DAVE, звонки Discord с 2026-03 не работают (pip install 'davey>=0.1.6,<0.2')"),
    (
        "discord.ext.voice_recv",
        "нет discord-ext-voice-recv: нечем принимать звук (pip install discord-ext-voice-recv==0.5.2a179)",
    ),
)


def check_dependencies(importer: Callable[[str], object] = importlib.import_module) -> list[str]:
    """Список сообщений о недостающих пакетах (пустой, если всё на месте)."""
    problems = []
    for name, message in REQUIRED:
        try:
            importer(name)
        except ImportError:
            problems.append(message)
    return problems


def describe_connect_error(exc: BaseException) -> str:
    """Понятное сообщение по типичным ошибкам входа и подключения."""
    name = type(exc).__name__
    code = getattr(exc, "code", None)
    if name == "LoginFailure":
        return "неверный токен бота (DISCORD_BOT_TOKEN)"
    if name == "PrivilegedIntentsRequired":
        return "не включён привилегированный intent в портале разработчика (Bot -> Privileged Gateway Intents)"
    if name == "ConnectionClosed" and code == 4014:
        return "доступ к голосовому каналу запрещён или бота выгнали: нужны права View Channel и Connect"
    if name == "ConnectionClosed" and code == 4017:
        return "канал требует DAVE, а сессия E2EE не поднялась: проверьте davey и версию discord.py"
    if name == "Forbidden":
        return "нет прав Connect/Speak в этом канале (Forbidden)"
    if name == "TimeoutError":
        return "таймаут подключения к голосовому каналу (сеть/UDP из контейнера, права Connect)"
    if name == "OpusNotLoaded":
        return "нет libopus в образе (apt install libopus0)"
    return f"{name}: {exc}"


class UnknownUser(RuntimeError):
    """SSRC ещё не сопоставлен с участником (бот не получил событие speaking)."""


class DaveSessionNotReady(RuntimeError):
    """Сессия DAVE не готова (подключение, смена эпохи или состава): кадр расшифровать нечем."""


def dave_info(vc, last_error: str | None = None) -> dict:
    """Состояние DAVE: версия протокола, готовность сессии, её статус, последняя ошибка (для пробы и панели)."""
    conn = getattr(vc, "_connection", None)
    session = getattr(conn, "dave_session", None)
    info: dict = {"protocol_version": getattr(conn, "dave_protocol_version", "?")}
    if session is not None:
        info["ready"] = bool(getattr(session, "ready", False))
        status = getattr(session, "status", None)
        info["status"] = getattr(status, "name", str(status)) if status is not None else None
    else:
        info["ready"] = None
    info["error"] = last_error
    return info


class FrameDecoder:
    """Расшифровка DAVE и декодирование Opus кадров участников одного голосового соединения.

    `vc` - голосовой клиент (можно подменять: после переподключения он другой). Декодер Opus у каждого участника свой (у Opus есть
    состояние между кадрами). Вызывается из потока приёма расширения; на каждого участника не из двух потоков сразу.
    `davey_module` и `decoder_factory` подставляются в тестах; по умолчанию davey и `discord.opus.Decoder`."""

    RESET_AFTER = 5                    # столько ошибок декодера подряд: декодер участника создаётся заново

    def __init__(self, vc=None, *, davey_module=None, decoder_factory: Callable[[], object] | None = None):
        self.vc = vc
        self._davey = davey_module
        self._factory = decoder_factory
        self._decoders: dict = {}
        self._fails: dict = {}

    def _media_audio(self):
        mod = self._davey if self._davey is not None else importlib.import_module("davey")
        return mod.MediaType.audio

    def _new_decoder(self):
        if self._factory is not None:
            return self._factory()
        import discord
        return discord.opus.Decoder()

    def decode(self, uid, opus: bytes) -> bytes:
        """Кадр Opus участника -> PCM s16le 48 кГц стерео (20 мс). Любой сбой (нет участника, DAVE не готов, ошибка расшифровки
        или декодера) поднимает исключение: вызывающий считает его и идёт дальше."""
        if isinstance(uid, str):
            raise UnknownUser("ssrc не сопоставлен с участником")
        frame = bytes(opus)
        conn = getattr(self.vc, "_connection", None)
        session = getattr(conn, "dave_session", None)
        version = getattr(conn, "dave_protocol_version", 0)
        if frame != OPUS_SILENCE and version:
            if session is None or not session.ready:
                raise DaveSessionNotReady("сессия DAVE не готова")
            frame = bytes(session.decrypt(uid, self._media_audio(), frame))
        dec = self._decoders.get(uid)
        if dec is None:
            dec = self._decoders[uid] = self._new_decoder()
        try:
            pcm = dec.decode(frame, fec=False)
        except Exception:
            n = self._fails[uid] = self._fails.get(uid, 0) + 1
            if n >= self.RESET_AFTER:
                self._decoders.pop(uid, None)
                self._fails[uid] = 0
            raise
        self._fails[uid] = 0
        return pcm

    def forget(self, uid) -> None:
        """Участник вышел: его декодер больше не нужен."""
        self._decoders.pop(uid, None)
        self._fails.pop(uid, None)


def build_sink(on_frame: Callable[[object, bytes], None], vc_getter: Callable[[], object], base=None):
    """Приёмник для discord-ext-voice-recv: просит сырой Opus (после транспортной, но до DAVE-расшифровки) и зовёт
    `on_frame(uid, кадр)` из потока приёма. uid: id участника, а пока SSRC не сопоставлен, строка «ssrc:N». Исключения
    `on_frame` приёмник не пробрасывает (иначе поток приёма расширения остановится). `base`: класс-основа (по умолчанию
    `voice_recv.AudioSink`; тесты подставляют свою)."""
    if base is None:
        from discord.ext import voice_recv
        base = voice_recv.AudioSink

    class RxSink(base):
        def wants_opus(self) -> bool:
            return True

        def write(self, user, data) -> None:
            opus = getattr(data, "opus", None)
            if not opus:                       # фейковые пакеты потерь и пустые
                return
            uid = getattr(user, "id", None) if user is not None else None
            if uid is None:
                ssrc = getattr(getattr(data, "packet", None), "ssrc", None)
                found = None
                if ssrc is not None:
                    try:
                        found = vc_getter()._get_id_from_ssrc(ssrc)
                    except Exception:
                        found = None
                uid = found if found is not None else f"ssrc:{ssrc}"
            try:
                on_frame(uid, opus)
            except Exception:                  # поток приёма не должен падать из-за обработчика
                pass

        def cleanup(self) -> None:
            pass

    return RxSink()
