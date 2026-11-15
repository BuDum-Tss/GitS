"""Голосовая сессия (один голосовой канал одного адаптера) и менеджер сессий.

Сессия знает: кто её открыл (адаптер и его возможности), кто в канале (говорящие, у каждого свой сегментатор речи), настройки
(политика перебивания) и очередь воспроизведения. Менеджер держит несколько сессий одновременно по id; id приходит от адаптера
в hello, повторный hello с тем же id заменяет прежнюю сессию (адаптер переподключился).
"""
import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .playback import Playback
from .vad import UtteranceSegmenter


class SessionLimitError(Exception):
    """Превышен лимит сессий или говорящих."""


class SessionTransport(Protocol):
    """Канал к адаптеру: движок шлёт управление (текст JSON) и кадры воспроизведения."""

    @property
    def closed(self) -> bool: ...

    async def send_control(self, text: str) -> None: ...

    async def send_play_frame(self, play_id: int, seq: int, pcm: bytes) -> None: ...

    async def close(self, reason: str) -> None: ...


@dataclass(frozen=True)
class Capabilities:
    echo_free: bool = False            # адаптер не отдаёт движку её собственный голос (у Discord так: поток участников чистый)
    can_barge_in: bool = False         # адаптер умеет мгновенно оборвать воспроизведение
    speakers_labelled: bool = True     # говорящие различимы по потокам (у Discord да, у шлюза ПК один говорящий)
    direct: bool = False               # канал «один на один»: обращение по имени не нужно


@dataclass
class Speaker:
    id: str
    name: str
    idx: int
    segmenter: UtteranceSegmenter
    is_owner: bool = False
    is_bot: bool = False
    joined_ts: float = field(default_factory=time.time)
    speaking_hint: bool = False        # подсказка адаптера (speaking.start/stop), в решениях не участвует
    barged_ts: float = 0.0             # когда этот говорящий в последний раз перебил её
    frames: int = 0
    last_seq: int = -1
    stt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)       # фразы одного говорящего распознаются по порядку

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "is_owner": self.is_owner}


@dataclass
class SessionStats:
    frames_in: int = 0
    bad_frames: int = 0
    utterances: int = 0
    dropped_utterances: int = 0
    stt_errors: int = 0
    spoken: int = 0
    barge_ins: int = 0
    echo_dropped: int = 0


class VoiceSession:
    def __init__(self, session_id: str, adapter: str, caps: Capabilities, transport: SessionTransport, barge_in: str,
                 barge_in_ms: int):
        self.id, self.adapter, self.caps, self.transport = session_id, adapter, caps, transport
        self.barge_in_policy = barge_in                  # действующая политика (уже с учётом возможностей адаптера)
        self.barge_in_ms = barge_in_ms
        self.speakers: dict[str, Speaker] = {}
        self.by_idx: dict[int, Speaker] = {}
        self.playback: Playback | None = None
        self.stats = SessionStats()
        self.created = time.time()
        self.closed = False
        self.stt_pending = 0
        self.tasks: set = set()

    @property
    def one_to_one(self) -> bool:
        """Канал «один на один»: адаптер так сказал или в канале один живой собеседник."""
        humans = [s for s in self.speakers.values() if not s.is_bot]
        return self.caps.direct or len(humans) == 1

    def public(self) -> dict:
        pb = self.playback
        return {
            "id": self.id, "adapter": self.adapter, "capabilities": asdict(self.caps),
            "barge_in": self.barge_in_policy, "speakers": [s.public() for s in self.speakers.values()],
            "speaking": pb.audible if pb is not None else False, "metrics": pb.metrics.public() if pb is not None else {}, "queue": len(pb.waiting_uids()) if pb is not None else 0,
            "age_s": round(time.time() - self.created, 1), "stats": asdict(self.stats),
        }


class SessionManager:
    def __init__(self, max_sessions: int = 4, max_speakers: int = 32):
        self.max_sessions, self.max_speakers = max_sessions, max_speakers
        self._sessions: dict[str, VoiceSession] = {}

    def get(self, session_id: str) -> VoiceSession | None:
        return self._sessions.get(session_id)

    def all(self) -> list[VoiceSession]:
        return list(self._sessions.values())

    def __len__(self) -> int:
        return len(self._sessions)

    def add(self, session: VoiceSession) -> VoiceSession | None:
        """Зарегистрировать сессию; возвращает прежнюю с тем же id (её закрывает вызывающий) или бросает при превышении лимита."""
        old = self._sessions.get(session.id)
        if old is None and len(self._sessions) >= self.max_sessions:
            raise SessionLimitError(f"открыто {len(self._sessions)} сессий из {self.max_sessions} возможных")
        self._sessions[session.id] = session
        return old

    def remove(self, session: VoiceSession) -> None:
        if self._sessions.get(session.id) is session:
            del self._sessions[session.id]
