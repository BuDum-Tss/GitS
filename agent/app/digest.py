"""Сводка событий внимания (ТЗ 29, ARC-4; ТЗ 03, ATT-26, ATT-27): D1-события копятся по модулям, потом их можно забрать
или предложить рассказать."""
from collections import deque

from .attention_core import (
    DIGEST_MAX,
    DIGEST_MIN_ITEMS,
    DIGEST_MIN_PRIORITY,
    DIGEST_QUIET_S,
    DIGEST_SPEECH_MAX,
    AttentionContext,
    Item,
)


class Digest:
    def __init__(self, ctx: AttentionContext):
        self.ctx = ctx
        self.buffers: dict[str, deque] = {}
        self.offered_at = 0.0
        self.speech = None          # SpeechCoordinator, ставит фасад

    def add(self, item: Item) -> None:
        buf = self.buffers.setdefault(item.module, deque(maxlen=DIGEST_MAX))
        buf.append({"item": item.id, "kind": item.kind, "text": item.text, "priority": round(item.priority, 1),
                    "count": item.count, "ts": self.ctx.clock(), "offered": False})

    def update_entry(self, item: Item, *, text: bool) -> None:
        """Склеенный повтор: обновить запись сводки (текст и счётчик либо только счётчик)."""
        for entry in self.buffers.get(item.module, ()):
            if entry["item"] == item.id:
                if text:
                    entry["text"] = item.text
                entry["count"] = item.count

    def counts(self) -> dict[str, int]:
        return {m: len(buf) for m, buf in self.buffers.items() if buf}

    def take(self, module: str | None = None) -> str:
        """Что накопилось в сводке (ATT-26): текст-факт; после выдачи буфер очищается."""
        modules = [module] if module else list(self.buffers)
        lines: list[tuple[float, str]] = []
        for m in modules:
            buf = self.buffers.pop(m, None)
            for e in buf or ():
                times = f" (×{e['count']})" if e["count"] > 1 else ""
                lines.append((e["priority"], f"- {self.ctx.module_title(m)}: {e['text']}{times}"))
        if not lines:
            return "В сводке пусто: ничего заметного не накопилось."
        lines.sort(key=lambda x: -x[0])
        return f"Накопилось в сводке ({len(lines)}):\n" + "\n".join(t for _, t in lines)

    def maybe_offer(self) -> None:
        """ATT-27: фокус вернулся в idle после разговора или реакции; накопилось >= 3 записей с priority >= 25; за 10 минут
        не было самостоятельных реплик - в очередь речи факт «накопилось» (не больше 5 самых важных)."""
        now = self.ctx.clock()
        if now - self.speech.speech.last_initiative < DIGEST_QUIET_S or now - self.offered_at < DIGEST_QUIET_S:
            return
        fresh = [(e, m) for m, buf in self.buffers.items() for e in buf
                 if not e["offered"] and e["priority"] >= DIGEST_MIN_PRIORITY]
        if len(fresh) < DIGEST_MIN_ITEMS:
            return
        fresh.sort(key=lambda x: -x[0]["priority"])
        top = fresh[:DIGEST_SPEECH_MAX]
        for e, _ in top:
            e["offered"] = True
        facts = "; ".join(f"{self.ctx.module_title(m)}: {e['text']}" for e, m in top)
        self.offered_at = now
        self.speech.speech.new(source="attention:digest", mode="fact", text=f"Накопилось: {facts}", priority=40, ttl=600.0,
                               key="attention:digest")
        self.speech.kick()
