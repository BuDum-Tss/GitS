"""Ввод и вывод звука адаптера «виртуальные кабели»: абстракции, устройства `soundcard` и заглушки для тестов.

Формат везде один: PCM s16le, 48 кГц, стерео, кадры по 20 мс (3840 байт). К формату движка (16 кГц моно) звук приводит
`cable_adapter`. Библиотека `soundcard` импортируется лениво, внутри методов: без неё (и без устройств) тесты идут на
`MemoryIn` и `MemoryOut`. Звук нигде не пишется на диск и не попадает в журнал, только счётчики.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable

logger = logging.getLogger("gits.audio_cable.io")

RATE = 48000
CHANNELS = 2
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000          # 960 отсчётов на канал
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2       # 3840 байт
MAX_IN_QUEUE = 50                                # кадров входа в очереди (1 с); при переполнении старые отбрасываются
OPEN_TIMEOUT_S = 5.0                             # сколько ждать, пока поток откроет устройство


class DeviceError(RuntimeError):
    """Устройство не найдено, неоднозначно или не открылось."""


class LoopError(DeviceError):
    """Вход и выход это один кабель: она услышала бы сама себя."""


# ── поиск устройств и проверка петли ───────────────────────────────────────────────────────────

_KIND_ENUM = {"speaker": "all_speakers", "microphone": "all_microphones"}
_STOP_WORDS = re.compile(r"(?<!\w)(input|output|вход|выход)(?!\w)", re.IGNORECASE)


def list_devices() -> dict[str, list[str]]:
    """Имена устройств: {"speaker": [...], "microphone": [...]} (выход и вход). Нужна библиотека soundcard."""
    return {kind: [d.name for d in _enumerate(kind)] for kind in _KIND_ENUM}


def _enumerate(kind: str) -> list:
    if kind not in _KIND_ENUM:
        raise ValueError(f"kind должен быть speaker или microphone, получено {kind!r}")
    try:
        import soundcard as sc
    except ImportError as e:
        raise DeviceError("не установлена библиотека soundcard (pip install -r services/audio_cable/requirements.txt)") from e
    return list(getattr(sc, _KIND_ENUM[kind])())


def find_device(kind: str, name_substring: str, devices: Iterable | None = None):
    """Устройство по подстроке имени без учёта регистра. `kind`: "speaker" (выход) или "microphone" (вход).

    Точное совпадение имени выигрывает; иначе подстрока должна подойти ровно одному устройству. Ошибка `DeviceError` со
    списком доступных имён. `devices` (объекты с `.name`) подставляют тесты."""
    needle = (name_substring or "").strip().lower()
    if not needle:
        raise DeviceError("не задано имя устройства")
    pool = list(devices) if devices is not None else _enumerate(kind)
    available = ", ".join(f"«{d.name}»" for d in pool) or "нет ни одного"
    exact = [d for d in pool if d.name.strip().lower() == needle]
    if len(exact) == 1:
        return exact[0]
    found = exact or [d for d in pool if needle in d.name.lower()]
    if not found:
        raise DeviceError(f"устройство «{name_substring}» не найдено ({kind}). Доступные: {available}")
    if len(found) > 1:
        names = ", ".join(f"«{d.name}»" for d in found)
        raise DeviceError(f"под «{name_substring}» подходит несколько устройств ({names}): уточни подстроку")
    return found[0]


def cable_base(name: str) -> str:
    """Базовое имя кабеля: без суффикса вендора в скобках и без слов Input/Output/Вход/Выход."""
    head = name.split("(", 1)[0]                          # Windows режет длинные имена: скобка может быть не закрыта
    head = _STOP_WORDS.sub(" ", head.lower())
    return re.sub(r"\s+", " ", head).strip(" -_.")


def same_cable(in_name: str, out_name: str) -> bool:
    """Петля: имена совпадают или это две стороны одного кабеля («CABLE Input (VB-Audio Virtual Cable)» и «CABLE Output (...)»)."""
    a, b = in_name.strip().lower(), out_name.strip().lower()
    if a == b:
        return True
    base_a, base_b = cable_base(in_name), cable_base(out_name)
    return bool(base_a) and base_a == base_b


# ── абстракции ─────────────────────────────────────────────────────────────────────────────────

class AudioIn(ABC):
    """Источник звука собеседников: асинхронный итератор кадров по 20 мс (48 кГц стерео s16le, 3840 байт)."""

    device_name: str = ""
    dropped: int = 0                                      # кадров отброшено из-за переполнения очереди

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[bytes]: ...


class AudioOut(ABC):
    """Приёмник её голоса: `feed` (кадры 48 кГц стерео), мгновенный `abort`, счётчики буфера и сыгранного."""

    device_name: str = ""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def begin(self) -> None:
        """Началась новая реплика: обнулить `played_ms`."""

    @abstractmethod
    def feed(self, pcm48_stereo: bytes) -> None: ...

    @abstractmethod
    def abort(self) -> None:
        """Сбросить всё неиграное (не дольше одного кадра)."""

    @property
    @abstractmethod
    def buffered_ms(self) -> int: ...

    @property
    @abstractmethod
    def played_ms(self) -> int: ...

    @abstractmethod
    async def drained(self) -> None:
        """Завершается, когда всё поданное сыграно или был abort."""


class BufferedOut(AudioOut):
    """Общая часть выхода: потокобезопасный буфер и поток-писатель. Кадр пишет `_write` (блокирующий, в потоке), при пустом
    буфере вызывается `_idle` (тишина: поток не обрывается). Устройство и заглушка отличаются только этими двумя методами."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._flush_tail = False                          # дописать неполный хвост нулями (конец реплики)
        self._writing = False                             # кадр взят и ещё пишется
        self._gen = 0                                     # поколение: растёт на abort и begin
        self._frames = 0                                  # настоящих кадров записано в этой реплике
        self.silence_frames = 0
        self.aborts = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    # ── поток ──

    async def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name=f"cable-out-{self.device_name}", daemon=True)
        self._thread.start()
        if not await asyncio.to_thread(self._ready.wait, OPEN_TIMEOUT_S):
            self._stop.set()
            raise DeviceError("выход не открылся за отведённое время")
        if self.error:
            raise DeviceError(self.error)

    async def stop(self) -> None:
        self._stop.set()
        self.abort()
        thread, self._thread = self._thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)

    def _thread_main(self) -> None:
        try:
            self._device_loop()
        except Exception as e:                             # ошибка устройства: только тип в журнал, звук не логируем
            self.error = f"{type(e).__name__}: {str(e)[:120]}"
            logger.error("кабель: выход остановился (%s)", self.error)
        finally:
            self._ready.set()

    def _device_loop(self) -> None:
        self._ready.set()
        self._loop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            frame = self._take()
            if frame is None:
                self._idle()
                continue
            try:
                self._write(frame)
            finally:
                with self._lock:
                    self._writing = False
                    self._frames += 1

    def _take(self) -> bytes | None:
        with self._lock:
            if len(self._buf) >= FRAME_BYTES:
                frame = bytes(self._buf[:FRAME_BYTES])
                del self._buf[:FRAME_BYTES]
            elif self._buf and self._flush_tail:
                frame = bytes(self._buf) + bytes(FRAME_BYTES - len(self._buf))
                self._buf.clear()
            else:
                return None
            self._writing = True
            return frame

    @abstractmethod
    def _write(self, frame: bytes) -> None: ...

    @abstractmethod
    def _idle(self) -> None: ...

    # ── то, что зовёт адаптер ──

    def begin(self) -> None:
        with self._lock:
            self._frames = 0
            self._flush_tail = False
            self._gen += 1

    def feed(self, pcm48_stereo: bytes) -> None:
        if pcm48_stereo:
            with self._lock:
                self._buf += pcm48_stereo

    def abort(self) -> None:
        with self._lock:
            if self._buf:
                self.aborts += 1
            self._buf.clear()
            self._flush_tail = False
            self._gen += 1

    @property
    def buffered_ms(self) -> int:
        return len(self._buf) * FRAME_MS // FRAME_BYTES

    @property
    def played_ms(self) -> int:
        return self._frames * FRAME_MS

    async def drained(self) -> None:
        with self._lock:
            gen = self._gen
            self._flush_tail = True
        while True:
            with self._lock:
                if self._gen != gen or (not self._buf and not self._writing):
                    return
            if self._thread is None or not self._thread.is_alive():
                return                                     # писать некому: ждать нечего
            await asyncio.sleep(0.01)


# ── устройства soundcard ──────────────────────────────────────────────────────────────────────

class SoundcardOut(BufferedOut):
    """Выход на колонки (виртуальный кабель «CABLE-B Input»): поток-писатель пишет кадры по 20 мс, при паузе тишину."""

    def __init__(self, device) -> None:
        super().__init__()
        self._device = device
        self.device_name = str(device.name)
        self._player = None
        self._silence = None

    def _device_loop(self) -> None:
        import numpy as np
        self._silence = np.zeros((FRAME_SAMPLES, CHANNELS), dtype=np.float32)
        with self._device.player(samplerate=RATE, channels=CHANNELS, blocksize=FRAME_SAMPLES) as player:
            self._player = player
            self._ready.set()
            self._loop()

    def _write(self, frame: bytes) -> None:
        import numpy as np
        data = np.frombuffer(frame, dtype="<i2").astype(np.float32).reshape(-1, CHANNELS) / 32768.0
        self._player.play(data)

    def _idle(self) -> None:
        self._player.play(self._silence)                  # блокируется на длину кадра: заодно задаёт темп потока
        self.silence_frames += 1


class SoundcardIn(AudioIn):
    """Вход с «микрофона» (виртуальный кабель «CABLE-A Output»): поток-запись кладёт кадры по 20 мс в очередь asyncio."""

    def __init__(self, device, *, max_queue: int = MAX_IN_QUEUE) -> None:
        self._device = device
        self.device_name = str(device.name)
        self.dropped = 0
        self.frames = 0
        self.error: str | None = None
        self._max = max_queue
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name=f"cable-in-{self.device_name}", daemon=True)
        self._thread.start()
        if not await asyncio.to_thread(self._ready.wait, OPEN_TIMEOUT_S):
            self._stop.set()
            raise DeviceError("вход не открылся за отведённое время")
        if self.error:
            raise DeviceError(self.error)

    async def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)
        if self._queue is not None:
            self._queue.put_nowait(None)

    def _thread_main(self) -> None:
        try:
            import numpy as np
            with self._device.recorder(samplerate=RATE, channels=CHANNELS, blocksize=FRAME_SAMPLES) as rec:
                self._ready.set()
                while not self._stop.is_set():
                    data = rec.record(numframes=FRAME_SAMPLES)
                    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    try:
                        self._loop.call_soon_threadsafe(self._push, pcm)
                    except RuntimeError:                   # цикл событий закрыт: слать некому
                        break
        except Exception as e:
            self.error = f"{type(e).__name__}: {str(e)[:120]}"
            logger.error("кабель: вход остановился (%s)", self.error)
        finally:
            self._ready.set()
            try:
                self._loop.call_soon_threadsafe(self._push, None)
            except RuntimeError:
                pass

    def _push(self, frame: bytes | None) -> None:
        q = self._queue
        if q is None:
            return
        if frame is None:
            q.put_nowait(None)
            return
        while q.qsize() >= self._max:                      # живую речь не догоняют: старые кадры жертвуем
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.dropped += 1
        self.frames += 1
        q.put_nowait(frame)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        q = self._queue
        if q is None:
            return
        while True:
            frame = await q.get()
            if frame is None:
                return
            yield frame


# ── заглушки для тестов ────────────────────────────────────────────────────────────────────────

class MemoryIn(AudioIn):
    """Вход из памяти: отдаёт заданные кадры (`pace_s` пауза между ними); без `keep_open` итерация кончается вместе с
    кадрами, с ним ждёт новые (`push`) до `close`."""

    def __init__(self, frames: Iterable[bytes] = (), *, pace_s: float = 0.0, keep_open: bool = False,
                 device_name: str = "memory-in") -> None:
        self.device_name = device_name
        self.dropped = 0
        self.started = False
        self.stopped = False
        self._pace = pace_s
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        for f in frames:
            self._queue.put_nowait(f)
        if not keep_open:
            self._queue.put_nowait(None)

    def push(self, frame: bytes) -> None:
        self._queue.put_nowait(frame)

    def close(self) -> None:
        self._queue.put_nowait(None)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self._queue.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame
            if self._pace:
                await asyncio.sleep(self._pace)


class MemoryOut(BufferedOut):
    """Выход в память: копит принятый звук (`audio`), `frame_delay_s` имитирует время записи кадра в устройство."""

    def __init__(self, *, frame_delay_s: float = 0.0, device_name: str = "memory-out") -> None:
        super().__init__()
        self.device_name = device_name
        self.audio = bytearray()
        self._delay = frame_delay_s

    def _write(self, frame: bytes) -> None:
        self.audio += frame
        if self._delay:
            self._stop.wait(self._delay)

    def _idle(self) -> None:
        self.silence_frames += 1
        self._stop.wait(0.002)
