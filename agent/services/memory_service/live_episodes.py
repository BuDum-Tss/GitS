"""Живая запись эпизодов (ТЗ 27): эпизод пишется, пока она бодрствует, а не только во сне.

Каждые `episodes.live.tick_s` секунд: селектор моментов (`MomentSelector.run`) кладёт закрытые моменты в таблицу как pending, потом
решается, пора ли писать. Пора, когда выполнено любое из условий:
  events     пиков (ярких событий) в pending-моментах не меньше `fork_after_events`;
  context    самый старый pending-пик скоро выйдет из её контекста (`will_leave_context`: новее него накопилось больше `context_share`
             предела контекста моста), надо успеть записать то, что вот-вот забудется;
  wait       самому старому pending-моменту больше `max_wait_s` (предохранитель);
  instant    есть пик не слабее `instant_score`.
Писать можно только когда она свободна (нет хода и речи, `busy`), не во сне, не чаще `min_gap_s` и не больше `per_hour` раз за час.
Один вызов писателя берёт до `max_per_fork` моментов (самые яркие и самые старые вперемешку). Недоступная модель или эмбеддер не
ошибка: эпизод остаётся pending или summarized, повтор в следующий тик или в сон. Очередь не растёт без конца: сверх `max_pending`
самые тусклые и старые отбрасываются со статусом skipped. Писатель без форка (`episodes.writer` = heavy или fast) пишет по одному
эпизоду за тик без условий-триггеров. Сам эпизод обрабатывает `SleepRunner.process_episode` (рассказ, факты, эмбеддинг, уведомление)."""
import asyncio
import logging
from typing import Callable

import services.memory_service.episodes as dom
from services.runtime.pi_runtime import MAX_CONTEXT_CHARS

logger = logging.getLogger("episodes")

MESSAGE_OVERHEAD = 60                # служебные символы сообщения в сессии сверх текста события (JSON.stringify моста)
CONTEXT_KINDS = {"user", "say", "module_out", "mistake", "approval"}     # события журнала, которые попадают в историю её сессии
DEFAULTS = {"enabled": True, "tick_s": 30.0, "min_gap_s": 45.0, "per_hour": 8, "max_pending": 20, "instant_score": 0.85,
            "instant_after_s": 20.0, "fork_after_events": 6, "context_share": 0.5, "max_wait_s": 1800.0, "max_per_fork": 4}


def will_leave_context(rows_after_peak: list[dict], max_chars: int, share: float) -> bool:
    """Выходит ли пик из её контекста: новее него накопилось (длина текста события + служебное) больше `share` * `max_chars`.
    Мост при каждом вызове модели обрезает историю по пределу `max_chars`, оставляя хвост около 0.6 предела."""
    used = sum(len(str(r.get("text") or "")) + MESSAGE_OVERHEAD for r in rows_after_peak)
    return used > max_chars * share


def live_cfg(settings: Callable[[], dict]) -> dict:
    """Настройки `episodes.live` поверх значений по умолчанию (неверные типы игнорируются)."""
    raw = (settings() or {}).get("episodes")
    raw = raw.get("live") if isinstance(raw, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for k, base in DEFAULTS.items():
        v = raw.get(k)
        if isinstance(base, bool):
            out[k] = v if isinstance(v, bool) else base
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[k] = type(base)(v)
    return out


class LiveEpisodes:
    def __init__(self, parts: Callable[[], object], *, settings: Callable[[], dict], clock: Callable[[], float], source,
                 busy: Callable[[], bool] = lambda: False, asleep: Callable[[], bool] = lambda: False,
                 max_context: int = MAX_CONTEXT_CHARS):
        """parts() -> части текущего персонажа (`EpisodeService.parts`: смена персонажа учитывается на каждом тике): store, selector,
        runner, summarizer; busy() - идёт её ход или речь; asleep() - идёт сон (он сам обработает остатки)."""
        self.parts, self.settings, self.clock, self.source = parts, settings, clock, source
        self.busy, self.asleep, self.max_context = busy, asleep, max_context
        self._state: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self.last_action = ""
        self.last_report: dict = {}                              # счётчики последней записи (summarized, skipped, failed)

    # ── жизненный цикл ───────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(live_cfg(self.settings)["tick_s"])
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Живая запись эпизодов: {e}")

    # ── один тик ─────────────────────────────────────────────────────────────

    async def tick(self) -> str:
        """Один проход. Возвращает, что сделано: off, asleep, idle (нечего писать), quiet (условий записи пока нет), busy, gap, hour, wrote, down."""
        self.last_action = await self._tick()
        return self.last_action

    async def _tick(self) -> str:
        cfg = live_cfg(self.settings)
        if not cfg["enabled"]:
            return "off"
        if self.asleep():
            return "asleep"
        p = self.parts()
        await p.selector.run()                                   # закрытые моменты -> pending (модель не нужна)
        pending = await asyncio.to_thread(p.store.select, status=dom.PENDING, limit=500, oldest_first=True)
        pending = await self._cap(p, pending, cfg)
        now = self.clock()
        state = self._state.setdefault(str(p.store.path), {"last": 0.0, "calls": []})
        writer = p.summarizer
        fork = bool(getattr(writer, "summarize_many", None) and writer.enabled())
        if pending:
            why = await self._trigger(p, pending, cfg, now) if fork else "single"
            if why is None:
                return "quiet"
            if self.busy():
                return "busy"
            if now - state["last"] < cfg["min_gap_s"]:
                return "gap"
            state["calls"] = [t for t in state["calls"] if now - t < 3600.0]
            if len(state["calls"]) >= cfg["per_hour"]:
                return "hour"
            return await self._write(p, pending, cfg, state, now, fork)
        return await self._retry_embed(p)

    async def _cap(self, p, pending: list[dict], cfg: dict) -> list[dict]:
        """Очередь не копится: сверх `max_pending` самые тусклые (при равенстве старые) отбрасываются со статусом skipped."""
        over = len(pending) - cfg["max_pending"]
        if over <= 0:
            return pending
        drop = sorted(pending, key=lambda e: (e["salience"], e["t_start"]))[:over]
        for e in drop:
            await asyncio.to_thread(p.store.update, e["id"], status=dom.SKIPPED, error="очередь переполнена")
        gone = {e["id"] for e in drop}
        return [e for e in pending if e["id"] not in gone]

    # ── условия ──────────────────────────────────────────────────────────────

    async def _peaks(self, p, pending: list[dict]) -> list[dict]:
        """Яркие события pending-моментов (пики): [{id, ts, episode}] по возрастанию номера."""
        thr = p.selector.cfg().threshold
        out = []
        for ep in pending:
            for ev in await asyncio.to_thread(p.store.events_of, ep["id"]):
                if ev["salience"] >= thr:
                    out.append({"id": ev["id"], "ts": ev["ts"], "episode": ep["id"]})
        return sorted(out, key=lambda x: x["id"])

    async def _trigger(self, p, pending: list[dict], cfg: dict, now: float) -> str | None:
        if any(e["salience"] >= cfg["instant_score"] for e in pending):
            return "instant"
        peaks = await self._peaks(p, pending)
        if len(peaks) >= cfg["fork_after_events"]:
            return "events"
        if now - min(e["t_end"] for e in pending) > cfg["max_wait_s"]:
            return "wait"
        if peaks and await self._leaving(peaks[0]["id"], cfg):
            return "context"
        return None

    async def _leaving(self, peak_id: int, cfg: dict) -> bool:
        """Выйдет ли самый старый пик из контекста: читаем журнал после него страницами, пока не станет ясно."""
        rows: list[dict] = []
        after = peak_id
        for _ in range(5):
            page = await asyncio.to_thread(self.source.after, after, 2000, CONTEXT_KINDS)
            if not page:
                break
            rows += page
            after = page[-1]["id"]
            if will_leave_context(rows, self.max_context, cfg["context_share"]):
                return True
            if len(page) < 2000:
                break
        return will_leave_context(rows, self.max_context, cfg["context_share"])

    # ── запись ───────────────────────────────────────────────────────────────

    @staticmethod
    def _pick(pending: list[dict], n: int) -> list[dict]:
        """До `n` моментов: самые яркие и самые старые вперемешку."""
        bright = sorted(pending, key=lambda e: (-e["salience"], e["t_start"]))
        old = sorted(pending, key=lambda e: e["t_start"])
        out: list[dict] = []
        for a, b in zip(bright, old, strict=True):
            for e in (a, b):
                if e not in out and len(out) < n:
                    out.append(e)
        return out

    async def _write(self, p, pending: list[dict], cfg: dict, state: dict, now: float, fork: bool) -> str:
        rep = {"llm_down": False, "summarized": 0, "embedded": 0, "failed": 0, "skipped": 0, "notified": 0}
        p.runner.embed_down = False                              # эмбеддер пробуем заново в каждый тик
        batch = self._pick(pending, cfg["max_per_fork"] if fork else 1)
        items = [(ep, await asyncio.to_thread(p.store.events_of, ep["id"])) for ep in batch]
        state["last"] = now
        if fork:
            results = await p.summarizer.summarize_many(items)
            for ep, _ in items:
                await p.runner.process_episode(ep, rep, result=results[ep["id"]])
        else:
            await p.runner.process_episode(batch[0], rep)
        if rep["llm_down"]:
            return "down"
        state["calls"].append(now)                              # пропущенные тоже считаются в per_hour
        self.last_report = rep
        return "wrote"

    async def _retry_embed(self, p) -> str:
        """Рассказ есть, эмбеддинга нет (эмбеддер был недоступен): пробуем снова, модель не нужна."""
        eps = await asyncio.to_thread(p.store.select, status=dom.SUMMARIZED, limit=1, oldest_first=True)
        if not eps:
            return "idle"
        p.runner.embed_down = False
        rep = {"llm_down": False, "summarized": 0, "embedded": 0, "failed": 0, "skipped": 0, "notified": 0}
        await p.runner.process_episode(eps[0], rep)
        return "wrote" if rep["embedded"] else "idle"
