"""Файл субтитров .srt для роликов, в которые субтитры не вжигаются (решение владельца 2026-10-11: субтитры в кадре только у
вертикальных коротких роликов; у горизонтальных, например youtube, рядом с финалом лежит .srt из тех же слов).

Страницы строятся тем же алгоритмом, что вжигаемые субтитры (`qa.caption_pages`, порт `template/src/timeline.ts`): слова
расшифровки или журнала речи по сегментам плана и текст закадра по его длительности.
"""
from __future__ import annotations

import copy

import qa


def _stamp(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def build_srt(plan: dict, words: list[dict], zones: dict) -> str:
    """Текст .srt. Пустая строка: нечего субтитровать (нет ни слов, ни закадра)."""
    forced = copy.deepcopy(plan)
    forced["captions"] = {**(forced.get("captions") or {}), "enabled": True, "source": "both" if words else "voiceover"}
    pages = qa.caption_pages(forced, words, zones)
    windows = []
    for k, p in enumerate(pages):
        nxt = pages[k + 1]["start"] if k + 1 < len(pages) else None
        end = p["end"] + 150
        if nxt is not None and nxt - p["end"] < 700:
            end = max(p["end"], nxt)
        windows.append((p["start"], min(end, plan["duration_ms"])))
    rows = []
    for n, (p, (a, b)) in enumerate(zip(pages, windows, strict=True), 1):
        text = " ".join(w["text"] for w in p["words"])
        if text.strip() and b > a:
            rows.append(f"{n}\n{_stamp(a)} --> {_stamp(b)}\n{text}\n")
    return "\n".join(rows)
