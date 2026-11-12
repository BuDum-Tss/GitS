"""Глубокий анализ: общая проверка чтения и путь через рабочего агента pi (с подделкой клиента, без сети и Claude)."""
import json
from pathlib import Path

import pytest

import analysis
import settings
from gits_sdk import pi_agent

ROOT = analysis._PROJECT_ROOT


@pytest.mark.parametrize("tool,inp,denied", [
    ("Read", {"file_path": str(ROOT / "agent" / "analysis.py")}, False),
    ("Read", {"file_path": str(ROOT.parent / "outside.txt")}, True),
    ("Read", {"file_path": str(ROOT / ".env")}, True),
    ("Grep", {"pattern": "x", "path": str(ROOT / "agent")}, False),
    ("Glob", {"pattern": "../**/*.py", "path": str(ROOT)}, True),
    ("Grep", {"pattern": "x", "path": str(ROOT), "glob": "../*"}, True),
    ("Bash", {"command": "ls"}, True),
    ("Write", {"file_path": str(ROOT / "a.txt")}, True),
    ("LS", {"path": str(ROOT)}, True),                 # для Claude LS не в списке
])
def test_check_read_claude(tool, inp, denied):
    assert (analysis.check_read(tool, inp) is not None) is denied


def test_check_read_ls_allowed_for_pi():
    assert analysis.check_read("LS", {"path": str(ROOT)}, analysis._READ_PI) is None
    assert analysis.check_read("LS", {"path": str(ROOT.parent)}, analysis._READ_PI) is not None


class FakeAgent:
    instances: list = []
    results: list = []
    answer = ""

    def __init__(self, **kw):
        self.kw = kw
        FakeAgent.instances.append(self)
        self.closed = False

    async def run(self, prompt):
        self.prompt = prompt
        cb = self.kw["can_use_tool"]
        FakeAgent.results.append(await cb("Read", {"file_path": str(ROOT / "agent" / "analysis.py")}))
        FakeAgent.results.append(await cb("Read", {"file_path": str(ROOT.parent / "x.txt")}))
        FakeAgent.results.append(await cb("Bash", {"command": "ls"}))
        yield pi_agent.TextEvent(FakeAgent.answer)
        yield pi_agent.EndEvent("ok", None, {"requests": 1}, 1)

    async def close(self):
        self.closed = True


@pytest.fixture
def pi_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "load", lambda: {"agent_runtime": "pi", "pi_model": "m", "pi_thinking": "low"})
    monkeypatch.setattr(analysis.events_log, "tail", lambda **kw: [])
    monkeypatch.setattr(analysis.events_log, "export_jsonl", lambda path, since=None: Path(path).write_text("", encoding="utf-8"))
    monkeypatch.setattr(pi_agent, "PiAgent", FakeAgent)
    FakeAgent.instances, FakeAgent.results = [], []
    FakeAgent.answer = json.dumps({"summary": "итог", "report": "разбор", "conclusions": ["а"], "fixes": ["б"]}, ensure_ascii=False)
    return tmp_path


async def test_analyze_on_pi_uses_read_only_agent(pi_env):
    res = await analysis.analyze("почему?", hours=1)
    assert res["summary"] == "итог" and Path(res["report_path"]).is_file()
    agent = FakeAgent.instances[0]
    assert agent.kw["tools"] == ["read", "grep", "find", "ls"]
    assert agent.kw["model"] == "m" and agent.kw["thinking"] == "low" and agent.kw["max_turns"] == analysis.PI_MAX_TURNS
    assert agent.closed
    allow_inside, deny_outside, deny_bash = FakeAgent.results
    assert allow_inside[0] is True
    assert deny_outside[0] is False and "внутри проекта" in deny_outside[1]
    assert deny_bash[0] is False


async def test_analyze_on_pi_bad_format_and_errors(pi_env, monkeypatch):
    FakeAgent.answer = "не json"
    with pytest.raises(RuntimeError, match="не по формату"):
        await analysis.analyze("q", hours=1)

    class Dead(FakeAgent):
        async def run(self, prompt):
            yield pi_agent.EndEvent("error", "нет входа: тест", {}, 0)

    monkeypatch.setattr(pi_agent, "PiAgent", Dead)
    with pytest.raises(RuntimeError, match="нет входа"):
        await analysis.analyze("q", hours=1)

    class Limit(FakeAgent):
        async def run(self, prompt):
            yield pi_agent.EndEvent("max_turns", None, {}, 60)

    monkeypatch.setattr(pi_agent, "PiAgent", Limit)
    with pytest.raises(RuntimeError, match="предел"):
        await analysis.analyze("q", hours=1)
