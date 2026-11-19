"""Внимание v2 (RUNTIME_SPEC 4.1, 4.3-4.5, 5.2): небольшой автомат `process_event` и `on_turn`.

Логика решений без запуска модели: что запустить, что прервать, что поставить в очередь. Ходы агента запускает Runtime
(`runtime.Runtime`), часы и планировщик инжектируются, поэтому в тестах время идёт только когда его двигают."""
from collections import deque
from typing import Callable

from . import render
from .clock import ClockScheduler, Scheduler, SystemClock
from .event import IMPORTANT, MAX_TEXT, USER, Event, EventState, level_name
from .eventlog import EventLog
from .kinds import KindRegistry
from .queue import QUEUE_MAX, Digest, PriorityQueue
from .runtime import ActionRecord, Runtime

CORE_WORKSPACE = "core"            # таймеры и служебные события ядра: окружением «вне фокуса» не считаются

EVENTS_BATCH = 5
WAKE_MIN = 30
INTERRUPT_MARGIN = 20
COALESCE_MS = 400
ON_TURN_S = 20
FOCUS_CAP = 60
INTERRUPT_BUDGET = 3
INTERRUPT_WINDOW_S = 60
FOCUS_STEER_GAP_S = 0.5             # критичные и важные события фокуса во время ответа человеку: не чаще (секунды)
STEER_GAP_S = 4.0                  # важные события одного вида доливаются в идущий ход не чаще (секунды)
_EPS = 1e-3


class AttentionV2:
    def __init__(self, registry: KindRegistry, eventlog: EventLog, runtime: Runtime, clock=None,
                 focus_getter: Callable[[], str | None] | None = None, *, scheduler: Scheduler | None = None,
                 idle_long_enough: Callable[[], bool] | None = None, proactive_enabled: Callable[[], bool] | None = None,
                 events_batch: int = EVENTS_BATCH, queue_max: int = QUEUE_MAX, wake_min: float = WAKE_MIN,
                 interrupt_margin: float = INTERRUPT_MARGIN, coalesce_ms: float = COALESCE_MS, on_turn_s: float = ON_TURN_S,
                 focus_cap: float = FOCUS_CAP, interrupt_budget: int = INTERRUPT_BUDGET,
                 interrupt_window_s: float = INTERRUPT_WINDOW_S, steer_gap_s: float = STEER_GAP_S):
        self.registry = registry
        self.log = eventlog
        self.runtime = runtime
        self.clock = clock or SystemClock()
        self.focus_getter = focus_getter
        self.scheduler = scheduler or ClockScheduler(self.clock)
        self.idle_long_enough = idle_long_enough or (lambda: False)
        self.proactive_enabled = proactive_enabled or (lambda: False)       # проактивные ходы по умолчанию выключены
        self.events_batch, self.wake_min = events_batch, wake_min
        self.interrupt_margin, self.coalesce_ms, self.on_turn_s = interrupt_margin, coalesce_ms, on_turn_s
        self.focus_cap, self.interrupt_budget, self.interrupt_window_s = focus_cap, interrupt_budget, interrupt_window_s
        self.steer_gap_s = steer_gap_s       # важные события одного вида доливаются в идущий ход не чаще этого (секунды)
        self._steered_at: dict[tuple[str, str], float] = {}

        self.digest = Digest()
        self.queue = PriorityQueue(queue_max, self.digest, self.clock, on_drop=self._on_queue_drop)
        self._window = None                  # разовый таймер окна слияния (COALESCE_MS)
        self._timer = None                   # разовый таймер ON_TURN_S
        self._interrupt_times: deque[float] = deque()
        self._arrived: dict[str, float] = {}
        self._latency: dict[str, list[float]] = {}
        self._m = {"launches": 0, "interrupts": 0, "steered": 0, "merged": 0, "expired": 0, "evicted": 0, "dropped": 0,
                   "idle_turns": 0, "empty_on_turn": 0, "interrupts_skipped_budget": 0, "requeued": 0}
        self._interrupt_reasons: dict[str, int] = {}

    # ---- приём ------------------------------------------------------------------------------------------------------

    async def process_event(self, e: Event) -> None:
        """Раздел 4.3: приём, нормализация, потолок вне фокуса, срок, слияние, затем запуск, прерывание или очередь."""
        self.log.record_new(e)
        self._arrived[e.id] = self.clock.monotonic()
        res = self.registry.normalize(e)
        if not res.accepted:
            self._discard(e, EventState.DROPPED, res.reason or "")
            return
        spec = res.spec
        if res.clamped_from is not None:
            self.log.note(e, "приоритет зажат потолком вида", before=res.clamped_from, after=e.priority,
                          max_priority=spec.max_priority)
        if self.focus_getter is not None and e.workspace != CORE_WORKSPACE and e.priority < USER and not spec.critical:
            if e.workspace != self.focus_getter() and e.priority > self.focus_cap:
                self.log.note(e, "приоритет зажат: окружение вне фокуса", before=e.priority, after=self.focus_cap)
                e.priority = self.focus_cap
        if e.is_expired(self.clock.time()):
            self.digest.add(e)
            self._discard(e, EventState.EXPIRED, "срок вышел до приёма")
            return
        if e.group_key and spec.coalesce and await self._try_merge(e):
            return

        running = list(self.runtime.current_batch())
        if not running:
            await self._on_idle(e)
        else:
            await self._on_busy(e, running)

    async def _on_idle(self, e: Event) -> None:
        top_queue = self.queue.max_priority()
        wins = e.priority > top_queue and e.priority >= self.wake_min
        self._enqueue(e)
        if wins:
            await self._launch(windowed=bool(e.group_key) and self.coalesce_ms > 0)
        else:
            self._arm_timer()                  # ждёт on_turn: по концу хода или разовым таймером

    async def _on_busy(self, e: Event, running: list[Event]) -> None:
        if any(x.prio >= USER for x in running):                   # USER-ход: человеку уже отвечают, не прерываем
            keys = {x.group_key for x in running if x.prio >= USER and x.group_key}
            # доливается только продолжение разговора (не ниже порога пробуждения); поддакивание и фон ждут в очереди
            if (e.group_key and e.group_key in keys and e.priority >= self.wake_min and self.runtime.can_steer
                    and await self.runtime.steer(e)):
                self._m["steered"] += 1
                self._mark_delivered(e, "доливается в идущий ход")
                return
            # окружение в фокусе: гибель, угроза и другие важные события игры не ждут конца ответа человеку, а доливаются сразу
            if self._focus_priority(e) and self.runtime.can_steer and await self.runtime.steer(e):
                self._m["steered"] += 1
                self._steered_at[(e.workspace, e.kind)] = self.clock.monotonic()
                self._mark_delivered(e, "доливается в идущий ход (важное в фокусе)")
                return
            self._enqueue(e)
            return
        top_busy = max(self.queue.max_priority(), max(x.prio for x in running))
        is_user = e.priority >= USER
        if is_user or e.priority > top_busy + self.interrupt_margin:
            spec = self.registry.get(e.kind)
            critical = bool(spec and spec.critical)                # тревога окружения (рейд, гибель) проходит мимо бюджета: шторм мелких прерываний её не глушит
            if is_user or critical or self._budget_ok():
                await self._interrupt(e, "user" if is_user else ("critical" if critical else "margin"))
                return
            self._m["interrupts_skipped_budget"] += 1
        # важное событие (гибель, угроза, падение) не ждёт конца долгого хода: доливается в идущий и агент видит его на ближайшем
        # шаге модели, а не через минуты; пассивные и слабые ждут в очереди; частые события одного вида не чаще STEER_GAP_S
        spec = self.registry.get(e.kind)
        if (e.priority >= IMPORTANT and not (spec and spec.passive) and self.runtime.can_steer and self._steer_gap_ok(e)
                and await self.runtime.steer(e)):
            self._m["steered"] += 1
            self._steered_at[(e.workspace, e.kind)] = self.clock.monotonic()
            self._mark_delivered(e, "доливается в идущий ход (важное)")
            return
        self._enqueue(e)

    def _focus_priority(self, e: Event) -> bool:
        """Важное событие окружения, в котором агент сейчас работает (или критичный вид): доливается в идущий ход даже во время
        ответа человеку. Пассивные не доливаются; события одного вида не чаще `FOCUS_STEER_GAP_S`."""
        spec = self.registry.get(e.kind)
        if (spec and spec.passive) or e.priority >= USER:         # реплики людей (даже других) ждут, как раньше
            return False
        critical = bool(spec and spec.critical)
        in_focus = self.focus_getter is not None and e.workspace == self.focus_getter()
        if not (critical or (in_focus and e.priority >= IMPORTANT)):
            return False
        last = self._steered_at.get((e.workspace, e.kind))
        return last is None or self.clock.monotonic() - last >= FOCUS_STEER_GAP_S

    def _steer_gap_ok(self, e: Event) -> bool:
        last = self._steered_at.get((e.workspace, e.kind))
        return last is None or self.clock.monotonic() - last >= self.steer_gap_s

    # ---- слияние (4.5) ------------------------------------------------------------------------------------------------

    async def _try_merge(self, e: Event) -> bool:
        """Слить с ожидающим (в очереди, ещё не запущенным) событием того же ключа: тексты по порядку прихода,
        максимум приоритета, самый поздний срок."""
        target = self.queue.find_group(e.workspace, e.kind, e.group_key)
        if target is None:
            return False
        before = target.prio
        spec = self.registry.get(e.kind)
        if spec is not None and spec.replace:                     # вид «последнее заменяет прежнее» (рабочий цикл): текст не копится
            target.text = render.one_line(e.text, MAX_TEXT)
        else:
            target.text = render.one_line(f"{target.text} {e.text}", MAX_TEXT)
        target.priority = max(target.prio, e.prio)
        if e.expires_at is not None:
            target.expires_at = e.expires_at if target.expires_at is None else max(target.expires_at, e.expires_at)
        target.data.setdefault("merged_ids", []).append(e.id)
        self._m["merged"] += 1
        self.log.note(target, "слито с событием того же group_key", merged=e.id, priority=target.priority)
        self._discard(e, EventState.DROPPED, f"слито в {target.id}", count=False)
        if target.priority > before and not self.runtime.current_batch():       # слияние подняло приоритет: возможно, пора будить
            if target.priority >= self.wake_min and self.queue.items()[0] is target:
                await self._launch(windowed=self.coalesce_ms > 0)
        return True

    # ---- запуск -----------------------------------------------------------------------------------------------------

    async def _launch(self, windowed: bool) -> None:
        if windowed:
            if self._window is None:
                self._window = self.scheduler.call_later(self.coalesce_ms / 1000.0, self._fire_window)
            return
        self._cancel_window()
        await self._launch_now()

    async def _fire_window(self) -> None:
        self._window = None
        if not self.runtime.current_batch():
            await self._launch_now()

    async def _launch_now(self) -> None:
        batch = self.queue.pop(self.events_batch)
        if batch:
            await self._start(batch)

    async def _start(self, batch: list[Event]) -> None:
        self._cancel_timer()
        for x in batch:
            self._mark_delivered(x, "запуск хода")
        self._m["launches"] += 1
        await self.runtime.trigger(batch, after_ms=0)

    async def _interrupt(self, e: Event, reason: str) -> None:
        now = self.clock.monotonic()
        self._interrupt_times.append(now)
        self._m["interrupts"] += 1
        self._interrupt_reasons[reason] = self._interrupt_reasons.get(reason, 0) + 1
        batch = [e] + self.queue.pop(self.events_batch - 1)
        for x in batch:
            self._mark_delivered(x, f"прерывание ({reason})")
        await self.runtime.interrupt(batch)

    def _budget_used(self) -> int:
        cutoff = self.clock.monotonic() - self.interrupt_window_s
        while self._interrupt_times and self._interrupt_times[0] <= cutoff:
            self._interrupt_times.popleft()
        return len(self._interrupt_times)

    def _budget_ok(self) -> bool:
        return self._budget_used() < self.interrupt_budget

    # ---- on_turn (4.4) ----------------------------------------------------------------------------------------------

    async def on_turn(self) -> None:
        """Зовёт Runtime по окончании хода (после того, как `current_batch()` очищена) и разовый таймер `ON_TURN_S`."""
        if self.runtime.current_batch() or self._window is not None:
            return                                         # ход идёт, или запуск уже назначен окном слияния
        self.queue.expire()
        if len(self.queue) and (self.queue.max_priority() >= self.wake_min
                                or self.queue.oldest_wait() >= self.on_turn_s - _EPS):
            await self._launch_now()
            return
        if len(self.queue):
            self._arm_timer()                              # остаток ожидания; свежее мелкое ещё не созрело
        if self.idle_long_enough() and self.proactive_enabled():
            self._m["idle_turns"] += 1
            await self.runtime.trigger_idle()
            return
        self._m["empty_on_turn"] += 1

    async def on_turn_end(self, *args, **kwargs) -> None:
        """Хук Runtime «ход закончился»."""
        await self.on_turn()

    def _arm_timer(self) -> None:
        """Разовый таймер на `ON_TURN_S` с прихода самого старого ожидающего события (не тик)."""
        if self._timer is not None or self.runtime.current_batch():
            return
        oldest = self.queue.oldest_queued_at()
        if oldest is None:
            return
        delay = max(0.0, oldest + self.on_turn_s - self.clock.time())
        self._timer = self.scheduler.call_later(delay, self._on_timer)

    async def _on_timer(self) -> None:
        self._timer = None
        await self.on_turn()

    def _cancel_timer(self) -> None:
        t, self._timer = self._timer, None
        if t is not None:
            t.cancel()

    def _cancel_window(self) -> None:
        w, self._window = self._window, None
        if w is not None:
            w.cancel()

    def close(self) -> None:
        self._cancel_timer()
        self._cancel_window()

    # ---- прерывание: что делает Runtime (5.2) ------------------------------------------------------------------------------

    def requeue_interrupted(self, events: list[Event], had_effects: bool) -> list[Event]:
        """События прерванной пачки. Без завершённых действий с эффектом возвращаются в очередь с `interrupted = True`
        (приоритет и срок прежние); иначе не возвращаются (двойное действие), и их описывает блок «прервано»."""
        back = []
        now = self.clock.time()
        for e in events:
            e.interrupted = True
            self.log.transition(e, EventState.INTERRUPTED, "ход прерван" + (", были действия с эффектом" if had_effects else ""))
            if had_effects:
                continue
            if e.is_expired(now):
                self.digest.add(e)
                self._discard(e, EventState.EXPIRED, "срок вышел после прерывания")
                continue
            self._enqueue(e, reason="возврат после прерывания")
            back.append(e)
        self._m["requeued"] += len(back)
        return back

    def build_interrupt_note(self, events: list[Event], actions: list[ActionRecord], started_at: float | None = None) -> str:
        """Блок «прервано» для следующего хода (6.3)."""
        return render.render_interrupt_note(events, actions, started_at)

    def render_batch(self, events: list[Event], interrupt_note: str = "") -> str:
        """Текст пачки для хода (6.2). Забирает накопленную сводку пропущенного: она уходит в начало именно этой пачки."""
        return render.render_batch(events, self.digest.take(), interrupt_note)

    # ---- внутреннее -------------------------------------------------------------------------------------------------

    def _enqueue(self, e: Event, reason: str = "") -> None:
        self.log.transition(e, EventState.QUEUED, reason)
        spec = self.registry.get(e.kind)
        self.queue.push(e, passive=bool(spec and spec.passive))

    def _mark_delivered(self, e: Event, reason: str = "") -> None:
        self.log.transition(e, EventState.DELIVERED, reason)
        t0 = self._arrived.pop(e.id, None)
        if t0 is not None:
            ms = (self.clock.monotonic() - t0) * 1000.0
            self._latency.setdefault(level_name(e.prio), []).append(ms)

    def _discard(self, e: Event, state: EventState, reason: str, count: bool = True) -> None:
        self.log.transition(e, state, reason)
        self._arrived.pop(e.id, None)
        if count:
            self._m[state.value] = self._m.get(state.value, 0) + 1

    def _on_queue_drop(self, e: Event, state: EventState) -> None:
        self._discard(e, state, "вышел срок в очереди" if state is EventState.EXPIRED else "очередь переполнена")

    # ---- метрики (раздел 10) --------------------------------------------------------------------------------------------

    def status(self) -> dict:
        latency = {name: {"count": len(v), "avg_ms": round(sum(v) / len(v), 3), "max_ms": round(max(v), 3)}
                   for name, v in self._latency.items() if v}
        return {
            "queue": {"size": len(self.queue), "max_priority": self.queue.max_priority(),
                      "oldest_wait_s": round(self.queue.oldest_wait(), 3)},
            "launches": self._m["launches"],
            "interrupts": {"total": self._m["interrupts"], "by_reason": dict(self._interrupt_reasons),
                           "skipped_budget": self._m["interrupts_skipped_budget"],
                           "budget": {"limit": self.interrupt_budget, "window_s": self.interrupt_window_s,
                                      "used": self._budget_used()}},
            "steered": self._m["steered"],
            "merged": self._m["merged"],
            "expired": self._m["expired"],
            "evicted": self._m["evicted"],
            "dropped": self._m["dropped"],
            "requeued": self._m["requeued"],
            "digest_pending": self.digest.total,
            "idle_turns": self._m["idle_turns"],
            "empty_on_turn": self._m["empty_on_turn"],
            "latency_by_level": latency,
            "latency_user": latency.get("USER"),
        }
