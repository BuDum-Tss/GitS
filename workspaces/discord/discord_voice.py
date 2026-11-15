"""Адаптер голосового канала Discord для движка голоса ядра (docs/VOICE_ENGINE.md, docs/DISCORD_VOICE_PROBE.md).

Адаптер делает только две вещи: поставляет движку звук участников канала (каждого отдельно) и играет то, что движок просит.
Распознавание, деление на фразы, решение «отвечать ли» и озвучка живут в ядре; фраза участника приходит ей событием
`voicechat.utterance`, ответ это инструмент ядра `voice.say`. Здесь нет ни распознавания, ни своих правил внимания.

Одна голосовая сессия на сервер (так устроен Discord: у бота одно голосовое соединение на сервер); id сессии движка
`discord:<guild_id>:<channel_id>`, адаптер `discord`. Возможности (hello): echo_free (поток участника чистый, своего голоса бот не
слышит), can_barge_in (play.stop обрывает воспроизведение за 1-2 кадра), speakers_labelled (у каждого участника свой поток),
direct=False (звук не с локального микрофона).

Приём: VoiceRecvClient (расширение discord-ext-voice-recv) отдаёт сырой Opus -> расшифровка DAVE и декодирование (`voice_rx`,
общая с пробой) -> 48 кГц стерео -> `to_16k_mono_stream` (свой потоковый ресемплер на участника) -> очередь -> `send_audio`.
Discord не шлёт пакеты, когда человек молчит, а движок закрывает фразу по тишине: поэтому пауза в потоке заполняется нулями в
реальном времени (до `SILENCE_FILL_S` после последнего звука). Звук нигде не пишется на диск и не логируется: только счётчики.

Воспроизведение: кадры движка (16 кГц моно) -> `to_48k_stereo_stream` -> `PcmPipe` (20 мс кадры 48 кГц стерео) -> `discord.PCMAudio`.
Недогруз заполняется тишиной, остановка (`on_stop`) обрывает за один кадр. Сыгранные мс считаются по реально отданным кадрам.

Режимы чатов (`common.ChatModes`, тот же эффективный режим, что у текстовых: канал, иначе сервер, иначе по умолчанию): `ignore` не
пускает (владелец из панели заходит, но не слушает и не говорит), `read` слушает, но не говорит (на play ack с причиной `error`,
движок видит «не сыграно»), `reply` как обычно. Режим перечитывается раз в секунду: смена в панели действует сразу.

Приватность: при входе бот пишет в текстовый чат канала объявление (настройки voice_announce, voice_announce_text) без пингов;
если писать некуда, это видно в панели. Другие боты не слушаются. Сквозное шифрование DAVE снимается только для приёма.

Приватные вызовы discord.py и расширения собраны в `voice_rx` (`_connection.dave_session`, `_get_id_from_ssrc`).
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
from dataclasses import dataclass, field

from common import MODE_IGNORE, MODE_READ, MODE_REPLY, KnownTargets, channel_target, person_name, scrub, split_place
from discord_bot import ChatModeError, kind_name
from voice_rx import FrameDecoder, build_sink, check_dependencies, dave_info, describe_connect_error

logger = logging.getLogger("gits_discord.voice")

ADAPTER = "discord"
VOICE_KINDS = ("voice", "stage_voice")
DEFAULT_ANNOUNCE = "Я слушаю этот канал: речь распознаётся в текст, звук не сохраняется."
ANNOUNCE_MAX = 500
ENGINE_WAIT_S = 6.0                # сколько ждать связи с движком голоса при входе
CONNECT_TIMEOUT_S = 30.0           # вход в голосовой канал Discord
TICK_S = 1.0                       # цикл сопровождения: режим, состав, одиночество, связь
RECONNECT_GRACE_S = 8.0            # столько соединение должно быть оборвано, чтобы мы переподключили его сами
RECONNECT_DELAYS = (3.0, 6.0, 12.0, 20.0, 30.0)
QUEUE_MAX = 400                    # кадров приёма в очереди (8 секунд одного участника); дальше звук отбрасывается
SILENCE_FILL_S = 1.5               # сколько после последнего звука заполнять паузу тишиной (движок закрывает фразу по тишине)
FILL_STEP_S = 0.04                 # период цикла передачи звука (заодно проверка пауз)
FILL_MIN_S = 0.1                   # пауза короче не заполняется (джиттер не должен дырявить речь)
FRAME_S = 0.02
FRAME16_BYTES = 640                # 20 мс, 16 кГц моно s16
FRAME48_BYTES = 3840               # 20 мс, 48 кГц стерео s16
PREBUFFER_MS = 60                  # столько накопить, прежде чем начать играть (гасит джиттер подачи)
PIPE_MAX_MS = 3000                 # не накапливать больше (движок подаёт в реальном времени, это защита)
PLAY_TAIL_S = 3.0                  # столько без прогресса (плеер не читает буфер) значит «игра зависла»
MAX_PLAYER_RESTARTS = 5            # перезапусков плеера на одну реплику
PLAY_STOP_REASONS = ("stop", "replaced", "barge_in", "closed", "error")
AUTOJOIN_RETRY_S = 60.0
PERM_NAMES = {"view_channel": "View Channel", "connect": "Connect", "speak": "Speak", "use_voice_activation": "Use Voice Activity"}


class VoiceError(Exception):
    """Голосовое действие не вышло по понятной причине (текст для владельца). code: unavailable | denied | bad_request."""

    def __init__(self, message: str, code: str = "unavailable"):
        super().__init__(message)
        self.code = code


class KnownVoice(KnownTargets):
    """Голосовые каналы, куда она уже заходила (отдельный файл: названия голосового и текстового каналов часто совпадают)."""

    FILE = "discord_voice_known.json"


class PcmPipe:
    """Файлоподобный источник для `discord.PCMAudio`: `read()` отдаёт ровно кадр 20 мс (48 кГц стерео s16). Питается из корутины,
    читается потоком плеера discord.py. Нет данных и не закончено: тишина (недогруз); закончено: пустые байты (конец)."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.done = False
        self.aborted = False
        self.abort_reason = ""
        self.frames = 0                # настоящих (не тишины) кадров отдано
        self.underruns = 0

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            if not self.aborted:
                self._buf += pcm

    def finish(self) -> None:
        with self._lock:
            self.done = True

    def abort(self, reason: str = "stop") -> None:
        with self._lock:
            self._buf.clear()
            if not self.aborted:
                self.abort_reason = reason
            self.done = self.aborted = True

    @property
    def buffered_ms(self) -> int:
        return len(self._buf) * 1000 // (FRAME48_BYTES * 50)

    @property
    def played_ms(self) -> int:
        return self.frames * 20

    def read(self, size: int = FRAME48_BYTES) -> bytes:
        with self._lock:
            if len(self._buf) >= FRAME48_BYTES:
                data = bytes(self._buf[:FRAME48_BYTES])
                del self._buf[:FRAME48_BYTES]
                self.frames += 1
                return data
            if self.done:
                if not self._buf:
                    return b""
                data = bytes(self._buf) + bytes(FRAME48_BYTES - len(self._buf))        # последний неполный кадр
                self._buf.clear()
                self.frames += 1
                return data
            self.underruns += 1
            return bytes(FRAME48_BYTES)


@dataclass
class _Speaker:
    uid: int
    sid: str
    name: str
    fed_until: float = 0.0             # до какого момента (монотонные часы) движку отдан звук, включая заполнение тишиной
    active_until: float = 0.0          # до какого момента после последнего звука паузу заполняем
    flushed: bool = True               # хвост фильтра ресемплера уже вытолкнут


@dataclass
class VoiceSession:
    guild: object
    channel: object
    id: str
    forced: bool                       # зашла по команде владельца из панели (режим «игнор» её не выгоняет)
    loop: asyncio.AbstractEventLoop
    vc: object = None
    client: object = None              # VoiceAdapterClient (или подмена в тестах)
    decoder: FrameDecoder | None = None
    state: str = "connecting"          # connecting | connected | reconnecting
    mode: str = MODE_REPLY
    listening: bool = True
    leaving: bool = False
    reconnecting: bool = False
    speakers: dict = field(default_factory=dict)         # id участника -> _Speaker
    resamplers: dict = field(default_factory=dict)       # id участника -> Resampler (трогает поток приёма)
    ignored: set = field(default_factory=set)            # id ботов в канале: их не слушаем
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_MAX))
    tasks: list = field(default_factory=list)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    pipe: PcmPipe | None = None
    playing_uid: str | None = None
    started_playing: bool = False
    alone_since: float | None = None
    announce: str = ""
    error: str = ""
    counters: dict = field(default_factory=dict)
    decode_errors: dict = field(default_factory=dict)
    since: float = 0.0

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def can_speak(self) -> bool:
        return self.mode == MODE_REPLY and not self.leaving


class DiscordVoiceAdapter:
    def __init__(self, bot, *, engine_url: str | None = None, engine_token: str | None = None):
        """bot - DiscordBot (клиент discord.py, режимы, владелец, настройки модуля). engine_url и engine_token: адрес и токен
        движка голоса (по умолчанию GITS_VOICE_URL / адрес шины и GITS_TOKEN; их выбирает VoiceAdapterClient)."""
        self.bot = bot
        self.engine_url, self.engine_token = engine_url, engine_token
        self.sessions: dict[int, VoiceSession] = {}
        self.known = KnownVoice(str(bot._dir))
        self.last_error = ""
        self.last_note = ""                  # чем кончилась прошлая сессия (для панели)
        self.clock = time.monotonic
        self._join_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._autojoin_retry_at = 0.0
        self._deps: tuple[float, list[str]] | None = None
        # точки подмены (тесты): без настоящих discord.py, расширения, davey и движка
        self.importer = importlib.import_module
        self.vc_class = None                 # класс голосового клиента; по умолчанию voice_recv.VoiceRecvClient
        self.sink_base = None
        self.decoder_factory = None
        self.davey_module = None
        self.client_factory = None           # по умолчанию VoiceAdapterClient
        self.source_factory = None           # по умолчанию discord.PCMAudio
        self.mentions_factory = None         # по умолчанию discord.AllowedMentions.none

    # ── настройки ────────────────────────────────────────────────────────────

    def _opt(self, name: str, default):
        return (self.bot.client.module_settings or {}).get(name, default)

    def announce_enabled(self) -> bool:
        value = self._opt("voice_announce", True)
        return value if isinstance(value, bool) else True

    def announce_text(self) -> str:
        text = self._opt("voice_announce_text", DEFAULT_ANNOUNCE)
        text = " ".join(str(text).split()) if isinstance(text, str) else ""
        return (text or DEFAULT_ANNOUNCE)[:ANNOUNCE_MAX]

    def alone_timeout(self) -> float:
        value = self._opt("voice_alone_timeout_s", 120)
        return 120.0 if isinstance(value, bool) or not isinstance(value, (int, float)) else max(0.0, float(value))

    def autojoin_setting(self) -> str:
        value = self._opt("voice_autojoin", "")
        return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""

    # ── поиск каналов ────────────────────────────────────────────────────────

    def _guilds(self) -> list:
        return list(getattr(self.bot._client, "guilds", None) or [])

    @staticmethod
    def voice_channels(guild) -> list:
        chans = [c for c in getattr(guild, "channels", None) or [] if kind_name(c) in VOICE_KINDS]
        return sorted(chans, key=lambda c: (getattr(c, "position", 0) or 0, str(c.name)))

    @staticmethod
    def humans(channel) -> list:
        return [m for m in getattr(channel, "members", None) or [] if not getattr(m, "bot", False)]

    def _is_owner(self, member) -> bool:
        return self.bot.is_owner_user(getattr(member, "id", None), [getattr(member, "name", None), getattr(member, "global_name", None)])

    def owner_channel(self):
        """Голосовой канал, где сейчас сидит владелец (по кэшу состояний голоса); None, если он нигде не сидит."""
        for g in self._guilds():
            for c in self.voice_channels(g):
                if any(self._is_owner(m) for m in self.humans(c)):
                    return c
        return None

    def owner_in(self, channel) -> bool:
        return any(self._is_owner(m) for m in self.humans(channel))

    def find_channel(self, raw) -> object:
        """Голосовой канал по `channel:<id>`, id, «Сервер / Канал», #названию или названию. Пусто: канал, где сидит владелец.
        Не нашли, не голосовой или не один: ValueError с понятным текстом."""
        text = str(raw or "").strip()
        if self.bot._client is None:
            raise ValueError("нет подключения к Discord")
        if not text:
            found = self.owner_channel()
            if found is None:
                raise ValueError("владельца нет ни в одном голосовом канале: укажи канал («Сервер / Канал», #название или id)")
            return found
        ident = text.split(":", 1)[1] if text.lower().startswith("channel:") else text
        if ident.isdigit():
            ch = self.bot._client.get_channel(int(ident))
            if ch is None or kind_name(ch) not in VOICE_KINDS:
                raise ValueError(f"канал {ident} не найден среди голосовых каналов, видимых боту")
            return ch
        guild_part, name = split_place(text)
        want, gwant = name.lstrip("#").strip().lower(), guild_part.lower()
        exact, part = [], []
        for g in self._guilds():
            if gwant and gwant not in str(g.name).lower():
                continue
            for c in self.voice_channels(g):
                cname = str(c.name).lower()
                if cname == want:
                    exact.append(c)
                elif want and want in cname:
                    part.append(c)
        hits = exact or part
        if len(hits) > 1:
            names = ", ".join(f"«{c.guild.name} / {c.name}»" for c in hits[:6])
            raise ValueError(f"название «{text[:60]}» подходит нескольким голосовым каналам ({names}): укажи «Сервер / Канал» или id")
        if not hits:
            raise ValueError(f"голосовой канал «{text[:60]}» не найден среди видимых боту")
        return hits[0]

    def is_unknown(self, channel) -> bool:
        """Канал, куда она ещё не заходила и где владельца нет (вход туда это действие с приватностью: нужно разрешение)."""
        return not self.known.has(str(channel.id)) and not self.owner_in(channel)

    def _me(self, guild):
        return getattr(guild, "me", None) or self.bot._me

    def missing_perms(self, channel, *, need_speak: bool = True) -> list[str]:
        """Каких прав боту не хватает в голосовом канале (названия как в Discord); пусто, если хватает или права неизвестны."""
        try:
            perms = channel.permissions_for(self._me(channel.guild))
        except Exception:
            return []
        names = ["view_channel", "connect"] + (["speak", "use_voice_activation"] if need_speak else [])
        return [PERM_NAMES[n] for n in names if not getattr(perms, n, True)]

    # ── вход и выход ─────────────────────────────────────────────────────────

    def dependency_problems(self, cached: bool = False) -> list[str]:
        """Чего не хватает для голоса. cached: ответ не старше минуты (панель собирается часто, а импорт отсутствующих пакетов дорог)."""
        now = self.clock()
        if cached and self._deps is not None and now - self._deps[0] < 60:
            return list(self._deps[1])
        problems = check_dependencies(self.importer)
        try:
            self.importer("gits_sdk.voice")
        except ImportError:
            problems.append("нет numpy: без него нет перевода звука (pip install numpy)")
        self._deps = (now, problems)
        return list(problems)

    async def join(self, channel, *, by_owner: bool = False) -> str:
        """Зайти в голосовой канал и начать слушать. by_owner: действие владельца из панели (режим «игнор» не мешает зайти, но тогда
        она не слушает и не говорит). Возвращает текст для владельца; ошибки: VoiceError, ChatModeError."""
        async with self._join_lock:
            return await self._join(channel, by_owner)

    async def _join(self, channel, by_owner: bool) -> str:
        if kind_name(channel) not in VOICE_KINDS:
            raise VoiceError("это не голосовой канал", "bad_request")
        problems = self.dependency_problems()
        if problems:
            raise VoiceError("голос недоступен: " + "; ".join(problems))
        ok, why = self.bot.state()
        if not ok:
            raise VoiceError(why)
        guild = channel.guild
        mode = self.bot.mode_of_channel(channel)
        label = f"{guild.name} / {channel.name}"
        if mode == MODE_IGNORE and not by_owner:
            raise ChatModeError("канал игнорируется (режим «игнор» в панели Discord): в такой голосовой канал она не заходит")
        current = self.sessions.get(guild.id)
        if current is not None and current.channel.id == channel.id and not current.leaving:
            return f"уже в голосовом канале «{label}»"
        missing = self.missing_perms(channel, need_speak=mode == MODE_REPLY)
        if missing:
            raise VoiceError(f"у бота нет прав в канале «{label}»: {', '.join(missing)}; выдай их роли бота на сервере", "denied")
        limit = getattr(channel, "user_limit", 0) or 0
        perms = None
        try:
            perms = channel.permissions_for(self._me(guild))
        except Exception:
            pass
        if limit and len(getattr(channel, "members", None) or []) >= limit and not (
                getattr(perms, "move_members", False) or getattr(perms, "administrator", False)):
            raise VoiceError(f"канал «{label}» заполнен ({limit} из {limit})")
        if current is not None:
            await self._end(current, "переход в другой канал")
        sess = VoiceSession(guild=guild, channel=channel, id=f"{ADAPTER}:{guild.id}:{channel.id}", forced=by_owner,
                            loop=asyncio.get_running_loop(), since=self.clock())
        sess.mode = mode
        sess.listening = mode != MODE_IGNORE
        sess.decoder = FrameDecoder(None, davey_module=self.davey_module, decoder_factory=self.decoder_factory)
        sess.client = self._make_engine(sess)
        await sess.client.start()
        if not await sess.client.wait_ready(ENGINE_WAIT_S):
            err = getattr(sess.client, "last_error", None)
            await sess.client.stop()
            raise VoiceError("движок голоса ядра недоступен (выключен или ещё запускается): сначала включи его"
                             + (f" ({scrub(err)[:80]})" if err else ""))
        try:
            sess.vc = await self._connect(channel)
        except asyncio.CancelledError:
            await sess.client.stop()
            raise
        except Exception as e:
            await sess.client.stop()
            self.last_error = self._explain(e)
            self.bot._changed()
            raise VoiceError(self.last_error, "denied" if type(e).__name__ == "Forbidden" else "unavailable") from e
        sess.decoder.vc = sess.vc
        sess.state = "connected"
        self.sessions[guild.id] = sess
        self.last_error = ""
        try:
            sess.vc.listen(self._make_sink(sess))
            await self._sync_members(sess)
        except Exception as e:
            await self._end(sess, "приём звука не запустился")
            self.last_error = self._explain(e)
            raise VoiceError(self.last_error) from e
        sess.announce = await self._announce(sess)
        self.known.add(channel_target(channel.id), channel.name, label)
        sess.tasks = [asyncio.create_task(self._pump(sess), name=f"discord-voice-pump-{sess.id}"),
                      asyncio.create_task(self._maintain(sess), name=f"discord-voice-keep-{sess.id}")]
        logger.info("вошла в голосовой канал %s (режим %s)", sess.id, mode)
        self.bot._changed()
        return self._joined_text(sess, label)

    def _joined_text(self, sess: VoiceSession, label: str) -> str:
        if not sess.listening:
            what = "режим «игнор»: она не слушает и не говорит"
        elif sess.mode == MODE_READ:
            what = "режим «чтение»: слушает, но говорить не будет"
        else:
            what = "слушает и может отвечать (инструмент voice.say)"
        return f"зашла в голосовой канал «{label}»: {what}. Объявление: {sess.announce or 'не требуется'}."

    async def _connect(self, channel):
        vc_cls = self.vc_class or self._default_vc_class()
        return await asyncio.wait_for(channel.connect(cls=vc_cls, timeout=CONNECT_TIMEOUT_S), CONNECT_TIMEOUT_S + 15)

    def _default_vc_class(self):
        from discord.ext import voice_recv
        return voice_recv.VoiceRecvClient

    def _explain(self, e: BaseException) -> str:
        name = type(e).__name__
        if name == "Forbidden":
            return "нет прав Connect/Speak в этом голосовом канале (Forbidden): выдай их роли бота"
        if name == "ClientException" and "already" in str(e).lower():
            return "бот уже подключён к голосовому каналу на этом сервере (подожди или отключи его)"
        if name in ("ImportError", "ModuleNotFoundError"):
            return "голос недоступен: " + scrub(e)[:120]
        return scrub(describe_connect_error(e), getattr(self.bot, "_token", ""))[:200]

    def _make_engine(self, sess: VoiceSession):
        title = f"{getattr(sess.guild, 'name', '') or ''} / {getattr(sess.channel, 'name', '') or ''}".strip(" /")
        kw = dict(adapter=ADAPTER, session=sess.id, title=title[:120], url=self.engine_url, token=self.engine_token, echo_free=True,
                  can_barge_in=True,
                  speakers_labelled=True, direct=False, on_play=lambda frames, uid: self._on_play(sess, frames, uid),
                  on_stop=lambda uid, reason: self._on_stop(sess, uid, reason), on_leave=lambda: self._on_engine_leave(sess))
        if self.client_factory is not None:
            return self.client_factory(**kw)
        from gits_sdk.voice import VoiceAdapterClient
        return VoiceAdapterClient(**kw)

    def _make_sink(self, sess: VoiceSession):
        return build_sink(lambda uid, opus: self._on_frame(sess, uid, opus), lambda: sess.vc, self.sink_base)

    async def leave(self, raw=None) -> str:
        """Выйти из голосового канала (одного: без указания, если сессия одна; иначе по названию сервера или канала)."""
        if not self.sessions:
            return "она не в голосовом канале"
        sessions = list(self.sessions.values())
        text = str(raw or "").strip().lower()
        if text.startswith("channel:"):
            text = text.split(":", 1)[1]
        if text:
            sessions = [s for s in sessions if text in (s.guild.name.lower(), s.channel.name.lower().lstrip("#"),
                                                        f"{s.guild.name} / {s.channel.name}".lower(), str(s.channel.id))
                        or text in s.guild.name.lower() or text in s.channel.name.lower()]
            if not sessions:
                raise VoiceError(f"в «{text[:60]}» она сейчас не сидит", "bad_request")
        elif len(sessions) > 1:
            names = ", ".join(f"«{s.guild.name} / {s.channel.name}»" for s in sessions)
            raise VoiceError(f"она сидит в нескольких каналах ({names}): укажи, из какого выйти", "bad_request")
        left = []
        for s in sessions:
            await self._end(s, "по просьбе")
            left.append(f"«{s.guild.name} / {s.channel.name}»")
        return "вышла из голосового канала " + ", ".join(left)

    async def _end(self, sess: VoiceSession, reason: str, *, error: str = "") -> None:
        """Закрыть сессию: остановить воспроизведение и приём, выйти из канала, закрыть связь с движком. Повторный вызов безопасен."""
        if sess.leaving:
            return
        sess.leaving = True
        if self.sessions.get(sess.guild.id) is sess:
            del self.sessions[sess.guild.id]
        if error:
            self.last_error = error
        me = asyncio.current_task()
        for task in sess.tasks:
            if task is not me:
                task.cancel()
        await asyncio.gather(*[t for t in sess.tasks if t is not me], return_exceptions=True)
        self._abort_play(sess)
        vc = sess.vc
        if vc is not None:
            for fn in ("stop_listening", "stop"):
                try:
                    getattr(vc, fn)()
                except Exception as e:
                    logger.debug("голос: %s: %s", fn, type(e).__name__)
            try:
                await asyncio.wait_for(vc.disconnect(force=True), 10)
            except Exception as e:
                logger.debug("голос: отключение: %s", type(e).__name__)
        try:
            await asyncio.wait_for(sess.client.stop(), 10)
        except Exception as e:
            logger.debug("голос: закрытие связи с движком: %s", type(e).__name__)
        self.last_note = f"вышла из «{sess.guild.name} / {sess.channel.name}»: {reason}"
        logger.info("вышла из голосового канала %s (%s)", sess.id, reason)
        self.bot._changed()

    async def close(self) -> None:
        for sess in list(self.sessions.values()):
            await self._end(sess, "модуль остановлен")
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def on_client_lost(self) -> None:
        """Клиент Discord закрыт (сбой входа, перезапуск): голосовые соединения погибли вместе с ним."""
        for sess in list(self.sessions.values()):
            await self._end(sess, "соединение с Discord потеряно", error="соединение с Discord потеряно: в голосовой канал надо зайти заново")

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── состав участников ────────────────────────────────────────────────────

    async def _sync_members(self, sess: VoiceSession) -> None:
        """Участники канала -> speaker.join / speaker.leave движку; боты не слушаются; в режиме «игнор» никого."""
        me_id = getattr(self._me(sess.guild), "id", None)
        members = [m for m in getattr(sess.channel, "members", None) or [] if getattr(m, "id", None) != me_id]
        sess.ignored = {m.id for m in members if getattr(m, "bot", False)}
        want = {m.id: m for m in members if not getattr(m, "bot", False)} if sess.listening else {}
        for uid in [u for u in sess.speakers if u not in want]:
            await self._drop_speaker(sess, uid)
        for uid, m in want.items():
            if uid not in sess.speakers:
                await self._add_speaker(sess, m)
        humans = [m for m in members if not getattr(m, "bot", False)]
        if humans:
            sess.alone_since = None
        elif sess.alone_since is None:
            sess.alone_since = self.clock()

    async def _add_speaker(self, sess: VoiceSession, member) -> None:
        name = person_name(member)[:80]
        sp = _Speaker(member.id, f"discord:{member.id}", name)
        sess.speakers[member.id] = sp
        try:
            await sess.client.speaker_join(sp.sid, name, is_owner=self._is_owner(member))
        except Exception as e:
            logger.debug("speaker.join: %s", type(e).__name__)

    async def _drop_speaker(self, sess: VoiceSession, uid: int) -> None:
        sp = sess.speakers.pop(uid, None)
        sess.resamplers.pop(uid, None)
        if sess.decoder is not None:
            sess.decoder.forget(uid)
        if sp is not None:
            try:
                await sess.client.speaker_leave(sp.sid)
            except Exception as e:
                logger.debug("speaker.leave: %s", type(e).__name__)

    # ── приём ────────────────────────────────────────────────────────────────

    def _on_frame(self, sess: VoiceSession, uid, opus: bytes) -> None:
        """Кадр Opus участника из ПОТОКА приёма расширения: расшифровка, декодирование, 16 кГц моно, в очередь цикла событий."""
        sess.count("frames_in")
        if sess.leaving or not sess.listening:
            sess.count("dropped_not_listening")
            return
        if uid in sess.ignored:
            return
        try:
            pcm48 = sess.decoder.decode(uid, opus)
        except Exception as e:
            kind = type(e).__name__
            sess.decode_errors[kind] = sess.decode_errors.get(kind, 0) + 1
            sess.count("decode_errors")
            return
        rs = sess.resamplers.get(uid)
        if rs is None:
            from gits_sdk.voice import to_16k_mono_stream
            rs = sess.resamplers[uid] = to_16k_mono_stream()
        pcm16 = rs.process(pcm48)
        sess.count("decoded")
        if pcm16:
            try:
                sess.loop.call_soon_threadsafe(self._enqueue, sess, uid, pcm16)
            except RuntimeError:                   # цикл событий закрыт: модуль останавливается
                pass

    def _enqueue(self, sess: VoiceSession, uid: int, pcm16: bytes) -> None:
        try:
            sess.queue.put_nowait((uid, pcm16))
        except asyncio.QueueFull:
            sess.count("queue_full")

    async def _pump(self, sess: VoiceSession) -> None:
        """Очередь приёма -> движок. Между кадрами заполняет паузы тишиной: Discord молчащим пакетов не шлёт."""
        while True:
            try:
                uid, pcm = await asyncio.wait_for(sess.queue.get(), FILL_STEP_S)
            except asyncio.TimeoutError:
                uid, pcm = None, b""
            try:
                if uid is not None:
                    await self._deliver(sess, uid, pcm)
                await self._fill_silence(sess)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                sess.count("pump_errors")
                logger.debug("голос: передача звука: %s", type(e).__name__)

    async def _send(self, sess: VoiceSession, sp: _Speaker, pcm: bytes) -> None:
        if not await sess.client.send_audio(sp.sid, pcm):
            sess.count("engine_dropped")

    async def _deliver(self, sess: VoiceSession, uid: int, pcm: bytes) -> None:
        sp = sess.speakers.get(uid)
        if sp is None:                             # заговорил раньше, чем увидели его вход в канал
            member = next((m for m in self.humans(sess.channel) if m.id == uid), None)
            if member is None or not sess.listening:
                sess.count("unknown_speaker")
                return
            await self._add_speaker(sess, member)
            sp = sess.speakers.get(uid)
            if sp is None:
                return
        now, dur = self.clock(), len(pcm) / (FRAME16_BYTES / FRAME_S)
        gap = now - dur - sp.fed_until
        if sp.fed_until and sp.fed_until < sp.active_until and gap >= FILL_MIN_S:      # потерянные или запоздавшие пакеты
            await self._send(sess, sp, bytes(int(gap / FRAME_S) * FRAME16_BYTES))
            sp.fed_until += int(gap / FRAME_S) * FRAME_S
        sp.fed_until = max(sp.fed_until, now - dur) + dur
        sp.active_until = now + SILENCE_FILL_S
        sp.flushed = False
        await self._send(sess, sp, pcm)

    async def _fill_silence(self, sess: VoiceSession) -> None:
        now = self.clock()
        for sp in list(sess.speakers.values()):
            if sp.fed_until >= sp.active_until:
                continue
            gap = min(now, sp.active_until) - sp.fed_until
            if gap < FILL_MIN_S:
                continue
            frames = int(gap / FRAME_S)
            data = bytes(frames * FRAME16_BYTES)
            rs = sess.resamplers.get(sp.uid)
            if rs is not None and not sp.flushed:
                data = rs.flush() + data
                sp.flushed = True
            sp.fed_until += frames * FRAME_S
            await self._send(sess, sp, data)

    # ── воспроизведение ──────────────────────────────────────────────────────

    def _source(self, pipe: PcmPipe):
        if self.source_factory is not None:
            return self.source_factory(pipe)
        import discord
        return discord.PCMAudio(pipe)

    async def _on_play(self, sess: VoiceSession, frames, uid: str) -> int:
        """Играет реплику движка (кадры 16 кГц моно). Возвращает сыгранные мс. Нельзя говорить (режим «чтение» или «игнор»,
        нет соединения): кадры выбрасываются, движку уходит ack с причиной error и 0 мс.

        Плеер discord.py может закончиться раньше времени (обрыв соединения, чужой stop): тогда, пока в буфере есть звук, он
        запускается заново (счётчик player_restarts), а не бросает остаток реплики. Конец игры ждём по прогрессу (буфер
        продолжает читаться), а не по длине буфера на момент конца подачи. На каждую реплику одна строка INFO в журнал."""
        vc = sess.vc
        if not sess.can_speak() or vc is None or not vc.is_connected():
            sess.count("refused_plays")
            frames.stop_reason = "error"
            frames.drain()
            logger.info("голос: реплика %s: отказ (говорить нельзя или нет соединения), режим %s", uid, sess.mode)
            return 0
        from gits_sdk.voice import to_48k_stereo_stream
        self._abort_play(sess, "replaced")
        pipe = PcmPipe()
        sess.pipe, sess.playing_uid, sess.started_playing = pipe, uid, False
        loop = asyncio.get_running_loop()
        cur = {"done": asyncio.Event(), "restarts": 0}
        fed, outcome, timed_out = 0, "completed", False

        def start() -> None:
            done = cur["done"] = asyncio.Event()

            def after(err) -> None:                # из потока плеера discord.py
                if err is not None:                # плеер упал: без этой строки обрыв реплики не имеет видимой причины
                    logger.warning("голос: плеер discord.py остановился с ошибкой %s: %s, сыграно %d мс, в буфере %d мс",
                                   type(err).__name__, scrub(err)[:200], pipe.played_ms, pipe.buffered_ms)
                try:
                    loop.call_soon_threadsafe(done.set)
                except RuntimeError:
                    pass

            vc.play(self._source(pipe), after=after)
            sess.started_playing = True

        async def restart() -> bool:
            """Плеер кончился, а звук в буфере ещё есть: запустить снова (дождавшись соединения не дольше 5 с)."""
            if cur["restarts"] >= MAX_PLAYER_RESTARTS:
                return False
            for _ in range(50):
                if vc.is_connected():
                    break
                await asyncio.sleep(0.1)
            cur["restarts"] += 1
            sess.count("player_restarts")
            logger.warning("голос: плеер остановился раньше времени (реплика %s, сыграно %d мс, в буфере %d мс): запускаю снова",
                           uid, pipe.played_ms, pipe.buffered_ms)
            try:
                start()
            except Exception as e:
                logger.warning("голос: плеер не перезапущен: %s", type(e).__name__)
                return False
            return True

        rs = to_48k_stereo_stream()
        try:
            async for pcm in frames:
                fed += 1
                if pipe.aborted:
                    break
                pipe.feed(rs.process(pcm))
                if not sess.started_playing:
                    if pipe.buffered_ms >= PREBUFFER_MS:
                        start()
                elif cur["done"].is_set() and pipe.buffered_ms and not await restart():
                    outcome = "error"
                    break
                while pipe.buffered_ms > PIPE_MAX_MS and not pipe.aborted:      # движок подаёт в реальном времени: это страховка
                    await asyncio.sleep(0.02)
            if not pipe.aborted and outcome == "completed":
                pipe.feed(rs.flush())
            pipe.finish()
            if not sess.started_playing and not pipe.aborted and pipe.buffered_ms:
                start()
            last_frames, last_move = -1, self.clock()
            while sess.started_playing and not pipe.aborted and outcome == "completed":
                if cur["done"].is_set():
                    if not pipe.buffered_ms:
                        break
                    if not await restart():
                        outcome = "error"
                    continue
                try:                                # конец игры ждём по прогрессу: буфер читается, значит, игра идёт
                    await asyncio.wait_for(cur["done"].wait(), 0.25)
                except asyncio.TimeoutError:
                    pass
                if pipe.frames != last_frames:
                    last_frames, last_move = pipe.frames, self.clock()
                elif self.clock() - last_move > PLAY_TAIL_S:
                    timed_out = True
                    sess.count("play_timeouts")
                    break
        except Exception:
            outcome = "error"
            self._abort_play(sess, "error")
            raise
        finally:
            if pipe.aborted:
                outcome = pipe.abort_reason or outcome
                sess.count("aborted_plays")
                if not frames.stop_reason:          # оборвала не просьба движка: ack не должен выглядеть как «доиграно»
                    frames.stop_reason = pipe.abort_reason if pipe.abort_reason in PLAY_STOP_REASONS else "stop"
            elif outcome == "error":
                frames.stop_reason = frames.stop_reason or "error"
            if sess.pipe is pipe:
                if sess.started_playing:
                    self._stop_vc(sess)
                sess.pipe, sess.playing_uid, sess.started_playing = None, None, False
            sess.count("underruns", pipe.underruns)
            logger.info("голос: реплика %s: кадров подано %d, сыграно %d мс, недогрузов %d, перезапусков плеера %d, итог %s%s", uid, fed,
                        pipe.played_ms, pipe.underruns, cur["restarts"], frames.stop_reason or outcome,
                        ", ТАЙМАУТ ожидания конца" if timed_out else "")
        sess.count("plays")
        return pipe.played_ms

    def _stop_vc(self, sess: VoiceSession) -> None:
        """Остановить ТОЛЬКО воспроизведение: у VoiceRecvClient `stop()` останавливает ещё и приём звука."""
        vc = sess.vc
        try:
            if vc is not None and vc.is_playing():
                getattr(vc, "stop_playing", vc.stop)()
        except Exception as e:
            logger.debug("голос: stop: %s", type(e).__name__)

    def _abort_play(self, sess: VoiceSession, reason: str = "stop") -> None:
        pipe = sess.pipe
        if pipe is not None:
            pipe.abort(reason)
            self._stop_vc(sess)

    def _on_stop(self, sess: VoiceSession, uid: str, reason: str) -> None:
        """play.stop движка (стоп, замена, перебивание): оборвать воспроизведение сразу."""
        if sess.pipe is not None and (not uid or sess.playing_uid in (None, uid)):
            self._abort_play(sess, reason)
            sess.count("stops")

    async def _on_engine_leave(self, sess: VoiceSession) -> None:
        """Инструмент ядра voice.leave: выйти. Выход делается отдельной задачей: он закрывает и саму связь с движком."""
        self._spawn(self._end(sess, "по просьбе движка"), "discord-voice-leave")

    # ── сопровождение сессии ─────────────────────────────────────────────────

    async def _maintain(self, sess: VoiceSession) -> None:
        down_since: float | None = None
        while not sess.leaving:
            try:
                await asyncio.wait_for(sess.wake.wait(), TICK_S)
            except asyncio.TimeoutError:
                pass
            sess.wake.clear()
            if sess.leaving:
                return
            try:
                sess.mode = self.bot.mode_of_channel(sess.channel)
                if sess.mode == MODE_IGNORE and not sess.forced:
                    await self._end(sess, "режим канала сменили на «игнор»", error="вышла из голосового канала: режим канала сменили на «игнор»")
                    return
                listening = sess.mode != MODE_IGNORE
                if listening != sess.listening:
                    sess.listening = listening
                    self.bot._changed()
                await self._sync_members(sess)
                connected = bool(sess.vc is not None and sess.vc.is_connected())
                if connected:
                    down_since = None
                    sess.state = "connected"
                else:
                    down_since = down_since or self.clock()
                    if self.clock() - down_since >= RECONNECT_GRACE_S and not sess.reconnecting:
                        await self._reconnect(sess)
                        if sess.leaving:
                            return
                        down_since = None
                timeout = self.alone_timeout()
                if timeout > 0 and sess.alone_since is not None and self.clock() - sess.alone_since >= timeout:
                    await self._end(sess, "в канале никого нет", error="")
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("голос: сопровождение: %s", type(e).__name__)

    async def _reconnect(self, sess: VoiceSession) -> None:
        """Соединение с голосовым каналом оборвано и сам discord.py не поднял: войти заново (движок и состав участников остаются)."""
        sess.reconnecting, sess.state = True, "reconnecting"
        self.bot._changed()
        try:
            for attempt, delay in enumerate(RECONNECT_DELAYS, start=1):
                if sess.leaving:
                    return
                try:
                    old = sess.vc
                    for fn in ("stop_listening", "stop"):
                        try:
                            getattr(old, fn)()
                        except Exception:
                            pass
                    try:
                        await asyncio.wait_for(old.disconnect(force=True), 10)
                    except Exception:
                        pass
                    sess.vc = await self._connect(sess.channel)
                    sess.decoder.vc = sess.vc
                    sess.vc.listen(self._make_sink(sess))
                    sess.state, sess.error = "connected", ""
                    sess.count("reconnects")
                    logger.info("голосовое соединение %s восстановлено (попытка %d)", sess.id, attempt)
                    self.bot._changed()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    sess.error = self._explain(e)
                    logger.warning("голосовое соединение %s: переподключение не вышло (%s)", sess.id, type(e).__name__)
                    await asyncio.sleep(delay)
            await self._end(sess, "не удалось переподключиться", error=f"не удалось переподключиться к голосовому каналу: {sess.error}")
        finally:
            sess.reconnecting = False

    # ── события Discord ──────────────────────────────────────────────────────

    async def on_voice_state(self, member, before, after) -> None:
        """Событие voice_states: состав канала, отключение или перемещение бота, автовход (voice_autojoin)."""
        try:
            guild = getattr(member, "guild", None)
            if guild is None:
                return
            sess = self.sessions.get(guild.id)
            me_id = getattr(self._me(guild), "id", None)
            if member.id == me_id:
                moved = getattr(after, "channel", None)
                if sess is not None and not sess.leaving and not sess.reconnecting and (moved is None or moved.id != sess.channel.id):
                    why = "её отключили от голосового канала" if moved is None else "её переместили в другой голосовой канал"
                    await self._end(sess, why, error=why)
                return
            if sess is not None:
                touched = {getattr(getattr(s, "channel", None), "id", None) for s in (before, after)}
                if sess.channel.id in touched:
                    sess.wake.set()
            target = getattr(after, "channel", None)
            if (target is not None and not getattr(member, "bot", False) and target != getattr(before, "channel", None)
                    and guild.id not in self.sessions and self._is_autojoin(target)):
                self._spawn(self._autojoin(target), "discord-voice-autojoin")
        except Exception as e:
            logger.debug("голос: событие состояния: %s", type(e).__name__)

    def on_ready(self) -> None:
        """Клиент Discord готов: если задан voice_autojoin и в канале уже есть люди, зайти."""
        if self.autojoin_setting():
            self._spawn(self._autojoin_scan(), "discord-voice-autojoin-scan")

    def _is_autojoin(self, channel) -> bool:
        want = self.autojoin_setting()
        if not want or kind_name(channel) not in VOICE_KINDS:
            return False
        try:
            return self.find_channel(want).id == channel.id
        except ValueError:
            return False

    async def _autojoin_scan(self) -> None:
        try:
            channel = self.find_channel(self.autojoin_setting())
        except ValueError as e:
            self.last_error = f"автовход (voice_autojoin): {e}"
            self.bot._changed()
            return
        if self.humans(channel) and channel.guild.id not in self.sessions:
            await self._autojoin(channel)

    async def _autojoin(self, channel) -> None:
        """Автовход: не чаще раза в минуту после неудачи (нет прав, нет движка), без вопросов (канал назвал сам владелец)."""
        if self.clock() < self._autojoin_retry_at or channel.guild.id in self.sessions:
            return
        try:
            await self.join(channel, by_owner=False)
        except asyncio.CancelledError:
            raise
        except (VoiceError, ChatModeError) as e:
            self._autojoin_retry_at = self.clock() + AUTOJOIN_RETRY_S
            self.last_error = f"автовход: {e}"
            self.bot._changed()
        except Exception as e:
            self._autojoin_retry_at = self.clock() + AUTOJOIN_RETRY_S
            self.last_error = f"автовход: {type(e).__name__}"
            self.bot._changed()

    # ── объявление ───────────────────────────────────────────────────────────

    def _mentions(self):
        if self.mentions_factory is not None:
            return self.mentions_factory()
        try:
            import discord
            return discord.AllowedMentions.none()
        except Exception:
            return None

    def _announce_places(self, sess: VoiceSession) -> list:
        guild, out = sess.guild, []
        for ch in (sess.channel, getattr(guild, "system_channel", None), *self.bot.writable_channels(guild)):
            if ch is not None and ch not in out and self.bot.mode_of_channel(ch) != MODE_IGNORE:
                out.append(ch)
        return out

    async def _announce(self, sess: VoiceSession) -> str:
        """Объявление в текстовый чат голосового канала, иначе в системный или первый канал, где можно писать. Без пингов.
        Возвращает, что вышло (для панели); сбой объявления на вход не влияет."""
        if not sess.listening:
            return "не нужно: она не слушает"
        if not self.announce_enabled():
            return "выключено настройкой"
        text, mentions = self.announce_text(), self._mentions()
        extra = {"allowed_mentions": mentions} if mentions is not None else {}
        tried = 0
        for ch in self._announce_places(sess):
            try:
                perms = ch.permissions_for(self._me(sess.guild))
                if not getattr(perms, "view_channel", True) or not getattr(perms, "send_messages", True):
                    continue
            except Exception:
                pass
            if tried >= 5:
                break
            tried += 1
            try:
                await self.bot._call(ch.send, text, **extra)
                return f"отправлено в «{'#' if ch is not sess.channel else ''}{ch.name}»"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("голос: объявление в %s: %s", getattr(ch, "id", "?"), type(e).__name__)
        return "пропущено: некуда писать (у бота нет прав писать в текстовые каналы сервера)"

    # ── для панели ───────────────────────────────────────────────────────────

    def channels_view(self, guild_id: int) -> list[dict]:
        """Голосовые каналы сервера из кэша (без сети): id, название, участники-люди, лимит, нехватка прав, сидит ли она здесь."""
        guild = next((g for g in self._guilds() if g.id == guild_id), None)
        if guild is None:
            return []
        here = self.sessions.get(guild_id)
        out = []
        for c in self.voice_channels(guild):
            out.append({"id": c.id, "name": str(c.name), "kind": kind_name(c), "members": [person_name(m) for m in self.humans(c)],
                        "bots": len(getattr(c, "members", None) or []) - len(self.humans(c)), "limit": getattr(c, "user_limit", 0) or 0,
                        "missing": self.missing_perms(c, need_speak=self.bot.mode_of_channel(c) == MODE_REPLY),
                        "here": here is not None and here.channel.id == c.id and not here.leaving})
        return out

    def status(self) -> dict:
        """Состояние для блока «Голос» панели: сессии, зависимости, последняя ошибка. Без сети."""
        sessions = []
        for s in self.sessions.values():
            client = s.client
            engine_ok = bool(getattr(client, "ready", False))
            sessions.append({
                "id": s.id, "guild": str(s.guild.name), "channel": str(s.channel.name), "state": s.state, "mode": s.mode,
                "listening": s.listening, "can_speak": s.can_speak(), "speaking": bool(s.pipe is not None and s.started_playing),
                "members": [sp.name for sp in s.speakers.values()], "engine": engine_ok,
                "engine_error": scrub(getattr(client, "last_error", "") or "")[:100], "announce": s.announce, "error": s.error,
                "forced": s.forced, "dave": dave_info(s.vc), "counters": dict(s.counters), "decode_errors": dict(s.decode_errors),
                "alone": s.alone_since is not None})
        return {"sessions": sessions, "last_error": self.last_error, "autojoin": self.autojoin_setting(),
                "problems": self.dependency_problems(cached=True), "note": self.last_note}
