"""Эпизоды, режим сна, дневник (ТЗ 27, EPI-1..EPI-12). Модель, эмбеддер и журнал: заглушки, сеть и LM Studio не нужны."""
import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from services.character import character
from interfaces import contracts
from services.tracing import events_log
from services.memory_service.episode_store import EpisodeStore
from services.memory_service.adapters import UserMemoryAdapter
from services.memory_service.diary import DayWriter
from services.memory_service.moment_selector import MomentSelector
from services.memory_service.episode_recall import EpisodeRecall
from services.memory_service.episode_summary import EpisodeSummarizer
from services.memory_service.sleep import SleepRunner
from services.memory_service.sleep_scheduler import SleepScheduler
from services.memory_service import episodes as dom
from services.memory_service.episode_service import EpisodeService, summary_event
from services.memory_service.search import HybridIndex, HybridSearch, SearchConfig
from services.memory_service.search.embedder import HashEmbedder

T0 = 1_800_000_000.0           # фиксированное «сегодня» тестов
MIN = 60.0


@pytest.fixture(autouse=True)
def no_real_pi(monkeypatch):
    """Сильная модель pi в тестах недоступна (мост и сеть не нужны): запасной писатель идёт на быструю заглушку."""
    from services.runtime import pi_llm

    async def down(system, user, timeout):
        raise RuntimeError("pi в тестах недоступна")

    monkeypatch.setattr(pi_llm, "ask_heavy", down)


# ── заглушки ─────────────────────────────────────────────────────────────────────────────────

class Journal:
    """EventSource в памяти: строки с номерами, как отдаёт events_log.after."""

    def __init__(self):
        self.rows = []
        self.deleted = []

    def add(self, ts, kind, text, **data):
        self.rows.append({"id": len(self.rows) + 1, "ts": ts, "kind": kind, "text": text, **({"data": data} if data else {})})
        return len(self.rows)

    def after(self, after_id=0, limit=500, kinds=None):
        return [r for r in self.rows if r["id"] > after_id and (not kinds or r["kind"] in kinds)][:limit]

    def last_id(self):
        return self.rows[-1]["id"] if self.rows else 0

    def delete_ids(self, ids):
        self.deleted += list(ids)
        self.rows = [r for r in self.rows if r["id"] not in ids]
        return len(ids)


class Clock:
    def __init__(self, t=T0 + 86400):
        self.t = t

    def __call__(self):
        return self.t


class LLM:
    """Скриптовая модель: ответ по функции от (system, user); считает вызовы."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def ask(self, system, user, *, timeout, max_tokens=40, temperature=0.3, background=False, image_b64=None):
        self.calls.append((system, user))
        res = self.answer(system, user, len(self.calls))
        if asyncio.iscoroutine(res):
            res = await res
        if isinstance(res, Exception):
            raise res
        return res


def ids_in(user: str) -> list[int]:
    import re
    return [int(x) for x in re.findall(r"^\[(\d+)\]", user, re.M)]


def good_summary(system, user, n):
    """Корректное резюме: ссылки на реально показанные события, имена и числа из них."""
    ids = ids_in(user)
    return json.dumps({"title": "Экзамен Ани", "refs": ids[:2],
                       "story": "Я узнала от Ани про экзамен по физике. Он завтра в 18:30. Я записала это и пообещала напомнить.",
                       "noticed": ["она просила запомнить"], "attitude": "Мне это важно, и я не хочу подвести Аню.",
                       "quotes": ["экзамен по физике завтра в 18:30", "выдуманная цитата"],
                       "facts": ["Аня сдаёт экзамен по физике завтра в 18:30"], "emotions": ["забота"], "participants": ["Аня"]},
                      ensure_ascii=False)


def bad_summary(system, user, n):
    return json.dumps({"title": "x", "refs": [999], "story": "Борис принёс 77 яблок. Я рада. Мы ушли.", "attitude": "Рада."}, ensure_ascii=False)


class Writer:
    def __init__(self):
        self.queue = []

    def enqueue_for_graph(self, speaker, text, source=None):
        self.queue.append((speaker, text, source))


def chat_day(j: Journal, t, who="Аня"):
    """Диалог про экзамен: яркие реплики (пик) и спокойные."""
    j.add(t, "user", f"{who} сказала: запомни, мне нравится физика, экзамен по физике завтра в 18:30")    # пик: 0.7
    j.add(t + 5, "say", "Запомнила, напомню")
    j.add(t + 9, "user", "спасибо")


def make_runner(tmp_path, journal, llm, clock=None, cfg=None, embedder=True, notify=None, writer=None):
    clock = clock or Clock()
    path = tmp_path / "episodes.db"
    store = EpisodeStore(path, clock)
    search = HybridSearch(HybridIndex(path, clock=clock), HashEmbedder() if embedder else None, llm,
                          SearchConfig(min_cos=0.2, min_cos_bm25=0.1))
    settings = {"episodes": {"notify_salience": 0.5, **(cfg or {})}}
    selector = MomentSelector(source=journal, store=store, settings=lambda: settings, clock=clock, character=lambda: "glados",
                              first_upto=lambda: 0)
    summarizer = EpisodeSummarizer(llm=llm, prompt=lambda: {"system": "Ты вспоминаешь.", "format": "JSON"},
                                   allow=lambda: ["ГЛаДОС"], settings=lambda: settings)
    prompt = lambda: {"system": "дай JSON text", "format": ""}                                     # noqa: E731
    days = DayWriter(llm=llm, store=store, search=search, digest_prompt=prompt, diary_prompt=prompt)
    writer = writer or Writer()
    runner = SleepRunner(selector=selector, summarizer=summarizer, store=store, search=search, writer=writer, days=days,
                         source=journal, settings=lambda: settings, clock=clock, notify=notify)
    return SimpleNamespace(store=store, search=search, selector=selector, summarizer=summarizer, runner=runner, writer=writer,
                           clock=clock, settings=settings, journal=journal, llm=llm)


def text_or_summary(system, user, n):
    """Один ответчик на всё: резюме эпизода, сводка и дневник."""
    if "Эпизод с" in user:
        return good_summary(system, user, n)
    return json.dumps({"text": "Я помогала Ане с экзаменом по физике."}, ensure_ascii=False)


# ── домен: проверка рассказа ─────────────────────────────────────────────────────────────────

SRC = [dom.Rec(1, T0, "user", "Аня сказала: экзамен по физике завтра в 18:30"), dom.Rec(2, T0 + 5, "say", "Запомнила, напомню"),
       dom.Rec(3, T0 + 9, "user", "спасибо")]


def parsed(**over):
    base = {"title": "Экзамен", "refs": [1, 2], "quotes": ["экзамен по физике завтра"], "noticed": ["Аня назвала время"],
            "story": "Я услышала про экзамен. Аня назвала время 18:30. Я пообещала напомнить.",
            "attitude": "Мне важно не подвести Аню."}
    base.update(over)
    return base


def test_valid_summary_passes_and_unknown_quote_is_dropped():
    ok, problems = dom.check_summary(parsed(quotes=["экзамен по физике завтра", "этого не говорили"]), SRC)
    assert problems == [] and ok["quotes"] == ["экзамен по физике завтра"]
    assert ok["attitude"] and ok["noticed"] == ["Аня назвала время"] and ok["summary"].startswith("Я услышала")


def test_trusted_summary_skips_names_numbers_and_refs_but_keeps_quotes():
    over = parsed(story="Я говорила с Борисом про 77 яблок и вспомнила прошлую неделю.", refs=[], quotes=["экзамен по физике", "нет такого"])
    assert dom.check_summary(over, SRC)[0] is None
    ok, problems = dom.check_summary(over, SRC, trusted=True)
    assert problems == [] and ok["quotes"] == ["экзамен по физике"]
    assert dom.check_summary(parsed(attitude=""), SRC, trusted=True)[0] is None


@pytest.mark.parametrize("over, expect", [
    ({"refs": [1, 77]}, "несуществующие события"),
    ({"refs": []}, "нет ссылок"),
    ({"story": "Я услышала про Бориса. Он пришёл. Мы поговорили."}, "имён нет"),
    ({"attitude": "Я рада за Бориса."}, "имён нет"),
    ({"noticed": ["она упомянула Бориса"]}, "имён нет"),
    ({"attitude": ""}, "attitude"),
    ({"story": "Я услышала про экзамен. Аня назвала время 19:45. Я пообещала напомнить."}, "чисел нет"),
    ({"story": "Я услышала про экзамен."}, "предложений"),
    ({"story": " ".join(["Я слушала."] * 7)}, "предложений"),
    ({"title": ""}, "заголовка"),
])
def test_summary_problems(over, expect):
    clean, problems = dom.check_summary(parsed(**over), SRC)
    assert clean is None and any(expect in p for p in problems)


def test_declined_name_and_allowed_name():
    ok, p = dom.check_summary(parsed(story="Я услышала от Ани про экзамен. Она назвала время 18:30. Я пообещала напомнить."), SRC)
    assert p == []                                                                # «Ани» - форма имени «Аня»
    clean, p = dom.check_summary(parsed(story="Я, ГЛаДОС, услышала про экзамен. Аня назвала время. Я пообещала напомнить."), SRC)
    assert clean is None and any("ГЛаДОС" in x for x in p)
    ok, p = dom.check_summary(parsed(story="Я, ГЛаДОС, услышала про экзамен. Аня назвала время. Я пообещала напомнить."), SRC,
                              allow=["ГЛаДОС"])
    assert p == []


# ── домен: сон ───────────────────────────────────────────────────────────────────────────────

def test_decide_sleep_modes():
    cfg = {"enabled": True, "idle_min": 15, "min_gap_min": 20, "schedule_hours": [3], "catchup_after_h": 12,
           "catchup_budget_tokens": 900}
    now = T0
    assert dom.decide_sleep(idle_s=20 * MIN, now=now, hour=14, last_sleep=0, cfg=cfg).mode == "idle"
    assert dom.decide_sleep(idle_s=0, now=now, hour=3, last_sleep=now - 13 * 3600, cfg=cfg).mode == "scheduled"
    plan = dom.decide_sleep(idle_s=60, now=now, hour=14, last_sleep=now - 13 * 3600, cfg=cfg)
    assert plan.mode == "catchup" and plan.budget_tokens == 900
    assert dom.decide_sleep(idle_s=60, now=now, hour=14, last_sleep=now - 2 * 3600, cfg=cfg) is None
    assert dom.decide_sleep(idle_s=3600, now=now, hour=14, last_sleep=now - 5 * MIN, cfg=cfg) is None      # слишком скоро
    assert dom.decide_sleep(idle_s=3600, now=now, hour=14, last_sleep=0, cfg={**cfg, "enabled": False}) is None


def test_diary_and_compress_days():
    assert dom.diary_days(today="2026-10-16", hour=10, days_with_events=["2026-10-14", "2026-10-15", "2026-10-16"],
                          written={"2026-10-14"}) == ["2026-10-15"]
    assert dom.diary_days(today="2026-10-16", hour=23, days_with_events=["2026-10-16"], written=set()) == ["2026-10-16"]
    today = datetime(2026, 10, 16).date().toordinal()
    assert dom.compress_days(today_ord=today, episodes_by_day={"2026-08-01": 2, "2026-10-15": 1}, compressed=set(),
                             after_days=30) == ["2026-08-01"]
    assert dom.compress_days(today_ord=today, episodes_by_day={"2026-08-01": 2}, compressed=set(), after_days=0) == []


# ── выбор моментов и хранилище ───────────────────────────────────────────────────────────────

async def test_selector_is_idempotent_and_keeps_open_moment(tmp_path):
    j = Journal()
    chat_day(j, T0)
    chat_day(j, T0 + 30 * MIN)                                   # через 30 минут: второй момент
    r = make_runner(tmp_path, j, LLM(good_summary), Clock(T0 + 30 * MIN + 100))
    got = await r.selector.run()
    assert got["episodes"] == 1 and r.store.get_meta("moments_upto") == 3         # второй момент ещё идёт
    assert (await r.selector.run())["episodes"] == 0
    r.clock.t = T0 + 30 * MIN + 2000                                              # время вышло: закрывается и он
    assert (await r.selector.run())["episodes"] == 1
    assert (await r.selector.run())["episodes"] == 0
    eps = r.store.select(oldest_first=True)
    assert [e["status"] for e in eps] == ["pending", "pending"] and eps[0]["first_id"] == 1 and eps[0]["last_id"] == 3
    assert [e["id"] for e in r.store.events_of(eps[0]["id"])] == [1, 2, 3]
    assert eps[0]["character"] == "glados" and eps[0]["peak_id"] == 1 and eps[0]["salience"] == 0.7
    assert eps[0]["stream"] == "moment" and "user" in eps[0]["participants"]


async def test_new_selector_starts_from_the_end_of_the_journal(tmp_path):
    j = Journal()
    chat_day(j, T0)
    store = EpisodeStore(tmp_path / "episodes.db")
    selector = MomentSelector(source=j, store=store, settings=lambda: {}, clock=Clock(T0 + 9999))
    assert (await selector.run())["episodes"] == 0 and store.get_meta("moments_upto") == 3    # прошлое не обрабатывается


# ── резюме ───────────────────────────────────────────────────────────────────────────────────

async def prepared_episode(r):
    await r.selector.run()
    ep = r.store.select()[0]
    return ep, r.store.events_of(ep["id"])


async def test_summary_passes_verification(tmp_path):
    j = Journal()
    chat_day(j, T0)
    r = make_runner(tmp_path, j, LLM(good_summary), Clock(T0 + 5000))
    ep, events = await prepared_episode(r)
    res = await r.summarizer.summarize(ep, events)
    assert res.ok and res.data["quotes"] == ["экзамен по физике завтра в 18:30"] and res.data["refs"] == [1, 2]
    assert "<<<" in r.llm.calls[0][1] and "[1]" in r.llm.calls[0][1]              # источники с номерами, как данные


async def test_bad_summary_gets_a_remark_then_fails(tmp_path):
    j = Journal()
    chat_day(j, T0)
    llm = LLM(bad_summary)
    r = make_runner(tmp_path, j, llm, Clock(T0 + 5000))
    ep, events = await prepared_episode(r)
    res = await r.summarizer.summarize(ep, events)
    assert not res.ok and not res.unavailable and len(llm.calls) == 2
    assert "Замечания" in llm.calls[1][1] and "несуществующие" in llm.calls[1][1]


async def test_second_attempt_can_fix_the_summary(tmp_path):
    j = Journal()
    chat_day(j, T0)
    llm = LLM(lambda s, u, n: bad_summary(s, u, n) if n == 1 else good_summary(s, u, n))
    r = make_runner(tmp_path, j, llm, Clock(T0 + 5000))
    ep, events = await prepared_episode(r)
    assert (await r.summarizer.summarize(ep, events)).ok


async def test_unavailable_model_is_not_a_failure(tmp_path):
    j = Journal()
    chat_day(j, T0)
    r = make_runner(tmp_path, j, LLM(lambda s, u, n: ""), Clock(T0 + 5000))
    ep, events = await prepared_episode(r)
    res = await r.summarizer.summarize(ep, events)
    assert res.unavailable and not res.ok


# ── сон ──────────────────────────────────────────────────────────────────────────────────────

def two_days(j):
    chat_day(j, T0)                      # вчера
    chat_day(j, T0 + 86400 - 3600)       # сегодня (clock = T0 + 86400)


async def test_sleep_pipeline_end_to_end(tmp_path):
    j = Journal()
    two_days(j)
    sent = []

    async def notify(ep):
        sent.append(ep)

    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 86400 + 4 * 3600), notify=notify)
    rep = await r.runner.run(dom.SleepPlan("idle", None))
    assert rep["cut"] == 2 and rep["summarized"] == 2 and rep["embedded"] == 2 and rep["failed"] == 0
    eps = r.store.select(oldest_first=True)
    assert [e["status"] for e in eps] == ["embedded", "embedded"] and all(e["version"] == 1 for e in eps)
    assert r.writer.queue and all(src == eps[i]["id"] for i, (_, _, src) in enumerate(r.writer.queue[:1]))   # факт знает эпизод
    assert eps[0]["quotes"] == ["экзамен по физике завтра в 18:30"] and eps[0]["emotions"] == ["забота"]
    assert len(sent) == 2 and sent[0]["summary"]                                   # episode.summary для ярких эпизодов
    hits = await r.search.search("экзамен по физике", collections=["episodes"], rewrite=False)
    assert {h.key.split("#")[0] for h in hits} >= {eps[0]["id"]}
    assert rep["digests"] == 2 and rep["diary"] >= 1 and r.store.diary()            # сводки и дневник
    assert r.store.get_meta("moments_upto") == 6 and r.store.get_meta("last_sleep") == r.clock.t


async def test_sleep_is_idempotent(tmp_path):
    j = Journal()
    two_days(j)
    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 86400 + 4 * 3600))
    await r.runner.run(dom.SleepPlan("idle", None))
    calls, queue, docs = len(r.llm.calls), list(r.writer.queue), r.search.index.count()
    rep = await r.runner.run(dom.SleepPlan("idle", None))
    assert rep["cut"] == rep["summarized"] == rep["embedded"] == rep["digests"] == rep["diary"] == 0
    assert len(r.llm.calls) == calls and r.writer.queue == queue and r.search.index.count() == docs


async def test_sleep_interrupts_between_episodes_and_resumes(tmp_path):
    j = Journal()
    for k in range(3):
        chat_day(j, T0 + k * 3 * 3600)
    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 86400))
    stop = {"on": False}

    def should_stop():
        return stop["on"]

    orig = r.summarizer.summarize

    async def summarize_then_wake(ep, events):
        res = await orig(ep, events)
        stop["on"] = True                 # пользователь вернулся посреди сна
        return res

    r.summarizer.summarize = summarize_then_wake
    rep = await r.runner.run(dom.SleepPlan("idle", None), should_stop)
    assert rep["interrupted"] and rep["summarized"] == 1                          # единица работы - один эпизод
    assert r.store.get_meta("last_sleep") is None                                  # прерванный сон не считается законченным
    counts = r.store.counts()
    assert counts["embedded"] == 1 and counts["pending"] == 2
    r.summarizer.summarize = orig
    rep = await r.runner.run(dom.SleepPlan("idle", None))                          # продолжение с того же места
    assert not rep["interrupted"] and rep["cut"] == 0 and rep["summarized"] == 2
    assert r.store.counts()["embedded"] == 3 and len(r.store.select(limit=100)) == 3


async def test_sleep_budget_limits_work(tmp_path):
    j = Journal()
    for k in range(3):
        chat_day(j, T0 + k * 3 * 3600)
    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 86400))
    rep = await r.runner.run(dom.SleepPlan("catchup", 1))
    assert rep["interrupted"] and rep["summarized"] == 1                           # бюджет кончился после первого эпизода


async def test_failed_episode_stays_without_summary_and_raw_search_works(tmp_path):
    j = Journal()
    chat_day(j, T0)
    r = make_runner(tmp_path, j, LLM(bad_summary), Clock(T0 + 5000))
    rep = await r.runner.run(dom.SleepPlan("idle", None))
    ep = r.store.select()[0]
    assert rep["failed"] == 1 and ep["status"] == "failed" and ep["summary"] == "" and ep["error"]
    assert r.store.events_of(ep["id"])                                             # источники на месте
    rep = await r.runner.run(dom.SleepPlan("idle", None))
    assert rep["summarized"] == 0                                                  # failed повторно не мучаем


async def test_model_down_keeps_episodes_pending(tmp_path):
    j = Journal()
    chat_day(j, T0)
    r = make_runner(tmp_path, j, LLM(lambda s, u, n: RuntimeError("нет связи")), Clock(T0 + 5000))
    rep = await r.runner.run(dom.SleepPlan("idle", None))
    assert rep["llm_down"] and r.store.select()[0]["status"] == "pending" and r.store.select()[0]["attempts"] == 0


async def test_embedder_down_keeps_summarized_status_then_recovers(tmp_path):
    class Flaky(HashEmbedder):
        up = False

        async def embed(self, texts, query=False):
            if not self.up:
                raise RuntimeError("down")
            return await super().embed(texts, query)

    j = Journal()
    chat_day(j, T0)
    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 5000))
    r.search.embedder = emb = Flaky()
    await r.runner.run(dom.SleepPlan("idle", None))
    assert r.store.select()[0]["status"] == "summarized"
    emb.up = True
    rep = await r.runner.run(dom.SleepPlan("idle", None))
    assert rep["embedded"] == 1 and r.store.select()[0]["status"] == "embedded"


async def test_compress_old_days_and_delete_raw_only_for_embedded(tmp_path):
    j = Journal()
    chat_day(j, T0)
    old = Clock(T0 + 50 * 86400)
    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 5000), cfg={"compress_after_days": 30, "raw_keep_days": 10})
    await r.runner.run(dom.SleepPlan("idle", None))
    assert j.deleted == []                                                         # свежее не трогаем
    r.clock.t = old.t
    rep = await r.runner.run(dom.SleepPlan("idle", None))
    ep = r.store.select()[0]
    assert rep["compressed"] == 1 and ep["compressed"] == 1 and ep["quotes"] == [] and ep["raw_deleted"] == 1
    assert j.deleted == [1, 2, 3] and r.store.events_of(ep["id"])                  # сырое удалено, копия источников осталась
    day = datetime.fromtimestamp(T0).date().isoformat()
    assert r.store.digest(day)["compressed"] == 1
    hits = await r.search.search("экзамен по физике завтра", collections=["episodes"], rewrite=False)
    assert hits and all("#q" not in h.key for h in hits)                           # цитаты сжатого эпизода из индекса убраны


async def test_raw_is_kept_when_episode_has_no_embedding(tmp_path):
    j = Journal()
    chat_day(j, T0)
    r = make_runner(tmp_path, j, LLM(bad_summary), Clock(T0 + 50 * 86400), cfg={"raw_keep_days": 10})
    await r.runner.run(dom.SleepPlan("idle", None))
    assert j.deleted == []


async def test_diary_is_first_person_by_model_and_checked(tmp_path):
    j = Journal()
    chat_day(j, T0)
    day_calls = []

    def answer(system, user, n):
        if "Эпизод с" in user:
            return good_summary(system, user, n)
        day_calls.append(user)
        return json.dumps({"text": "Я говорила с Борисом."}, ensure_ascii=False) if len(day_calls) <= 2 else \
            json.dumps({"text": "Я помогала Ане с экзаменом."}, ensure_ascii=False)

    r = make_runner(tmp_path, j, LLM(answer), Clock(T0 + 86400 + 4 * 3600))
    await r.runner.run(dom.SleepPlan("idle", None))
    diary = r.store.diary()
    assert not diary or "Борис" not in diary[0]["text"]                            # выдуманное имя не прошло проверку


# ── чтение ───────────────────────────────────────────────────────────────────────────────────

async def test_recall_finds_and_expands_episode(tmp_path):
    j = Journal()
    two_days(j)
    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 86400 + 4 * 3600))
    await r.runner.run(dom.SleepPlan("idle", None))
    rec = EpisodeRecall(store=r.store, search=r.search, source=j)
    found = await rec.find("помнишь экзамен по физике у Ани")
    assert found and found[0]["kind"] in ("episode", "diary", "digest")
    eid = next(f["id"] for f in found if f["kind"] == "episode")
    ep = await rec.expand(eid)
    assert ep["events"] and ep["summary"] and ep["version"] == 1
    text = await rec.recall_episode(eid)
    assert "События-источники" in text and "экзамен по физике" in text
    assert await rec.recall_episode("нет-такого") == ""
    assert (await rec.listing())[0]["id"]
    tail = await rec.recent_tail(5)                                                # хвост после последней нарезки
    assert isinstance(tail, list)


async def test_memory_reader_recall_keeps_default_behaviour_without_episodes():
    async def search(q, limit=6):
        return [{"fact": "факт про чай", "uuid": "u1"}]

    ad = UserMemoryAdapter(lambda: None, lambda: SimpleNamespace(search=search))
    assert isinstance(ad, contracts.MemoryReader)
    assert await ad.recall("чай") == [{"text": "факт про чай", "kind": "fact", "id": "u1", "importance": 4}]


async def test_memory_reader_recall_adds_episodes_when_connected():
    class Rec:
        async def find(self, q, k=5):
            return [{"id": "m1-3", "kind": "episode", "text": "поход", "when": "x", "score": 1}]

    ad = UserMemoryAdapter(lambda: None, lambda: None, lambda: SimpleNamespace(recall=Rec()))
    assert await ad.recall("поход") == [{"text": "поход", "kind": "episode", "id": "m1-3", "importance": 3}]


async def test_recall_tool_uses_episodes(monkeypatch):
    from environments import builtin
    from environments.base import ToolError

    class Rec:
        async def find(self, q, k=5):
            return [{"id": "m1-3", "kind": "episode", "text": "поход на озеро", "when": "2026-10-15 10:00", "score": 1}]

        async def recall_episode(self, eid):
            return "Эпизод m1-3" if eid == "m1-3" else ""

    class Registry:
        def __init__(self):
            self.envs = {}

        def add(self, env):
            self.envs[env.id] = env

    comp = SimpleNamespace(episodes=SimpleNamespace(recall=Rec()), graph=None)
    mem = SimpleNamespace(facts=lambda: [])
    reg = Registry()
    builtin.register(reg, comp, mem)
    tools = {t.name: t for t in reg.envs["assistant"].tools}
    assert "эпизод m1-3" in await tools["recall"].call({"query": "озеро"})
    assert await tools["recall_episode"].call({"id": "m1-3"}) == "Эпизод m1-3"
    with pytest.raises(ToolError):
        await tools["recall_episode"].call({"id": "x"})


# ── планировщик сна ──────────────────────────────────────────────────────────────────────────

def make_scheduler(r, **kw):
    kw.setdefault("grace_s", 0.05)
    kw.setdefault("watch_s", 0.01)
    return SleepScheduler(r.runner, store=r.store, settings=lambda: r.settings, clock=r.clock, **kw)


async def test_scheduler_sleeps_at_idle_and_reports(tmp_path):
    j = Journal()
    chat_day(j, T0)
    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 86400), cfg={"idle_min": 15})
    sch = make_scheduler(r, idle_seconds=lambda: 20 * MIN)
    assert sch.state == "awake"
    assert await sch.tick()
    assert sch.state == "sleeping"
    await sch._task
    assert sch.state == "awake" and sch.last_report["summarized"] == 1 and sch.status()["last_sleep"]
    assert not await sch.tick()                                                    # слишком рано для следующего


async def test_scheduler_does_not_sleep_when_busy_or_disabled(tmp_path):
    j = Journal()
    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 86400))
    assert not await make_scheduler(r, idle_seconds=lambda: 3600, busy=lambda: True).tick()
    r.settings["episodes"]["enabled"] = False
    assert not await make_scheduler(r, idle_seconds=lambda: 3600).tick()


async def test_user_return_interrupts_sleep_within_seconds(tmp_path):
    j = Journal()
    chat_day(j, T0)

    async def slow(system, user, n):
        await asyncio.sleep(30)               # LM Studio генерирует долго и отменить её нельзя
        return good_summary(system, user, n)

    r = make_runner(tmp_path, j, LLM(slow), Clock(T0 + 86400))
    idle = {"v": 20 * MIN}
    sch = make_scheduler(r, idle_seconds=lambda: idle["v"])
    await sch.tick()
    await asyncio.sleep(0.05)
    t = time.monotonic()
    idle["v"] = 0.5                           # нажата клавиша
    await asyncio.wait_for(sch._task, 5)
    assert time.monotonic() - t < 2.0 and sch.state == "awake"
    assert sch.last_report["interrupted"] and sch.interrupted_after_s is not None and sch.interrupted_after_s < 2
    assert r.store.select()[0]["status"] == "pending"                              # эпизод остался нетронутым


async def test_user_message_and_conversation_interrupt_sleep(tmp_path):
    j = Journal()
    chat_day(j, T0)

    async def slow(system, user, n):
        await asyncio.sleep(30)

    r = make_runner(tmp_path, j, LLM(slow), Clock(T0 + 86400))
    last = {"v": 0.0}
    sch = make_scheduler(r, idle_seconds=lambda: 20 * MIN, last_user_ts=lambda: last["v"])
    await sch.tick()
    await asyncio.sleep(0.05)
    last["v"] = r.clock() + 1                 # пришла реплика пользователя
    await asyncio.wait_for(sch._task, 5)
    assert sch.last_report["interrupted"]


async def test_forced_sleep_ignores_input_and_wakes_only_on_command(tmp_path):
    """Сон по кнопке: ввод, реплики и разговор его не будят; будит interrupt (кнопка «Проснуться», смена состояния)."""
    j = Journal()
    chat_day(j, T0)

    async def slow(system, user, n):
        await asyncio.sleep(30)

    r = make_runner(tmp_path, j, LLM(slow), Clock(T0 + 86400))
    last, changes = {"v": 0.0}, []
    sch = make_scheduler(r, idle_seconds=lambda: 0.5, last_user_ts=lambda: last["v"], busy=lambda: True)
    sch.on_change = lambda: changes.append(sch.state)
    assert sch.force() and sch.state == "sleeping" and sch.plan.mode == "forced" and changes == ["sleeping"]
    last["v"] = r.clock() + 1                           # клавиши, реплика и разговор подряд
    await asyncio.sleep(0.15)
    assert sch.state == "sleeping" and not sch.should_stop() and sch.status()["mode"] == "forced" and sch.status()["since"]
    sch.interrupt("команда")
    await asyncio.wait_for(sch._task, 5)
    assert sch.state == "awake" and sch.last_report["interrupted"] and changes == ["sleeping", "awake"]


async def test_force_turns_a_running_idle_sleep_into_a_forced_one(tmp_path):
    j = Journal()
    chat_day(j, T0)

    async def slow(system, user, n):
        await asyncio.sleep(30)

    r = make_runner(tmp_path, j, LLM(slow), Clock(T0 + 86400))
    idle = {"v": 20 * MIN}
    sch = make_scheduler(r, idle_seconds=lambda: idle["v"])
    await sch.tick()
    assert sch.plan.mode == "idle" and sch.force() and sch.plan.mode == "forced"
    idle["v"] = 0.1                                     # пользователь вернулся: принудительный сон не прерывается
    await asyncio.sleep(0.1)
    assert sch.state == "sleeping"
    sch.interrupt()
    assert not sch.force()                              # уже прерывают
    await asyncio.wait_for(sch._task, 5)


async def test_scheduler_loop_and_stop(tmp_path):
    j = Journal()
    r = make_runner(tmp_path, j, LLM(text_or_summary), Clock(T0 + 86400))
    sch = make_scheduler(r, idle_seconds=lambda: 3600, poll_s=0.01)
    await sch.start_loop()
    await asyncio.sleep(0.1)
    await sch.stop()
    assert sch.store.get_meta("last_sleep") is not None


# ── episode.summary во внимание, сервис, персонажи ───────────────────────────────────────────

async def test_episode_summary_event_goes_to_think_route(tmp_path):
    from test_impulses import Rig, Spy
    rig = Rig(tmp_path)
    spy = Spy()
    rig.att.attach_reflection(spy)
    ep = {"id": "m1-3", "title": "Экзамен Ани", "summary": "Я записала экзамен.", "salience": 0.8, "day": "2026-10-16", "stream": "chat"}
    await rig.att.submit("memory", summary_event(ep))
    await rig.att.settle()
    assert [e.kind for e in spy.events] == ["episode.summary"] and spy.events[0].origin == "external"
    assert spy.events[0].data["episode"] == "m1-3" and rig.att.last_decision.get("route") == "think"


def fake_companion(folder, llm):
    comp = SimpleNamespace(_dir=folder, _llm=llm, memory_writer=Writer(), idle_seconds=lambda: 0.0, _last_user_ts=0.0,
                           _settings=SimpleNamespace(load=lambda: {"episodes": {}}))
    return comp


async def test_service_keeps_episodes_per_character(tmp_path):
    folder_a, folder_b = tmp_path / "a", tmp_path / "b"
    comp = fake_companion(folder_a, LLM(text_or_summary))
    submitted = []

    class Att:
        async def submit(self, module, event):
            submitted.append((module, event["kind"]))

    j = Journal()
    chat_day(j, T0)
    svc = EpisodeService(comp, Att(), clock=Clock(T0 + 5000), embedder=HashEmbedder(), source=j)
    p1 = svc.parts()
    p1.store.set_meta("moments_upto", 0)
    await p1.selector.run()
    assert svc.store.select()
    comp._dir = folder_b                                   # переключение персонажа: своя папка и свои эпизоды
    p2 = svc.parts()
    assert p2 is not p1 and svc.store.select() == []
    comp._dir = folder_a
    assert svc.parts() is p1 and svc.store.select()
    svc.attach_scheduler()
    snap = await svc.snapshot()
    assert snap["episodes"] and snap["sleep"]["state"] == "awake"
    await p1.runner.run(dom.SleepPlan("idle", None))
    assert ("memory", "episode.summary") in submitted


async def test_non_default_character_does_not_process_foreign_history(tmp_path, monkeypatch):
    j = Journal()
    chat_day(j, T0)
    comp = fake_companion(tmp_path / "x", LLM(text_or_summary))
    other = SimpleNamespace(id="other", name="Другая", aliases=())
    monkeypatch.setattr(character, "active", lambda: other)
    svc = EpisodeService(comp, SimpleNamespace(), clock=Clock(T0 + 5000), embedder=HashEmbedder(), source=j)
    assert (await svc.parts().selector.run())["episodes"] == 0


# ── журнал: номера, чтение после отметки, удаление ───────────────────────────────────────────

def test_events_log_after_and_delete_ids():
    events_log.log("user", "раз")
    events_log.log("trace", "служебное")
    events_log.log("say", "два", emotion="x")
    src = events_log
    rows_ = src.after(0, 10, {"user", "say"})
    assert [r["text"] for r in rows_] == ["раз", "два"] and rows_[0]["id"] < rows_[1]["id"] and rows_[1]["data"]["emotion"] == "x"
    assert src.after(rows_[0]["id"], 10, {"user", "say"})[0]["text"] == "два"
    assert src.last_id() >= rows_[1]["id"]
    assert src.delete_ids([rows_[0]["id"]]) == 1 and [r["text"] for r in src.after(0, 10, {"user"})] == []
    assert isinstance(src, contracts.EventSource)
    assert "boundary" in events_log.KINDS


def test_prompts_have_episode_sections():
    from services.character import prompts
    for fn in (prompts.episode, prompts.diary, prompts.digest):
        p = fn()
        assert p["system"] and p["format"] and "{name}" not in p["system"]
    assert "от первого лица" in prompts.episode()["system"]
