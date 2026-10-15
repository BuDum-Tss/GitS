"""Трасса задержки реплики (ТЗ 24, TRC-1, TRC-2): метки этапов от конца фразы до первого звука.

Трасса заводится при входе события (голос, сообщение дашборда, Telegram, событие модуля): `start()` в ядре или
`wire_new()` в модуле. Модуль кладёт свои метки в поле `trace` события шины, ядро принимает его через `from_bus()`.
Метки - миллисекунды `time.perf_counter()`: на одной машине этот счётчик общий для процессов (QPC в Windows, CLOCK_MONOTONIC
в Linux), поэтому метки модулей и ядра сравнимы. Каждая метка ставится один раз: повторная (например, фоновый вызов модели
после ответа) не портит первую.

Запись: одна строка вида `trace` на трассу в журнале (events_log.log_nowait), когда ядро закончило разговор и голос сообщил
о звуке (или звука не будет). Сводка p50/p95 по окну времени: `summarize()`, отдаётся как GET /api/latency.

Трасса только собирает цифры: поведение агента от неё не зависит. Модуль стоит на `agent/`, поэтому имя файла не совпадает
со стандартным `trace`.
"""
import asyncio
import contextvars
import math
import time
import uuid

import events_log

# этапы в порядке прохождения реплики (TRC-1)
STAGES = ("mic_end", "stt_done", "core_in", "classify_done", "route", "retrieve_done", "llm_first_token", "llm_done",
          "first_sentence_ready", "tts_first_audio", "play_start")
ORIGIN_STAGES = ("mic_end", "core_in")          # от чего отсчитываем: конец фразы, если есть, иначе приход в ядро
OPEN_TTL_S = 300.0                              # незавершённые трассы старше этого выбрасываются из памяти без записи
AUDIO_WAIT_S = 15.0                             # сколько ждём отметки звука, если реплика ушла в голос

_open: dict[str, "Trace"] = {}
_current: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar("latency_trace", default=None)


def now_ms() -> float:
    return time.perf_counter() * 1000.0


class Trace:
    """Одна трасса. Метки - абсолютные миллисекунды perf_counter; запись переводит их в отсчёт от начала."""

    __slots__ = ("id", "source", "marks", "core_done", "pending", "speak_sent", "audio", "written", "timer", "created")

    def __init__(self, source: str, trace_id: str | None = None, marks: dict | None = None):
        self.id = trace_id or uuid.uuid4().hex[:12]
        self.source = str(source or "unknown")[:40]
        self.marks: dict[str, float] = dict(marks or {})
        self.core_done = False          # ядро закончило обработку обращения
        self.pending = 0                # реплик этой трассы в очереди речи, ещё не сыгранных и не снятых
        self.speak_sent = False         # реплику отправили модулю голоса: ждём его отметку звука
        self.audio = False              # отметки звука пришли
        self.written = False
        self.timer = None
        self.created = time.time()

    def mark(self, stage: str, at: float | None = None) -> None:
        if stage not in self.marks:      # первая метка побеждает
            self.marks[stage] = now_ms() if at is None else at

    def wire(self) -> dict:
        """Вид для шины (поле `trace` события или команды speak): id, источник и метки."""
        return {"id": self.id, "source": self.source, "marks": {k: round(v, 3) for k, v in self.marks.items()}}


# ── вход ────────────────────────────────────────────────────────────────────────

def start(source: str) -> Trace:
    """Новая трасса в ядре (обращение из дашборда, чата, без модуля)."""
    t = Trace(source)
    t.mark("core_in")
    _register(t)
    return t


def wire_new(source: str, marks: dict | None = None) -> dict:
    """Трасса в модуле: возвращает словарь для поля `trace` события (метки модуля)."""
    t = Trace(source, marks=marks)
    return t.wire()


def from_bus(wire, source: str) -> Trace | None:
    """Ядро приняло событие с полем `trace`: адоптирует трассу модуля и ставит метку прихода. Без поля - None."""
    if not isinstance(wire, dict) or not isinstance(wire.get("id"), str) or not wire["id"]:
        return None
    marks = wire.get("marks") if isinstance(wire.get("marks"), dict) else {}
    clean = {k: float(v) for k, v in marks.items() if k in STAGES and isinstance(v, (int, float)) and not isinstance(v, bool)}
    existing = _open.get(wire["id"])
    if existing is not None:
        for k, v in clean.items():
            existing.marks.setdefault(k, v)
        existing.mark("core_in")
        return existing
    t = Trace(str(wire.get("source") or source), trace_id=wire["id"][:40], marks=clean)
    t.mark("core_in")
    _register(t)
    return t


def _register(t: Trace) -> None:
    now = time.time()
    for k in [k for k, v in _open.items() if now - v.created > OPEN_TTL_S]:
        _open.pop(k, None)
    _open[t.id] = t


# ── контекст ────────────────────────────────────────────────────────────────────

def bind(t: Trace | None):
    """Сделать трассу текущей для вызовов в этой задаче (contextvar копируется в созданные задачи)."""
    return _current.set(t)


def unbind(token) -> None:
    _current.reset(token)


def current() -> Trace | None:
    return _current.get()


def mark(stage: str, t: Trace | None = None) -> None:
    """Поставить метку текущей трассе. Без трассы ничего не делает (тесты, фоновые вызовы без обращения)."""
    t = t or _current.get()
    if t is not None:
        t.mark(stage)


# ── завершение ──────────────────────────────────────────────────────────────────

def expect_audio(t: Trace | None) -> None:
    """Реплика трассы поставлена в очередь речи."""
    if t is not None:
        t.pending += 1


def resolve_audio(t: Trace | None) -> None:
    """Реплика трассы сыграна или снята (очередь речи отдала решение)."""
    if t is not None:
        t.pending = max(0, t.pending - 1)
        _maybe_write(t)


def speak_sent(t: Trace | None) -> None:
    """Реплику отправили модулю голоса: запись ждёт его отметку звука (или AUDIO_WAIT_S)."""
    if t is not None:
        t.speak_sent = True


def finish_core(t: Trace | None) -> None:
    """Ядро закончило обращение: запись, если звук не ожидается."""
    if t is None:
        return
    t.core_done = True
    _maybe_write(t)


def audio_arrived(wire) -> None:
    """Модуль голоса прислал свои метки (`voice.trace`): tts_first_audio, play_start."""
    if not isinstance(wire, dict):
        return
    t = _open.get(str(wire.get("id") or ""))
    if t is None:
        return
    marks = wire.get("marks") if isinstance(wire.get("marks"), dict) else {}
    for k, v in marks.items():
        if k in STAGES and isinstance(v, (int, float)) and not isinstance(v, bool):
            t.marks.setdefault(k, float(v))
    t.audio = True
    _maybe_write(t)


def _maybe_write(t: Trace) -> None:
    if t.written or not t.core_done or t.pending > 0:
        return
    if t.speak_sent and not t.audio:
        if t.timer is None:
            try:
                t.timer = asyncio.get_running_loop().call_later(AUDIO_WAIT_S, _force, t.id)
            except RuntimeError:
                _write(t)              # нет цикла событий: пишем без ожидания звука
        return
    _write(t)


def _force(trace_id: str) -> None:
    t = _open.get(trace_id)
    if t is not None and not t.written:
        _write(t)


def _write(t: Trace) -> None:
    t.written = True
    if t.timer is not None:
        t.timer.cancel()
        t.timer = None
    _open.pop(t.id, None)
    origin = _origin(t)
    rel = {k: round(v - origin, 2) for k, v in t.marks.items()} if origin is not None else {}
    data = {"trace_id": t.id, "source": t.source, "marks": rel}
    if "play_start" in t.marks and origin is not None:
        data["end_to_sound_ms"] = round(t.marks["play_start"] - origin, 2)
    events_log.log_nowait("trace", t.source, **data)


def _origin(t: Trace) -> float | None:
    for k in ORIGIN_STAGES:
        if k in t.marks:
            return t.marks[k]
    return min(t.marks.values()) if t.marks else None


def open_count() -> int:
    return len(_open)


# ── сводка ──────────────────────────────────────────────────────────────────────

def percentile(values: list[float], p: float) -> float | None:
    """Порядковая статистика (ближний ранг): p в долях, 0.5 и 0.95."""
    if not values:
        return None
    s = sorted(values)
    return round(s[max(0, min(len(s) - 1, math.ceil(p * len(s)) - 1))], 2)


def _stats(values: list[float]) -> dict:
    return {"n": len(values), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}


def summarize(rows: list[dict]) -> dict:
    """Сводка по записям `trace` (events_log.tail, поле data): p50 и p95 по шагам и от конца фразы до звука.

    Шаг - время от предыдущего этапа, который есть в трассе, до этого этапа (в порядке STAGES). Отсутствующий этап
    в шаге не участвует."""
    steps: dict[str, list[float]] = {}
    at: dict[str, list[float]] = {}
    end_to_sound: list[float] = []
    by_source: dict[str, int] = {}
    n = 0
    for r in rows:
        d = r.get("data") if isinstance(r, dict) else None
        if not isinstance(d, dict) or not isinstance(d.get("marks"), dict):
            continue
        marks = {k: v for k, v in d["marks"].items() if k in STAGES and isinstance(v, (int, float))}
        if not marks:
            continue
        n += 1
        src = str(d.get("source") or "?")
        by_source[src] = by_source.get(src, 0) + 1
        prev = None
        for stage in STAGES:
            if stage not in marks:
                continue
            at.setdefault(stage, []).append(float(marks[stage]))
            if prev is not None:
                steps.setdefault(stage, []).append(float(marks[stage]) - float(marks[prev]))
            prev = stage
        if isinstance(d.get("end_to_sound_ms"), (int, float)):
            end_to_sound.append(float(d["end_to_sound_ms"]))
    return {
        "traces": n,
        "by_source": by_source,
        "steps": {s: _stats(steps[s]) for s in STAGES if s in steps},
        "at": {s: _stats(at[s]) for s in STAGES if s in at},
        "end_to_sound": _stats(end_to_sound),
    }


def latency_report(window_s: float = 3600.0) -> dict:
    """Сводка трасс за окно (дашборд GET /api/latency)."""
    rows = events_log.tail(limit=5000, kinds={"trace"}, since=time.time() - window_s)
    return {"window_s": window_s, **summarize(rows)}
