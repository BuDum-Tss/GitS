"""Классификатор событий и обращений (ТЗ 29, ARC-4; ТЗ 03, ATT-4a, ATT-8, ATT-28, ATT-32).

Классификатор только подсказывает: без него (сервис не запущен, настройка выключена, ошибка) всё идёт по подсказкам модуля.
"""
import decide

from .attention_core import CLASSIFIER_QUEUE_MAX, REPLY_CATEGORIES, TEXT_LOG, AttentionContext, Item
from .dispatcher import Dispatcher


class EventClassifier:
    def __init__(self, ctx: AttentionContext, dispatcher: Dispatcher):
        self.ctx, self.dispatcher = ctx, dispatcher
        self.pending = 0

    def needs(self, item: Item, att: dict) -> bool:
        """ATT-4a: подсказки модуля хватает, если есть и urgency_hint, и addressed_to_me, а текст не от человека."""
        if not att["classifier"] or not item.text.strip():
            return False
        return item.sender is not None or item.urgency_hint is None or item.addressed_to_me is None

    async def run(self, item: Item, cfg: dict, att: dict, declared: int | None) -> None:
        """Классифицировать и принять решение: сразу (очередь переполнена, ATT-32), иначе в фоне, чтобы очередь событий
        модуля не стояла."""
        if self.pending >= CLASSIFIER_QUEUE_MAX:
            # ATT-32: очередь к классификатору длиннее 20 - новые события идут по подсказкам модуля (без подсказки urgency = 1)
            item.data = {**item.data, "classifier_skipped": "queue"}
            if item.urgency_hint is None:
                item.urgency = 1
            self.apply(item, None, declared)
            await self.dispatcher.finish(item, cfg, att)
            return
        self.pending += 1
        self.ctx.spawn(self.classify_and_finish(item, cfg, att, declared))

    async def classify_and_finish(self, item: Item, cfg: dict, att: dict, declared: int | None) -> None:
        ctx = self.ctx
        try:
            questions = decide.event_questions(ctx.policy_options() if ctx.policy_options else None)
            answers = await ctx.decider.classify(item.text, questions, timeout=decide.EVENT_TIMEOUT_S)
        finally:
            self.pending -= 1
        ms = ctx.decider.last_ms
        self.apply(item, decide.parse_event(answers, att["min_conf_event"]), declared)
        await self.dispatcher.finish(item, cfg, att, ms=ms)

    @staticmethod
    def apply(item: Item, cls: decide.EventClass | None, declared: int | None) -> None:
        """ATT-8: срочность = максимум из подсказки модуля и категории; spam даёт 0; без классификатора - подсказка или 1."""
        hint = item.urgency_hint if item.urgency_hint is not None else (declared if isinstance(declared, int) else None)
        if item.urgency is not None and cls is None:
            return                                        # уже назначена (очередь классификатора переполнена)
        if cls is not None and cls.category is not None:
            item.urgency = 0 if cls.category == "spam" else max(hint if hint is not None else 0, cls.urgency or 0)
            item.data = {**item.data, "category": cls.category, "category_conf": round(cls.category_conf, 3)}
        else:
            item.urgency = hint if hint is not None else 1
        if cls is not None:                               # ТЗ 18: подсказки для менеджера фокуса (первый эшелон)
            extra = {}
            if cls.focus_needed is not None:
                extra.update(focus_needed=cls.focus_needed, focus_conf=round(cls.focus_conf, 3))
            if cls.policy is not None:
                extra.update(policy_hint=cls.policy, policy_conf=round(cls.policy_conf, 3))
            if extra:
                item.data = {**item.data, **extra}
        if item.addressed_to_me is None and cls is not None:
            item.addressed_to_me = cls.addressed
        # «ждёт ответа»: да/нет-вопрос модели на живой проверке сильно смещён в «нет» (10 из 20 ответов нужных не нашла), поэтому
        # вопрос и просьба по категории тоже означают, что ответа ждут
        item.needs_reply = bool(cls is not None and (cls.needs_reply or cls.category in REPLY_CATEGORIES))

    async def classify_input(self, text: str, stop_speech) -> decide.InputClass | None:
        """ATT-28: намерение обращения до маршрутизатора. None - классификатора нет или он не уверен (всё как раньше)."""
        ctx = self.ctx
        att = ctx.att()
        if not att["classifier"] or not text.strip():
            return None
        answers = await ctx.decider.classify(text, decide.INPUT_QUESTIONS, timeout=decide.INPUT_TIMEOUT_S)
        cls = decide.parse_input(answers, att["min_conf_input"])
        ctx.log.log_nowait("attention", text[:TEXT_LOG], input=True, intent=cls.intent if cls else None,
                           needs_memory=cls.needs_memory if cls else None, classifier_ms=ctx.decider.last_ms,
                           available=answers is not None)
        if cls is not None and cls.intent == "stop":
            await stop_speech("classifier")
        return cls
