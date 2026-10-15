"""Контракты ТЗ 29 (ARC-3, шаг R2): адаптеры реализуют свои интерфейсы, делегируют старым модулям и сценарии
(`Companion`, `Attention`, `Router`) работают на заглушках контрактов без LM Studio, графа, диска и сети."""
import time
from types import SimpleNamespace

import pytest
from hub_helpers import FakeCompanion, FakeIO
from test_attention import Clock, StubDecider

import character
import contracts
import decide
import events_log
import fast_brain
import settings
from adapters.approver import ApprovalsModule
from adapters.classifier_gliner import GlinerClassifier
from adapters.event_log_sqlite import SqliteEventLog
from adapters.llm_lmstudio import LMStudioLLM
from adapters.memory import UserMemoryAdapter
from adapters.people import RelationshipsAdapter
from adapters.settings_provider import SettingsModule
from attention import Attention
from companion import Companion
from graph_memory import GraphMemory
from router import Router


# ── адаптеры удовлетворяют контрактам ──────────────────────────────────────────────────────

@pytest.mark.parametrize("obj, contract", [
    (LMStudioLLM(), contracts.LanguageModel),
    (GlinerClassifier(), contracts.Classifier),
    (SqliteEventLog(), contracts.EventLog),
    (SettingsModule(), contracts.SettingsProvider),
    (ApprovalsModule(), contracts.Approver),
    (RelationshipsAdapter(lambda: None), contracts.PeopleDirectory),
    (UserMemoryAdapter(lambda: None, lambda: None), contracts.MemoryReader),
    (UserMemoryAdapter(lambda: None, lambda: None), contracts.MemoryWriter),
    (GraphMemory.__new__(GraphMemory), contracts.FactGraph),     # методы класса без запуска backend
    (decide.Decider(), contracts.Classifier),
    (Router(None, None, lambda: ""), contracts.ToolInvoker),
    (time.time, contracts.Clock),
])
def test_adapters_satisfy_their_contracts(obj, contract):
    assert isinstance(obj, contract)


def test_adapters_delegate_to_the_old_modules(monkeypatch):
    """Адаптер не дублирует логику: вызов уходит в тот же модуль, что и раньше."""
    seen = []
    monkeypatch.setattr(events_log, "log", lambda kind, text="", **data: seen.append(("log", kind, text, data)))
    monkeypatch.setattr(settings, "load", lambda: {"vision": True})

    SqliteEventLog().log("say", "привет", emotion="happy")
    assert seen == [("log", "say", "привет", {"emotion": "happy"})]
    assert SettingsModule().load() == {"vision": True}


# ── сценарии на заглушках контрактов ───────────────────────────────────────────────────────

class FakeLLM:
    """LanguageModel: заранее заданный ответ, все вызовы записываются."""

    def __init__(self, reply=""):
        self.reply, self.calls = reply, []

    async def ask(self, system, user, *, timeout, max_tokens=40, temperature=0.3, background=False, image_b64=None):
        self.calls.append(user)
        return self.reply


class FakeLog:
    """EventLog: журнал в списке."""

    def __init__(self):
        self.entries = []

    def log(self, kind, text="", **data):
        self.entries.append((kind, text))

    def log_nowait(self, kind, text="", **data):
        self.entries.append((kind, text))


class FakeMemory:
    """MemoryReader и MemoryWriter в памяти процесса: без файлов и графа."""

    def __init__(self):
        self.dialogue, self.fact_items, self.graph_items, self.meta_ = [], [], [], {}

    def recent_dialogue(self, n=8):
        return self.dialogue[-n:]

    def recent_activity(self, n=5):
        return []

    def facts(self):
        return [{"text": f["text"], "ts": 0.0} for f in self.fact_items]

    def context_text(self, query=""):
        return ""

    def context_block(self, dialogue=6, query=""):
        return ""

    async def graph_block(self, query):
        return ""

    async def self_block(self, query):
        return ""

    def graph_status(self):
        return None

    def relevant(self, query="", k=8):
        return []

    def meta(self, key, default=None):
        return self.meta_.get(key, default)

    def set_meta(self, key, value):
        self.meta_[key] = value

    def add_dialogue(self, role, text):
        self.dialogue.append({"role": role, "text": text, "ts": time.time()})

    def add_fact(self, text, kind="user", importance=3, due_days=None):
        self.fact_items.append({"text": text, "kind": kind, "importance": importance})
        return True

    def add_activity(self, app, category, summary):
        pass

    def enqueue_for_graph(self, speaker, text):
        self.graph_items.append((speaker, text))

    def forget_matching(self, text):
        return 0

    def rebind(self, memory_dir):
        pass


async def test_companion_converses_on_fake_contracts_and_leaves_no_files(tmp_path, monkeypatch):
    """Реплика идёт через заглушку модели, журнал и память в списках; на диск ничего не пишется."""
    async def no_reaction(text, rel=None):
        return None
    monkeypatch.setattr(fast_brain, "react_chat", no_reaction)       # эмоция после ответа: модели нет
    monkeypatch.setattr(fast_brain, "publish", lambda *a, **k: None)

    llm, log, mem = FakeLLM("Привет, я здесь."), FakeLog(), FakeMemory()
    mdir = tmp_path / "companion-memory"          # папка памяти компаньона (conftest заводит свою tmp_path/memory)
    companion = Companion(str(mdir), rel=None, llm=llm, memory_reader=mem, memory_writer=mem,
                          event_log=log, settings_provider=SimpleNamespace(
                              load=lambda: dict(settings.DEFAULTS), update=lambda **f: {},
                              quiet_active=lambda cfg=None: False),
                          approver=SimpleNamespace(has_pending=lambda: False,
                                                   try_answer=lambda text: False))
    await companion.on_user_message("Привет")

    assert len(llm.calls) == 1                                       # одна реплика модели, ответ ушёл в вывод речи
    assert companion.presence.snapshot()["last_say"] == "Привет, я здесь."
    assert ("user", "Привет") in log.entries and ("say", "Привет, я здесь.") in log.entries
    assert [d["role"] for d in mem.dialogue][-2:] == ["user", character.active().id]
    assert not mdir.exists()                                         # память взята контрактом: диска не трогаем


async def test_attention_registers_sender_and_memorises_on_fake_contracts(tmp_path):
    """Событие от человека: профиль заведён, реплика записана, памятный факт и очередь графа идут через заглушки."""
    class FakePeople:
        def __init__(self):
            self.calls = []

        def ensure_person(self, key, name, source="", **extra):
            self.calls.append(("ensure", key, name))
            return True

        def note(self, key, text):
            self.calls.append(("note", key, text))

        def get(self, subject):
            return None

    people, mem, log = FakePeople(), FakeMemory(), FakeLog()
    cfg = dict(settings.DEFAULTS)
    att = Attention(FakeCompanion(), FakeIO(), clock=Clock(), decider=StubDecider(None),
                    settings_source=lambda: cfg, rules_file=tmp_path / "rules.json", idle_source=lambda: 0.0,
                    autopump=False, people=people, memory_writer=mem, event_log=log,
                    approver=SimpleNamespace(has_pending=lambda: False, formulating=lambda: False))
    text = "Маша переезжает в Казань"
    await att.submit("tg", {"kind": "tg.dm", "text": text, "urgency_hint": 1, "addressed_to_me": False,
                            "memorable": True, "sender": {"id": "tg:2", "name": "Маша"}})
    await att.settle()

    assert ("ensure", "tg:2", "Маша") in people.calls and ("note", "tg:2", text) in people.calls
    assert mem.fact_items == [{"text": text, "kind": "episode", "importance": 3}]
    assert mem.graph_items == [("Маша", text)]
    assert any(kind == "attention" for kind, _ in log.entries)       # решение внимания записано в журнал


async def test_router_plans_on_fake_llm_and_logs_on_fake_log():
    """Просьба без подходящего инструмента: планировщик (заглушка модели) отвечает «не могу», журнал пишет в список."""
    class FakeTool:
        id = "home.light"
        name = "light"
        risk = "auto"

        def signature(self):
            return "home.light(on: bool) - включить свет"

    class FakeRegistry:
        async def search_async(self, text):
            return [FakeTool()]

        def describe(self):
            return "- home.light: включить свет"

    llm = FakeLLM('{"kind": "action", "feasible": false, "reason": "нет такого инструмента", "steps": []}')
    log = FakeLog()
    router = Router(FakeRegistry(), lambda text: None, lambda: "", llm=llm, event_log=log,
                    settings_provider=SettingsModule(), approver=ApprovalsModule())
    outcome = await router.handle("включи телевизор")

    assert len(llm.calls) == 1 and router.last_kind == "cannot"
    assert outcome and "нет такого инструмента" in outcome
    assert log.entries == [("plan", "не могу: включи телевизор")]
