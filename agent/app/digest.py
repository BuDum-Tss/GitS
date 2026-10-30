"""Входящая очередь событий внимания (ТЗ 29, ARC-4; ТЗ 03, ATT-26): одна очередь на все модули.

События срочности ниже «сказать сразу» копятся здесь. Порядок и вытеснение определяет вес записи: срочность × новизна × среда.
Новизна это 1 / число записей того же вида в очереди (рутина одного вида не заглушает редкое); среда (`env_of`): записи модуля
текущей среды весят вдвое больше, чужие вдвое меньше, в обычной среде все равны. Когда очередь полна, уходит запись с наименьшим
весом (при равенстве самая старая), а не просто самая старая. Забрать накопившееся можно инструментом или пачкой для размышления
(`inbox.py`, `take(limit=)` берёт самые весомые, остальное остаётся).

Рядом с очередью живёт история судеб событий (`history`, для вкладки «Состояние»): каждое событие внимания записано с исходом:
`queued` (ждёт в очереди), `context` (попало в контекст модели: пачкой для размышления, по просьбе «что я пропустила» или в
контекст ответа), `said` (говорится сразу), `dropped` (выброшено, срочность 0), `evicted` (вытеснено из полной очереди)."""
from collections import Counter, deque
from typing import Callable

from .attention_core import DIGEST_MAX, AttentionContext, Item

ENV_BOOST, ENV_PENALTY = 2.0, 0.5
NO_MODULE_ENVS = ("assistant", "sleep")          # эти среды не принадлежат модулю: все модули равны
HISTORY_MAX = 200
BATCHES_MAX = 20
CONTEXT_EVENTS = 6                               # столько записей очереди видно модели в контексте ответа


class Digest:
    def __init__(self, ctx: AttentionContext):
        self.ctx = ctx
        self.entries: list[dict] = []
        self.env_of: Callable[[], str | None] = lambda: None     # текущая среда агента (ставит ядро)
        self.history: deque[dict] = deque(maxlen=HISTORY_MAX)     # судьбы событий, новые в конце
        self.batches: deque[dict] = deque(maxlen=BATCHES_MAX)     # {"ts", "via", "ids"}: что и когда ушло модели

    # ── история ──────────────────────────────────────────────────────────────

    def _rec(self, item_id: str) -> dict | None:
        for rec in reversed(self.history):
            if rec["id"] == item_id:
                return rec
        return None

    def note(self, item: Item, state: str) -> None:
        """Событие не попало в очередь, но у него есть исход (`said`, `dropped`): записать в историю."""
        self.history.append({"id": item.id, "ts": self.ctx.clock(), "module": item.module, "kind": item.kind,
                             "text": item.text[:300], "priority": round(item.priority, 1), "count": item.count, "state": state,
                             "via": "", "context_ts": 0.0})

    def _mark(self, ids: list[str], via: str) -> None:
        now = self.ctx.clock()
        for item_id in ids:
            rec = self._rec(item_id)
            if rec is not None:
                rec.update(state="context", via=via, context_ts=now)

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
        self.note(item, "queued")
        if len(self.entries) > DIGEST_MAX:
            weights = self.weights()
            lowest = min(range(len(self.entries)), key=lambda i: (weights[i], self.entries[i]["ts"], i))
            gone = self.entries.pop(lowest)
            rec = self._rec(gone["item"])
            if rec is not None and rec["state"] == "queued":
                rec["state"] = "evicted"

    def update_entry(self, item: Item, *, text: bool) -> None:
        """Склеенный повтор: обновить запись сводки (текст и счётчик либо только счётчик)."""
        for entry in self.entries:
            if entry["item"] == item.id:
                if text:
                    entry["text"] = item.text
                entry["count"] = item.count
        rec = self._rec(item.id)
        if rec is not None:
            rec["count"] = item.count
            if text:
                rec["text"] = item.text[:300]

    # ── чтение ───────────────────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        return dict(Counter(e["module"] for e in self.entries))

    def take(self, module: str | None = None, limit: int | None = None, via: str = "asked") -> str:
        """Что накопилось в сводке (ATT-26): текст-факт, самые весомые первыми. `module` берёт только записи модуля, `limit`
        только столько самых весомых; взятое из очереди уходит (в истории оно получает исход `context`, `via`: `batch` пачка для
        размышления, `asked` по просьбе), остальное остаётся."""
        weights = self.weights()
        chosen = [i for i, e in enumerate(self.entries) if module is None or e["module"] == module]
        chosen.sort(key=lambda i: -weights[i])
        if limit is not None:
            chosen = chosen[:limit]
        if not chosen:
            return "В сводке пусто: ничего заметного не накопилось."
        lines, ids = [], []
        for i in chosen:
            e = self.entries[i]
            ids.append(e["item"])
            times = f" (×{e['count']})" if e["count"] > 1 else ""
            lines.append(f"- {self.ctx.module_title(e['module'])}: {e['text']}{times}")
        gone = set(chosen)
        self.entries = [e for i, e in enumerate(self.entries) if i not in gone]
        self._mark(ids, via)
        self.batches.append({"ts": self.ctx.clock(), "via": via, "ids": ids})
        return f"Накопилось в сводке ({len(lines)}):\n" + "\n".join(lines)

    def peek(self, limit: int = CONTEXT_EVENTS) -> str:
        """Самые весомые записи очереди для контекста ответа, не забирая их: она видит, на что может отреагировать. В истории
        они помечаются `context` (via `reply`), из очереди не уходят."""
        if not self.entries:
            return ""
        weights = self.weights()
        order = sorted(range(len(self.entries)), key=lambda i: -weights[i])[:limit]
        lines, ids = [], []
        for i in order:
            e = self.entries[i]
            ids.append(e["item"])
            times = f" (×{e['count']})" if e["count"] > 1 else ""
            lines.append(f"- {self.ctx.module_title(e['module'])}: {e['text'][:200]}{times}")
        self._mark(ids, "reply")
        return "События, которые накопились и ещё ждут (для тебя, не тема разговора: упоминай только то, что уместно сейчас):\n" + "\n".join(lines)

    def view(self, now: float) -> dict:
        """Для вкладки «Состояние»: очередь с весами и история исходов (новые первыми)."""
        weights = self.weights()
        queue = [{"id": e["item"], "module": e["module"], "kind": e["kind"], "text": e["text"], "count": e["count"],
                  "priority": e["priority"], "weight": round(w, 2), "age_s": round(now - e["ts"], 1)}
                 for e, w in zip(self.entries, weights, strict=True)]
        queue.sort(key=lambda r: -r["weight"])
        history = [dict(rec, age_s=round(now - rec["ts"], 1)) for rec in list(self.history)[-120:]][::-1]
        return {"queue": queue, "history": history, "batches": list(self.batches)[-5:][::-1]}
