"""Менеджер внимания: ЗАГЛУШКА этапа 1 (ТЗ 02, раздел 9.4). Полная реализация - ТЗ 03.

Интерфейс (`Attention`) задан здесь и остаётся прежним: шина модулей зовёт только эти четыре метода. Заглушка почти
ничего не решает: не оценивает важность, не ведёт очередь речи и фокус, а только пропускает события дальше самым простым
путём, чтобы модули уже работали:

  submit            - пишет событие в журнал (вид module_event); обращение владельца к ней с reply_channel отдаёт в
                      разговор как UserInput(kind=module); `memorable` кладёт в очередь графа памяти; прочее не озвучивает;
  submit_speech     - verbatim: сказать как есть, если она сейчас молчит и пользователь не говорил последние 5 с;
                      fact: сформулировать реплику по факту (companion._speak_about);
  submit_question   - задать вопрос через approvals (core.ask и вопросы задач);
  on_job_update     - смена состояния задачи на done или failed становится фактом для реплики. Вопрос задачи
                      (waiting_user) здесь НЕ задаётся: шина модулей уже вызывает submit_question с защитой от повторов
                      по question_id (MOD-20) и сама отправляет job.answer.

Методы быстрые: долгое (разговор, реплика по факту) уходит в фоновые задачи. Все пути речи в ТЗ 03 станут кандидатами
очереди речи, а сам класс будет заменён целиком с теми же методами.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import approvals
import events_log

logger = logging.getLogger("attention")

USER_QUIET_S = 5.0          # verbatim-реплика модуля не перебивает пользователя, говорившего меньше стольких секунд назад
_TEXT_LOG = 200


@dataclass
class Question:
    """Вопрос пользователю: от модуля (core.ask), из задачи модуля (waiting_user) или от самого ядра."""

    kind: str                                   # approval | choice | text
    question: str                               # факт: о чём спросить; формулировку вслух строит ядро
    options: list[str] | None = None
    timeout_s: float = 300
    source: str = "core"                        # id модуля или "core"
    context: dict = field(default_factory=dict)
    urgency_hint: int | None = None
    job_id: str | None = None                   # для вопросов задач
    question_id: str | None = None


class Attention:
    def __init__(self, companion, io, *, clock=time.time):
        self.companion = companion              # Companion: say, _speak_about, graph
        self.io = io                            # ConversationIO: обращения владельца из модулей
        self._clock = clock
        self._job_states: dict[tuple[str, str], str] = {}
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(f"Фоновая задача внимания: {task.exception()}")

    # ── события ─────────────────────────────────────────────────────────────

    async def submit(self, module_id: str, event: dict) -> None:
        """Событие модуля (тело `event`, ТЗ 01 раздел 7.1)."""
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else None
        reply = event.get("reply_channel") if isinstance(event.get("reply_channel"), dict) else None
        events_log.log("module_event", str(event.get("text", ""))[:_TEXT_LOG], module=module_id, event_kind=event.get("kind"),
                       urgency_hint=event.get("urgency_hint"), addressed_to_me=event.get("addressed_to_me"),
                       sender=(sender or {}).get("name") or (sender or {}).get("id"))
        text = str(event.get("text", ""))
        if event.get("addressed_to_me") and sender and sender.get("is_owner") and reply:
            self._spawn(self.io.from_module(text, reply, sender))        # обращение владельца: разговор
        if event.get("memorable"):
            graph = getattr(self.companion, "graph", None)
            if graph is not None:
                graph.enqueue((sender or {}).get("name") or module_id, text)

    async def submit_speech(self, module_id: str, say: dict) -> None:
        """Речь модуля (тело `say`, ТЗ 01 раздел 7.2)."""
        text = str(say.get("text", "")).strip()
        if not text:
            return
        if say.get("mode", "fact") == "verbatim":
            quiet = self._clock() - getattr(self.companion, "_last_user_ts", 0.0) >= USER_QUIET_S
            if self.companion.is_speaking() or not quiet:
                events_log.log("module_event", "реплика модуля отброшена: она говорит или пользователь говорил",
                               module=module_id, dropped=True)
                return
            await self.companion.say(text, say.get("emotion"))
        else:
            self._spawn(self.companion._speak_about(text))

    async def submit_question(self, q: Question) -> Any:
        """Задать вопрос пользователю. Возвращает (answer, by), как approvals.ask."""
        return await approvals.ask(q.kind, q.question, q.options, q.timeout_s, q.source,
                                   announce=self._announce, context=q.context)

    async def _announce(self, fact: str) -> None:
        await self.companion._speak_about(fact, must=True, question=True)

    async def on_job_update(self, module_id: str, job: dict) -> None:
        """Ход задачи модуля (тело `job.update`, ТЗ 01 раздел 9)."""
        key, state = (module_id, str(job.get("job_id"))), job.get("state")
        before = self._job_states.get(key)
        self._job_states[key] = state
        if len(self._job_states) > 500:                     # старые задачи забываем: словарь не растёт без конца
            self._job_states.pop(next(iter(self._job_states)))
        if state == before or state not in ("done", "failed"):
            return
        title = job.get("title") or job.get("job_id")
        if state == "done":
            fact = f"Задача «{title}» (модуль {module_id}) выполнена."
        else:
            reason = (job.get("error") or {}).get("message") or "причина не названа"
            fact = f"Задача «{title}» (модуль {module_id}) не удалась: {reason}."
        if job.get("text"):
            fact += f" Подробности: {str(job['text'])[:300]}"
        self._spawn(self.companion._speak_about(fact))
