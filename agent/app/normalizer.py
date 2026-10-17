"""Нормализация события модуля (ТЗ 29, ARC-4; ТЗ 03): тело события -> Item, отправитель -> человек и память (TG-14)."""
import logging
import secrets

import latency_trace
from domain.event import ORIGIN_EXTERNAL, ORIGIN_SELF, SELF_MODULE

from .attention_core import SPEECH_DEFAULT_URGENCY, TEXT_MAX, AttentionContext, Item

logger = logging.getLogger("attention")


class Normalizer:
    def __init__(self, ctx: AttentionContext):
        self.ctx = ctx

    @staticmethod
    def make_item(module_id: str, ev: dict, now: float) -> Item:
        sender = ev.get("sender") if isinstance(ev.get("sender"), dict) else None
        reply = ev.get("reply_channel") if isinstance(ev.get("reply_channel"), dict) else None
        hint = ev.get("urgency_hint")
        hint = int(hint) if isinstance(hint, (int, float)) and not isinstance(hint, bool) else None
        ttl = ev.get("expires_in_s")
        return Item(id="a-" + secrets.token_hex(4), module=module_id, kind=str(ev.get("kind") or f"{module_id}.event"),
                    text=str(ev.get("text") or "")[:TEXT_MAX], data=ev.get("data") if isinstance(ev.get("data"), dict) else {},
                    sender=sender, reply_channel=reply,
                    addressed_to_me=ev["addressed_to_me"] if isinstance(ev.get("addressed_to_me"), bool) else None,
                    urgency=None, needs_reply=False, memorable=bool(ev.get("memorable")), created=now,
                    expires=now + float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 else None,
                    dedupe_key=str(ev["dedupe_key"]) if ev.get("dedupe_key") else None, urgency_hint=hint, updated=now,
                    trace=latency_trace.from_bus(ev.get("trace"), str(ev.get("kind") or f"{module_id}.event")),
                    origin=ORIGIN_SELF if module_id == SELF_MODULE else ORIGIN_EXTERNAL)

    @staticmethod
    def make_speech_item(module_id: str, say: dict, text: str, now: float) -> tuple[Item, str, float | None]:
        """Item для речи модуля (тело `say`, ТЗ 01 раздел 7.2) и её режим и срок жизни."""
        mode = "verbatim" if say.get("mode", "fact") == "verbatim" else "fact"
        hint = say.get("urgency_hint")
        hint = int(hint) if isinstance(hint, (int, float)) and not isinstance(hint, bool) else SPEECH_DEFAULT_URGENCY
        ttl = say.get("expires_in_s")
        ttl = float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 else None
        item = Item(id="a-" + secrets.token_hex(4), module=module_id, kind=f"{module_id}.say", text=text[:TEXT_MAX], data={},
                    sender=None, reply_channel=None, addressed_to_me=None, urgency=hint, needs_reply=False,
                    memorable=False, created=now, expires=None, dedupe_key=None, urgency_hint=hint, updated=now)
        return item, mode, ttl

    @staticmethod
    def is_owner_address(item: Item) -> bool:
        """R1: владелец обращается к ней (addressed_to_me или личка) и ему есть куда ответить."""
        return bool(item.sender and item.sender.get("is_owner") and item.reply_channel
                    and (item.addressed_to_me is True or item.kind.endswith(".dm")))

    def note_sender(self, item: Item, *, skip_memory: bool) -> None:
        """TG-14: общее правило для любых модулей. Если у события есть sender.id и sender.name: человек заводится в
        PeopleDirectory (ensure_person, source - модуль) и его реплика записывается (note). Если событие memorable: оно идёт
        в очередь графа (MemoryWriter); сырой текст события в долгую память не пишется: событие запоминают эпизоды. Всё до решения, чтобы профиль и память не
        зависели от того, будет ли событие озвучено. Обращение владельца (R1) идёт разговором: его реплику в граф и память
        кладёт сам разговор, второй раз не пишем. Сбой записи событие не останавливает."""
        sender = item.sender
        if not sender or not sender.get("id") or not sender.get("name"):
            return
        ident, name = str(sender["id"]), str(sender["name"])
        try:
            item.first_contact = bool(self.ctx.people.ensure_person(ident, name, source=item.module))
            self.ctx.people.note(ident, item.text)
            if item.memorable and not skip_memory:
                self.ctx.writer.enqueue_for_graph(name, item.text)
                item.memory_done = True
        except Exception as e:
            logger.warning(f"Запись отправителя {ident} в relationships и память: {e}")
