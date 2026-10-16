"""Прогон ядра на устойчивость (ТЗ 17, FLOW-2 и FLOW-3): ядро без модулей (или с настоящими модулями в режиме заглушек).

Поднимается настоящее ядро (Companion, менеджер внимания и фокуса, шина модулей, дашборд на свободном порту, временная папка
памяти). Модель, классификатор и речь подменены заглушками; сети, микрофона, игры и Claude нет. Нагрузка:
  - реплики в чат через `POST /api/chat`;
  - события от фиктивных модулей через клиентов SDK (`ModuleClient.event`);
  - подключение и отключение фиктивных модулей, в том числе обрыв соединения без закрытия;
  - заглушка очереди памяти-графа (`enqueue` от разговора, разбор по таймеру).
Раз в `--sample` секунд снимаются: память процесса (RSS), задержка цикла событий, число задач asyncio, число ошибок в журнале,
размеры очередей (речь, внимание, граф, шаги трассы).

Критерии (ТЗ 17, FLOW-2): ни одного необработанного исключения и записи уровня ERROR; рост RSS меньше 50 МБ (после разогрева);
задержка цикла событий p99 меньше 200 мс; очереди не растут без предела (максимум в границах, конец не выше середины с запасом);
число задач не растёт без предела; ядро закрывается за разумное время и не оставляет своих задач.

Запуск:
  python agent/tools/soak_core.py --minutes 10                  # FLOW-2, как в ТЗ
  python agent/tools/soak_core.py --minutes 2 --sample 2        # короткий прогон
  python agent/tools/soak_core.py --minutes 3 --real-modules telegram,voice,pet   # FLOW-3, настоящие модули с заглушками
  --out docs/CORE_SOAK_RUN.md  записать отчёт в файл; код выхода 0, если все критерии выполнены.
"""
import argparse
import asyncio
import json
import logging
import os
import random
import socket
import sys
import tempfile
import time
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
for _p in (AGENT, AGENT.parent / "sdk"):              # ядро и пакет gits_sdk (до установки пакета)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

CHAT_PERIOD_S = 2.5          # пауза между репликами в чат: ~24 в минуту, заметно чаще живого разговора
CRITERIA = {"rss_growth_mb": 50.0, "loop_lag_p99_ms": 200.0, "queue_cap": 200, "tasks_growth": 60}
PHRASES = ["привет, как дела", "расскажи, что ты умеешь", "какая сегодня погода", "запомни, что я люблю чай",
           "нет, не так, я имел в виду другое", "спасибо, хорошего дня", "что ты помнишь обо мне"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[max(0, min(len(s) - 1, int(q * len(s) + 0.9999) - 1))], 2)


class _ErrorCounter(logging.Handler):
    """Считает записи уровня ERROR и выше (необработанные исключения тоже попадают сюда через обработчик цикла)."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(f"{record.name}: {record.getMessage()}"[:300])


class LoopLag:
    """Задержка цикла событий: насколько сон на `period` секунд затянулся (мс)."""

    def __init__(self, period: float = 0.05):
        self.period = period
        self.samples: list[float] = []
        self._task: asyncio.Task | None = None

    async def _run(self):
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(self.period)
            self.samples.append(max(0.0, (time.perf_counter() - t0 - self.period) * 1000))

    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def take(self) -> list[float]:
        out, self.samples = self.samples, []
        return out


class StubGraph:
    """Очередь памяти-графа без FalkorDB: `enqueue` ставит, `drain` разбирает по одному (как фоновый разбор настоящего графа)."""

    def __init__(self):
        self.queue: list[dict] = []
        self.total = 0

    def enqueue(self, speaker, text, ts=None, source=None):
        self.queue.append({"speaker": speaker, "text": text[:200]})
        self.queue = self.queue[-500:]
        self.total += 1
        return len(self.queue)

    def drain(self, n: int = 1):
        del self.queue[:n]

    def status_text(self):
        return None

    async def context_block(self, query):
        return ""

    async def self_memories(self, query):
        return []


def _rss_mb() -> float:
    import psutil
    return psutil.Process().memory_info().rss / 1048576


def _fake_manifest(module_id: str) -> dict:
    return {"id": module_id, "title": module_id, "description": "Фиктивный модуль прогона.", "version": "1.0.0",
            "subscribe": ["presence"], "tools": [],
            "emits": [{"kind": f"{module_id}.ping", "description": "пинг", "default_urgency": 1}]}


class Soak:
    def __init__(self, minutes: float, sample_s: float, memory_dir: str, real_modules: list[str], seed: int = 1):
        self.minutes, self.sample_s, self.memory_dir = minutes, sample_s, memory_dir
        self.real_modules = real_modules
        self.rng = random.Random(seed)
        self.counter = _ErrorCounter()
        self.lag = LoopLag()
        self.unhandled: list[str] = []
        self.samples: list[dict] = []
        self.load = {"chat": 0, "events": 0, "connects": 0, "drops": 0, "kills": 0, "chat_errors": 0}
        self.restart_ok: dict[str, bool] = {}
        self.shutdown_s: float | None = None
        self.leftover_tasks = 0

    # ── сборка ядра ────────────────────────────────────────────────────────────

    async def build(self):
        import decide
        import events_log
        import fast_brain
        import settings
        import speech_out
        from aiohttp import web
        from tools import measure_e2e as m

        import core_service
        import dashboard
        from companion import Companion
        from environments.registry import Registry
        from relationships import Relationships

        names = ("LOCALAPPDATA", "GITS_SECRETS_FILE", "GITS_VOICE_FAKE", "GITS_PET_HEADLESS", "GITS_DESKTOP_FAKE", "GITS_NO_DECIDER",
                 "GITS_NO_MODULES")
        self._saved_env = {k: os.environ.get(k) for k in names}               # возвращаются в close()
        os.environ["LOCALAPPDATA"] = str(Path(self.memory_dir) / "localappdata")        # файл токена модулей не в настоящем профиле
        os.environ["GITS_SECRETS_FILE"] = str(Path(self.memory_dir) / "нет-secrets.env")
        os.environ["GITS_VOICE_FAKE"] = "1"
        os.environ["GITS_PET_HEADLESS"] = "1"
        os.environ["GITS_DESKTOP_FAKE"] = "1"
        os.environ["GITS_NO_DECIDER"] = "1"
        if self.real_modules:
            os.environ.pop("GITS_NO_MODULES", None)
        else:
            os.environ["GITS_NO_MODULES"] = "1"
        settings.init(self.memory_dir)
        events_log.init(self.memory_dir)
        self._saved = (fast_brain.publish, fast_brain.react_chat, decide.decide)

        async def no_reaction(text, rel=None):
            return None

        async def no_classifier(*args, **kwargs):
            return None

        fast_brain.publish = lambda *a, **k: None
        fast_brain.react_chat = no_reaction
        decide.decide = no_classifier

        self.port = free_port()
        presence = speech_out.PresenceTracker()
        self.companion = Companion(self.memory_dir, Relationships(self.memory_dir), speech=m._Speech(), presence=presence,
                                   llm=m._SimLLM(20.0, 2))
        self.companion.router = m._Router()
        self.graph = StubGraph()
        self.companion.graph = self.graph
        registry = Registry()
        from supervisor import load_specs
        want = set(self.real_modules)
        specs = [s for s in load_specs() if s.id in want]
        self.layer = core_service.wire_modules(self.companion, registry, port=self.port,
                                               battery=lambda: {"level": 1.0, "sleeping": False}, specs=specs)
        dashboard._MEMORY_DIR = Path(self.memory_dir)
        dashboard.set_modules(self.layer.modules, self.layer.hub, self.layer.supervisor)
        dashboard.set_attention(self.layer.attention)
        dashboard.set_focus(self.layer.focus)
        dashboard.set_presence(presence)
        dashboard.set_registry(registry)
        dashboard.set_modes(self.layer.profiles)
        loop = asyncio.get_running_loop()
        dashboard.ctx.chat_callback = lambda msg: loop.create_task(self.layer.io.from_dashboard(msg))
        self.runner = web.AppRunner(dashboard.build_app(), shutdown_timeout=2)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        await self.layer.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self._baseline_tasks = {t for t in asyncio.all_tasks()}

    async def close(self):
        import decide
        import events_log
        import fast_brain
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(self.layer.stop(), 30)
            await self.runner.cleanup()
        finally:
            self.shutdown_s = round(time.perf_counter() - t0, 2)
            fast_brain.publish, fast_brain.react_chat, decide.decide = self._saved
            for k, v in self._saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            events_log.flush()
        await asyncio.sleep(0.2)
        me = asyncio.current_task()
        self.leftover_tasks = sum(1 for t in asyncio.all_tasks() if t is not me and not t.done() and t not in self._baseline_tasks
                                  and t is not self._sampler and t is not self.lag._task)

    # ── нагрузка ──────────────────────────────────────────────────────────────

    async def chat_load(self, deadline: float, period: float):
        import aiohttp
        async with aiohttp.ClientSession() as http:
            while time.time() < deadline:
                try:
                    async with http.post(f"{self.base}/api/chat", json={"message": self.rng.choice(PHRASES)}) as r:
                        if r.status == 200:
                            self.load["chat"] += 1
                        else:
                            self.load["chat_errors"] += 1
                except Exception:
                    self.load["chat_errors"] += 1
                await asyncio.sleep(period)

    async def module_load(self, deadline: float, name: str, period: float):
        """Фиктивный модуль: подключается, шлёт события, периодически отключается (то закрытием, то обрывом соединения)."""
        from gits_sdk import ModuleClient
        url = f"ws://127.0.0.1:{self.port}/module/ws"
        while time.time() < deadline:
            client = ModuleClient(_fake_manifest(name), url=url, token=self.layer.hub.token)
            client.reconnect_delays = (0.2, 0.5)
            task = asyncio.get_running_loop().create_task(client.run())
            try:
                await client.wait_connected(10)
                self.load["connects"] += 1
                life = time.time() + self.rng.uniform(4, 12)
                while time.time() < min(life, deadline):
                    if await client.event(f"{name}.ping", self.rng.choice(PHRASES), urgency_hint=self.rng.randint(0, 3)):
                        self.load["events"] += 1
                    await asyncio.sleep(period)
                if self.rng.random() < 0.5 and client._ws is not None:
                    try:
                        client._ws._writer.transport.abort()           # обрыв без закрытия (как падение процесса)
                        self.load["drops"] += 1
                        await asyncio.sleep(1.0)                        # клиент переподключится сам
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                pass
            finally:
                await client.stop()
                try:
                    await asyncio.wait_for(task, 10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task.cancel()
            await asyncio.sleep(self.rng.uniform(0.3, 1.5))

    async def graph_load(self, deadline: float):
        """Разбор очереди графа заглушкой раз в 0,5 с и вброс фактов от разговора."""
        while time.time() < deadline:
            self.graph.enqueue("Пользователь", self.rng.choice(PHRASES))
            self.graph.drain(2)
            await asyncio.sleep(0.5)

    async def kill_load(self, deadline: float):
        """FLOW-3: убить процесс настоящего модуля и убедиться, что супервизор поднял его снова, а ядро живо."""
        import psutil
        await asyncio.sleep(min(20.0, self.minutes * 60 / 4))
        sup = self.layer.supervisor
        for mid in self.real_modules:
            if time.time() > deadline - 30:
                break
            st = sup.status(mid)
            if not st or not st.get("pid"):
                self.restart_ok[mid] = False
                continue
            old = st["pid"]
            try:
                psutil.Process(old).kill()
                self.load["kills"] += 1
            except Exception:
                continue
            end = time.time() + 60
            ok = False
            while time.time() < end:
                await asyncio.sleep(1.0)
                cur = sup.status(mid)
                if cur and cur.get("pid") and cur["pid"] != old and cur.get("status") in ("running", "ready", "connected", "starting"):
                    ok = True
                    break
            self.restart_ok[mid] = ok

    # ── снятие показателей ─────────────────────────────────────────────────────

    def snapshot(self, t: float) -> dict:
        import flow_trace
        att = self.layer.attention
        lag = self.lag.take()
        return {"t": round(t, 1), "rss_mb": round(_rss_mb(), 1), "tasks": len(asyncio.all_tasks()),
                "lag_p99_ms": percentile(lag, 0.99), "lag_max_ms": round(max(lag), 1) if lag else None,
                "q_speech": len(att.speech.items), "q_attention": len(att._tasks), "q_graph": len(self.graph.queue),
                "trace_len": len(flow_trace.records()), "errors": len(self.counter.records) + len(self.unhandled),
                "modules": len(self.layer.hub.connections()), "_lag": lag}

    async def sampler(self, t0: float, deadline: float):
        while time.time() < deadline:
            await asyncio.sleep(self.sample_s)
            self.samples.append(self.snapshot(time.time() - t0))

    # ── прогон ────────────────────────────────────────────────────────────────

    async def run(self) -> dict:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda lp, ctx: self.unhandled.append(str(ctx.get("message")) + " " + repr(ctx.get("exception"))))
        logging.getLogger().addHandler(self.counter)
        await self.build()
        self.lag.start()
        t0 = time.time()
        deadline = t0 + self.minutes * 60
        warm = min(30.0, self.minutes * 60 * 0.2)
        loads = [self.chat_load(deadline, CHAT_PERIOD_S), self.graph_load(deadline), self.module_load(deadline, "soaka", 0.15),
                 self.module_load(deadline, "soakb", 0.3)]
        if self.real_modules:
            loads.append(self.kill_load(deadline))
        self._sampler = loop.create_task(self.sampler(t0, deadline))
        try:
            await asyncio.gather(*loads, self._sampler)
            self.samples.append(self.snapshot(time.time() - t0))
        finally:
            await self.lag.stop()
            await self.close()
            logging.getLogger().removeHandler(self.counter)
        return self.report(warm)

    def report(self, warm: float) -> dict:
        s = self.samples
        lag_all = [x for sm in s for x in sm.pop("_lag", [])]
        after = [x for x in s if x["t"] >= warm] or s
        base = after[0]["rss_mb"] if after else 0.0
        growth = round(max(x["rss_mb"] for x in s) - base, 1) if s else 0.0
        end_growth = round(s[-1]["rss_mb"] - base, 1) if s else 0.0
        half = len(s) // 2

        def q_ok(key):
            vals = [x[key] for x in s]
            if not vals:
                return True
            late = vals[half:] or vals
            return max(vals) <= CRITERIA["queue_cap"] and max(late) <= max(vals[:half] or [0]) + 20

        tasks_growth = (s[-1]["tasks"] - after[0]["tasks"]) if s else 0
        checks = {
            "нет необработанных исключений и ERROR": not self.counter.records and not self.unhandled,
            f"рост RSS < {CRITERIA['rss_growth_mb']:.0f} МБ": growth < CRITERIA["rss_growth_mb"],
            f"задержка цикла p99 < {CRITERIA['loop_lag_p99_ms']:.0f} мс": (percentile(lag_all, 0.99) or 0) < CRITERIA["loop_lag_p99_ms"],
            "очередь речи ограничена": q_ok("q_speech"),
            "очередь внимания ограничена": q_ok("q_attention"),
            "очередь графа ограничена": q_ok("q_graph"),
            f"рост числа задач < {CRITERIA['tasks_growth']}": tasks_growth < CRITERIA["tasks_growth"],
            "ядро закрылось за 30 с": self.shutdown_s is not None and self.shutdown_s < 30,
            "после закрытия нет оставшихся задач": self.leftover_tasks == 0,
        }
        if self.real_modules:
            checks["убитый модуль перезапущен, ядро живо"] = bool(self.restart_ok) and all(self.restart_ok.values())
        return {"minutes": self.minutes, "sample_s": self.sample_s, "mode": "real-modules:" + ",".join(self.real_modules) if self.real_modules else "core-only",
                "load": self.load, "samples": s, "rss_growth_mb": growth, "rss_end_growth_mb": end_growth,
                "lag_p50_ms": percentile(lag_all, 0.5), "lag_p99_ms": percentile(lag_all, 0.99), "lag_max_ms": round(max(lag_all), 1) if lag_all else None,
                "tasks_growth": tasks_growth, "shutdown_s": self.shutdown_s, "leftover_tasks": self.leftover_tasks,
                "errors": self.counter.records[:20] + self.unhandled[:20], "restart_ok": self.restart_ok, "checks": checks,
                "passed": all(checks.values())}


def to_markdown(rep: dict) -> str:
    s = rep["samples"]
    lines = [f"Режим: `{rep['mode']}`, длительность {rep['minutes']} мин, снятие раз в {rep['sample_s']} с.", "",
             f"Нагрузка: реплик в чат {rep['load']['chat']}, событий от модулей {rep['load']['events']}, подключений фиктивных модулей "
             f"{rep['load']['connects']}, обрывов соединения {rep['load']['drops']}, убито процессов модулей {rep['load']['kills']}.", "",
             "| критерий | итог |", "|---|---|"]
    lines += [f"| {k} | {'да' if v else 'НЕТ'} |" for k, v in rep["checks"].items()]
    lines += ["", f"RSS: рост после разогрева (максимум) {rep['rss_growth_mb']} МБ, к концу {rep['rss_end_growth_mb']} МБ. Задержка цикла: p50 "
              f"{rep['lag_p50_ms']} мс, p99 {rep['lag_p99_ms']} мс, максимум {rep['lag_max_ms']} мс. Рост числа задач {rep['tasks_growth']}. "
              f"Закрытие ядра {rep['shutdown_s']} с, оставшихся задач {rep['leftover_tasks']}.", "",
              "| t, с | RSS, МБ | задач | лаг p99, мс | лаг max, мс | речь | внимание | граф | шагов трассы | ошибок | модулей |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    step = max(1, len(s) // 12)
    for x in s[::step] + ([s[-1]] if s and (len(s) - 1) % step else []):
        lines.append(f"| {x['t']} | {x['rss_mb']} | {x['tasks']} | {x['lag_p99_ms']} | {x['lag_max_ms']} | {x['q_speech']} | "
                     f"{x['q_attention']} | {x['q_graph']} | {x['trace_len']} | {x['errors']} | {x['modules']} |")
    if rep["errors"]:
        lines += ["", "Ошибки:"] + [f"- {e}" for e in rep["errors"]]
    return "\n".join(lines) + "\n"


async def run_soak(minutes: float, sample_s: float, real_modules=(), memory_dir: str | None = None, seed: int = 1) -> dict:
    """Точка входа для тестов и скрипта: возвращает отчёт (dict). Глобалы, которые подменяются, возвращаются на место."""
    if memory_dir is not None:
        return await Soak(minutes, sample_s, memory_dir, list(real_modules), seed).run()
    with tempfile.TemporaryDirectory() as tmp:
        return await Soak(minutes, sample_s, tmp, list(real_modules), seed).run()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--sample", type=float, default=10.0, help="период снятия показателей, с")
    ap.add_argument("--real-modules", default="", help="FLOW-3: id настоящих модулей через запятую (telegram,voice,pet)")
    ap.add_argument("--out", default="")
    ap.add_argument("--json", action="store_true", help="напечатать отчёт JSON")
    args = ap.parse_args(argv)
    real = [x for x in args.real_modules.split(",") if x]
    rep = asyncio.run(run_soak(args.minutes, args.sample, real))
    md = to_markdown(rep)
    print(json.dumps({k: v for k, v in rep.items() if k != "samples"}, ensure_ascii=False, indent=2) if args.json else md)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
