"""Выбор ярких моментов журнала и запись их эпизодами (ТЗ 27, EPI-2, EPI-3, EPI-7): дешёвая часть, без LLM.

Читает события после отметки «обработано до события N» (`meta.moments_upto` в хранилище эпизодов), выбирает моменты чистыми
функциями `moments.select_moments` и пишет закрытые в статусе `pending` вместе с источниками. Повторный запуск ничего не дублирует:
id эпизода зависит только от диапазона событий. Отметка не уходит за открытые моменты и за последние `before_s` секунд (в них
ещё может появиться пик, контекст которого начнётся раньше). Если отметки нет, чтение начинается с конца журнала (`first_upto`
или `source.last_id`): прошлое само по себе не обрабатывается, для него есть `backfill`."""
import asyncio
import dataclasses
import logging
import time
from typing import Callable

from interfaces.contracts import EventSource
from services.memory_service import moments as mom

logger = logging.getLogger("episodes")

READ_LIMIT = 3000
MARK = "moments_upto"


class MomentSelector:
    def __init__(self, *, source: EventSource, store, settings: Callable[[], dict], clock: Callable[[], float] = time.time,
                 character: Callable[[], str] = lambda: "", first_upto: Callable[[], int] | None = None):
        """store - `EpisodeStore`; settings() -> настройки ядра (раздел `episodes`); character() -> id персонажа;
        first_upto() -> с какого события начинать, если отметки ещё нет (по умолчанию конец журнала)."""
        self.source, self.store, self.settings, self.clock = source, store, settings, clock
        self.character = character
        self.first_upto = first_upto or source.last_id

    def cfg(self) -> mom.MomentCfg:
        raw = (self.settings() or {}).get("episodes")
        raw = raw if isinstance(raw, dict) else {}
        moments = raw.get("moments") if isinstance(raw.get("moments"), dict) else {}
        live = raw.get("live") if isinstance(raw.get("live"), dict) else {}
        return mom.MomentCfg.from_settings({**{k: live[k] for k in ("instant_score", "instant_after_s") if k in live}, **moments})

    async def run(self, *, final: bool = False, should_stop: Callable[[], bool] = lambda: False) -> dict:
        """Записать закрытые моменты после отметки. final=True закрывает и идущие сейчас. Возвращает
        {"episodes": новых, "calm": 0, "upto": отметка}."""
        return await asyncio.to_thread(self._run, final, should_stop)

    async def backfill(self, since_ts: float, limit: int = 20, should_stop: Callable[[], bool] = lambda: False) -> int:
        """Разовое наполнение памяти: `limit` самых ярких моментов истории журнала с `since_ts`; отметка не двигается."""
        return await asyncio.to_thread(self._backfill, float(since_ts), int(limit), should_stop)

    # ── обычный ход ──────────────────────────────────────────────────────────

    def _run(self, final: bool, should_stop) -> dict:
        cfg = self.cfg()
        upto = self.store.get_meta(MARK)
        if upto is None:
            upto = int(self.first_upto())
            self.store.set_meta(MARK, upto)
        made = 0
        for _ in range(20):
            if should_stop():
                break
            rows = self.source.after(upto, READ_LIMIT, mom.READ_KINDS)
            if not rows:
                break
            full = len(rows) >= READ_LIMIT
            now = self.clock()
            if full:                                          # окно кончилось на середине: хвост считается открытым
                now = min(now, float(rows[-1].get("ts") or now))
            new_upto, n = self._process(rows, cfg, now, final and not full)
            if full and new_upto <= upto:                     # страница плотнее окна контекста: режем как есть
                new_upto, n = self._process(rows, cfg, now, True)
            made += n
            if new_upto <= upto:
                break
            upto = new_upto
            self.store.set_meta(MARK, upto)
            if not full:
                break
        return {"episodes": made, "calm": 0, "upto": upto}

    def _process(self, rows: list[dict], cfg: mom.MomentCfg, now: float, final: bool) -> tuple[int, int]:
        """Строки -> запись закрытых моментов; возвращает (новая отметка, записано эпизодов)."""
        taken = self.store.days()
        moments = mom.select_moments(rows, cfg, now, taken=taken, final=final)
        made = 0
        for m in moments:
            if m.closed and not self.store.has_peak(m.peaks) and self.store.add_episode(self._episode(m), self._events(m)):
                made += 1
        open_first = min((m.first_id for m in moments if not m.closed), default=None)
        limit_ts = float("inf") if final else now - cfg.before_s
        mark = 0
        for r in rows:
            if (open_first is not None and r["id"] >= open_first) or float(r.get("ts") or 0) >= limit_ts:
                break
            mark = r["id"]
        return mark, made

    # ── история ──────────────────────────────────────────────────────────────

    def _backfill(self, since_ts: float, limit: int, should_stop) -> int:
        cfg = self.cfg()
        cfg = dataclasses.replace(cfg, per_day=10 ** 6)
        rows, after = [], 0
        while not should_stop():
            page = self.source.after(after, READ_LIMIT, mom.READ_KINDS)
            if not page:
                break
            after = page[-1]["id"]
            rows += [r for r in page if float(r.get("ts") or 0) >= since_ts]
        if should_stop():
            return 0
        best = sorted(mom.select_moments(rows, cfg, self.clock(), final=True), key=lambda m: (-m.salience, m.first_id))[:max(0, limit)]
        return sum(1 for m in best if not self.store.has_peak(m.peaks) and self.store.add_episode(self._episode(m), self._events(m)))

    # ── запись ───────────────────────────────────────────────────────────────

    def _episode(self, m: mom.Moment) -> dict:
        real = [e for e in m.events if not e.scene]
        actors = sorted({e.actor for e in real if e.actor})
        who = ["user"] if any(e.kind == "user" for e in real) else []
        peak = next((e for e in m.events if e.id == m.peak_id), m.events[0])
        module = str(peak.label.split(".")[0]) if peak.kind in ("attention", "module_event") and "." in peak.label else ""
        return {"id": m.id, "t_start": m.events[0].ts, "t_end": m.events[-1].ts, "first_id": m.first_id, "last_id": m.last_id,
                "stream": "moment", "module": module, "participants": who + actors, "peak_id": m.peak_id,
                "salience": round(m.salience, 3), "status": "pending",
                "character": self.character(), "day": m.day}

    @staticmethod
    def _events(m: mom.Moment) -> list[dict]:
        return [{"id": e.id, "ts": e.ts, "kind": e.label if e.kind in ("attention", "module_event") else e.kind,
                 "text": (f"{e.actor}: " if e.actor else "") + e.text, "salience": e.score} for e in m.events]
