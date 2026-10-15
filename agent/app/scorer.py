"""Оценка события: веса и приоритет, решение D0-D4 (ТЗ 29, ARC-4; ТЗ 03, ATT-9, ATT-10).

Формулы лежат в `domain/scoring.py`; здесь только сбор весов из профиля, людей, правил и состояния пользователя.
"""
from domain import scoring

from .attention_core import DEFAULT_HALF_LIFE, AttentionContext, Item


class Scorer:
    def __init__(self, ctx: AttentionContext, focus):
        self.ctx = ctx
        self.focus = focus

    def w_profile(self, kind: str) -> float:
        weights = {}
        if self.ctx.profiles is not None:
            weights = (getattr(self.ctx.profiles.current(), "attention", None) or {}).get("weights") or {}
        w = scoring.longest_match({k: v for k, v in weights.items() if isinstance(v, (int, float))}, kind, 1.0)
        return float(w)

    def w_sender(self, sender: dict | None, first_contact: bool = False) -> float:
        if not sender:
            return 1.0
        if sender.get("is_owner"):
            return 1.5
        rec = None
        if not first_contact:        # первое сообщение человека: профиль заведён только что, он ещё «неизвестный»
            for key in (sender.get("id"), sender.get("name")):
                if key:
                    rec = self.ctx.people.get(str(key))
                    if rec is not None:
                        break
        if rec is None:
            return 0.8
        return 1.3 if float(rec.get("affinity", 0.0)) / 100.0 >= 0.6 else 1.0

    @staticmethod
    def half_life(kind: str, att: dict) -> float:
        return scoring.half_life(kind, att["half_life"], DEFAULT_HALF_LIFE["*"])

    def user_state(self, item: Item | None, urgency: int, addressed: bool | None, cfg: dict, att: dict) -> tuple[float, bool]:
        """(вес w_user, пользователь отошёл). «Не отвлекать» режет вес до 0.3, кроме срочного обращения к ней."""
        w = 1.0
        if self.ctx.quiet(cfg) and not (urgency == 5 and addressed):
            w = 0.3
        away = self.ctx.idle() > att["away_after_s"]
        return w, away

    def score(self, item: Item, cfg: dict, att: dict) -> dict:
        ctx = self.ctx
        now = ctx.clock()
        urgency = int(item.urgency if item.urgency is not None else 1)
        w_user, away = self.user_state(item, urgency, item.addressed_to_me, cfg, att)
        parts = {"w_profile": self.w_profile(item.kind), "w_sender": self.w_sender(item.sender, item.first_contact),
                 "w_rule": ctx.rules.weight(item.module, item.kind, ctx.sender_name(item.sender)), "w_user": w_user,
                 "freshness": scoring.freshness(now - item.created, self.half_life(item.kind, att))}
        parts["priority"] = scoring.priority(urgency, parts["w_profile"], parts["w_sender"], parts["w_rule"], w_user,
                                             parts["freshness"])
        parts["away"] = away
        return parts

    def decide(self, priority: float, att: dict, away: bool) -> str:
        top = self.focus.top()
        return scoring.decide(priority, att["thresholds"], away, top.kind, top.priority, att["interrupt_margin"])
