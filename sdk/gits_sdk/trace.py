"""Метки трассы задержки в модуле (ТЗ 24, ТЗ 31 MC-4): `now_ms()` и `wire_new()`.

Модуль ставит метки своих этапов (`mic_end`, `stt_done`, ...) и кладёт трассу в поле `trace` события или команды; разбирает,
складывает и записывает трассы ядро (`agent/latency_trace.py`). Формат один: `{id, source, marks}`, метки - миллисекунды
`time.perf_counter()` (общий счётчик процессов на одной машине). Тест `agent/tests/test_contract_modules.py` сверяет формат
с `latency_trace`.
"""
from __future__ import annotations

import time
import uuid

STAGES = ("mic_end", "stt_done", "core_in", "classify_done", "route", "retrieve_done", "llm_first_token", "llm_done",
          "first_sentence_ready", "tts_first_audio", "play_start")


def now_ms() -> float:
    return time.perf_counter() * 1000.0


def wire_new(source: str, marks: dict | None = None) -> dict:
    """Трасса для поля `trace` события: id, источник и метки (первые три знака после запятой)."""
    return {"id": uuid.uuid4().hex[:12], "source": str(source or "unknown")[:40],
            "marks": {k: round(float(v), 3) for k, v in (marks or {}).items()}}


def mark(wire: dict, stage: str, at: float | None = None) -> None:
    """Поставить метку в трассу модуля; первая метка побеждает."""
    marks = wire.setdefault("marks", {})
    if stage not in marks:
        marks[stage] = round(now_ms() if at is None else at, 3)
