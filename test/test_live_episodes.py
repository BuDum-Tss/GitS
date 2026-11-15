"""Живая запись эпизодов (`live_episodes.py`) и писатель-форк (`episode_fork.py`). Часы, журнал, мост и модель: заглушки;
настоящая среда pi не запускается (мост `fake_pi_bridge.py`), на живой сессии проверено не было."""
import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from interfaces.contracts import EndEvent, TextEvent
from services.memory_service import episodes as dom
from services.memory_service.episode_fork import ForkedEpisodeWriter, parse_list
from services.memory_service.live_episodes import LiveEpisodes, live_cfg, will_leave_context
from services.runtime.pi_runtime import PiRuntime

from test_episodes import LLM, Clock, Journal, good_summary, make_runner, no_real_pi, text_or_summary    # noqa: F401
from test_moments import STRONG, att

T0 = 1_800_000_000.0
BRIDGE = Path(__file__).with_name("fake_pi_bridge.py")


def story(moment, **over):
    el = {"moment": moment, "title": "Разговор",
          "text": "Я говорила с пользователем, он просил меня запомнить важное про физику. Мне это важно, и я это запомнила."}
    el.update(over)
    return el


def ids_of(prompt: str) -> list[str]:
    return re.findall(r"<момент (\S+)>", prompt)


class FakeRuntime:
    """AgentRuntime-заглушка: отвечает по сценарию `reply(prompt) -> str` и пишет в файл копии сессии."""

    def __init__(self, copy, reply, log):
        self.copy, self.reply, self.log = copy, reply, log

    async def start(self, system, tools):
        self.log.append(("start", system, list(tools), self.copy.read_text()))

    async def run(self, prompt):
        self.copy.write_text(self.copy.read_text() + "+")             # форк пишет только в свою копию
        self.log.append(("run", prompt))
        yield TextEvent(self.reply(prompt))
        yield EndEvent("ok", None, {})

    async def close(self):
        self.log.append(("close",))


def fork_all(prompt):
    return json.dumps([story(i) for i in ids_of(prompt)], ensure_ascii=False)


class Fork:
    """Писатель-форк на заглушках: живой файл сессии, журнал вызовов среды, флаг занятости."""

    def __init__(self, tmp_path, r, reply=fork_all, busy=None, content="сессия"):
        self.live = tmp_path / "resident_pi_session.json"
        self.live.write_text(content, encoding="utf-8")
        self.log, self.reply, self.busy_now, self.runtimes = [], reply, False, 0
        self.copies = []

        def factory(copy):
            self.runtimes += 1
            self.copies.append(copy)
            return FakeRuntime(copy, lambda p: self.reply(p), self.log)

        self.writer = ForkedEpisodeWriter(fallback=r.summarizer, session_path=lambda: self.live, system=lambda: "её система",
                                          busy=busy or (lambda: self.busy_now), settings=lambda: r.settings, allow=lambda: ["ГЛаДОС"],
                                          runtime_factory=factory, tmp_root=str(tmp_path))


def plain_summary(system, user, n):
    """Строгий писатель (запасной путь): рассказ без имён и чисел, которых нет в событиях."""
    return json.dumps({"title": "Разговор", "refs": [int(x) for x in re.findall(r"^\[(\d+)\]", user, re.M)][:1],
                       "text": "Я говорила с пользователем. Он просил меня запомнить важное."}, ensure_ascii=False)


def plain_or_text(system, user, n):
    return plain_summary(system, user, n) if "Эпизод с" in user else json.dumps({"text": "Я говорила с пользователем."}, ensure_ascii=False)


def one_peak_journal(n=1, gap=1000):
    j = Journal()
    for k in range(n):
        j.add(T0 + k * gap, "user", STRONG)
    return j


async def pending_of(r, journal_now=None):
    await r.selector.run()
    return r.store.select(status="pending", oldest_first=True, limit=100)


# ── форк: копия сессии ───────────────────────────────────────────────────────────────────────

async def test_fork_writes_into_a_copy_and_leaves_the_live_file_untouched(tmp_path):
    r = make_runner(tmp_path, one_peak_journal(), LLM(good_summary), Clock(T0 + 5000))
    f = Fork(tmp_path, r, content="сессия на 3320 сообщений")
    before = f.live.read_bytes()
    (ep,) = await pending_of(r)
    res = await f.writer.summarize(ep, r.store.events_of(ep["id"]))
    assert res.ok and "Мне это важно" in res.data["text"] and res.data["title"] == "Разговор"
    assert f.live.read_bytes() == before                              # живой файл байт в байт прежний
    kinds = [x[0] for x in f.log]
    assert kinds == ["start", "run", "close"] and f.log[0][1] == "её система" and f.log[0][2] == []     # без инструментов
    assert f.log[0][3] == "сессия на 3320 сообщений"                  # в копии та же история
    assert f.copies[0] != f.live and not f.copies[0].exists() and not f.copies[0].parent.exists()      # копия удалена
    prompt = f.log[1][1]
    assert f"<момент {ep['id']}>" in prompt and "<<<" in prompt and "[1]" in prompt and "реши, хочешь ли ты это помнить" in prompt
    assert r.llm.calls == []                                          # запасной путь не понадобился


async def test_fork_with_real_pi_runtime_on_fake_bridge(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    r = make_runner(tmp_path, one_peak_journal(), LLM(lambda s, u, n: ""), Clock(T0 + 5000))
    (ep,) = await pending_of(r)
    seen = {}

    class Spy(PiRuntime):
        async def close(self):
            seen["copy"] = self._session_path().read_text()           # копия на момент закрытия
            seen["path"] = self._session_path()
            await super().close()

    replies = [json.dumps([story(ep["id"])], ensure_ascii=False)]
    f = Fork(tmp_path, r, content="5")
    f.writer.runtime_factory = lambda copy: Spy(command=[sys.executable, str(BRIDGE)], auth_file=str(auth), session_file=copy,
                                                faux_responses=replies, max_context_chars=300000)
    res = await f.writer.summarize(ep, r.store.events_of(ep["id"]))
    assert res.ok and f.live.read_text() == "5"                       # ход записан в копию (счётчик моста), не в живой файл
    assert seen["copy"] == "6" and not seen["path"].exists()


async def test_runtime_gets_a_bigger_context_limit_and_no_tools(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    for limit, want in ((300000, 300000), (None, 120000)):
        rt = PiRuntime(command=[sys.executable, str(BRIDGE)], auth_file=str(auth), session_file=tmp_path / "s.txt", max_context_chars=limit)
        await rt.start("система", [])
        text = "".join([e.text async for e in rt.run("info") if isinstance(e, TextEvent)])
        await rt.close()
        assert json.loads(text) == {"tools": 0, "max_context_chars": want, "session_file": str(tmp_path / "s.txt")}


async def test_busy_session_is_not_copied(tmp_path):
    r = make_runner(tmp_path, one_peak_journal(), LLM(good_summary), Clock(T0 + 5000))
    f = Fork(tmp_path, r)
    f.busy_now = True
    (ep,) = await pending_of(r)
    res = await f.writer.summarize(ep, r.store.events_of(ep["id"]))
    assert res.unavailable and f.runtimes == 0 and r.llm.calls == []   # ни копии, ни запасного пути: эпизод ждёт
    assert list(tmp_path.glob("gits-fork-*")) == []
    calls = {"n": 0}

    def busy_after_copy():
        calls["n"] += 1
        return calls["n"] >= 2                                         # ход начался, пока копировали
    f.writer.busy = busy_after_copy
    res = await f.writer.summarize(ep, r.store.events_of(ep["id"]))
    assert res.unavailable and f.runtimes == 0 and list(tmp_path.glob("gits-fork-*")) == []


async def test_garbage_is_retried_then_accepted_or_marked_retry(tmp_path):
    r = make_runner(tmp_path, one_peak_journal(), LLM(lambda s, u, n: ""), Clock(T0 + 5000))
    (ep,) = await pending_of(r)
    answers = iter(["мусор без json", "[{плохо", json.dumps([story(ep["id"])], ensure_ascii=False)])
    f = Fork(tmp_path, r, reply=lambda p: next(answers))
    res = await f.writer.summarize(ep, r.store.events_of(ep["id"]))
    assert res.ok and [x[0] for x in f.log].count("run") == 3 and "не разобрался" in f.log[2][1]
    f2 = Fork(tmp_path, r, reply=lambda p: "опять мусор")
    res = await f2.writer.summarize(ep, r.store.events_of(ep["id"]))
    assert not res.ok and res.retry and not res.unavailable and [x[0] for x in f2.log].count("run") == 3
    rep = {"llm_down": False, "summarized": 0, "embedded": 0, "failed": 0, "skipped": 0, "notified": 0}
    for attempt in (1, 2):                                              # две неудачи: эпизод ещё pending
        await r.runner.process_episode(r.store.get(ep["id"]), rep, result=res)
        assert r.store.get(ep["id"])["status"] == "pending" and r.store.get(ep["id"])["attempts"] == attempt
    await r.runner.process_episode(r.store.get(ep["id"]), rep, result=res)
    assert r.store.get(ep["id"])["status"] == "failed" and rep["failed"] == 1       # третья: failed


async def test_empty_array_is_a_decision_not_an_error(tmp_path):
    r = make_runner(tmp_path, one_peak_journal(2), LLM(good_summary), Clock(T0 + 5000))
    f = Fork(tmp_path, r, reply=lambda p: "[]")
    pend = await pending_of(r)
    res = await f.writer.summarize_many([(e, r.store.events_of(e["id"])) for e in pend])
    assert all(x.skip and not x.retry and not x.ok for x in res.values()) and [x[0] for x in f.log].count("run") == 1
    rep = {"llm_down": False, "summarized": 0, "embedded": 0, "failed": 0, "skipped": 0, "notified": 0}
    for e in pend:
        await r.runner.process_episode(e, rep, result=res[e["id"]])
    eps = r.store.select(limit=10)
    assert {e["status"] for e in eps} == {"skipped"} and rep["skipped"] == 2 and eps[0]["error"] == "не стоит запоминать"
    assert eps[0]["attempts"] == 0 and r.store.counts()["skipped"] == 2
    await r.runner.run(dom.SleepPlan("idle", None))                     # сон их не трогает
    assert {e["status"] for e in r.store.select(limit=10)} == {"skipped"}


async def test_partial_answer_skips_the_rest_and_bad_element_is_retried(tmp_path):
    r = make_runner(tmp_path, one_peak_journal(3), LLM(good_summary), Clock(T0 + 5000))
    pend = await pending_of(r)
    a, b, c = (e["id"] for e in pend)
    answers = iter([json.dumps([story(a), story(b, text="")], ensure_ascii=False),        # c не включён, b неверный
                    json.dumps([story(b)], ensure_ascii=False)])
    f = Fork(tmp_path, r, reply=lambda p: next(answers))
    res = await f.writer.summarize_many([(e, r.store.events_of(e["id"])) for e in pend])
    assert res[a].ok and res[b].ok and res[c].skip
    remark = f.log[2][1].rsplit("По моментам", 1)[1]                    # повторный вопрос только по неверному моменту
    assert b in remark and a not in remark and c not in remark


async def test_trusted_check_does_not_need_names_from_events(tmp_path):
    r = make_runner(tmp_path, one_peak_journal(), LLM(lambda s, u, n: ""), Clock(T0 + 5000))
    (ep,) = await pending_of(r)
    f = Fork(tmp_path, r, reply=lambda p: json.dumps([story(ep["id"], text="Я вспомнила разговор с Борисом на прошлой неделе про 77 яблок.")], ensure_ascii=False))
    res = await f.writer.summarize(ep, r.store.events_of(ep["id"]))
    assert res.ok and "Борисом" in res.data["text"]                       # имена и числа форка не сверяются с событиями


async def test_fallback_when_fork_is_impossible(tmp_path):
    r = make_runner(tmp_path, one_peak_journal(), LLM(plain_summary), Clock(T0 + 5000))
    (ep,) = await pending_of(r)
    events = r.store.events_of(ep["id"])
    f = Fork(tmp_path, r)
    f.live.unlink()                                                      # нет сессии
    assert (await f.writer.summarize(ep, events)).ok and f.runtimes == 0 and len(r.llm.calls) == 1
    f = Fork(tmp_path, r)
    f.writer.runtime_factory = lambda copy: (_ for _ in ()).throw(RuntimeError("нет node"))     # мост не поднялся
    assert (await f.writer.summarize(ep, events)).ok and len(r.llm.calls) == 2
    r.settings["episodes"]["writer"] = "fast"                            # форк выключен настройкой
    f = Fork(tmp_path, r)
    assert (await f.writer.summarize(ep, events)).ok and f.runtimes == 0 and len(r.llm.calls) == 3
    r.settings["episodes"]["writer"] = "fork"
    r.llm.answer = lambda s, u, n: ""                                    # недоступно всё
    f = Fork(tmp_path, r)
    f.live.unlink()
    res = await f.writer.summarize(ep, events)
    assert res.unavailable and not res.ok


def test_parse_list_forms():
    assert parse_list("текст [{\"moment\": \"a\"}] хвост") == [{"moment": "a"}]
    assert parse_list("[]") == [] and parse_list('{"moment": "a"}') == [{"moment": "a"}]
    assert parse_list('{"episodes": [{"moment": "b"}]}') == [{"moment": "b"}] and parse_list("нет json") is None


# ── условие выхода из контекста ──────────────────────────────────────────────────────────────

def test_will_leave_context():
    rows = [{"text": "а" * 940}] * 60                                   # 60 * (940 + 60) = 60000
    assert not will_leave_context(rows, 120000, 0.5)
    assert will_leave_context(rows + [{"text": "б"}], 120000, 0.5)
    assert not will_leave_context([], 120000, 0.5) and will_leave_context(rows, 120000, 0.4)


def test_live_cfg_defaults_and_overrides():
    cfg = live_cfg(lambda: {"episodes": {"live": {"tick_s": 5, "enabled": False, "per_hour": "много", "context_share": 0.4}}})
    assert cfg["tick_s"] == 5 and cfg["enabled"] is False and cfg["per_hour"] == 8 and cfg["context_share"] == 0.4
    assert cfg["min_gap_s"] == 45 and cfg["fork_after_events"] == 6 and cfg["instant_score"] == 0.85 and cfg["max_wait_s"] == 1800


# ── живой цикл ───────────────────────────────────────────────────────────────────────────────

def make_live(tmp_path, journal, now, live=None, reply=fork_all, llm=None, busy=None, asleep=None):
    r = make_runner(tmp_path, journal, llm or LLM(plain_or_text), Clock(now))
    r.settings["episodes"]["live"] = {"tick_s": 0.01, **(live or {})}
    f = Fork(tmp_path, r, reply=reply)
    r.runner.summarizer = f.writer
    parts = SimpleNamespace(store=r.store, selector=r.selector, runner=r.runner, summarizer=f.writer)
    live_ep = LiveEpisodes(lambda: parts, settings=lambda: r.settings, clock=r.clock, source=journal,
                           busy=busy or (lambda: f.busy_now), asleep=asleep or (lambda: False))
    return r, f, live_ep


async def test_instant_peak_is_written_right_away_one_batch(tmp_path):
    j = Journal()
    j.add(T0, "attention", "Колонист погиб", decision="D4", urgency=5, routes=["think"], salience=0.0)
    r, f, live = make_live(tmp_path, j, T0 + 25)
    assert await live.tick() == "wrote"                                 # через 20 секунд, не через 2 минуты
    (ep,) = r.store.select()
    assert ep["status"] == "embedded" and ep["title"] and "Мне это важно" in ep["text"]
    assert await live.tick() == "idle"


async def test_trigger_by_number_of_peaks_batches_four_and_respects_gap(tmp_path):
    j = one_peak_journal(6, gap=400)
    r, f, live = make_live(tmp_path, j, T0 + 6 * 400 + 500)
    assert await live.tick() == "wrote"
    assert len(ids_of([x[1] for x in f.log if x[0] == "run"][0])) == 4                      # max_per_fork
    assert r.store.counts()["embedded"] == 4 and r.store.counts()["pending"] == 2
    assert await live.tick() == "quiet"                                  # осталось 2 пика, других условий нет
    r.settings["episodes"]["live"]["fork_after_events"] = 2
    assert await live.tick() == "gap"                                    # условие есть, но пауза между вызовами модели не прошла
    r.clock.t += 46
    assert await live.tick() == "wrote" and r.store.counts()["embedded"] == 6


async def test_trigger_by_context_budget(tmp_path):
    j = one_peak_journal()
    for k in range(70):
        j.add(T0 + 10 + k, "say", "а" * 1000)                           # она наговорила больше половины контекста
    r, f, live = make_live(tmp_path, j, T0 + 5000)
    assert await live.tick() == "wrote" and r.store.counts()["embedded"] == 1
    (tmp_path / "b").mkdir()
    r2, f2, live2 = make_live(tmp_path / "b", one_peak_journal(), T0 + 500)
    assert await live2.tick() == "quiet"                                 # нового мало, пик не уходит из контекста


async def test_trigger_by_max_wait(tmp_path):
    r, f, live = make_live(tmp_path, one_peak_journal(), T0 + 500)
    assert await live.tick() == "quiet"
    r.clock.t = T0 + 2000                                                # самому старому моменту больше 1800 секунд
    assert await live.tick() == "wrote"


async def test_not_while_busy_or_asleep_or_disabled(tmp_path):
    j = Journal()
    j.add(T0, "attention", "Колонист погиб", decision="D4", urgency=5, routes=["think"])
    r, f, live = make_live(tmp_path, j, T0 + 25)
    f.busy_now = True
    assert await live.tick() == "busy" and r.store.counts()["pending"] == 1 and f.runtimes == 0     # момент записан, эпизод ждёт
    f.busy_now = False
    state = {"sleep": True}
    live.asleep = lambda: state["sleep"]
    assert await live.tick() == "asleep"
    state["sleep"] = False
    r.settings["episodes"]["live"]["enabled"] = False
    assert await live.tick() == "off"
    r.settings["episodes"]["live"]["enabled"] = True
    assert await live.tick() == "wrote"


async def test_per_hour_limit_and_skipped_count_in_it(tmp_path):
    j = Journal()
    for k in range(3):
        j.add(T0 + k * 1000, "attention", f"Событие {k}", decision="D4", urgency=5, routes=["think"])
    r, f, live = make_live(tmp_path, j, T0 + 3 * 1000, live={"per_hour": 2, "min_gap_s": 1, "max_per_fork": 1}, reply=lambda p: "[]")
    assert await live.tick() == "wrote" and live.last_report["skipped"] == 1   # пропуск тоже вызов модели
    r.clock.t += 2
    assert await live.tick() == "wrote"
    r.clock.t += 2
    assert await live.tick() == "hour"
    r.clock.t += 3600
    assert await live.tick() == "wrote" and r.store.counts()["skipped"] == 3 and r.store.counts()["pending"] == 0


async def test_max_pending_drops_the_dimmest_oldest(tmp_path):
    j = Journal()
    j.add(T0, "attention", "слабое", decision="D3", urgency=0, routes=["speech"])             # 0.65
    j.add(T0 + 1000, "attention", "сильное", decision="D4", urgency=5, routes=["speech"])        # 0.95
    j.add(T0 + 2000, "attention", "среднее", decision="D3", urgency=4, routes=["chat"])         # 0.85
    r, f, live = make_live(tmp_path, j, T0 + 5000, live={"max_pending": 2})
    f.busy_now = True
    await live.tick()
    by = {e["first_id"]: e["status"] for e in r.store.select(limit=10)}
    assert by == {1: "skipped", 2: "pending", 3: "pending"}             # тусклый отброшен, не ошибка и не failed
    assert r.store.get(r.store.select(status="skipped")[0]["id"])["attempts"] == 0


async def test_unavailable_model_keeps_pending_and_retries_next_tick(tmp_path):
    j = Journal()
    j.add(T0, "attention", "Колонист погиб", decision="D4", urgency=5, routes=["think"])
    r, f, live = make_live(tmp_path, j, T0 + 25, llm=LLM(lambda s, u, n: ""))
    f.live.unlink()                                                      # форка нет, запасной писатель молчит
    assert await live.tick() == "down" and r.store.select()[0]["status"] == "pending"
    r.clock.t += 50
    assert await live.tick() == "down"                                   # не ошибка: пробует снова
    f.live.write_text("сессия", encoding="utf-8")
    r.clock.t += 50
    assert await live.tick() == "wrote" and r.store.select()[0]["status"] == "embedded"


async def test_embedder_down_leaves_summarized_then_recovers(tmp_path):
    from services.memory_service.search.embedder import HashEmbedder

    class Flaky(HashEmbedder):
        up = False

        async def embed(self, texts, query=False):
            if not self.up:
                raise RuntimeError("down")
            return await super().embed(texts, query)

    j = Journal()
    j.add(T0, "attention", "Колонист погиб", decision="D4", urgency=5, routes=["think"])
    r, f, live = make_live(tmp_path, j, T0 + 25)
    r.search.embedder = emb = Flaky()
    assert await live.tick() == "wrote" and r.store.select()[0]["status"] == "summarized"
    emb.up = True
    assert await live.tick() == "wrote" and r.store.select()[0]["status"] == "embedded"


async def test_sleep_still_processes_leftovers(tmp_path):
    j = one_peak_journal(2)
    r, f, live = make_live(tmp_path, j, T0 + 3000)
    await r.selector.run()
    assert r.store.counts()["pending"] == 2                              # живой цикл ничего не писал (нет условий)
    rep = await r.runner.run(dom.SleepPlan("idle", None))
    assert rep["summarized"] == 2 and r.store.counts()["embedded"] == 2 and rep["skipped"] == 0


async def test_character_switch_has_its_own_state_and_parts(tmp_path):
    j = Journal()
    j.add(T0, "attention", "Колонист погиб", decision="D4", urgency=5, routes=["think"])
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    r1, f1, _ = make_live(tmp_path / "a", j, T0 + 25)
    r2, f2, _ = make_live(tmp_path / "b", j, T0 + 25)
    cur = {"p": SimpleNamespace(store=r1.store, selector=r1.selector, runner=r1.runner, summarizer=f1.writer)}
    live = LiveEpisodes(lambda: cur["p"], settings=lambda: r1.settings, clock=r1.clock, source=j)
    assert await live.tick() == "wrote" and await live.tick() == "idle"
    cur["p"] = SimpleNamespace(store=r2.store, selector=r2.selector, runner=r2.runner, summarizer=f2.writer)
    assert await live.tick() == "wrote"                                  # у второго персонажа своя пауза и своя таблица
    assert r1.store.counts()["embedded"] == 1 and r2.store.counts()["embedded"] == 1


async def test_loop_starts_and_stops(tmp_path):
    j = Journal()
    j.add(T0, "attention", "Колонист погиб", decision="D4", urgency=5, routes=["think"])
    r, f, live = make_live(tmp_path, j, T0 + 25)
    await live.start()
    for _ in range(100):
        await asyncio.sleep(0.02)
        if r.store.counts()["embedded"]:
            break
    await live.stop()
    assert r.store.counts()["embedded"] == 1 and live._task is None


async def test_service_starts_live_loop_next_to_scheduler(tmp_path):
    from services.memory_service.episode_service import EpisodeService
    from services.memory_service.search.embedder import HashEmbedder
    j = Journal()
    comp = SimpleNamespace(_dir=tmp_path / "a", _llm=LLM(text_or_summary), idle_seconds=lambda: 0.0,
                           _last_user_ts=0.0, _settings=SimpleNamespace(load=lambda: {"episodes": {"live": {"tick_s": 0.01}}}))
    svc = EpisodeService(comp, SimpleNamespace(submit=None), clock=Clock(T0 + 5000), embedder=HashEmbedder(), source=j)
    await svc.start()
    assert svc.live is not None and svc.live._task is not None and svc.scheduler is not None
    assert svc._live_busy() is False
    comp.resident = SimpleNamespace(busy=True)
    assert svc._live_busy() is True                                     # она отвечает: эпизод не пишется
    await svc.stop()
    assert svc.live._task is None


_ = (att, good_summary)
