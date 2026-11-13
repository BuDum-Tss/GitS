"""Слова для субтитров (ТЗ 20, раздел 10).

Распознавание живой речи (WhisperX: MON-25…MON-27, MON-29) отложено владельцем («не переусложнять»): `transcribe_live`
отвечает `unavailable`. Главный путь первого видео — MON-26a: субтитры из журнала речи без распознавания. Текст и время её
реплик берутся из журнала агента игры (события `say` протокола `events_log`) и, если есть, из событий голосового модуля
`voice.speech` (`segment`/`ended`: время каждого предложения). Время слов внутри реплики делится пропорционально длине слов
в символах (текст известен точно, приближение допустимо). Сдвиг относительно видео считается от времени начала записи
(`started_at`). Реплика, которой нет в звуке записи (глушилка, сбой), субтитра не получает.

Результат — `words.json` того же вида, что у WhisperX: `[{"i", "text", "start_ms", "end_ms", "conf"}]`. Отступление от
ТЗ: поле `source: "speech_log"` из MON-26a в самом файле не пишется. Шаблон Remotion (`template/src/schema.ts`) понимает
`source` слова как путь исходника-сегмента, и слово с `source: "speech_log"` не нашло бы своего сегмента и пропало бы.
Происхождение слов записано в `transcript/meta.json` (`origin: "speech_log"`), а `source` слова ставится только когда у
записи несколько видеофайлов.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from gits_sdk import ToolError

import media

CHAR_MS = 62                 # мс на символ при оценке длительности реплики (как в шаблоне: voiceoverWords)
MIN_UTTERANCE_MS = 800
SILENCE_FLOOR_DB = -55.0     # окно тише этого в звуке записи считается беззвучным (реплики в звуке нет)
MIN_WORD_MS = 80
GAP_AFTER_MS = 40            # зазор между последним словом реплики и началом следующей


def transcribe_live(*_args, **_kw):
    """Расшифровка живой речи людей не включена в этой версии модуля (WhisperX отложен владельцем)."""
    raise ToolError("распознавание живой речи не включено в этой версии модуля (WhisperX отложен). Субтитры работают для "
                    "закадра и записей, где говорит только она (журнал речи)", code="unavailable")


@dataclass
class Utterance:
    start_ts: float                  # unix-время начала реплики
    text: str
    end_ts: float | None = None      # unix-время конца, если известно (голосовой модуль)


def load_speech_log(path: Path) -> list[Utterance]:
    """Журнал речи -> реплики по времени. Формат: список записей `{"ts", "kind", "text", "data"}` (выгрузка `events_log`) или
    объект `{"lines": [...]}`. Понимаются `kind: "say"` (текст и время реплики) и `kind: "voice.speech"` с
    `data.phase` `segment`/`ended` (время каждого предложения точнее). Если в журнале есть `voice.speech`, одноимённые `say`
    пропускаются."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("lines", []) if isinstance(raw, dict) else raw
    rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("ts"), (int, float))]
    segments, ends, says = [], [], []
    for r in rows:
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        kind = r.get("kind")
        text = str(r.get("text") or data.get("text") or "").strip()
        if kind == "voice.speech":
            phase = data.get("phase")
            if phase == "segment" and text:
                segments.append((float(r["ts"]), text, data.get("utterance_id")))
            elif phase == "ended":
                ends.append((float(r["ts"]), data.get("utterance_id")))
        elif kind in (None, "say") and text:
            says.append(Utterance(float(r["ts"]), text, float(r["end_ts"]) if isinstance(r.get("end_ts"), (int, float)) else None))
    if not segments:
        return sorted(says, key=lambda u: u.start_ts)
    out: list[Utterance] = []
    segments.sort()
    for k, (ts, text, uid) in enumerate(segments):
        end = None
        later = [e for e, u in ends if e >= ts and (uid is None or u in (None, uid))]
        if k + 1 < len(segments) and (not later or segments[k + 1][0] < min(later)):
            end = segments[k + 1][0]
        elif later:
            end = min(later)
        out.append(Utterance(ts, text, end))
    spoken = {re.sub(r"\W+", " ", u.text).strip().lower() for u in out}
    out += [u for u in says if not any(s and s in re.sub(r"\W+", " ", u.text).strip().lower() for s in spoken)]
    return sorted(out, key=lambda u: u.start_ts)


def estimate_ms(text: str) -> int:
    return max(MIN_UTTERANCE_MS, len(text) * CHAR_MS)


Audible = Callable[[float, float], Awaitable[bool]]


async def words_from_speech_log(lines: list[Utterance], started_at: float, duration_s: float, audible: Audible | None,
                                *, source: str | None = None) -> tuple[list[dict], dict]:
    """Слова для `words.json` и сводка. `audible(start_s, dur_s)` отвечает, слышна ли реплика в звуке записи (None: считать
    слышимой). Время слов — в миллисекундах видео записи."""
    words: list[dict] = []
    skipped = {"outside": 0, "silent": 0}
    ordered = sorted(lines, key=lambda u: u.start_ts)
    for k, u in enumerate(ordered):
        start_s = u.start_ts - started_at
        if start_s < 0 or start_s >= duration_s:
            skipped["outside"] += 1
            continue
        dur_ms = int((u.end_ts - u.start_ts) * 1000) if u.end_ts and u.end_ts > u.start_ts else estimate_ms(u.text)
        if k + 1 < len(ordered):                                       # следующая реплика не накладывается на эту
            room = int((ordered[k + 1].start_ts - u.start_ts) * 1000) - GAP_AFTER_MS
            if room >= MIN_WORD_MS * max(1, len(u.text.split())):
                dur_ms = min(dur_ms, room)
        dur_ms = max(MIN_UTTERANCE_MS // 4, min(dur_ms, int((duration_s - start_s) * 1000)))
        if audible is not None and not await audible(start_s, dur_ms / 1000):
            skipped["silent"] += 1
            continue
        parts = re.findall(r"\S+", u.text)
        if not parts:
            continue
        weights = [len(p) + 1 for p in parts]
        total = sum(weights)
        cursor = start_s * 1000
        for part, weight in zip(parts, weights):
            span = dur_ms * weight / total
            word = {"i": len(words), "text": part, "start_ms": round(cursor), "end_ms": max(round(cursor + span), round(cursor) + MIN_WORD_MS),
                    "conf": None}
            if source:
                word["source"] = source
            words.append(word)
            cursor += span
    return words, {"origin": "speech_log", "utterances": len(ordered), "words": len(words), "skipped": skipped}


def make_audible(parts: list[tuple[Path, float, float]]) -> Audible:
    """Проверка «реплика есть в звуке записи»: окно покрыто куском звука хотя бы наполовину и в нём не цифровой ноль.
    `parts` — [(файл, смещение начала от начала видео, секунд)]."""
    async def check(start_s: float, dur_s: float) -> bool:
        for path, offset, seconds in parts:
            lo, hi = max(start_s, offset), min(start_s + dur_s, offset + seconds)
            if hi - lo < dur_s * 0.5:
                continue
            if not path.is_file():
                continue
            level = await media.max_level(path, lo - offset, hi - lo)
            return level is not None and level > SILENCE_FLOOR_DB
        return False
    return check


def words_between(words: list[dict], from_ms: int, to_ms: int, limit: int = 400) -> list[dict]:
    """Слова с таймингами в отрезке (инструмент агента `transcript_words`): не весь файл, не больше `limit`."""
    return [w for w in words if w["end_ms"] > from_ms and w["start_ms"] < to_ms][:limit]
