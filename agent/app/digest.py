"""Входящая очередь (сводка) событий внимания (ТЗ 29, ARC-4; ТЗ 03, ATT-26): D1-события копятся по модулям; их можно забрать
инструментом или отдать размышлению пачкой (`inbox.py`)."""
from collections import deque

from .attention_core import DIGEST_MAX, AttentionContext, Item


class Digest:
    def __init__(self, ctx: AttentionContext):
        self.ctx = ctx
        self.buffers: dict[str, deque] = {}

    def add(self, item: Item) -> None:
        buf = self.buffers.setdefault(item.module, deque(maxlen=DIGEST_MAX))
        buf.append({"item": item.id, "kind": item.kind, "text": item.text, "priority": round(item.priority, 1),
                    "count": item.count, "ts": self.ctx.clock()})

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
