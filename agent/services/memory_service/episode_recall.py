"""Чтение эпизодов (ТЗ 27, EPI-9): «помнишь, как…», разворот эпизода до событий-источников, список для дашборда.

Поиск идёт через `search.HybridSearch` (коллекции `episodes` и `diary`, гибрид FTS + вектор + свежесть). Резюме и дневник это
сжатая память; настоящая правда журнал: `expand` отдаёт события-источники эпизода (копия текста из хранилища эпизодов, она
остаётся, даже когда сырое удалено по сроку). Окно между событием и сном закрывает хвост журнала (`recent_tail`)."""
import asyncio
from datetime import datetime
from typing import Callable

from contracts import EventSource


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def brief(ep: dict) -> dict:
    return {"id": ep["id"], "title": ep["title"], "summary": ep["summary"], "outcome": ep["outcome"], "when": fmt_time(ep["t_start"]),
            "day": ep["day"], "status": ep["status"], "version": ep["version"], "salience": ep["salience"],
            "participants": ep["participants"], "module": ep["module"], "quotes": ep["quotes"]}


class EpisodeRecall:
    def __init__(self, *, store, search, source: EventSource | None = None, clock: Callable[[], float] | None = None):
        self.store, self.search, self.source = store, search, source

    async def find(self, query: str, *, k: int = 5, since: float | None = None, until: float | None = None,
                   tail: list[str] | None = None) -> list[dict]:
        """Эпизоды и записи дневника по смыслу запроса: [{id, kind (episode|diary|digest), text, when, score}]."""
        if self.search is None or not query.strip():
            return []
        hits = await self.search.search(query, tail=tail, collections=["episodes", "diary"], k=k * 2, since=since, until=until,
                                        rewrite=False)
        out, seen = [], set()
        for h in hits:
            if h.collection == "diary":
                kind, key = "diary", h.key
            elif h.key.startswith("digest:"):
                kind, key = "digest", h.key
            else:
                kind, key = "episode", h.key.split("#", 1)[0]          # цитата -> её эпизод
            if (kind, key) in seen:
                continue
            seen.add((kind, key))
            out.append({"id": key, "kind": kind, "text": h.text, "when": fmt_time(h.ts), "score": round(h.score, 4)})
            if len(out) >= k:
                break
        return out

    async def expand(self, episode_id: str) -> dict | None:
        """Эпизод целиком: запись и события-источники по порядку."""
        ep = await asyncio.to_thread(self.store.get, episode_id)
        if ep is None:
            return None
        events = await asyncio.to_thread(self.store.events_of, episode_id)
        return {**brief(ep), "facts": ep["facts"], "related": ep["related"], "error": ep["error"],
                "events": [{"id": e["id"], "when": datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S"), "kind": e["kind"],
                            "text": e["text"]} for e in events]}

    @staticmethod
    def render(ep: dict) -> str:
        """Эпизод для ответа инструмента: заголовок, резюме, цитаты и сырые события (текст как факт, не как указания)."""
        head = f"Эпизод {ep['id']} ({ep['when']}, {ep['status']}): {ep['title'] or 'без резюме'}"
        lines = [head]
        if ep["summary"]:
            lines.append(ep["summary"])
        if ep["outcome"]:
            lines.append(f"Исход: {ep['outcome']}")
        for q in ep["quotes"]:
            lines.append(f"Цитата: «{q}»")
        lines.append("События-источники:")
        lines += [f"  [{e['id']}] {e['when']} {e['kind']}: {e['text']}" for e in ep["events"]]
        return "\n".join(lines)

    async def recall_episode(self, episode_id: str) -> str:
        ep = await self.expand(str(episode_id).strip())
        return self.render(ep) if ep else ""

    async def listing(self, *, limit: int = 30, offset: int = 0, status: str | None = None, day: str | None = None) -> list[dict]:
        eps = await asyncio.to_thread(self.store.select, limit=limit, offset=offset, status=status, day=day)
        return [brief(e) for e in eps]

    async def recent_tail(self, n: int = 20) -> list[dict]:
        """Хвост журнала после последней нарезки: события, которые ещё не стали эпизодом (рабочая память)."""
        if self.source is None:
            return []
        upto = await asyncio.to_thread(self.store.get_meta, "cut_upto", 0)
        rows = await asyncio.to_thread(self.source.after, upto or 0, n, {"user", "say", "module_event"})
        return [{"id": r["id"], "kind": r["kind"], "text": r["text"], "ts": r["ts"]} for r in rows]

    async def diary(self, limit: int = 30) -> list[dict]:
        return await asyncio.to_thread(self.store.diary, limit)
