"""Запись решения внимания в журнал (ТЗ 29, ARC-4; ТЗ 03, ATT-29; ТЗ 24, TRC-4, TRC-6)."""
import journal
import salience

from .attention_core import TEXT_LOG, AttentionContext, Item


class DecisionLog:
    def __init__(self, ctx: AttentionContext):
        self.ctx = ctx

    def write(self, item: Item, att: dict, *, score: dict | None = None, rule: str | None = None,
              ms: float | None = None, rule_id: str | None = None, **extra) -> None:
        """ATT-29: решение в журнале с множителями, приоритетом и временем классификатора."""
        ctx = self.ctx
        score = score or {}
        record = {"item": item.id, "module": item.module, "event_kind": item.kind, "urgency": item.urgency,
                  "w_profile": score.get("w_profile"), "w_sender": score.get("w_sender"), "w_rule": score.get("w_rule"),
                  "w_user": score.get("w_user"), "freshness": score.get("freshness"),
                  "priority": round(item.priority, 1) if score else None, "decision": item.decision,
                  "classifier_ms": ms, "rule": rule, "rule_id": rule_id, "count": item.count,
                  "category": item.data.get("category") if isinstance(item.data, dict) else None, **extra}
        # яркость (ТЗ 24, TRC-4): срочность, оценка модуля игры (если прислана), итог, явная просьба, новизна
        appraisal = item.data.get("appraisal_importance") if isinstance(item.data, dict) else None
        sal, reasons = salience.score(salience.factors_of(urgency=item.urgency, appraisal=appraisal, kind=item.kind,
                                                          memorable=item.memorable, novelty=item.first_contact))
        record.update(salience=sal, reasons=reasons)
        ctx.last_decision = record
        ctx.decisions.append({**record, "text": item.text[:TEXT_LOG], "ts": item.created})
        ctx.log.log_nowait("attention", item.text[:TEXT_LOG], **{k: (round(v, 3) if isinstance(v, float) else v)
                                                                 for k, v in record.items()})
        # журнал решений (ТЗ 24, TRC-6): маршрут, уверенность классификатора, эшелон и версия правил, трасса события
        classified = bool(item.data.get("category")) if isinstance(item.data, dict) else False
        journal.decision(item.decision or "?", "attention", item.text[:200],
                         confidence=item.data.get("category_conf") if isinstance(item.data, dict) else None,
                         echelon="gliner" if classified else "rules", rule=rule, rule_id=rule_id,
                         trace_id=item.trace.id if item.trace is not None else None, module=item.module,
                         salience=sal)
