"""Сборка конвейера из заглушек и автоответчик на вопросы (для тестов)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from jobs import JobStore
from pipeline import Pipeline
from tests.stubs import FakeRenderer, FakeRunner, StubCore, StubQA, layout_for


class Rig:
    def __init__(self, tmp: Path, settings: dict | None = None, runner=None, qa=None, renderer=None):
        self.tmp = tmp
        self.layout = layout_for(tmp)
        self.store = JobStore(self.layout.jobs)
        self.core = StubCore(tmp, {"output_dir": str(tmp / "out"), **(settings or {})})
        self.runner = runner or FakeRunner()
        self.renderer = renderer or FakeRenderer()
        self.qa = qa or StubQA()
        self.pipeline = Pipeline(self.store, self.core, self.runner, self.layout, self.renderer, self.qa)

    def new_job(self, **request):
        req = {"sources": [], "brief": "20 секунд: ГЛаДОС объясняет подопытному правило безопасности", "source_kind": "narrated",
               "platform": "shorts", "platform_explicit": True, "format": "vertical", "duration_s": 12, "voiceover": None, **request}
        job = self.store.create(req)
        job.data["persona"] = {"name": "ГЛаДОС", "card": self.core.persona_card}
        self.store.save(job)
        return job

    def reload(self, job):
        return self.store.load(job.job_id)

    async def answerer(self, job, answers: list):
        """Отвечает на вопросы задачи по очереди: каждый новый вопрос получает следующий ответ из списка."""
        last = None
        for answer in answers:
            qid = await self._wait(lambda seen=last: (lambda q: q if q not in (None, seen) else None)(self.pipeline.waiting_question(job.job_id)))
            last = qid
            self.pipeline.deliver_answer(job.job_id, qid, answer)

    @staticmethod
    async def _wait(pred, timeout=60):
        async def loop():
            while True:
                v = pred()
                if v:
                    return v
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(loop(), timeout)

    async def run(self, job, answers: list | None = None, timeout=180):
        task = asyncio.create_task(self.answerer(job, answers or []))
        try:
            await asyncio.wait_for(self.pipeline.run_job(job), timeout)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return job
