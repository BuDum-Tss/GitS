"""Моменты длинных исходников: ранжирование по сигналам, подбор из кэша, текст для агента (команды `scan`, `moments`, `peek`).

Агент-монтажёр картинок не видит, поэтому интересное место ищется по сигналам: громкий пик, резкая смена кадра, тишина перед
громким. Главное здесь звук (большая часть оценки), смены кадра только добавка: так делают и готовые инструменты нарезки
(auto-editor, PySceneDetect). Здесь только чистые функции без ffmpeg и диска: их тестируют на готовых рядах.
"""
from __future__ import annotations

import difflib
import re

WINDOW_S = 6                    # окно оценки момента
MIN_GAP_S = 10                  # минимум между пиками двух моментов
TOP_N = 60                      # сколько моментов хранится на видео
PAD_BEFORE_MS, PAD_AFTER_MS = 4000, 4000     # запас вокруг пика: окно 6 с и по секунде на разгон и затухание
MIN_SCORE = 0.05
W_PEAK, W_CONTRAST, W_ACTIVITY = 0.45, 0.25, 0.30      # звук 0,7, картинка 0,3
W_SPEECH, W_LONG_PHRASE = 0.10, 0.05                    # бонус окну с речью и за длинную фразу в нём
SPEECH_MIN_S = 2.0                                       # сколько секунд речи в окне, чтобы считать его «с речью»
LONG_PHRASE_WORDS = 6
TEXT_FRAGMENT = 60
QUIET_LEVEL = 0.15              # «тихо» по нормированной громкости
CACHE_VERSION = 1
DEFAULT_LIMIT, MAX_LIMIT = 15, 40


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(q * (len(ordered) - 1) + 0.5)))] if ordered else 0.0


def normalized_loudness(rms_db: list) -> list[float]:
    """Громкость по секундам 0..1: от тихого фона (10-й процентиль) до пика (99-й). Ровный звук без перепадов даёт нули:
    выделять в нём нечего. Пропуски (нет данных) считаются тишиной."""
    vals = [-90.0 if v is None else float(v) for v in rms_db]
    if not vals:
        return []
    lo, hi = _percentile(vals, 0.10), _percentile(vals, 0.99)
    if hi - lo < 3.0:
        return [0.0] * len(vals)
    return [min(1.0, max(0.0, (v - lo) / (hi - lo))) for v in vals]


def _clip(x: float) -> float:
    return min(1.0, max(0.0, x))


def rank_moments(analysis: dict, duration_s: float, *, window_s: int = WINDOW_S, min_gap_s: int = MIN_GAP_S,
                 top: int = TOP_N) -> list[dict]:
    """Кандидаты-моменты видео по рядам анализа (`rms_db`, `motion`, `cuts`, `silences`, по секундам).

    Для каждой секунды окно `window_s` вокруг неё оценивается: пик громкости, контраст пика с тем, что было секунд за четыре до
    него («тишина перед громким»), и активность картинки (смены кадра и их сила). Потом жадно берутся лучшие с подавлением соседей:
    пики ближе `min_gap_s` секунд друг к другу не берутся. Без звука оценку даёт только картинка.
    """
    rms = analysis.get("rms_db") or []
    n = max(len(rms), len(analysis.get("motion") or []), len(analysis.get("cuts") or []))
    if n == 0 or duration_s <= 0:
        return []
    has_audio = bool(analysis.get("has_audio", any(v is not None for v in rms)))
    loud = normalized_loudness(rms) if has_audio else [0.0] * n
    loud += [0.0] * (n - len(loud))
    motion = list(analysis.get("motion") or []) + [0.0] * n
    cuts = list(analysis.get("cuts") or []) + [0] * n
    m_hi = max(_percentile(motion[:n], 0.95), 0.05)           # движение нормируется по самому видео, а не по общему порогу
    half = window_s // 2
    ends = [e for _, e in (analysis.get("silences") or [])]
    cover, seg_of = _speech_cover(analysis.get("speech") or [], n)
    cand: list[tuple[float, int, dict]] = []
    for c in range(n):
        a, b = max(0, c - half), min(n, c + half)
        peak_i = max(range(a, b), key=lambda i: loud[i])
        peak = loud[peak_i]
        # центр момента: громкий пик, а где звук ровный (или его нет), самое активное по картинке место окна
        at_i = max(range(a, b), key=lambda i: loud[i] + 0.4 * (min(1.0, cuts[i]) * 0.6 + min(1.0, motion[i] / m_hi) * 0.4))
        before = loud[max(0, peak_i - 4):peak_i]
        contrast = _clip(peak - sum(before) / len(before)) if before else 0.0
        n_cuts = sum(cuts[a:b])
        activity = _clip(0.6 * min(1.0, n_cuts / 2) + 0.4 * _clip(sum(motion[a:b]) / max(1, b - a) / m_hi))
        score = ((W_PEAK * peak + W_CONTRAST * contrast + W_ACTIVITY * activity) if has_audio else activity) * (1 - W_SPEECH - W_LONG_PHRASE)
        tags = []
        text = ""
        if sum(cover[a:b]) >= SPEECH_MIN_S:
            seg = next((analysis["speech"][seg_of[i]] for i in range(a, b) if seg_of[i] >= 0), None)
            score += W_SPEECH
            tags.append("речь")
            if seg is not None:
                text = str(seg.get("text") or "")
                if len(text.split()) >= LONG_PHRASE_WORDS:
                    score += W_LONG_PHRASE
                    tags.append("реплика")
        score = min(1.0, score)
        if peak >= 0.6:
            tags.append("громко")
        if n_cuts >= 1:
            tags.append("смена кадра")
        quiet_before = bool(before) and sum(before) / len(before) <= QUIET_LEVEL
        silence_ended = any(a <= e <= peak_i + 1 for e in ends) and peak >= 0.4
        if has_audio and ((contrast >= 0.45 and quiet_before) or silence_ended):
            tags.append("резкая тишина")
        if motion_mean_hi(motion[a:b], m_hi):
            tags.append("движение")
        cand.append((score, at_i, {"tags": tags, "text": text}))
    cand.sort(key=lambda x: (-x[0], x[1]))
    picked: list[dict] = []
    for score, peak_i, extra in cand:
        if len(picked) >= top or score < MIN_SCORE:
            break
        if any(abs(peak_i - p["_sec"]) < min_gap_s for p in picked):
            continue
        at_ms = peak_i * 1000 + 500
        in_ms = max(0, at_ms - PAD_BEFORE_MS)
        out_ms = min(int(duration_s * 1000), at_ms + PAD_AFTER_MS)
        if out_ms - in_ms < 1000:                              # пик у самого края короткого файла
            continue
        picked.append({"_sec": peak_i, "at_ms": at_ms, "in_ms": in_ms, "out_ms": out_ms, "score": round(score, 2),
                       "tags": extra["tags"], "text": short_text(extra["text"], TEXT_FRAGMENT)})
    for p in picked:
        del p["_sec"]
    return picked


def _speech_cover(speech: list[dict], n: int) -> tuple[list[float], list[int]]:
    """По секундам: доля секунды, занятая речью, и номер куска речи (-1: речи нет)."""
    cover, seg_of = [0.0] * n, [-1] * n
    for k, seg in enumerate(speech):
        a_ms, b_ms = int(seg.get("start_ms", 0)), int(seg.get("end_ms", 0))
        for sec in range(max(0, a_ms // 1000), min(n, b_ms // 1000 + 1)):
            part = min(b_ms, (sec + 1) * 1000) - max(a_ms, sec * 1000)
            if part > 0:
                cover[sec] += part / 1000
                if seg_of[sec] < 0:
                    seg_of[sec] = k
    return [min(1.0, c) for c in cover], seg_of


def short_text(text: str, limit: int) -> str:
    """Текст не длиннее `limit` символов, обрезанный по слову, с многоточием."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def motion_mean_hi(window: list[float], m_hi: float) -> bool:
    return bool(window) and sum(window) / len(window) / m_hi >= 0.5


def assign_ids(per_source: list[tuple[str, list[dict]]]) -> list[dict]:
    """Общий список моментов с id m1... в порядке видео и ранга."""
    out, k = [], 1
    for name, items in per_source:
        for it in items:
            out.append({"id": f"m{k}", "source": name, **it})
            k += 1
    return out


def clock(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def select_moments(moments: list[dict], *, source: str | None = None, limit: int = DEFAULT_LIMIT, from_ms: int | None = None,
                   to_ms: int | None = None) -> list[dict]:
    """Лучшие моменты по убыванию оценки; фильтр по источнику и по времени пика (от и до, мс). Равные оценки: по времени."""
    limit = max(1, min(MAX_LIMIT, limit))
    found = [m for m in moments
             if (source is None or m["source"] == source) and (from_ms is None or m["at_ms"] >= from_ms)
             and (to_ms is None or m["at_ms"] <= to_ms)]
    found.sort(key=lambda m: (-m["score"], m["source"], m["at_ms"]))
    return found[:limit]


def moment_line(m: dict) -> str:
    """`m7 имя.mp4 [12:05–12:11] in=725000 out=731000 at=728500 0.82 громко, смена кадра`."""
    return (f"{m['id']} {m['source']} [{clock(m['in_ms'])}–{clock(m['out_ms'])}] in={m['in_ms']} out={m['out_ms']} at={m['at_ms']} "
            f"{m['score']:.2f} {', '.join(m.get('tags') or []) or 'без примет'}"
            + (f" «{m['text']}»" if m.get("text") else ""))


def loudness_around(analysis: dict, t_ms: int, radius_s: int = 3) -> list[tuple[int, float | None]]:
    """Уровни громкости (дБ) по секундам вокруг времени: [(секунда, дБ)]; секунд вне файла нет."""
    rms = analysis.get("rms_db") or []
    sec = t_ms // 1000
    return [(s, rms[s]) for s in range(max(0, sec - radius_s), min(len(rms), sec + radius_s + 1))]


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def merge_texts(texts: list[str], similarity: float = 0.85) -> list[str]:
    """Тексты с соседних кадров без дублей: пустые и (почти) повторы убираются, порядок сохраняется."""
    out: list[str] = []
    kept: list[str] = []
    for t in texts:
        key = _norm(t)
        if not key:
            continue
        if any(key == k or difflib.SequenceMatcher(None, key, k, autojunk=False).ratio() >= similarity for k in kept):
            continue
        kept.append(key)
        out.append(" ".join(t.split()))
    return out
