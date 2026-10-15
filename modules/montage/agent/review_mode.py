"""Режим приёмки и согласие автопроверки с человеком (ТЗ 20, MON-QA-5, MON-QA-6).

Журнал `jobs/qa_verdicts.jsonl` (вне папок задач) копит решения человека на `plan_approval` и `review` вместе с итогами
автоматических проверок на тот момент. Согласие считается по решениям `review`: автопроверка «pass» совпадает с человеком
«готово» без правок, автопроверка «fix» — с «правки». Режим `auto` включается только при ≥ 20 решений и согласии ≥ 90 %.
"""
from __future__ import annotations

import time

MODES = ("human", "auto_then_human", "auto")
AUTO_MIN_DECISIONS = 20
AUTO_MIN_AGREEMENT = 0.90
AUTO_MIN_OVERALL = 4


def verdict_record(job_id: str, stage: str, answer: str, notes: str | None, qa: dict | None) -> dict:
    """Запись для `qa_verdicts.jsonl`: job_id, стадия, ответ человека, его правки, итоги автопроверок на этот момент."""
    return {"ts": round(time.time(), 3), "job_id": job_id, "stage": stage, "answer": answer, "notes": notes or "", "qa": qa or {}}


def agreement(verdicts: list[dict]) -> dict:
    """{"decisions": N, "agreed": M, "share": M/N}: по решениям на стадии review, где автопроверка что-то вынесла."""
    rows = [v for v in verdicts if v.get("stage") == "review" and (v.get("qa") or {}).get("passed") is not None]
    agreed = 0
    for v in rows:
        human_ok = v.get("answer") == "готово"
        if bool(v["qa"]["passed"]) == human_ok:
            agreed += 1
    n = len(rows)
    return {"decisions": n, "agreed": agreed, "share": (agreed / n) if n else 0.0}


def agreement_text(stats: dict) -> str:
    """Факт для state: «согласие с вами: 17/20, 85 %»."""
    if not stats["decisions"]:
        return "согласие с вами: решений ещё нет"
    return f"согласие с вами: {stats['agreed']}/{stats['decisions']}, {round(stats['share'] * 100)} %"


def auto_allowed(stats: dict) -> tuple[bool, str]:
    """Можно ли включать `auto`: (да/нет, причина с цифрами)."""
    if stats["decisions"] < AUTO_MIN_DECISIONS:
        return False, (f"решений человека {stats['decisions']} из {AUTO_MIN_DECISIONS} нужных для режима auto; "
                       f"{agreement_text(stats)}")
    if stats["share"] < AUTO_MIN_AGREEMENT:
        return False, f"{agreement_text(stats)}, для режима auto нужно не меньше {round(AUTO_MIN_AGREEMENT * 100)} %"
    return True, agreement_text(stats)


def effective_mode(requested: str, stats: dict) -> tuple[str, str | None]:
    """Режим, который действует на самом деле. Запрошенный `auto` без права на него превращается в `auto_then_human`;
    второе значение — причина (с цифрами) или None."""
    if requested not in MODES:
        return "human", f"неизвестный режим «{requested}»"
    if requested == "auto":
        ok, why = auto_allowed(stats)
        if not ok:
            return "auto_then_human", why
    return requested, None


def skip_human_review(mode: str, qa_summary: dict) -> bool:
    """`auto`: финал без вопроса, если технических ошибок нет и смысловая оценка не ниже 4."""
    return (mode == "auto" and qa_summary.get("technical_errors") == 0
            and (qa_summary.get("semantic_overall") or 0) >= AUTO_MIN_OVERALL and qa_summary.get("semantic_verdict") == "pass")
