"""Ход задач модулей (ТЗ 29, ARC-4; ТЗ 03, ATT-12): состояние задачи -> событие внимания."""
import secrets

import tool_trace

from .attention_core import TEXT_MAX, AttentionContext, Item
from .dispatcher import Dispatcher
from .items import ItemStore


TOOL_STAGE = "tool:"      # стадия задачи «идёт вызов инструмента»: `tool:<Имя>:<номер>`, номер делает каждую стадию новой


class JobTracker:
    def __init__(self, ctx: AttentionContext, store: ItemStore, dispatcher: Dispatcher):
        self.ctx, self.store, self.dispatcher = ctx, store, dispatcher
        self.states: dict[tuple[str, str], tuple] = {}

    @staticmethod
    def describe(module_id: str, job: dict, state, stage) -> tuple[int, str]:
        """(срочность, факт) по состоянию задачи."""
        title = job.get("title") or job.get("job_id")
        if state == "done":
            urgency, text = 3, f"Задача «{title}» (модуль {module_id}) выполнена."
        elif state == "failed":
            reason = (job.get("error") or {}).get("message") or "причина не названа"
            urgency, text = 4, f"Задача «{title}» (модуль {module_id}) не удалась: {reason}."
        elif state == "cancelled":
            urgency, text = 1, f"Задача «{title}» (модуль {module_id}) отменена."
        else:
            urgency = 1
            text = f"Задача «{title}» (модуль {module_id}): состояние {state}" + (f", стадия {stage}." if stage else ".")
        if job.get("text"):
            text += f" Подробности: {str(job['text'])[:300]}"
        return urgency, text

    async def on_update(self, module_id: str, job: dict) -> None:
        """Ход задачи модуля (тело `job.update`, ТЗ 01 раздел 9). Вопрос задачи (waiting_user) здесь не задаётся:
        шина уже вызывает submit_question с защитой от повторов по question_id (MOD-20) и сама отправляет job.answer."""
        ctx = self.ctx
        key, state, stage = (module_id, str(job.get("job_id"))), job.get("state"), job.get("stage")
        if state == "running" and str(stage or "").startswith(TOOL_STAGE):
            # внутренний вызов Claude-помощника (`tool:<Имя>:<номер>`): в чат, не в внимание
            tool_trace.note("claude", str(stage).split(":")[1], str(job.get("text") or ""))
            return
        before = self.states.get(key)
        self.states[key] = (state, stage)
        if len(self.states) > 500:                          # старые задачи забываем: словарь не растёт без конца
            self.states.pop(next(iter(self.states)))
        if before == (state, stage) or state == "waiting_user":
            return
        urgency, text = self.describe(module_id, job, state, stage)
        now = ctx.clock()
        item = Item(id="a-" + secrets.token_hex(4), module=module_id, kind=f"{module_id}.job", text=text[:TEXT_MAX],
                    data={"job_id": job.get("job_id"), "state": state, "stage": stage}, sender=None, reply_channel=None,
                    addressed_to_me=False, urgency=urgency, needs_reply=False, memorable=False, created=now, expires=None,
                    dedupe_key=f"job:{module_id}:{job.get('job_id')}:{state}:{stage}", urgency_hint=urgency, updated=now)
        if self.store.merge_duplicate(item, now):
            return
        self.store.register(item)
        cfg = ctx.settings()
        att = ctx.att(cfg)
        muted = ctx.rules.muted(module_id, item.kind, None)
        await self.dispatcher.finish(item, cfg, att, **({"rule": "R3", "force": "D1", "rule_id": muted.id} if muted else {}))
