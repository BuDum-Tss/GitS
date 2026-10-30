"""Входящая очередь: события срочности ниже «сказать сразу» копятся в сводке и уходят размышлению пачкой.

Вместо оценки важности перед моделью модель сама видит накопившееся и решает, о чём сказать, что запомнить, а что пропустить.
Будит её `tick` внимания, если сразу выполнены условия: в очереди есть что-то заметное (срочность 2+) либо набралось много
мелочи; прошло не меньше `min_interval_s` с прошлой пачки; нет разговора и вопроса; «не отвлекать» выключено; пользователь
не отошёл; очередь речи свободна. Пачка это одно событие `inbox.batch` без отправителя: оно недоверенное, по нему можно
только сказать или запомнить, но не действовать.
"""
from .attention_core import AttentionContext, Item

NOTABLE_URGENCY = 2        # события этой срочности и выше сами по себе стоят пачки
MANY_ITEMS = 8             # столько мелочи тоже стоит пачки
BATCH_MAX = 10             # в пачку идут самые весомые записи очереди, остальное ждёт следующей
BATCH_KIND = "inbox.batch"


class Inbox:
    def __init__(self, ctx: AttentionContext, digest, focus, speech):
        self.ctx, self.digest, self.focus, self.speech = ctx, digest, focus, speech
        self.last_wake = 0.0

    def _entries(self) -> list[dict]:
        return self.digest.entries

    def ready(self, now: float) -> bool:
        ctx = self.ctx
        cfg = ctx.settings()
        inbox = ctx.att(cfg).get("inbox") or {}
        if ctx.on_think is None or now - self.last_wake < float(inbox.get("min_interval_s", 60.0)):
            return False
        entries = self._entries()
        if not entries:
            return False
        if not (len(entries) >= MANY_ITEMS or any(e["priority"] >= NOTABLE_URGENCY * 20 for e in entries)):
            return False
        if self.focus.top().kind in ("conversation", "question") or ctx.quiet(cfg):
            return False
        if ctx.idle() > ctx.att(cfg)["away_after_s"] or self.speech.speech.busy():
            return False
        return True

    def tick(self, now: float) -> bool:
        """Если пора, отдать накопившееся размышлению. True, если пачка ушла."""
        if not self.ready(now):
            return False
        self.last_wake = now
        text = self.digest.take(limit=BATCH_MAX)
        item = Item(id="inbox-" + str(int(now * 1000)), module="inbox", kind=BATCH_KIND, text=text, data={}, sender=None,
                    reply_channel=None, addressed_to_me=False, urgency=2, needs_reply=False, memorable=False, created=now,
                    expires=None, dedupe_key=None, urgency_hint=2, updated=now)
        self.ctx.on_think(item)
        return True
