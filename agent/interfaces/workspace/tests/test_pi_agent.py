"""Клиент рабочего агента pi: приведение имён к виду Claude, протокол на поддельном процессе Python и на настоящем
`worker.mjs` с поддельным поставщиком (без сети). Нет node или `npm ci`: тесты с настоящим процессом пропускаются."""
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from gits_sdk.pi_agent import (
    CustomTool, EndEvent, PiAgent, PiAgentError, TextEvent, ToolUse, claude_tool_names, default_runtime_dir,
    pi_tool_names, to_claude,
)

RUNTIME = default_runtime_dir()
needs_node = pytest.mark.skipif(not shutil.which("node") or not (RUNTIME / "node_modules").is_dir(),
                                reason="нет node или зависимостей worker.mjs")


# ── приведение имён ────────────────────────────────────────────────

def test_to_claude_paths_are_absolute_against_cwd(tmp_path):
    r = to_claude("read", {"path": "a/b.txt", "offset": 3}, str(tmp_path))
    assert r["tool"] == "Read"
    assert Path(r["input"]["file_path"]) == (tmp_path / "a" / "b.txt")
    assert r["input"]["offset"] == 3


def test_to_claude_traversal_is_visible(tmp_path):
    r = to_claude("read", {"path": "../../secret.txt"}, str(tmp_path / "x"))
    assert Path(r["input"]["file_path"]) == (tmp_path.parent.parent / "secret.txt") or ".." not in r["input"]["file_path"]


@pytest.mark.parametrize("tool,inp,name,key", [
    ("write", {"path": "o.txt", "content": "c"}, "Write", "file_path"),
    ("bash", {"command": "ls", "timeout": 5}, "Bash", "command"),
    ("grep", {"pattern": "x", "path": "src", "glob": "*.py", "ignoreCase": True}, "Grep", "pattern"),
    ("find", {"pattern": "*.py"}, "Glob", "pattern"),
    ("ls", {}, "LS", "path"),
])
def test_to_claude_table(tmp_path, tool, inp, name, key):
    r = to_claude(tool, inp, str(tmp_path))
    assert r["tool"] == name and key in r["input"]


def test_to_claude_edit_first_replacement(tmp_path):
    r = to_claude("edit", {"path": "f.py", "edits": [{"oldText": "a", "newText": "b"}, {"oldText": "c", "newText": "d"}]}, str(tmp_path))
    assert r["tool"] == "Edit"
    assert r["input"]["old_string"] == "a" and r["input"]["new_string"] == "b" and len(r["input"]["edits"]) == 2


def test_to_claude_unknown_and_name_lists(tmp_path):
    assert to_claude("custom", {"x": 1}, str(tmp_path)) == {"tool": "custom", "input": {"x": 1}}
    assert claude_tool_names(["read", "find"]) == ["Read", "Glob"]
    assert pi_tool_names(["Read", "Glob", "Grep"]) == ["read", "find", "grep"]


# ── поддельный процесс на Python ───────────────────────────────────

FAKE = textwrap.dedent('''
    import json, sys
    def out(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        m = json.loads(line)
        t = m["type"]
        if t == "init":
            if m.get("provider") == "nologin":
                out({"type": "error", "fatal": True, "message": "нет входа: тест"}); continue
            out({"type": "ready"})
        elif t == "prompt":
            out({"type": "permission", "id": m["id"], "call_id": "c1", "tool": "read", "input": {"path": "x.txt"}})
            out({"type": "tool_call", "id": m["id"], "call_id": "c2", "name": "ping", "args": {"a": 1}})
        elif t in ("permission_result", "tool_result"):
            sys.stderr.write("got " + json.dumps(m) + "\\n"); sys.stderr.flush()
            out({"type": "text", "id": "1", "text": "ответ " + json.dumps(m, sort_keys=True, ensure_ascii=False)})
            if t == "tool_result":
                out({"type": "end", "id": "1", "status": "ok", "error": None, "usage": {"requests": 1}, "turns": 1})
        elif t == "shutdown":
            break
''')


def _fake(tmp_path, **kw):
    script = tmp_path / "fake.py"
    script.write_text(FAKE, encoding="utf-8")
    return PiAgent(cwd=tmp_path, command=[sys.executable, str(script)], runtime_dir=tmp_path, **kw)


async def test_fake_process_permission_and_custom_tool(tmp_path):
    seen = []

    async def can_use(tool, inp):
        seen.append((tool, inp))
        return False, "нельзя"

    async def ping(args):
        return "понг"

    agent = _fake(tmp_path, can_use_tool=can_use, custom_tools=[CustomTool("ping", "d", {"type": "object"}, ping)])
    events = [e async for e in agent.run("привет")]
    await agent.close()
    assert seen == [("Read", {"file_path": str(tmp_path / "x.txt")})]
    texts = [e.text for e in events if isinstance(e, TextEvent)]
    assert any('"allow": false' in t and "нельзя" in t for t in texts)
    assert any('"ok": true' in t and "понг" in t for t in texts)
    assert isinstance(events[-1], EndEvent) and events[-1].status == "ok"


async def test_no_callback_or_failing_callback_denies(tmp_path):
    async def boom(tool, inp):
        raise ValueError("сбой")

    for cb in (None, boom):
        agent = _fake(tmp_path, can_use_tool=cb)
        events = [e async for e in agent.run("привет")]
        await agent.close()
        assert any('"allow": false' in e.text for e in events if isinstance(e, TextEvent))


async def test_failing_custom_tool_reports_error(tmp_path):
    async def bad(args):
        raise RuntimeError("упал")

    agent = _fake(tmp_path, custom_tools=[CustomTool("ping", "d", {"type": "object"}, bad)])
    events = [e async for e in agent.run("привет")]
    await agent.close()
    assert any('"ok": false' in e.text and "упал" in e.text for e in events if isinstance(e, TextEvent))


async def test_fatal_init_error_is_pi_agent_error(tmp_path):
    agent = _fake(tmp_path, provider="nologin")
    with pytest.raises(PiAgentError, match="нет входа"):
        async for _ in agent.run("привет"):
            pass
    await agent.close()


async def test_process_death_ends_turn_with_error(tmp_path):
    script = tmp_path / "die.py"
    script.write_text('import sys, json\nfor l in sys.stdin:\n    m = json.loads(l)\n    if m["type"] == "init":\n'
                      '        print(json.dumps({"type": "ready"}), flush=True)\n    elif m["type"] == "prompt":\n        sys.exit(3)\n',
                      encoding="utf-8")
    agent = PiAgent(cwd=tmp_path, command=[sys.executable, str(script)], runtime_dir=tmp_path)
    events = [e async for e in agent.run("привет")]
    await agent.close()
    assert events[-1].status == "error" and "завершился" in events[-1].error


def test_missing_node_or_deps_give_clear_error(tmp_path, monkeypatch):
    agent = PiAgent(cwd=tmp_path, runtime_dir=tmp_path)
    with pytest.raises(PiAgentError, match="нет файла"):
        agent._resolve_command()
    (tmp_path / "worker.mjs").write_text("", encoding="utf-8")
    with pytest.raises(PiAgentError, match="npm ci"):
        agent._resolve_command()
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(PiAgentError, match="node"):
        agent._resolve_command()


def test_child_env_has_no_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("MY_TOKEN", "x")
    monkeypatch.setenv("HARMLESS_VAR", "1")
    env = PiAgent._clean_env()
    assert "OPENAI_API_KEY" not in env and "MY_TOKEN" not in env and env["HARMLESS_VAR"] == "1"


# ── настоящий worker.mjs ───────────────────────────────────────────

def _real(tmp_path, responses, **kw):
    return PiAgent(cwd=tmp_path, provider="faux", faux_responses=responses, runtime_dir=RUNTIME, **kw)


@needs_node
async def test_real_worker_read_allowed_and_denied(tmp_path):
    (tmp_path / "n.txt").write_text("привет", encoding="utf-8")
    asked = []

    async def can_use(tool, inp):
        asked.append((tool, inp))
        return True, ""

    resp = [{"tool_calls": [{"name": "read", "args": {"path": "n.txt"}}]}, {"text": "прочла"}]
    agent = _real(tmp_path, resp, tools=["read"], can_use_tool=can_use)
    events = [e async for e in agent.run("прочитай")]
    await agent.close()
    assert asked == [("Read", {"file_path": str(tmp_path / "n.txt")})]
    uses = [e for e in events if isinstance(e, ToolUse)]
    assert uses and uses[0].claude_tool == "Read"
    assert events[-1].status == "ok" and events[-1].turns == 2


@needs_node
async def test_real_worker_bash_denied_does_not_run(tmp_path):
    mark = tmp_path / "mark.txt"
    cmd = f'node -e "require(\'fs\').writeFileSync(process.argv[1],\'x\')" "{mark.as_posix()}"'

    async def deny(tool, inp):
        return False, "нет"

    resp = [{"tool_calls": [{"name": "bash", "args": {"command": cmd}}]}, {"text": "ок"}]
    for cb in (deny, None):
        agent = _real(tmp_path, resp, tools=["bash"], can_use_tool=cb)
        events = [e async for e in agent.run("запусти")]
        await agent.close()
        assert events[-1].status == "ok"
        assert not mark.exists()
        assert not [e for e in events if isinstance(e, ToolUse)]


@needs_node
async def test_real_worker_custom_tool_and_no_login(tmp_path):
    async def ping(args):
        return "понг"

    resp = [{"tool_calls": [{"name": "ping", "args": {}}]}, {"text": "готово"}]
    agent = _real(tmp_path, resp, tools=[], custom_tools=[CustomTool("ping", "d", {"type": "object", "properties": {}}, ping)])
    last, end = await agent.ask("пинг")
    await agent.close()
    assert last == "готово" and end.status == "ok"

    nologin = PiAgent(cwd=tmp_path, runtime_dir=RUNTIME, auth_file=tmp_path / "нет.json")
    with pytest.raises(PiAgentError, match="нет входа"):
        await nologin.ask("привет")
    await nologin.close()
