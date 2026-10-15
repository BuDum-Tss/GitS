"""Стек фокуса внимания (ТЗ 29, ARC-4; ТЗ 03, ATT-20-ATT-22): conversation, reaction и question поверх idle.

Это не менеджер фокуса модулей (`focus.py`, ТЗ 18): здесь только то, чем она занята сама (разговор, реакция, вопрос).
"""
import secrets

from .attention_core import (
    CONVERSATION_FOCUS_P,
    CONVERSATION_IDLE_S,
    FOCUS_DEPTH,
    RETURN_P,
    RETURN_WINDOW_S,
    AttentionContext,
    Focus,
    REACTION_MAX_AGE_S,
)


class FocusStack:
    def __init__(self, ctx: AttentionContext):
        self.ctx = ctx
        now = ctx.clock()
        self.stack: list[Focus] = [Focus("f-idle", "idle", "", "", 0, now, now, False)]
        self.reaction_focus: dict[str, str] = {}      # id события -> id фокуса reaction
        self.speech = None                            # SpeechCoordinator, ставит фасад
        self.digest = None                            # Digest, ставит фасад
        self.publish()

    def top(self) -> Focus:
        return self.stack[-1]

    def reset(self) -> None:
        del self.stack[1:]

    def publish(self) -> None:
        top = self.stack[-1]
        self.ctx.presence.update(focus=top.kind if top.kind == "idle" else f"{top.kind}: {top.topic}", busy=top.kind != "idle")

    def push(self, kind: str, topic: str, source: str, priority: int, resumable: bool) -> Focus:
        now = self.ctx.clock()
        f = Focus("f-" + secrets.token_hex(3), kind, topic, source, priority, now, now, resumable)
        self.stack.append(f)
        while len(self.stack) - 1 > FOCUS_DEPTH:                   # ATT-21: без учёта дна стека
            victims = [x for x in self.stack[1:-1] if x.resumable] or self.stack[1:-1]
            if not victims:
                break
            self.stack.remove(victims[0])
        self.publish()
        return f

    def touch_conversation(self, topic: str | None = None) -> None:
        now = self.ctx.clock()
        conv = next((f for f in self.stack if f.kind == "conversation"), None)
        if conv is None:
            self.push("conversation", (topic or "")[:80], "user", CONVERSATION_FOCUS_P, True)
            return
        conv.last_active = now
        if topic:
            conv.topic = topic[:80]
            self.publish()

    def pop_reaction(self, item_id: str) -> None:
        fid = self.reaction_focus.pop(item_id, None)
        if fid:
            self.pop(fid)

    def pop(self, focus_id: str) -> None:
        idx = next((i for i, f in enumerate(self.stack) if f.id == focus_id and i > 0), None)
        if idx is None:
            return
        was_top = idx == len(self.stack) - 1
        gone = self.stack.pop(idx)
        top = self.stack[-1]
        if was_top and gone.kind in ("reaction", "question") and top.kind == "conversation" \
                and self.ctx.clock() - top.last_active < RETURN_WINDOW_S:
            # ATT-22: разговор прервался; возвращаться ли к теме, решает модель (может промолчать)
            self.speech.speech.new(source="attention:return", mode="fact", priority=RETURN_P, ttl=120.0,
                                   key="attention:return",
                                   text=f"Разговор о «{top.topic}» прервался из-за «{gone.topic}». "
                                        "Можно вернуться к теме или промолчать.")
            self.speech.kick()
        self.publish()
        if top.kind == "idle" and gone.kind in ("conversation", "reaction"):
            self.digest.maybe_offer()

    def tick(self, now: float) -> None:
        """Сроки (ATT-20): conversation снимается через 60 с без обращений и ответов, зависшая reaction - через 15 минут."""
        for f in list(self.stack[1:]):
            if (f.kind == "conversation" and now - f.last_active > CONVERSATION_IDLE_S) or \
                    (f.kind == "reaction" and now - f.started > REACTION_MAX_AGE_S):
                for item_id, fid in list(self.reaction_focus.items()):
                    if fid == f.id:
                        del self.reaction_focus[item_id]
                self.pop(f.id)
