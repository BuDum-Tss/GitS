"""Помощник на компьютере: общая политика разрешений для Claude и pi, ветка pi (подделка PiAgent и настоящий worker.mjs
с поддельным поставщиком). Без сети, без Claude."""
import shutil

import pytest

import assistant_agent as aa
from gits_sdk import pi_agent
from gits_sdk.pi_agent import default_runtime_dir

ROOT = aa._PROJECT_ROOT
RUNTIME = default_runtime_dir()
needs_node = pytest.mark.skipif(not shutil.which("node") or not (RUNTIME / "node_modules").is_dir(),
                                reason="нет node или зависимостей worker.mjs")


class Asker:
    def __init__(self, answer=False):
        self.answer, self.calls = answer, []

    async def __call__(self, question, danger):
        self.calls.append((question, danger))
        return self.answer


@pytest.fixture(autouse=True)
def _reset():
    yield
    aa.configure(None, None)


# ── общая политика (имена в виде Claude, из обеих веток) ───────────

@pytest.mark.parametrize("tool,inp", [
    ("Read", {"file_path": str(ROOT / "modules" / "desktop" / "manifest.py")}),
    ("Grep", {"pattern": "x", "path": str(ROOT / "agent")}),
    ("Glob", {"pattern": "*.py", "path": str(ROOT / "agent")}),
])
async def test_read_inside_roots_without_question(tool, inp):
    ask = Asker()
    assert await aa.decide(tool, inp, ask) == (True, "")
    assert ask.calls == []


@pytest.mark.parametrize("tool,inp", [
    ("Read", {"file_path": str(ROOT.parent / "outside.txt")}),
    ("Glob", {"pattern": "../**/*.py", "path": str(ROOT)}),
    ("Glob", {"pattern": str(ROOT.parent / "*"), "path": str(ROOT)}),
    ("Grep", {"pattern": "x", "path": str(ROOT), "glob": "../*"}),
    ("Bash", {"command": "ls"}),
    ("Write", {"file_path": str(ROOT / "a.txt"), "content": "x"}),
    ("Edit", {"file_path": str(ROOT / "a.txt"), "old_string": "a", "new_string": "b"}),
    ("WebSearch", {"query": "погода"}),
    ("LS", {"path": str(ROOT)}),
])
async def test_everything_else_asks(tool, inp):
    ask = Asker(False)
    allow, why = await aa.decide(tool, inp, ask)
    assert allow is False and why
    assert len(ask.calls) == 1
    ask2 = Asker(True)
    assert (await aa.decide(tool, inp, ask2))[0] is True


async def test_secret_read_and_danger_always_ask():
    ask = Asker(False)
    assert (await aa.decide("Read", {"file_path": str(ROOT / ".env")}, ask))[0] is False
    assert ask.calls and ask.calls[0][1]               # опасность названа
    ask = Asker(False)
    await aa.decide("Bash", {"command": "rm -rf /"}, ask)
    assert ask.calls and ask.calls[0][1]


# ── ветка pi с подделкой PiAgent ───────────────────────────────────

class FakeAgent:
    instances: list = []
    script = None

    def __init__(self, **kw):
        self.kw = kw
        self.closed = False
        self.decisions = []
        FakeAgent.instances.append(self)

    async def run(self, prompt):
        self.prompt = prompt
        async for ev in type(self).script(self):
            yield ev

    async def close(self):
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    FakeAgent.instances = []
    monkeypatch.setattr(pi_agent, "PiAgent", FakeAgent)
    aa.configure({"agent_runtime": "pi", "pi_model_heavy": "тяжёлая", "pi_thinking": "low"}, None)
    return FakeAgent


async def _usage_log():
    log = []

    async def usage(tokens, text):
        log.append((tokens, text))
    return log, usage


async def test_pi_branch_ok_usage_and_on_tool(fake):
    async def script(self):
        yield pi_agent.ToolUse("read", {"path": "x"}, "Read", {"file_path": "a.py"})
        yield pi_agent.TextEvent("готово")
        yield pi_agent.EndEvent("ok", None, {"requests": 2, "input": 100, "output": 20}, 2)
    fake.script = script
    log, usage = await _usage_log()
    tools = []

    async def on_tool(name, summary):
        tools.append((name, summary))

    out = await aa.run_task("сделай", "контекст", Asker(), usage, on_tool)
    ag = fake.instances[0]
    assert out == "готово" and log == [(120, "готово")] and tools == [("Read", "a.py")]
    assert ag.kw["model"] == "тяжёлая" and ag.kw["thinking"] == "low" and ag.kw["max_turns"] == aa.PI_MAX_TURNS
    assert ag.kw["tools"] == ["read", "find", "grep", "bash", "write", "edit"]
    assert [t.name for t in ag.kw["custom_tools"]] == ["web_search"]
    assert "контекст" in ag.prompt and "сделай" in ag.prompt and ag.closed


async def test_pi_branch_policy_and_user_refusal(fake, tmp_path):
    results = []

    async def script(self):
        cb = self.kw["can_use_tool"]
        results.append(await cb("Read", {"file_path": str(ROOT / "modules" / "desktop" / "manifest.py")}))
        results.append(await cb("Read", {"file_path": str(tmp_path / "x.txt")}))
        results.append(await cb("Bash", {"command": "echo 1"}))
        yield pi_agent.EndEvent("ok", None, {}, 1)
    fake.script = script
    ask = Asker(False)
    log, usage = await _usage_log()
    await aa.run_task("t", "", ask, usage)
    assert [r[0] for r in results] == [True, False, False]
    assert len(ask.calls) == 2                       # чтение внутри проекта без вопроса, остальное спросили
    assert "команду" in ask.calls[1][0]


@pytest.mark.parametrize("status,needle", [("max_turns", "предел"), ("aborted", "прервана")])
async def test_pi_branch_limits(fake, status, needle):
    async def script(self):
        yield pi_agent.TextEvent("частично")
        yield pi_agent.EndEvent(status, None, {"input": 5, "output": 1}, 40)
    fake.script = script
    log, usage = await _usage_log()
    out = await aa.run_task("t", "", Asker(), usage)
    assert out.startswith("частично") and needle in out and log[0][0] == 6
    assert "Claude" not in out


async def test_pi_branch_error_returns_none(fake):
    async def script(self):
        yield pi_agent.EndEvent("error", "нет входа", {}, 0)
    fake.script = script
    log, usage = await _usage_log()
    assert await aa.run_task("t", "", Asker(), usage) is None
    assert log == [(0, None)] and fake.instances[0].closed


async def test_pi_branch_client_exception_returns_none(fake):
    async def script(self):
        raise pi_agent.PiAgentError("нет node")
        yield
    fake.script = script
    log, usage = await _usage_log()
    assert await aa.run_task("t", "", Asker(), usage) is None and log == [(0, None)]


async def test_web_search_goes_to_core_after_consent(fake):
    queries = []

    async def core_search(q):
        queries.append(q)
        return "результат"

    async def script(self):
        tool = self.kw["custom_tools"][0]
        self.out = []
        try:
            self.out.append(await tool.handler({"query": "погода"}))
        except RuntimeError as e:
            self.out.append("отказ:" + str(e))
        yield pi_agent.EndEvent("ok", None, {}, 1)
    fake.script = script
    aa.configure({"agent_runtime": "pi"}, core_search)
    log, usage = await _usage_log()
    await aa.run_task("t", "", Asker(False), usage)
    assert queries == [] and fake.instances[0].out[0].startswith("отказ:")      # без «да» ядро не зовут
    fake.instances.clear()
    await aa.run_task("t", "", Asker(True), usage)
    assert queries == ["погода"] and fake.instances[0].out == ["результат"]


async def test_claude_branch_untouched_when_setting_is_claude(monkeypatch):
    called = []

    async def fake_pi(*a, **k):
        called.append("pi")

    async def fake_claude(prompt, ask, usage, on_tool):
        called.append("claude")
        return "ответ"
    monkeypatch.setattr(aa, "_run_pi", fake_pi)
    monkeypatch.setattr(aa, "_run_claude", fake_claude)
    for cfg in (None, {}, {"agent_runtime": "claude"}):
        aa.configure(cfg, None)
        assert await aa.run_task("t", "", Asker(), None) == "ответ"
    assert called == ["claude"] * 3


# ── настоящий worker.mjs с поддельным поставщиком ──────────────────

@needs_node
async def test_real_worker_policy_end_to_end(monkeypatch, tmp_path):
    mark = tmp_path / "mark.txt"
    cmd = f'node -e "require(\'fs\').writeFileSync(process.argv[1],\'x\')" "{mark.as_posix()}"'
    responses = [
        {"tool_calls": [{"name": "read", "args": {"path": "modules/desktop/manifest.py"}}]},
        {"tool_calls": [{"name": "read", "args": {"path": str(tmp_path / "out.txt")}}]},
        {"tool_calls": [{"name": "bash", "args": {"command": cmd}}]},
        {"tool_calls": [{"name": "web_search", "args": {"query": "погода"}}]},
        {"text": "остановилась"},
    ]
    real = pi_agent.PiAgent

    def factory(**kw):
        return real(provider="faux", faux_responses=responses, runtime_dir=RUNTIME, **kw)
    monkeypatch.setattr(pi_agent, "PiAgent", factory)
    searched = []

    async def core_search(q):
        searched.append(q)
        return "r"
    aa.configure({"agent_runtime": "pi"}, core_search)
    ask = Asker(False)
    log, usage = await _usage_log()
    out = await aa.run_task("задача", "", ask, usage)
    assert out == "остановилась"
    assert not mark.exists() and searched == []
    assert len(ask.calls) == 3                       # чтение вне проекта, команда, поиск; чтение в проекте без вопроса
    assert log and log[0][0] >= 0
