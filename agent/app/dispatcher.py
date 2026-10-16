"""Решение и действие по событию (ТЗ 29, ARC-4; ТЗ 03, ATT-10): приоритет, D0-D4, сводка, очередь речи, хук фокуса."""
import logging

import routes
from domain import rules

from .attention_core import TEXT_MAX, AttentionContext, Item
from .decision_log import DecisionLog
from .digest import Digest
from .focus_stack import FocusStack
from .items import event_fact
from .scorer import Scorer
from .speech_coordinator import SpeechCoordinator

logger = logging.getLogger("attention")


class Dispatcher:
    def __init__(self, ctx: AttentionContext, scorer: Scorer, digest: Digest, speech: SpeechCoordinator,
                 focus: FocusStack, decisions: DecisionLog):
        self.ctx, self.scorer, self.digest, self.speech, self.focus, self.decisions = (
            ctx, scorer, digest, speech, focus, decisions)

    async def finish(self, item: Item, cfg: dict, att: dict, *, rule: str | None = None, force: str | None = None,
                     ms: float | None = None, rule_id: str | None = None) -> None:
        """Приоритет, решение и действие по событию."""
        score = self.scorer.score(item, cfg, att)
        item.priority = score["priority"]
        decision = force or self.scorer.decide(item.priority, att, score["away"])
        item.decision = decision
        think = self.think_route(item, decision)
        if not (think and item.origin == "self"):          # импульс самого агента не озвучивается и не уходит в сводку: он идёт в размышление
            self.act(item, decision)
        self.decisions.write(item, att, score=score, rule=rule, ms=ms, rule_id=rule_id, routes=routes.of_decision(decision, think=think),
                             **({"route": "think"} if think else {}))
        if think:
            self.ctx.on_think(item)
        if self.ctx.on_decided is not None:                    # ТЗ 18: менеджер фокуса получает решение (не ждёт)
            try:
                self.ctx.on_decided(item, decision, item.priority, rule)
            except Exception:
                logger.exception("Хук менеджера фокуса упал (решение внимания уже применено)")

    def think_route(self, item: Item, decision: str) -> bool:
        """ТЗ 30, IMP-4: направить ли событие в размышление (правило в domain/rules.py). Без размышления маршрута нет."""
        if self.ctx.on_think is None:
            return False
        return rules.think_route(origin=item.origin, kind=item.kind, decision=decision, urgency=item.urgency,
                                 priority=item.priority, memorable=item.memorable, needs_reply=item.needs_reply,
                                 spoken=item.spoken)

    def act(self, item: Item, decision: str) -> None:
        """Что происходит по решению (ATT-10)."""
        ctx = self.ctx
        if decision == "D0":
            return
        if item.memorable and not item.memory_done:
            ctx.writer.enqueue_for_graph(ctx.sender_name(item.sender) or item.module, item.text)
        if decision == "D1":
            self.digest.add(item)
            return
        key = f"attention:{item.module}:{item.dedupe_key}" if item.dedupe_key else None
        ttl = (item.expires - ctx.clock()) if item.expires is not None else 600.0
        if ttl <= 0:
            return
        self.speech.speech.new(source=f"attention:{item.id}", mode="fact", text=event_fact(ctx, item),
                               priority=int(round(item.priority)), ttl=ttl, key=key, item_id=item.id)
        if decision == "D4":
            self.react(item, item.text[:80], f"attention:{item.id}")
        self.speech.kick()

    def react(self, item: Item, topic: str, source: str) -> None:
        """D4: поверх стека встаёт фокус reaction, он снимется, когда реплику скажут или отбросят."""
        focus = self.focus.push("reaction", topic, source, int(round(item.priority)), False)
        self.focus.reaction_focus[item.id] = focus.id

    def speech_item(self, item: Item, text: str, say: dict, module_id: str, mode: str, ttl: float | None,
                    cfg: dict, att: dict) -> None:
        """Речь модуля после создания Item: правило R3, оценка, сводка или очередь речи (ТЗ 01, раздел 7.2)."""
        if self.ctx.rules.muted(module_id, item.kind, None) is not None:
            item.priority = 0.0
            item.decision = "D1"
            self.digest.add(item)
            self.decisions.write(item, att, rule="R3")
            return
        score = self.scorer.score(item, cfg, att)
        item.priority = score["priority"]
        decision = self.scorer.decide(item.priority, att, score["away"])
        item.decision = decision
        if decision == "D1":
            self.digest.add(item)
        elif decision != "D0":
            key = say.get("key")
            self.speech.speech.new(source=f"module:{module_id}", mode=mode, text=text[:TEXT_MAX] if mode == "fact" else text,
                                   emotion=say.get("emotion"), priority=int(round(item.priority)), ttl=ttl,
                                   key=f"module:{module_id}:{key}" if key else None, item_id=item.id)
            if decision == "D4":
                self.react(item, text[:80], f"module:{module_id}")
            self.speech.kick()
        self.decisions.write(item, att, score=score)
