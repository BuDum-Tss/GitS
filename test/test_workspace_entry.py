"""Вводная модуля при входе в среду (WORKSPACE_CONTRACT §7): ответ на workspace.enter доходит до агента и до заметки о среде."""
import pytest

from core_service import entry_intro
from environments.builtin import _workspace_tools
from services.spaces.link import WorkspaceLink
from services.spaces.manager import WorkspaceManager
from services.spaces import model as dom

MANIFEST = {"id": "montage", "role": "workspace", "title": "Монтаж видео", "workspace": {
    "id": "montage", "title": "Монтаж", "purpose": "делать ролики", "when_to_use": "просят смонтировать видео",
    "entry": ["user_message", "self_impulse"]}}

INTRO = {"ok": True, "brief": "Сначала прочитай навык remotion-best-practices.",
         "skills": [{"name": "remotion-best-practices", "description": "правила Remotion"}, {"name": "remotion-render", "description": "рендер"}],
         "units": [{"id": "u1", "title": "Шортс про кота", "state": "draft"}], "site": "http://localhost:3000", "junk": "x"}


class Module:
    def __init__(self, answer):
        self.answer = answer

    async def request(self, name, body, timeout=0):
        return self.answer if name == "workspace.enter" else {"ok": True}


def build(answer):
    mod = Module(answer)
    link = WorkspaceLink(connection_of=lambda m: mod if m == "montage" else None)
    mgr = WorkspaceManager(activate=link.activate, afk=link.afk,
                           release=lambda ws, reason, force: link.release(reason=reason, force=force, module=ws),
                           connected=lambda ws: True, modules_of=lambda: [("montage", MANIFEST, "Монтаж")])
    return link, mgr


async def test_link_keeps_only_known_entry_fields_and_clears_on_release():
    link, _ = build(INTRO)
    res = await link.activate("montage", "ролик")
    assert res["ok"] and set(res["entry"]) == {"brief", "skills", "units", "site"}
    assert link.entries["montage"]["skills"][0]["name"] == "remotion-best-practices"
    again = await link.activate("montage", "")
    assert again["already"] and again["entry"]["site"] == "http://localhost:3000"
    await link.release("user")
    assert "montage" not in link.entries


async def test_entry_limits():
    big = {"ok": True, "brief": "б" * 2000, "skills": [{"name": f"s{i}", "description": "д" * 500} for i in range(50)],
           "units": [{"id": f"u{i}", "title": "t", "state": "s"} for i in range(20)]}
    link, _ = build(big)
    entry = (await link.activate("montage", ""))["entry"]
    assert len(entry["brief"]) == 800 and len(entry["skills"]) == 30 and len(entry["units"]) == 10
    assert len(entry["skills"][0]["description"]) == 160


async def test_enter_tool_tells_agent_skill_names_and_units():
    _, mgr = build(INTRO)
    tools = {t.name: t for t in _workspace_tools(mgr)}
    out = await tools["enter"].call({"workspace": "montage", "goal": "ролик"})
    assert "сейчас в среде «Монтаж»" in out and "Сначала прочитай навык" in out
    assert "remotion-best-practices — правила Remotion" in out and "remotion-render" in out
    assert "u1 «Шортс про кота» (draft)" in out and "Сайт: http://localhost:3000" in out
    assert len(out) <= 1800
    assert mgr.entry["montage"]["brief"].startswith("Сначала")


async def test_manager_drops_entry_on_exit():
    _, mgr = build(INTRO)
    res = await mgr.enter("montage", dom.Reason.USER, "ролик")
    assert res["ok"] and res["entry"]["brief"] and "montage" in mgr.entry
    await mgr.exit("montage")
    assert "montage" not in mgr.entry


async def test_without_intro_everything_as_before():
    _, mgr = build({"ok": True})
    tools = {t.name: t for t in _workspace_tools(mgr)}
    out = await tools["enter"].call({"workspace": "montage", "goal": "ролик"})
    assert out == "сейчас в среде «Монтаж»" and "montage" not in mgr.entry


def test_entry_intro_for_note():
    text = entry_intro({"brief": "Читай навыки.", "skills": [{"name": "a"}, {"name": "b"}]})
    assert "Читай навыки." in text and "a, b" in text
    assert entry_intro(None) == "" and entry_intro({}) == ""
    assert len(entry_intro({"brief": "я" * 2000})) == 600
