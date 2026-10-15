"""Вопросы пользователю (ТЗ 29, ARC-4; ТЗ 03, ATT-19): очередь речи, фокус question, доставка в модуль-канал."""
import asyncio
from typing import Any

from conversation_io import Channel, current_channel

from .attention_core import AttentionContext, Question, clamp
from .focus_stack import FocusStack
from .speech_coordinator import SpeechCoordinator


class QuestionService:
    def __init__(self, ctx: AttentionContext, speech: SpeechCoordinator, focus: FocusStack):
        self.ctx, self.speech, self.focus = ctx, speech, focus
        self.channel: Channel | None = None      # куда доставлен вопрос (ответ принимается правилом R2)

    def chat_module(self) -> str | None:
        """Подключённый включённый модуль-канал (chat.channel), куда можно отправить вопрос владельцу."""
        ctx = self.ctx
        if ctx.hub is None:
            return None
        found = [c.module_id for c in ctx.hub.connections()
                 if "chat.channel" in (c.manifest or {}).get("capabilities", [])
                 and (ctx.modules is None or ctx.modules.is_enabled(c.module_id))]
        return "telegram" if "telegram" in found else (found[0] if found else None)

    async def submit(self, q: Question) -> Any:
        """Задать вопрос пользователю (ATT-19). Возвращает (answer, by), как approvals.ask. Вопрос ставится в очередь речи
        (приоритет urgency_hint*20 в пределах 40-90, по умолчанию 60); заданным он считается, когда прозвучал; если пользователь
        отошёл и есть модуль-канал, вопрос уходит владельцу туда."""
        ctx = self.ctx
        att = ctx.att()
        hint = q.urgency_hint if isinstance(q.urgency_hint, (int, float)) and not isinstance(q.urgency_hint, bool) else 3
        priority = int(clamp(hint * 20, 40, 90))
        away_module = self.chat_module() if ctx.idle() > att["away_after_s"] else None
        focus = self.focus.push("question", q.question[:80], f"question:{q.source}", priority, True)

        async def announce(fact: str) -> None:
            if away_module is not None:
                await self.announce_in_module(fact, away_module)
                return
            cand = self.speech.speech.new(source="question", mode="fact", text=fact, priority=priority, must=True,
                                          question=True)
            self.speech.kick()
            try:
                await cand.done
            except asyncio.CancelledError:
                self.speech.speech.discard(cand, "cancelled")        # ответ пришёл раньше, чем вопрос озвучили
                raise

        try:
            return await ctx.approver.ask(q.kind, q.question, q.options, q.timeout_s, q.source, announce=announce,
                                          context=q.context)
        finally:
            self.channel = None
            self.focus.pop(focus.id)

    async def announce_in_module(self, fact: str, module: str) -> None:
        """Вопрос владельцу в модуль-канал: формулировку строит модель, доставка - обычным путём ответа в канал модуля."""
        channel = Channel("module", module, "owner")
        self.channel = channel
        token = current_channel.set(channel)
        try:
            await self.speech.speak_about(fact, must=True, question=True, direct=True)
        finally:
            current_channel.reset(token)
