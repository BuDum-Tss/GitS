"""Клей менеджера внимания с очередью речи (ТЗ 29, ARC-4; ТЗ 03, ATT-13).

Владеет `SpeechQueue`: ставит в неё реплики и факты компаньона, говорит выбранного кандидата через компаньона и реагирует на
итог (сказано, отброшено): фокус реакции снимается, устаревшее нерассказанное уходит в сводку.
"""
import latency_trace
from conversation_io import current_channel
from speech_queue import SpeechCandidate, SpeechQueue

from .attention_core import (
    CONVERSATION_P,
    PROACTIVE_P,
    QUESTION_ANNOUNCE_P,
    TEXT_LOG,
    AttentionContext,
    NullSpeech,
)


class SpeechCoordinator:
    def __init__(self, ctx: AttentionContext, items, digest, focus):
        self.ctx = ctx
        self.items = items              # ItemStore
        self.digest = digest
        self.focus = focus
        companion = ctx.companion
        self.speech = SpeechQueue(
            speech=getattr(companion, "speech", None) or NullSpeech(), play=self.play, presence=ctx.presence.snapshot,
            chattiness=lambda: str(ctx.settings().get("chattiness", "normal")), question_pending=ctx.approver.has_pending,
            repeats=lambda text: bool(getattr(companion, "_repeats_recent", lambda t: False)(text)),
            on_drop=self.dropped, on_played=self.played, clock=ctx.clock)

    # ── планировщик ──────────────────────────────────────────────────────────

    async def pump(self) -> None:
        await self.speech.pump(self.ctx.att()["interrupt_margin"])

    def kick(self) -> None:
        if self.ctx.autopump:
            self.ctx.spawn(self.speech.pump(self.ctx.att()["interrupt_margin"]))

    # ── вход для компаньона ──────────────────────────────────────────────────

    def enqueue_say(self, text: str, emotion: str | None = None, priority: int | None = None, *,
                    source: str | None = None) -> SpeechCandidate:
        """Готовая реплика компаньона -> очередь (ATT-13). Источник по контексту: в формулировке вопроса - question,
        в обработке обращения - conversation (приоритет 95), иначе проактивная (приоритет «болтовни»)."""
        if self.ctx.approver.formulating():
            cand = self.speech.new(source="question", mode="verbatim", text=text, emotion=emotion,
                                   priority=priority or QUESTION_ANNOUNCE_P)
        elif current_channel.get() is not None:
            self.focus.touch_conversation()
            cand = self.speech.new(source="conversation", mode="verbatim", text=text, emotion=emotion,
                                   priority=CONVERSATION_P, channel=current_channel.get())
        else:
            cand = self.speech.new(source=source or "proactive", mode="verbatim", text=text, emotion=emotion,
                                   priority=priority if priority is not None else PROACTIVE_P)
        self.bind_trace(cand)
        self.kick()
        return cand

    def enqueue_fact(self, fact: str, user_text: str = "", must: bool = False, question: bool = False) -> SpeechCandidate:
        """Факт, по которому компаньон сам сформулирует реплику (`_speak_about`) -> очередь (ATT-13)."""
        if question or self.ctx.approver.formulating():
            cand = self.speech.new(source="question", mode="fact", text=fact, priority=QUESTION_ANNOUNCE_P, must=must,
                                   question=True)
        elif current_channel.get() is not None:
            self.focus.touch_conversation()
            cand = self.speech.new(source="conversation", mode="fact", text=fact, priority=CONVERSATION_P,
                                   user_text=user_text, must=must, question=question, channel=current_channel.get())
        else:
            cand = self.speech.new(source="proactive", mode="fact", text=fact, priority=PROACTIVE_P, user_text=user_text,
                                   must=must)
        self.bind_trace(cand)
        self.kick()
        return cand

    @staticmethod
    def bind_trace(cand: SpeechCandidate) -> None:
        """Реплика из обращения несёт трассу задержки до речи (ТЗ 24, TRC-1); очередь ждёт её сыгранности."""
        cand.trace = latency_trace.current()
        latency_trace.expect_audio(cand.trace)

    async def speak_about(self, fact: str, user_text: str = "", must: bool = False, question: bool = False,
                          direct: bool = False):
        """Факт компаньону на формулировку (публичный `speak_about`). Заглушки без него имеют только `_speak_about`."""
        speak = getattr(self.ctx.companion, "speak_about", None)
        if speak is None:
            speak = self.ctx.companion._speak_about
        await speak(fact, user_text, must, question, direct=direct)

    # ── воспроизведение и итоги ──────────────────────────────────────────────

    async def play(self, cand: SpeechCandidate) -> str | None:
        """Сказать кандидата: готовая реплика - `companion.say`, факт - `companion.speak_about` (формулирует модель)."""
        companion = self.ctx.companion
        before = getattr(companion, "last_utterance_id", None)
        token = current_channel.set(cand.channel)
        trace_token = latency_trace.bind(getattr(cand, "trace", None))
        try:
            if cand.mode == "verbatim":
                await companion.say(cand.text, cand.emotion, cand.priority, direct=True)
            else:
                await self.speak_about(cand.text, cand.user_text, cand.must, cand.question, direct=True)
        finally:
            latency_trace.unbind(trace_token)
            current_channel.reset(token)
        uid = getattr(companion, "last_utterance_id", None)
        return uid if uid != before else None

    def played(self, cand: SpeechCandidate) -> None:
        latency_trace.resolve_audio(getattr(cand, "trace", None))
        if cand.item_id:
            item = self.items.items.get(cand.item_id)
            if item is not None:
                item.spoken = True
            self.focus.pop_reaction(cand.item_id)
        if cand.family == "conversation":
            self.focus.touch_conversation()

    def dropped(self, cand: SpeechCandidate, reason: str) -> None:
        latency_trace.resolve_audio(getattr(cand, "trace", None))
        self.ctx.log.log_nowait("attention", cand.text[:TEXT_LOG], speech_dropped=reason, source=cand.source,
                                priority=cand.priority)
        if cand.item_id:
            item = self.items.items.get(cand.item_id)
            if item is not None and ((reason in ("expired", "overflow") and cand.family in ("attention", "announce"))
                                     or (reason == "stopped" and cand.family == "announce")):
                self.digest.add(item)                        # ATT-26: устаревшее нерассказанное попадает в сводку
            self.focus.pop_reaction(cand.item_id)
