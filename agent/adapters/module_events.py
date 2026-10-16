"""Адаптер контракта `EventSink` (ТЗ 29, ARC-7): события модулей идут в менеджер внимания, эмоции в быструю модель и
присутствие. Единственное место, где шина модулей встречается с `attention` и `fast_brain`."""
from typing import Any

import fast_brain
from gits_sdk import protocol as P


class AttentionEventSink:
    def __init__(self, attention, presence=None):
        self.attention = attention
        self.presence = presence

    async def submit(self, module_id: str, event: dict) -> None:
        await self.attention.submit(module_id, event)

    async def submit_speech(self, module_id: str, say: dict) -> None:
        await self.attention.submit_speech(module_id, say)

    def on_emotion(self, module_id: str, emotion: Any) -> bool:
        """False: эмоции нет в списке быстрой модели (шина пишет предупреждение)."""
        if emotion not in fast_brain.EMOTIONS:
            return False
        fast_brain.publish(f"module:{module_id}", {"emotion": emotion, "action": "none", "source": module_id})
        if self.presence is not None:
            self.presence.update(emotion=emotion)
        return True

    async def on_job_update(self, module_id: str, job: dict) -> None:
        await self.attention.on_job_update(module_id, job)

    async def submit_question(self, module_id: str, job_id: str, question_id: str, question: dict) -> tuple[Any, str]:
        from attention import Question
        q = Question(kind=question["kind"], question=question["question"], options=question.get("options"),
                     timeout_s=question.get("timeout_s", P.ASK_TIMEOUT_DEFAULT), source=module_id,
                     context={"job_id": job_id, **(question.get("context") or {})},
                     urgency_hint=question.get("urgency_hint"), job_id=job_id, question_id=question_id)
        return await self.attention.submit_question(q)
