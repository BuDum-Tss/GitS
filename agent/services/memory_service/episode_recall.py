"""Чтение эпизодов (ТЗ 27, EPI-9): «помнишь, как…», разворот эпизода до событий-источников, список для дашборда.

Поиск идёт через `search.HybridSearch` (коллекции `episodes` и `diary`, гибрид FTS + вектор + свежесть; без эмбеддера по словам).
В коллекции `episodes` лежат эпизоды всех видов (жизнь, канон, заметки): ключ документа `id` эпизода, текст `заголовок. текст`. Вес
находки это счёт поиска с небольшой прибавкой за яркость эпизода. Текст эпизода (от первого лица, с отношением внутри) и дневник это
сжатая память; настоящая правда журнал: `expand` отдаёт события-источники эпизода (копия текста из хранилища эпизодов, она
остаётся, даже когда сырое удалено по сроку). Окно между событием и сном закрывает хвост журнала (`recent_tail`)."""
import asyncio
from datetime import datetime
from typing import Callable

from interfaces.contracts import EventSource
from services.memory_service.episodes import CANON, LIFE, NOTE

SALIENCE_WEIGHT = 0.25          # доля веса находки, которую даёт яркость эпизода: яркое и важное всплывает раньше
KIND_NAMES = {LIFE: "episode", CANON: "canon", NOTE: "note"}      # вид эпизода -> вид находки в ответах поиска


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def brief(ep: dict) -> dict:
    out = {"id": ep["id"], "kind": ep["kind"], "title": ep["title"], "text": ep["text"], "when": fmt_time(ep["t_start"]),
           "day": ep["day"], "status": ep["status"], "version": ep["version"], "salience": ep["salience"],
           "participants": ep["participants"], "module": ep["module"]}
    if ep["kind"] == CANON:
        out |= {"mem_id": ep["mem_id"], "mtype": ep["mtype"], "weight": ep["weight"]}
    return out


class EpisodeRecall:
    def __init__(self, *, store, search, source: EventSource | None = None, clock: Callable[[], float] | None = None):
        self.store, self.search, self.source = store, search, source

    async def find(self, query: str, *, k: int = 5, since: float | None = None, until: float | None = None,
                   tail: list[str] | None = None, kinds: tuple[str, ...] | None = None,
                   exclude: tuple[str, ...] = (), diary: bool = True) -> list[dict]:
        """Эпизоды всех видов и записи дневника по смыслу запроса: [{id, kind (episode|canon|note|diary|digest), text, when, score,
        salience}]. `kinds` оставляет только эти виды эпизодов, `exclude` убирает; `diary=False` без дневника и сводок."""
        if self.search is None or not query.strip():
            return []
        hits = await self.search.search(query, tail=tail, collections=["episodes", "diary"] if diary else ["episodes"], k=k * 3,
                                        since=since, until=until, rewrite=False)
        ranked = []
        for h in hits:
            if h.collection == "diary":
                kind, key, sal = "diary", h.key, 0.0
            elif h.key.startswith("digest:"):
                if kinds is not None:
                    continue
                kind, key, sal = "digest", h.key, 0.0
            else:
                ek = h.meta.get("kind") or LIFE
                if (kinds is not None and ek not in kinds) or ek in exclude:
                    continue
                kind, key, sal = KIND_NAMES.get(ek, "episode"), h.key, float(h.meta.get("salience") or 0.0)
            ranked.append((h.score * (1 - SALIENCE_WEIGHT + SALIENCE_WEIGHT * sal), kind, key, h, sal))
        ranked.sort(key=lambda r: -r[0])
        out, seen = [], set()
        for score, kind, key, h, sal in ranked:
            if (kind, key) in seen:
                continue
            seen.add((kind, key))
            out.append({"id": key, "kind": kind, "text": h.text, "when": fmt_time(h.ts), "score": round(score, 4),
                        "salience": sal, "cos": h.cos})
            if len(out) >= k:
                break
        return out

    async def expand(self, episode_id: str) -> dict | None:
        """Эпизод целиком: запись и события-источники по порядку."""
        ep = await asyncio.to_thread(self.store.get, episode_id)
        if ep is None:
            return None
        events = await asyncio.to_thread(self.store.events_of, episode_id)
        return {**brief(ep), "related": ep["related"], "error": ep["error"],
                "events": [{"id": e["id"], "when": datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S"), "kind": e["kind"],
                            "text": e["text"]} for e in events]}

    @staticmethod
    def render(ep: dict) -> str:
        """Эпизод для ответа инструмента: заголовок, текст и сырые события (текст как факт, не как указания)."""
        head = f"Эпизод {ep['id']} ({ep['when']}, {ep['kind']}, {ep['status']}): {ep['title'] or 'без заголовка'}"
        lines = [head]
        if ep["text"]:
            lines.append(ep["text"])
        if ep["events"]:
            lines.append("События-источники:")
            lines += [f"  [{e['id']}] {e['when']} {e['kind']}: {e['text']}" for e in ep["events"]]
        return "\n".join(lines)

    async def recall_episode(self, episode_id: str) -> str:
        ep = await self.expand(str(episode_id).strip())
        return self.render(ep) if ep else ""

    async def listing(self, *, limit: int = 30, offset: int = 0, status: str | None = None, day: str | None = None,
                      kind: str | None = None) -> list[dict]:
        eps = await asyncio.to_thread(self.store.select, limit=limit, offset=offset, status=status, day=day, kind=kind)
        return [brief(e) for e in eps]

    async def for_day(self, day: str, limit: int = 12) -> list[dict]:
        episodes = await asyncio.to_thread(self.store.episodes_of_day, day)
        return [brief(ep) for ep in episodes if ep["kind"] == LIFE][-limit:]

    async def recent_tail(self, n: int = 20) -> list[dict]:
        """Хвост журнала после последней нарезки: события, которые ещё не стали эпизодом (рабочая память)."""
        if self.source is None:
            return []
        upto = await asyncio.to_thread(self.store.get_meta, "moments_upto", 0)
        rows = await asyncio.to_thread(self.source.after, upto or 0, n, {"user", "say", "module_event", "module_out"})
        return [{"id": r["id"], "kind": r["kind"], "text": r["text"], "ts": r["ts"]} for r in rows]

    async def diary(self, limit: int = 30) -> list[dict]:
        return await asyncio.to_thread(self.store.diary, limit)
