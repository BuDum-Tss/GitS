"""Эхо-модуль: пример модуля ГЛаДОС и тестовый собеседник ядра (ТЗ 01, MOD-27).

Инструмент `echo.repeat` возвращает свой текст. Инструмент `echo.count` запускает задачу: считает до `n` раз в секунду,
шлёт прогресс, на середине спрашивает «продолжать?» и по ответу идёт дальше или останавливается.

Запуск вручную (ядро должно работать, токен берётся из файла ядра):  python echo_module.py
"""
import asyncio
import logging

from glados_module import ModuleClient, ToolError
from glados_module.protocol import new_job_id

MANIFEST = {
    "id": "echo",
    "title": "Эхо",
    "description": "Тестовый модуль: повторяет текст и умеет считать до числа задачей со стадиями и вопросом.",
    "version": "1.0.0",
    "subscribe": ["presence", "module"],
    "tools": [
        {"name": "repeat", "description": "Повторить переданный текст без изменений.",
         "params": {"text": {"type": "string", "description": "что повторить"}},
         "risk": "auto", "availability": "always", "keywords": ["эхо", "повтори"]},
        {"name": "count", "description": "Посчитать до числа n задачей: прогресс, вопрос на середине, итог.",
         "params": {"n": {"type": "integer", "description": "до скольки считать"}},
         "risk": "auto", "availability": "always", "job": True, "timeout_s": 10},
    ],
    "emits": [{"kind": "echo.counted", "description": "Счёт закончен", "default_urgency": 1}],
}

CHOICE_CONTINUE, CHOICE_STOP = "продолжать", "остановить"


def build_client(interval: float = 1.0, **kwargs) -> ModuleClient:
    """Собрать клиента. `interval` секунд между числами (в тестах меньше)."""
    client = ModuleClient(MANIFEST, **kwargs)
    runners: dict[str, asyncio.Task] = {}            # job_id -> задача счёта
    answers: dict[str, asyncio.Future] = {}          # job_id -> ожидаемый ответ на вопрос

    @client.tool("repeat")
    async def repeat(args):
        return args["text"]

    async def count(job_id: str, n: int) -> None:
        title = f"Счёт до {n}"
        try:
            await client.job_update(job_id, title=title, state="running", stage="counting", progress=0.0,
                                    text=f"Считаю до {n}.")
            asked = n < 2
            for i in range(1, n + 1):
                await asyncio.sleep(interval)
                await client.job_update(job_id, progress=i / n, text=f"Досчитала до {i} из {n}.")
                if not asked and i >= n // 2:
                    asked = True
                    answers[job_id] = asyncio.get_running_loop().create_future()
                    await client.job_update(
                        job_id, state="waiting_user", stage="halfway", text=f"Сосчитано {i} из {n}.",
                        question={"question_id": "q-1", "kind": "choice", "question": "Продолжать счёт?",
                                  "options": [CHOICE_CONTINUE, CHOICE_STOP], "timeout_s": 600})
                    answer = await answers[job_id]
                    if answer == CHOICE_STOP:
                        await client.job_update(job_id, state="cancelled", text=f"Остановлено на {i} из {n}.")
                        return
                    await client.job_update(job_id, state="running", stage="counting")
            await client.job_update(job_id, state="done", stage="finished", progress=1.0,
                                    text=f"Досчитала до {n}.")
            await client.event("echo.counted", f"Эхо досчитало до {n}.", data={"job_id": job_id, "n": n})
        except asyncio.CancelledError:
            await client.job_update(job_id, state="cancelled", text="Задача отменена.")
            raise
        except Exception as e:
            await client.job_update(job_id, state="failed", text="Счёт сорвался.",
                                    error={"code": "internal", "message": str(e) or type(e).__name__})
        finally:
            answers.pop(job_id, None)
            runners.pop(job_id, None)

    @client.tool("count")
    async def count_tool(args):
        n = args.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 10000:
            raise ToolError("n должно быть целым от 1 до 10000", code="bad_request")
        job_id = new_job_id()
        runners[job_id] = asyncio.create_task(count(job_id, n))
        return {"job_id": job_id}

    @client.on_job_answer
    async def job_answer(job_id, question_id, answer):
        fut = answers.get(job_id)
        if fut is not None and not fut.done():
            fut.set_result(answer)

    @client.on_job_cancel
    async def job_cancel(job_id):
        task = runners.get(job_id)
        if task is None:
            raise ToolError("нет такой задачи", code="unknown")
        task.cancel()

    @client.on_reconnect
    async def reconnected():
        await client.resend_jobs()                    # MOD-22

    return client


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    build_client().main()
