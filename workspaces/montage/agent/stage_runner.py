"""Общее для исполнителей стадий агента монтажа (ТЗ 20, раздел 8, MON-15, MON-16, MON-17).

Исполнитель стадии это `AgentRunner` (сейчас `pi_runner.PiRunner`; заглушка для тестов в agent/tests/stubs.py): он выполняет одну
стадию агента и возвращает `StageResult`. Здесь общее для них: описания своих инструментов, итог стадии, ошибка стадии,
системная подсказка и пересказ журнала для новой сессии.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from agent_tools import JobEnv
from layout import Layout

log = logging.getLogger("montage.runner")

STAGE_TIMEOUT_S = 40 * 60


# свои инструменты агента: описания и JSON-схемы в `pi_runner`
OWN_TOOL_DESCRIPTIONS = {
    "ask_user": "Задать вопрос пользователю (через ядро) и получить ответ. Только на стадиях clarify (до 2 раз) и build "
                "(1 раз). kind: choice (с options) или text.",
    "progress": "Сообщить ход работы: progress от 0 до 1 внутри стадии и короткий текст-факт.",
    "synthesize_voice": "Синтезировать реплику её голосом в work/public/vo/<id>.wav. Возвращает duration_ms. "
                        "Единственный способ получить закадр.",
    "transcript_words": "Слова расшифровки с таймингами в отрезке from_ms..to_ms (не весь файл).",
}
class StageFailed(Exception):
    """Агент не смог выполнить стадию (сообщение для журнала, без трассировки)."""


@dataclass
class StageResult:
    text: str = ""
    tokens: int = 0
    session_id: str | None = None
    turns: int = 0
    hit_turn_limit: bool = False


class AgentRunner(Protocol):
    async def run(self, env: JobEnv, stage: str, prompt: str, *, model: str, max_turns: int, readonly: bool = False,
                  fresh: bool = False) -> StageResult: ...


def build_system_prompt(layout: Layout, persona_card: str) -> str:
    """editor_system.md + карточка персонажа (core.persona) + регламент стиля + руководство ремесла (MON-15)."""
    parts = [(layout.prompts / "editor_system.md").read_text(encoding="utf-8").replace("@@TEMPLATE@@", str(layout.template).replace("\\", "/"))]
    if persona_card.strip():
        parts.append("# Карточка персонажа (core.persona)\n\n" + persona_card.strip())
    parts.append("# Регламент стиля (resources/<Имя>/montage/APERTURE_STYLE.md)\n\n" + layout.style.read_text(encoding="utf-8"))
    parts.append("# Руководство по ремеслу (docs/EDITING_CRAFT.md)\n\n" + (layout.docs / "EDITING_CRAFT.md").read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def recap_from_log(rows: list[dict]) -> str:
    """Краткий пересказ хода задачи из log.jsonl для новой сессии, когда старая недоступна (MON-8)."""
    lines = []
    for r in rows:
        if r.get("kind") in ("stage", "answer", "revision", "agent_done"):
            lines.append("- " + ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("ts",) and v not in (None, ""))[:240])
    return "\n".join(lines[-30:])


def _short(e: BaseException) -> str:
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return text[:240]
