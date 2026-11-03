"""Классификатор обращений (ТЗ 03, ATT-28): намерение обращения до маршрутизатора.

События модулей классификатор больше не оценивает: срочность берётся из подсказки модуля (`Normalizer.assign_urgency`). Без
сервиса (не запущен, настройка выключена, ошибка) обращение идёт как раньше, по правилам.
"""
import decide

from .attention_core import TEXT_LOG, AttentionContext


class InputClassifier:
    def __init__(self, ctx: AttentionContext):
        self.ctx = ctx

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
