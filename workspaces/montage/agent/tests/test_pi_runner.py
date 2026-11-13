"""Исполнитель стадии на pi: подделка PiAgent (журнал, предел ходов, продолжение, readonly, песочница, свои инструменты),
выбор исполнителя по настройке и настоящий worker.mjs с поддельным поставщиком (пропуск без node). Без сети и Claude."""
import shutil

import pytest

from agent_tools import JobEnv
from claude_runner import SdkRunner, StageFailed
from gits_sdk import pi_agent
from gits_sdk.pi_agent import EndEvent, PiAgentError, TextEvent, ToolUse
from pi_runner import PiRunner, RuntimeRunner, is_pi_session, use_pi
from tests.rig import Rig

RUNTIME = pi_agent.default_runtime_dir()
needs_node = pytest.mark.skipif(not shutil.which("node") or not (RUNTIME / "node_modules").is_dir(),
                                reason="нет node или зависимостей worker.mjs")
CFG = {"agent_runtime": "pi", "pi_model_heavy": "heavy-model", "pi_thinking": "low"}


class FakeAgent:
    """Сценарий: шаги `("perm", имя, поля)`, `("tool", имя, args)`, `("text", s)`, `("use",)`, `("end", EndEvent)`."""
    made: list = []
    script: list = []
    raise_first_with: Exception | None = None

    def __init__(self, **kw):
        self.kw = kw
        self.verdicts, self.tool_results, self.closed = [], [], False
        FakeAgent.made.append(self)

    async def run(self, prompt):
        self.prompt = prompt
        if FakeAgent.raise_first_with is not None and len(FakeAgent.made) == 1:
            raise FakeAgent.raise_first_with
        for step in FakeAgent.script:
            kind = step[0]
            if kind == "perm":
                self.verdicts.append(await self.kw["can_use_tool"](step[1], step[2]))
            elif kind == "tool":
                tool = {t.name: t for t in (self.kw["custom_tools"] or [])}[step[1]]
                try:
                    self.tool_results.append((True, await tool.handler(step[2])))
                except Exception as e:
                    self.tool_results.append((False, str(e)))
            elif kind == "text":
                yield TextEvent(step[1])
            elif kind == "use":
                yield ToolUse("read", {}, "Read", {})
            elif kind == "end":
                yield step[1]

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset():
    FakeAgent.made, FakeAgent.raise_first_with = [], None
    FakeAgent.script = [("text", "готово"), ("end", EndEvent("ok", None, {"input": 100, "output": 20}, 3))]


def make(tmp_path):
    rig = Rig(tmp_path)
    job = rig.new_job()
    (job.work / "scripts").mkdir(parents=True, exist_ok=True)
    runner = PiRunner(rig.layout, settings=lambda: CFG, agent_factory=FakeAgent)
    env = JobEnv(job=job, store=rig.store, core=rig.core, layout=rig.layout, stage="build")
    return rig, job, env, runner


def logged(rig, job, kind):
    return [r for r in rig.store.read_log(job, 500) if r.get("kind") == kind]


async def test_result_and_agent_done_log(tmp_path):
    FakeAgent.script = [("use",), ("text", "собрала"), ("end", EndEvent("ok", None, {"input": 100, "output": 20}, 3))]
    rig, job, env, runner = make(tmp_path)
    res = await runner.run(env, "build", "собери", model="sonnet", max_turns=12)
    assert (res.text, res.tokens, res.turns, res.hit_turn_limit) == ("собрала", 120, 3, False)
    assert res.session_id and is_pi_session(res.session_id) and res.session_id.startswith(str(job.folder))
    done = logged(rig, job, "agent_done")[0]
    assert done["tokens"] == 120 and done["turns"] == 3 and done["tools"] == 1 and done["model"] == "heavy-model"
    assert done["subtype"] == "success" and done["is_error"] is False and done["usage"]["input_tokens"] == 100
    kw = FakeAgent.made[0].kw
    assert kw["model"] == "heavy-model" and kw["thinking"] == "low" and kw["max_turns"] == 12
    assert kw["tools"] == ["read", "write", "edit", "find", "grep", "bash"] and kw["cwd"] == str(job.work)
    assert (job.folder / "system_prompt.md").is_file() and kw["system_file"].endswith("system_prompt.md")
    assert FakeAgent.made[0].closed
    assert rig.core.usage == []                       # лимит Claude на pi не учитывается


async def test_turn_limit_and_errors(tmp_path):
    FakeAgent.script = [("end", EndEvent("max_turns", None, {"input": 5, "output": 1}, 9))]
    rig, job, env, runner = make(tmp_path)
    res = await runner.run(env, "build", "p", model="x", max_turns=9)
    assert res.hit_turn_limit and logged(rig, job, "agent_done")[0]["subtype"] == "error_max_turns"
    FakeAgent.script = [("end", EndEvent("error", "сбой поставщика", {}, 1))]
    with pytest.raises(StageFailed, match="сбой поставщика"):
        await runner.run(env, "build", "p", model="x", max_turns=9)
    FakeAgent.script = [("end", EndEvent("aborted", None, {}, 1))]
    with pytest.raises(StageFailed, match="прерван"):
        await runner.run(env, "build", "p", model="x", max_turns=9)


async def test_resume_fresh_and_readonly_sessions(tmp_path):
    rig, job, env, runner = make(tmp_path)
    first = await runner.run(env, "story", "p", model="x", max_turns=5)
    job.data["claude"]["session_id"] = first.session_id
    again = await runner.run(env, "plan", "p", model="x", max_turns=5)
    assert again.session_id == first.session_id and FakeAgent.made[-1].kw["session_file"] == first.session_id
    fresh = await runner.run(env, "plan", "p", model="x", max_turns=5, fresh=True)
    assert fresh.session_id != first.session_id
    ro = await runner.run(env, "review", "p", model="x", max_turns=5, readonly=True)
    assert FakeAgent.made[-1].kw["session_file"] is None and ro.session_id is None
    job.data["claude"]["session_id"] = "6f1d2c3e-claude-session-id"        # id сессии Claude не годится для pi
    other = await runner.run(env, "plan", "p", model="x", max_turns=5)
    assert is_pi_session(other.session_id)


async def test_lost_session_retries_with_recap(tmp_path):
    rig, job, env, runner = make(tmp_path)
    first = await runner.run(env, "story", "p", model="x", max_turns=5)
    job.data["claude"]["session_id"] = first.session_id
    FakeAgent.made.clear()
    FakeAgent.raise_first_with = PiAgentError("нет сессии")
    res = await runner.run(env, "plan", "исходный запрос", model="x", max_turns=5)
    assert res.text == "готово" and len(FakeAgent.made) == 2
    assert "Прежняя сессия недоступна" in FakeAgent.made[1].prompt and "исходный запрос" in FakeAgent.made[1].prompt
    assert FakeAgent.made[1].kw["session_file"] != first.session_id
    assert logged(rig, job, "session_lost")
    FakeAgent.made.clear()
    job.data["claude"]["session_id"] = None
    with pytest.raises(StageFailed):
        await runner.run(env, "plan", "p", model="x", max_turns=5)      # без продолжения повтора нет


async def test_sandbox_verdicts_reach_agent_and_log_and_readonly(tmp_path):
    rig, job, env, runner = make(tmp_path)
    FakeAgent.script = [("perm", "Read", {"file_path": str(job.work / "a.txt")}),
                        ("perm", "Write", {"file_path": str(job.work / "x.ts"), "content": "x"}),
                        ("perm", "Bash", {"command": "curl http://evil"}),
                        ("perm", "Edit", {"file_path": str(job.folder.parent / "other" / "f"), "old_string": "a", "new_string": "b",
                                          "edits": [{"oldText": "a", "newText": "b"}]}),
                        ("end", EndEvent("ok", None, {}, 1))]
    await runner.run(env, "build", "p", model="x", max_turns=5)
    assert [x[0] for x in FakeAgent.made[0].verdicts] == [True, True, False, False]
    rows = logged(rig, job, "sandbox")
    assert [(r["tool"], r["allowed"]) for r in rows] == [("Read", True), ("Write", True), ("Bash", False), ("Edit", False)]
    assert all(r["stage"] == "build" for r in rows)
    FakeAgent.script = [("perm", "Write", {"file_path": str(job.work / "x.ts")}), ("perm", "Bash", {"command": "ffprobe a.mp4"}),
                        ("perm", "Read", {"file_path": str(job.work / "a.txt")}), ("end", EndEvent("ok", None, {}, 1))]
    await runner.run(env, "review", "p", model="x", max_turns=5, readonly=True)
    kw = FakeAgent.made[-1].kw
    assert kw["tools"] == ["read", "find"] and not kw["custom_tools"]
    assert [x[0] for x in FakeAgent.made[-1].verdicts] == [False, False, True]


async def test_custom_tools_pass_sandbox_and_run(tmp_path):
    rig, job, env, runner = make(tmp_path)
    job.transcript.mkdir(parents=True, exist_ok=True)
    (job.transcript / "words.json").write_text('[{"w":"привет","start_ms":0,"end_ms":400}]', encoding="utf-8")
    FakeAgent.script = [("tool", "progress", {"progress": 2, "text": "идёт"}),
                        ("tool", "transcript_words", {"from_ms": 0, "to_ms": 1000}),
                        ("tool", "ask_user", {"question": "q?", "kind": "text"}),
                        ("tool", "synthesize_voice", {"id": "bad id", "text": "т"}),
                        ("end", EndEvent("ok", None, {}, 1))]
    seen = []

    async def progress(frac, text):
        seen.append((frac, text))

    async def ask(q, kind, options):
        return "ответ"
    env.progress, env.ask = progress, ask
    await runner.run(env, "build", "p", model="x", max_turns=5)
    assert [t.name for t in FakeAgent.made[0].kw["custom_tools"]] == ["ask_user", "progress", "synthesize_voice", "transcript_words"]
    res = FakeAgent.made[0].tool_results
    assert seen == [(1.0, "идёт")] and res[0] == (True, "ok") and "привет" in res[1][1]
    assert res[2] == (True, "ответ") and res[3][0] is False and "bad_request" in res[3][1]
    assert [r["tool"] for r in logged(rig, job, "sandbox")] == ["mcp__montage__" + n for n in
                                                               ("progress", "transcript_words", "ask_user", "synthesize_voice")]
    assert logged(rig, job, "agent_done")[0]["tools"] == 4        # каждый вызов, как ToolUseBlock у Claude


async def test_ask_user_denied_by_sandbox_outside_clarify_and_build(tmp_path):
    rig, job, env, runner = make(tmp_path)
    FakeAgent.script = [("tool", "ask_user", {"question": "q", "kind": "text"}), ("end", EndEvent("ok", None, {}, 1))]
    await runner.run(env, "plan", "p", model="x", max_turns=5)
    ok, text = FakeAgent.made[0].tool_results[0]
    assert ok is False and "denied" in text
    assert logged(rig, job, "sandbox")[0]["allowed"] is False


def test_runtime_selection():
    class R:
        def __init__(self, n):
            self.n = n

        async def run(self, *a, **k):
            return self.n
    cfg = {}
    rr = RuntimeRunner(R("claude"), R("pi"), lambda: cfg)
    assert rr.pick().n == "claude"
    cfg["agent_runtime"] = "pi"
    assert rr.pick().n == "pi" and use_pi(cfg) and not use_pi({"agent_runtime": "claude"}) and not use_pi(None)


def test_main_build_wires_runtime_runner():
    import main
    client, manager = main.build()
    runner = manager.pipeline.runner
    assert isinstance(runner, RuntimeRunner) and isinstance(runner.claude, SdkRunner) and isinstance(runner.pi, PiRunner)
    assert runner.pick() is runner.claude                          # настройки ядра ещё не пришли: прежняя ветка Claude


async def test_sandbox_check_has_no_claude_sdk_dependency(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)       # импорт упал бы с ImportError
    rig, job, env, runner = make(tmp_path)
    from sandbox import Sandbox
    ok, _ = await Sandbox(job.folder, rig.layout).check("Read", {"file_path": str(job.work / "a")})
    assert ok
    await runner.run(env, "build", "p", model="x", max_turns=5)


@needs_node
async def test_real_worker_with_sandbox(tmp_path):
    rig, job, env, _ = make(tmp_path)
    (job.work / "a.txt").write_text("привет", encoding="utf-8")
    mark = job.work / "mark.txt"
    script = [{"tool_calls": [{"name": "read", "args": {"path": "a.txt"}}]},
              {"tool_calls": [{"name": "bash", "args": {"command": f"node -e 1 > {mark.as_posix()} && curl http://x"}}]},
              {"tool_calls": [{"name": "write", "args": {"path": "../outside.txt", "content": "x"}}]},
              {"tool_calls": [{"name": "write", "args": {"path": "ok.txt", "content": "x"}}]},
              {"tool_calls": [{"name": "progress", "args": {"progress": 0.5, "text": "тик"}}]},
              {"text": "всё"}]
    runner = PiRunner(rig.layout, settings=lambda: CFG,
                      agent_factory=lambda **kw: pi_agent.PiAgent(provider="faux", faux_responses=script, runtime_dir=RUNTIME, **kw))
    seen = []

    async def progress(frac, text):
        seen.append((frac, text))
    env.progress = progress
    res = await runner.run(env, "build", "поехали", model="x", max_turns=20)
    assert res.text == "всё" and not res.hit_turn_limit
    rows = [(r["tool"], r["allowed"]) for r in logged(rig, job, "sandbox")]
    assert rows == [("Read", True), ("Bash", False), ("Write", False), ("Write", True), ("mcp__montage__progress", True)]
    assert not mark.exists() and not (job.work.parent / "outside.txt").exists()
    assert (job.work / "ok.txt").is_file() and seen == [(0.5, "тик")]
