"""Конвейер сна (ТЗ 27, EPI-5, EPI-7, EPI-10): нарезка -> резюме -> факты в граф -> эмбеддинги -> связи -> сводки -> дневник ->
сжатие старого.

Единица работы один эпизод; перед каждым эпизодом и между шагами проверяется `should_stop` (безопасная точка), запросы к модели
короткие. Все шаги идемпотентны: у эпизода статус (`pending`, `summarized`, `embedded`, `failed`), у нарезки отметка «обработано до
события N», у фактов флаг `facts_done`, у уведомления флаг `notified`. Прерванный сон продолжается с того же места, повтор безопасен.
Недоступная модель останавливает LLM-шаги, а не портит данные: эпизоды остаются в прежнем статусе до следующего сна."""
import asyncio
import logging
import time
from datetime import datetime
from typing import Awaitable, Callable

from app.diary import DayWriter
from app.episode_summary import EpisodeSummarizer
from contracts import EventSource, MemoryWriter
from domain import episodes as dom

logger = logging.getLogger("episodes")

Notify = Callable[[dict], Awaitable[None]]


def episodes_cfg(settings: Callable[[], dict]) -> dict:
    raw = (settings() or {}).get("episodes")
    return raw if isinstance(raw, dict) else {}


class SleepRunner:
    def __init__(self, *, cutter, summarizer: EpisodeSummarizer, store, search, writer: MemoryWriter, days: DayWriter,
                 source: EventSource, settings: Callable[[], dict], clock: Callable[[], float] = time.time,
                 notify: Notify | None = None, hooks: list | None = None):
        """notify(эпизод) шлёт событие `episode.summary` в конвейер внимания (маршрут think); hooks - сценарии после сна
        (ТЗ 28: разбор ошибок), каждый получает отчёт."""
        self.cutter, self.summarizer, self.store, self.search = cutter, summarizer, store, search
        self.writer, self.days, self.source, self.settings, self.clock = writer, days, source, settings, clock
        self.notify, self.hooks = notify, hooks if hooks is not None else []
        self.spent = 0
        self.embed_down = False

    # ── один сон ─────────────────────────────────────────────────────────────

    async def run(self, plan: dom.SleepPlan, should_stop: Callable[[], bool] = lambda: False) -> dict:
        self.spent, self.embed_down = 0, False
        rep = {"mode": plan.mode, "started": self.clock(), "cut": 0, "summarized": 0, "embedded": 0, "failed": 0, "digests": 0,
               "diary": 0, "compressed": 0, "raw_deleted": 0, "notified": 0, "llm_down": False, "interrupted": False, "tokens": 0}

        def stop() -> bool:
            return should_stop() or (plan.budget_tokens is not None and self.spent >= plan.budget_tokens)

        steps = (self._step_cut, self._step_episodes, self._step_days, self._step_compress)
        for step in steps:
            if stop():
                rep["interrupted"] = True
                break
            await step(rep, stop)
        rep["tokens"] = self.spent
        rep["interrupted"] = rep["interrupted"] or should_stop()
        rep["finished"] = self.clock()
        if not rep["interrupted"]:
            await asyncio.to_thread(self.store.set_meta, "last_sleep", rep["finished"])
        await asyncio.to_thread(self.store.set_meta, "last_report", rep)
        for hook in self.hooks:
            try:
                await hook(rep)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Сон: шаг после сна: {e}")
        return rep

    # ── шаги ─────────────────────────────────────────────────────────────────

    async def _step_cut(self, rep: dict, stop) -> None:
        got = await self.cutter.run(should_stop=stop)
        rep["cut"] = got["episodes"]

    async def _step_episodes(self, rep: dict, stop) -> None:
        cfg = episodes_cfg(self.settings)
        notified = 0
        for status in (dom.PENDING, dom.SUMMARIZED):
            eps = await asyncio.to_thread(self.store.select, status=status, limit=200, oldest_first=True)
            for ep in eps:
                if stop():                                          # безопасная точка между эпизодами
                    rep["interrupted"] = True
                    return
                if status == dom.PENDING:
                    if rep["llm_down"]:
                        continue
                    ep = await self._summarize(ep, rep)
                    if ep is None:
                        continue
                await self._facts(ep)
                if not self.embed_down and await self._embed(ep, cfg):
                    rep["embedded"] += 1
                notified += await self._notify(ep, cfg, notified, rep)

    async def _summarize(self, ep: dict, rep: dict) -> dict | None:
        events = await asyncio.to_thread(self.store.events_of, ep["id"])
        res = await self.summarizer.summarize(ep, events)
        self.spent += res.tokens
        if res.unavailable:
            rep["llm_down"] = True
            return None
        if not res.ok:
            rep["failed"] += 1
            await asyncio.to_thread(self.store.update, ep["id"], status=dom.FAILED, attempts=ep.get("attempts", 0) + 1,
                                    error="; ".join(res.problems)[:300])
            return None
        d = res.data
        parts = sorted({*ep.get("participants", []), *d["participants"]})
        emotions = sorted({*ep.get("emotions", []), *d["emotions"]})
        await asyncio.to_thread(self.store.update, ep["id"], title=d["title"], summary=d["summary"], outcome=d["outcome"],
                                quotes=d["quotes"], facts=d["facts"], participants=parts, emotions=emotions,
                                status=dom.SUMMARIZED, error="")
        rep["summarized"] += 1
        return await asyncio.to_thread(self.store.get, ep["id"])

    async def _facts(self, ep: dict) -> None:
        """Факты эпизода в очередь графа через MemoryWriter, с ссылкой на эпизод (один раз)."""
        if ep.get("facts_done") or not ep.get("facts"):
            return
        for fact in ep["facts"]:
            await asyncio.to_thread(self.writer.enqueue_for_graph, "Пользователь", fact, ep["id"])
        await asyncio.to_thread(self.store.update, ep["id"], facts_done=1)

    async def _embed(self, ep: dict, cfg: dict) -> bool:
        if self.search is None or ep["status"] != dom.SUMMARIZED:
            return False
        meta = {"episode": ep["id"], "day": ep["day"], "stream": ep["stream"], "salience": ep["salience"]}
        await self.search.add("episodes", ep["id"], f"{ep['title']}. {ep['summary']}", ts=ep["t_end"], meta=meta)
        if self.search.embedder is not None and not self.search.last_embedded:
            self.embed_down = True                                  # эмбеддер недоступен: статус не двигаем, повтор в следующий сон
            return False
        for i, q in enumerate(ep.get("quotes") or []):
            await self.search.add("episodes", f"{ep['id']}#q{i}", q, ts=ep["t_end"], meta={**meta, "quote": True})
        related = await self._related(ep, cfg)
        await asyncio.to_thread(self.store.update, ep["id"], status=dom.EMBEDDED, related=related)
        ep["status"] = dom.EMBEDDED
        return True

    async def _related(self, ep: dict, cfg: dict) -> list[str]:
        """Слияние с существующими: похожие эпизоды того же потока связываются ссылкой (записи не удаляются)."""
        thr = cfg.get("related_cos")
        thr = float(thr) if isinstance(thr, (int, float)) and not isinstance(thr, bool) else 0.82
        hits = await self.search.search(f"{ep['title']}. {ep['summary']}", collections=["episodes"], k=5, rewrite=False,
                                        meta={"stream": ep["stream"]})
        return [h.key for h in hits if h.key != ep["id"] and "#" not in h.key and not h.key.startswith("digest:")
                and h.cos is not None and h.cos >= thr][:3]

    async def _notify(self, ep: dict, cfg: dict, already: int, rep: dict) -> int:
        """`episode.summary` во внимание: только яркие эпизоды и не больше `notify_max` за сон (маршрут think, ТЗ 30)."""
        if self.notify is None or ep.get("notified") or ep["status"] not in (dom.SUMMARIZED, dom.EMBEDDED) or not ep.get("summary"):
            return 0
        thr = cfg.get("notify_salience")
        thr = float(thr) if isinstance(thr, (int, float)) and not isinstance(thr, bool) else 0.5
        cap = cfg.get("notify_max")
        cap = int(cap) if isinstance(cap, int) and not isinstance(cap, bool) else 3
        if ep["salience"] < thr or already >= cap:
            return 0
        try:
            await self.notify(ep)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"episode.summary не отправлен: {e}")
            return 0
        await asyncio.to_thread(self.store.update, ep["id"], notified=1)
        rep["notified"] += 1
        return 1

    async def _step_days(self, rep: dict, stop) -> None:
        if rep["llm_down"]:
            return
        now = datetime.fromtimestamp(self.clock())
        have = sorted(await asyncio.to_thread(self.store.days))
        for day in have[-7:][::-1]:                                  # сводки: последние семь суток, новые первыми
            if stop():
                return
            ok, spent, down = await self.days.digest(day)
            self.spent += spent
            rep["digests"] += int(ok)
            if down:
                rep["llm_down"] = True
                return
        cfg = episodes_cfg(self.settings)
        hour = cfg.get("diary_hour")
        written = await asyncio.to_thread(self.store.diary_days)
        for day in dom.diary_days(today=now.date().isoformat(), hour=now.hour, days_with_events=have, written=written,
                                  diary_hour=hour if isinstance(hour, int) and not isinstance(hour, bool) else 22):
            if stop():
                return
            ok, spent, down = await self.days.diary(day)
            self.spent += spent
            rep["diary"] += int(ok)
            if down:
                rep["llm_down"] = True
                return

    async def _step_compress(self, rep: dict, stop) -> None:
        """Сжатие старых эпизодов в сводки суток и удаление сырого по сроку (EPI-10): сырое удаляется только у эпизода с резюме
        и эмбеддингом; копия текста источников остаётся в эпизоде."""
        cfg = episodes_cfg(self.settings)
        now = self.clock()
        after = cfg.get("compress_after_days")
        after = after if isinstance(after, int) and not isinstance(after, bool) else 30
        digests = {d["day"] for d in await asyncio.to_thread(self.store.digests, 400) if d["compressed"]}
        by_day = await asyncio.to_thread(self.store.days)
        for day in dom.compress_days(today_ord=datetime.fromtimestamp(now).date().toordinal(), episodes_by_day=by_day,
                                     compressed=digests, after_days=after):
            if stop():
                return
            eps = await asyncio.to_thread(self.store.episodes_of_day, day)
            if any(e["status"] not in (dom.EMBEDDED, dom.FAILED) for e in eps):
                continue                                              # нужна сводка и эмбеддинг, потом сжимаем
            dig = await asyncio.to_thread(self.store.digest, day)
            if not (dig and dig.get("text")):
                if rep["llm_down"]:
                    continue
                ok, spent, down = await self.days.digest(day)
                self.spent += spent
                rep["llm_down"] = rep["llm_down"] or down
                if not ok:
                    continue
            for e in eps:
                for i in range(len(e.get("quotes") or [])):
                    if self.search is not None:
                        await self.search.delete("episodes", f"{e['id']}#q{i}")
                await asyncio.to_thread(self.store.update, e["id"], quotes=[], compressed=1)
            await asyncio.to_thread(self.store.set_digest, day, (await asyncio.to_thread(self.store.digest, day))["text"],
                                    len(eps), compressed=True)
            rep["compressed"] += len(eps)
        keep = cfg.get("raw_keep_days")
        if isinstance(keep, int) and not isinstance(keep, bool) and keep > 0:
            rep["raw_deleted"] = await self._delete_raw(now - keep * 86400.0)

    async def _delete_raw(self, cutoff: float) -> int:
        eps = [e for e in await asyncio.to_thread(self.store.select, status=dom.EMBEDDED, until=cutoff, limit=100, oldest_first=True)
               if not e.get("raw_deleted") and e["t_end"] <= cutoff and e.get("summary")]
        n = 0
        for e in eps:
            ids = [ev["id"] for ev in await asyncio.to_thread(self.store.events_of, e["id"])]
            n += await asyncio.to_thread(self.source.delete_ids, ids)
            await asyncio.to_thread(self.store.update, e["id"], raw_deleted=1)
        return n
