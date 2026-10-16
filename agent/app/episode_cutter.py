"""Нарезка журнала на эпизоды (ТЗ 27, EPI-2, EPI-3, EPI-7): дешёвая часть, без LLM.

Читает события после отметки «обработано до события N» (`meta.cut_upto` в хранилище эпизодов), режет закрытые сегменты
чистыми функциями `domain.episodes` и пишет эпизоды в статусе `pending` вместе с источниками. Отметка и эпизоды сдвигаются
в одной транзакции, повторный запуск ничего не дублирует (id эпизода зависит только от диапазона событий).
Спокойные события идут счётчиками в суточную сводку."""
import asyncio
import logging
import time
from datetime import datetime
from typing import Callable

import salience
from contracts import EventSource
from domain import episodes as dom

logger = logging.getLogger("episodes")

READ_LIMIT = 3000
KINDS = set(dom.CHAT_KINDS) | {"module_event", "focus", *dom.STREAM_KINDS, *dom.BOUNDARY_KINDS}


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def enrich(row: dict) -> dict:
    """Яркость событий модулей: журнал внимания хранит срочность, оценку и вид события; веса берёт `salience` (ТЗ 24)."""
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    kind = row.get("kind")
    if "salience" in data or kind not in ("module_event", "game", "telegram"):
        return row
    f = salience.factors_of(urgency=data.get("urgency_hint"), appraisal=data.get("appraisal_importance"),
                            memorable=bool(data.get("memorable")), kind=str(data.get("event_kind") or kind))
    return {**row, "data": {**data, "salience": salience.score(f)[0]}}


class EpisodeCutter:
    def __init__(self, *, source: EventSource, store, settings: Callable[[], dict], clock: Callable[[], float] = time.time,
                 character: Callable[[], str] = lambda: "", first_upto: Callable[[], int] | None = None):
        """store - `EpisodeStore`; settings() -> настройки ядра (раздел `episodes`); character() -> id персонажа;
        first_upto() -> с какого события начинать, если отметки ещё нет (новый персонаж не режет чужую историю)."""
        self.source, self.store, self.settings, self.clock = source, store, settings, clock
        self.character, self.first_upto = character, first_upto

    def cfg(self) -> dom.CutCfg:
        raw = (self.settings() or {}).get("episodes")
        return dom.CutCfg.from_settings((raw or {}).get("cut") if isinstance(raw, dict) else None)

    async def run(self, *, final: bool = False, should_stop: Callable[[], bool] = lambda: False) -> dict:
        """Нарезать всё закрытое после отметки. final=True закрывает и последний открытый сегмент (долгий простой).
        Возвращает {"episodes": новых, "calm": спокойных событий, "upto": отметка}."""
        return await asyncio.to_thread(self._run, final, should_stop)

    def _run(self, final: bool, should_stop) -> dict:
        cfg = self.cfg()
        upto = self.store.get_meta("cut_upto")
        if upto is None:
            upto = self.first_upto() if self.first_upto else 0
            self.store.set_meta("cut_upto", upto)
        made = calm_n = 0
        for _ in range(20):
            if should_stop():
                break
            rows = self.source.after(upto, READ_LIMIT, KINDS)
            if not rows:
                break
            full = len(rows) >= READ_LIMIT
            recs = [r for r in (dom.rec_of(enrich(row)) for row in rows) if r is not None]
            now = self.clock()
            if full and recs:                      # окно кончилось на середине: хвост считается открытым
                now = min(now, recs[-1].ts)
            segs, new_upto = dom.split_segments(recs, cfg, now, final=final and not full)
            if full and new_upto <= upto and recs:
                segs, new_upto = dom.split_segments(recs, cfg, now, final=True)       # один сегмент больше окна: режем как есть
            if new_upto <= upto:
                break
            m, c = self._store_segments(segs, cfg, new_upto)
            made, calm_n, upto = made + m, calm_n + c, new_upto
            if not full:
                break
        return {"episodes": made, "calm": calm_n, "upto": upto}

    def _store_segments(self, segs: list[list[dom.Rec]], cfg: dom.CutCfg, new_upto: int) -> tuple[int, int]:
        made = calm_n = 0
        for seg in segs:
            cands, calm = dom.cut(seg, cfg)
            for c in cands:
                if self.store.add_episode(self._episode(c), self._events(c)):
                    made += 1
            by_day: dict[str, list[dom.Rec]] = {}
            for r in calm:
                by_day.setdefault(day_of(r.ts), []).append(r)
            for day, recs in by_day.items():
                kinds: dict[str, int] = {}
                for r in recs:
                    kinds[r.kind] = kinds.get(r.kind, 0) + 1
                self.store.add_calm(day, len(recs), kinds, recs[0].id, recs[-1].id, marker=f"{seg[-1].id}:{day}")
                calm_n += len(recs)
        self.store.set_meta("cut_upto", new_upto)
        return made, calm_n

    def _episode(self, c: dom.Candidate) -> dict:
        actors = sorted({e.actor for e in c.events if e.actor})
        who = ["user"] if any(e.kind == "user" for e in c.events) else []
        module = "" if c.stream == "chat" else c.stream.split(":")[0]
        emotions = sorted({str(e.data.get("emotion")) for e in c.events if e.kind == "emotion" and e.data.get("emotion")})
        return {"id": c.id, "t_start": c.events[0].ts, "t_end": c.events[-1].ts, "first_id": c.first_id, "last_id": c.last_id,
                "stream": c.stream, "module": module, "participants": who + actors, "peak_id": c.peak_id,
                "salience": round(c.salience, 3), "emotions": emotions, "status": dom.PENDING,
                "character": self.character(), "day": day_of(c.events[0].ts)}

    @staticmethod
    def _events(c: dom.Candidate) -> list[dict]:
        return [{"id": e.id, "ts": e.ts, "kind": e.kind, "text": (f"{e.actor}: " if e.actor else "") + e.text,
                 "salience": e.salience} for e in c.events]
