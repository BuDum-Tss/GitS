"""Хранилище событий внимания и склейка повторов (ТЗ 29, ARC-4; ТЗ 03, ATT-6).

Решение «можно ли склеить» чистое и лежит в `domain/dedupe.py`; здесь только состояние и обновление очереди речи и сводки.
"""
from domain import dedupe
from domain.event import Event

from .attention_core import TEXT_LOG, AttentionContext, Item


def event_fact(ctx: AttentionContext, item: Item) -> str:
    """Факт для реплики (ATT-11): событие, повторы и, если можно ответить, через какой модуль."""
    parts = [f"Событие модуля «{ctx.module_title(item.module)}» ({item.kind}): {item.text}"]
    if item.count > 1:
        parts.append(f"(повторилось {item.count} раз)")
    if item.needs_reply and item.reply_channel:
        parts.append(f"На это можно ответить через модуль «{ctx.module_title(str(item.reply_channel.get('module')))}», "
                     "но только если пользователь попросит.")
    return " ".join(parts)


def event_of(item: Item) -> Event:
    """Item внимания -> Event для размышления (ТЗ 30)."""
    return Event(kind=item.kind, text=item.text, origin=item.origin, module=item.module, ts=item.created, sender=item.sender,
                 urgency=item.urgency, priority=item.priority, memorable=item.memorable, needs_reply=item.needs_reply,
                 data=dict(item.data) if isinstance(item.data, dict) else {}, item_id=item.id)


class ItemStore:
    def __init__(self, ctx: AttentionContext):
        self.ctx = ctx
        self.items: dict[str, Item] = {}
        self.by_key: dict[tuple[str, str], Item] = {}
        self.speech = None          # SpeechCoordinator, ставит фасад
        self.digest = None          # Digest, ставит фасад

    def register(self, item: Item) -> None:
        self.items[item.id] = item
        if item.dedupe_key:
            self.by_key[(item.module, item.dedupe_key)] = item
        for old_id in dedupe.oldest_ids({i.id: i.created for i in self.items.values()}):
            old = self.items.pop(old_id, None)
            if old is not None and old.dedupe_key and self.by_key.get((old.module, old.dedupe_key)) is old:
                del self.by_key[(old.module, old.dedupe_key)]

    def merge_duplicate(self, item: Item, now: float) -> bool:
        """ATT-6: тот же dedupe_key за последние 60 с и ещё не озвучено - счётчик и новый текст, не новое событие."""
        if not item.dedupe_key:
            return False
        old = self.by_key.get((item.module, item.dedupe_key))
        if old is None or not dedupe.can_merge(True, old.spoken, old.updated, now):
            return False
        old.count += 1
        old.text, old.updated = item.text, now
        for cand in self.speech.speech.items:
            if cand.item_id == old.id:
                cand.text = event_fact(self.ctx, old)
        self.digest.update_entry(old, text=True)
        self.ctx.log.log_nowait("attention", old.text[:TEXT_LOG], item=old.id, module=old.module, event_kind=old.kind,
                                merged=True, count=old.count)
        return True
