"""Речь в исходниках: нарезка на куски для распознавания, сборка расшифровки, поиск по ней (команды `scan`, `speech`, `find`).

Распознаёт общий контейнер stt (stt_client.py), у него есть только текст куска без времён слов. Поэтому время куска речи это
время сегмента, а внутри куска положение слова только оценивается по его месту в тексте (и так и подписывается).
Чистые функции без ffmpeg и сети: тесты кормят их готовыми рядами.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

from moments import clock, short_text
from stt_client import PCM_BYTES_PER_S, SttUnavailable

CHUNK_MAX_S = 25.0               # длиннее не режем в stt: короткие куски распознаются точнее и быстрее
CHUNK_MIN_S = 0.4                # короче stt всё равно не берёт (0,2 с) и выдаёт мусор
CHUNK_PAD_S = 0.15
MAX_CHUNKS = 400                 # потолок на видео (около 100 минут речи), чтобы часы фона не уходили в stt
CONCURRENCY = 2
SPEECH_LINES, SPEECH_CHARS = 60, 200
FIND_LIMIT = 15
FIND_PAD_MS = 4000


def speech_spans(silences: list, duration_s: float, min_len: float = 0.3) -> list[tuple[float, float]]:
    """Участки между тишинами (silencedetect): там возможна речь. Нет тишин: весь файл одним участком."""
    spans, cursor = [], 0.0
    for start, end in sorted((float(a), float(b)) for a, b in silences):
        if start - cursor >= min_len:
            spans.append((cursor, start))
        cursor = max(cursor, end)
    if duration_s - cursor >= min_len:
        spans.append((cursor, duration_s))
    return spans


def _quiet_cut(rms_db: list, lo: float, hi: float) -> float:
    """Где резать длинный участок без пауз: самая тихая секунда в окне [lo, hi] (вторая половина длины куска)."""
    secs = range(int(lo), max(int(lo) + 1, int(hi)))
    if not rms_db:
        return hi
    best = min(secs, key=lambda s: (-90.0 if s >= len(rms_db) or rms_db[s] is None else rms_db[s], -s))   # при равенстве позже: меньше кусков
    return min(hi, max(lo, best + 0.5))


def split_chunks(spans: list[tuple[float, float]], duration_s: float, rms_db: list | None = None, *,
                 max_len: float = CHUNK_MAX_S, min_len: float = CHUNK_MIN_S, pad: float = CHUNK_PAD_S,
                 max_chunks: int = MAX_CHUNKS) -> list[tuple[int, int]]:
    """Куски для stt `[(начало_мс, конец_мс)]` не длиннее `max_len`: участки между паузами склеиваются, пока куска хватает, а
    слишком длинный участок режется в самом тихом месте. Паузы остаются границами кусков."""
    parts: list[tuple[float, float]] = []
    for start, end in spans:
        while end - start > max_len:
            cut = _quiet_cut(rms_db or [], start + max_len * 0.5, start + max_len)
            parts.append((start, cut))
            start = cut
        parts.append((start, end))
    chunks: list[tuple[float, float]] = []
    for start, end in parts:
        if chunks and end - chunks[-1][0] <= max_len:
            chunks[-1] = (chunks[-1][0], end)
        else:
            chunks.append((start, end))
    out = []
    for start, end in chunks:
        a, b = max(0.0, start - pad), min(duration_s, end + pad)
        if end - start >= min_len:
            out.append((int(a * 1000), int(b * 1000)))
    return out[:max_chunks]


async def transcribe_chunks(chunks: list[tuple[int, int]], read_pcm: Callable[[int, int], bytes],
                            transcribe: Callable[[bytes], Awaitable[str]], *, concurrency: int = CONCURRENCY,
                            on_progress: Callable[[float], None] | None = None) -> tuple[list[dict], bool]:
    """Куски в stt не больше `concurrency` одновременно. Возвращает (речь `[{start_ms, end_ms, text}]` по времени, сервис недоступен).
    Пустой текст (мусор, тишина) кусок пропускает. Сервис отказал: остальные куски не шлются, уже распознанное сохраняется."""
    results: list[str | None] = [None] * len(chunks)
    sem = asyncio.Semaphore(concurrency)
    state = {"down": False, "done": 0}

    async def one(i: int, chunk: tuple[int, int]) -> None:
        async with sem:
            if state["down"]:
                return
            pcm = await asyncio.to_thread(read_pcm, *chunk)
            try:
                results[i] = await transcribe(pcm)
            except SttUnavailable:
                state["down"] = True
                return
            state["done"] += 1
            if on_progress is not None:
                on_progress(state["done"] / len(chunks))

    await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks)))
    speech = [{"start_ms": a, "end_ms": b, "text": " ".join(t.split())}
              for (a, b), t in zip(chunks, results) if t and t.strip()]
    return speech, state["down"]


def pcm_slice_bounds(start_ms: int, end_ms: int) -> tuple[int, int]:
    """Байты PCM куска в файле s16le 16 кГц: начало и длина (чётные: сэмпл два байта)."""
    return (start_ms * PCM_BYTES_PER_S // 1000) // 2 * 2, ((end_ms - start_ms) * PCM_BYTES_PER_S // 1000) // 2 * 2


# ---------------------------------------------------------------------------------------------------- показ и поиск

def speech_lines(speech: list[dict], from_ms: int | None = None, to_ms: int | None = None, *, max_lines: int = SPEECH_LINES,
                 max_chars: int = SPEECH_CHARS) -> tuple[list[str], int]:
    """Строки `[12:05–12:11] текст` по времени для диапазона; возвращает (строки, сколько ещё не показано)."""
    rows = [s for s in sorted(speech, key=lambda s: s["start_ms"])
            if (from_ms is None or s["end_ms"] >= from_ms) and (to_ms is None or s["start_ms"] <= to_ms)]
    lines = [f"[{clock(s['start_ms'])}–{clock(s['end_ms'])}] {short_text(s['text'], max_chars)}" for s in rows[:max_lines]]
    return lines, max(0, len(rows) - max_lines)


_WORD = re.compile(r"[\w]+", re.U)


def stem(word: str) -> str:
    """Основа слова для сравнения без окончаний: слово от 4 букв берётся без последних двух (но не короче 4 букв), короткое целиком.
    Без внешних библиотек: «ракета», «ракету», «ракеты» дают одну основу."""
    w = word.casefold().replace("ё", "е")
    return w if len(w) < 4 else w[:max(4, len(w) - 2)]


def tokens(text: str) -> list[str]:
    return [t.casefold().replace("ё", "е") for t in _WORD.findall(text)]


def _word_matches(query_word: str, token: str) -> bool:
    q = query_word.casefold().replace("ё", "е")
    return token == q if len(q) < 4 else token.startswith(stem(q))


def find_in_speech(speech: list[dict], query: str, duration_s: float, *, limit: int = FIND_LIMIT) -> list[dict]:
    """Куски речи, где встречаются все слова запроса (без регистра и окончаний). Для каждого: оценка времени `at_ms`
    по положению первого найденного слова в тексте (слов-времён нет!) и окно in/out вокруг неё ±4 с. Точная фраза выше россыпи слов."""
    words = tokens(query)
    if not words:
        return []
    found = []
    for seg in speech:
        toks = tokens(seg["text"])
        pos = []
        for w in words:
            idx = next((i for i, t in enumerate(toks) if _word_matches(w, t)), None)
            if idx is None:
                break
            pos.append(idx)
        else:
            adjacent = len(words) > 1 and all(b - a == 1 for a, b in zip(pos, pos[1:]))
            at = seg["start_ms"] + int((seg["end_ms"] - seg["start_ms"]) * (min(pos) + 0.5) / max(1, len(toks)))
            found.append({"at_ms": at, "start_ms": seg["start_ms"], "end_ms": seg["end_ms"], "text": seg["text"],
                          "in_ms": max(0, at - FIND_PAD_MS), "out_ms": min(int(duration_s * 1000) or at + FIND_PAD_MS, at + FIND_PAD_MS),
                          "_rank": (0 if adjacent or len(words) == 1 else 1, seg["start_ms"])})
    found.sort(key=lambda f: f["_rank"])
    for f in found:
        del f["_rank"]
    return found[:limit]


def find_line(hit: dict) -> str:
    """`источник [12:05–12:31] in=… out=… (примерно at=…): «строка»`; время внутри куска оценено, не измерено."""
    return (f"{hit['source']} [{clock(hit['start_ms'])}–{clock(hit['end_ms'])}] in={hit['in_ms']} out={hit['out_ms']} "
            f"(~at={hit['at_ms']}): «{short_text(hit['text'], SPEECH_CHARS)}»")
