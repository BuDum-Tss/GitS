"""Приём события: Normalizer -> HardRules -> Classifier -> Scorer -> Dispatcher (ТЗ 29, ARC-4; ТЗ 25, FAST-1).

  1. Normalizer (`normalizer.py`): тело события -> Item, отправитель -> человек и память;
  2. дедупликация (`items.py`, решение в `domain/dedupe.py`);
  3. HardRules (здесь): правила до классификатора (ATT-7) R1 обращение владельца -> разговор; R2 ответ на вопрос в том же
     канале; R3 временное правило mute -> в сводку; R4 критичное (urgency_hint 5 и объявлено в манифесте) -> срочность 5;
  4. Classifier (`event_classifier.py`): срочность и категория (ATT-8);
  5. Scorer и Dispatcher (`scorer.py`, `dispatcher.py`): приоритет, D0-D4, сводка или очередь речи.
"""
import logging

from .announce import Announcer, is_announce_event
from .attention_core import TEXT_LOG, AttentionContext, Item
from .decision_log import DecisionLog
from .dispatcher import Dispatcher
from .event_classifier import EventClassifier
from .items import ItemStore
from .normalizer import Normalizer
from .questions import QuestionService

logger = logging.getLogger("attention")


class HardRules:
    """Правила R1-R4 до классификатора. `apply` возвращает True, если событие уже обработано."""

    def __init__(self, ctx: AttentionContext, dispatcher: Dispatcher, decisions: DecisionLog, questions: QuestionService):
        self.ctx, self.dispatcher, self.decisions, self.questions = ctx, dispatcher, decisions, questions

    async def apply(self, item: Item, cfg: dict, att: dict, to_conversation: bool, declared: int | None) -> bool:
        ctx = self.ctx
        # R1: обращение владельца к ней - это не событие внимания, а реплика разговору (CORE-24)
        if to_conversation:
            item.decision = "R1"
            ctx.spawn(ctx.io.from_module(item.text, item.reply_channel, item.sender))
            self.decisions.write(item, att, rule="R1")
            return True
        # R2: ответ владельца на вопрос, заданный в этом же канале
        channel = self.questions.channel
        if (ctx.approver.has_pending() and item.sender and item.sender.get("is_owner") and item.reply_channel
                and channel is not None and item.reply_channel.get("module") == channel.module):
            if await ctx.approver.try_answer(item.text):
                item.decision = "R2"
                self.decisions.write(item, att, rule="R2")
                return True
        # R3: временное правило mute - в сводку, классификатор не нужен
        muted = ctx.rules.muted(item.module, item.kind, ctx.sender_name(item.sender))
        if muted is not None:
            item.urgency = item.urgency_hint if item.urgency_hint is not None else 1
            await self.dispatcher.finish(item, cfg, att, rule="R3", force="D1", rule_id=muted.id)
            return True
        # R4: критичное событие, объявленное модулем как срочное, обходит классификатор
        if item.urgency_hint == 5 and declared == 5:
            item.urgency = 5
            await self.dispatcher.finish(item, cfg, att, rule="R4")
            return True
        return False


class EventPipeline:
    def __init__(self, ctx: AttentionContext, normalizer: Normalizer, store: ItemStore, rules: HardRules,
                 classifier: EventClassifier, dispatcher: Dispatcher, announcer: Announcer, decisions: DecisionLog):
        self.ctx, self.normalizer, self.store, self.rules = ctx, normalizer, store, rules
        self.classifier, self.dispatcher, self.announcer, self.decisions = classifier, dispatcher, announcer, decisions

    async def submit(self, module_id: str, event: dict) -> None:
        """Событие модуля (тело `event`, ТЗ 01 раздел 7.1)."""
        ctx = self.ctx
        now = ctx.clock()
        item = self.normalizer.make_item(module_id, event, now)
        if item.kind not in ctx.silent_kinds:
            ctx.log.log_nowait("module_event", item.text[:TEXT_LOG], module=module_id, event_kind=item.kind,
                               urgency_hint=item.urgency_hint, addressed_to_me=item.addressed_to_me,
                               sender=ctx.sender_name(item.sender))
        handler = ctx.kind_handlers.get(item.kind)
        if handler is not None:
            ctx.spawn(self._run_kind_handler(handler, item))
            return
        hook = ctx.kind_hooks.get(item.kind)
        if hook is not None:
            ctx.spawn(self._run_kind_handler(hook, item))                 # побочное действие; событие идёт дальше обычным путём
        if is_announce_event(item.kind, event):
            self.store.register(item)                                      # AN-2: мимо формулы, но с записью решения
            await self.announcer.announce(item)
            return
        if self.store.merge_duplicate(item, now):
            return
        self.store.register(item)
        cfg = ctx.settings()
        att = ctx.att(cfg)
        to_conversation = self.normalizer.is_owner_address(item)
        self.normalizer.note_sender(item, skip_memory=to_conversation)
        info = ctx.emit_info(module_id, item.kind)
        declared = info.get("default_urgency") if info else None
        if await self.rules.apply(item, cfg, att, to_conversation, declared):
            return
        if self.classifier.needs(item, att):
            await self.classifier.run(item, cfg, att, declared)
            return
        self.classifier.apply(item, None, declared)
        await self.dispatcher.finish(item, cfg, att)

    @staticmethod
    async def _run_kind_handler(handler, item: Item) -> None:
        try:
            await handler(item)
        except Exception as e:
            logger.warning(f"Обработчик события «{item.kind}»: {e}")

    async def submit_speech(self, module_id: str, say: dict) -> None:
        """Речь модуля (тело `say`, ТЗ 01 раздел 7.2): проходит ту же оценку важности, затем очередь речи."""
        text = str(say.get("text", "")).strip()
        if not text:
            return
        ctx = self.ctx
        item, mode, ttl = self.normalizer.make_speech_item(module_id, say, text, ctx.clock())
        if say.get("announce") is True:
            self.store.register(item)
            await self.announcer.announce(item, mode=mode, emotion=say.get("emotion"), ttl=ttl)      # AN-2
            return
        cfg = ctx.settings()
        att = ctx.att(cfg)
        self.dispatcher.speech_item(item, text, say, module_id, mode, ttl, cfg, att)
