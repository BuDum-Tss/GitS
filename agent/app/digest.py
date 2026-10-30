"""Входящая очередь событий внимания (ТЗ 29, ARC-4; ТЗ 03, ATT-26): одна очередь на все модули.

События срочности ниже «сказать сразу» копятся здесь. Порядок и вытеснение определяет вес записи: срочность × новизна × среда.
Новизна это 1 / число записей того же вида в очереди (рутина одного вида не заглушает редкое); среда (`env_of`): записи модуля
текущей среды весят вдвое больше, чужие вдвое меньше, в обычной среде все равны. Когда очередь полна, уходит запись с наименьшим
весом (при равенстве самая старая), а не просто самая старая. Забрать накопившееся можно инструментом или пачкой для размышления
(`inbox.py`, `take(limit=)` берёт самые весомые, остальное остаётся)."""
from collections import Counter
from typing import Callable

from .attention_core import DIGEST_MAX, AttentionContext, Item

ENV_BOOST, ENV_PENALTY = 2.0, 0.5
NO_MODULE_ENVS = ("assistant", "sleep")          # эти среды не принадлежат модулю: все модули равны


class Digest:
    def __init__(self, ctx: AttentionContext):
        self.ctx = ctx
        self.entries: list[dict] = []
        self.env_of: Callable[[], str | None] = lambda: None     # текущая среда агента (ставит ядро)

    # ── вес ──────────────────────────────────────────────────────────────────

    def weights(self) -> list[float]:
        """Вес каждой записи очереди (в том же порядке)."""
        kinds = Counter(e["kind"] for e in self.entries)
        env = self.env_of()
        if env in NO_MODULE_ENVS:
            env = None
        out = []
        for e in self.entries:
            w = (e["priority"] / 20.0 + 0.5) / kinds[e["kind"]]
            if env is not None:
                w *= ENV_BOOST if e["module"] == env else ENV_PENALTY
            out.append(w)
        return out

    # ── запись ───────────────────────────────────────────────────────────────

    def add(self, item: Item) -> None:
        self.entries.append({"item": item.id, "module": item.module, "kind": item.kind, "text": item.text,
                             "priority": round(item.priority, 1), "count": item.count, "ts": self.ctx.clock()})
        if len(self.entries) > DIGEST_MAX:
            weights = self.weights()
            lowest = min(range(len(self.entries)), key=lambda i: (weights[i], self.entries[i]["ts"], i))
            del self.entries[lowest]

    def update_entry(self, item: Item, *, text: bool) -> None:
        """Склеенный повтор: обновить запись сводки (текст и счётчик либо только счётчик)."""
        for entry in self.entries:
            if entry["item"] == item.id:
                if text:
                    entry["text"] = item.text
                entry["count"] = item.count

    # ── чтение ───────────────────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        return dict(Counter(e["module"] for e in self.entries))

    def take(self, module: str | None = None, limit: int | None = None) -> str:
        """Что накопилось в сводке (ATT-26): текст-факт, самые весомые первыми. `module` берёт только записи модуля, `limit`
        только столько самых весомых; взятое из очереди уходит, остальное остаётся."""
        weights = self.weights()
        chosen = [i for i, e in enumerate(self.entries) if module is None or e["module"] == module]
        chosen.sort(key=lambda i: -weights[i])
        if limit is not None:
            chosen = chosen[:limit]
        if not chosen:
            return "В сводке пусто: ничего заметного не накопилось."
        lines = []
        for i in chosen:
            e = self.entries[i]
            times = f" (×{e['count']})" if e["count"] > 1 else ""
            lines.append(f"- {self.ctx.module_title(e['module'])}: {e['text']}{times}")
        gone = set(chosen)
        self.entries = [e for i, e in enumerate(self.entries) if i not in gone]
        return f"Накопилось в сводке ({len(lines)}):\n" + "\n".join(lines)
