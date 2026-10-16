"""Среда агента в контракте модулей (ТЗ 32, WS-4): поле манифеста `workspace`, запросы `workspace.*` и старые имена-алиасы."""
import copy

import pytest

from gits_sdk import protocol as P
from gits_sdk.protocol import Manifest
from helpers import MANIFEST, make_client, running

WS = {"id": "demo", "title": "Демо", "purpose": "тест", "entry": ["user_message", "self_impulse"], "interruptible": True,
      "safe_points": ["кадр записан"], "signals_in": ["telegram.*"], "done_when": "задача закончилась"}


def with_ws(ws):
    return {**copy.deepcopy(MANIFEST), "workspace": ws}


def test_valid_workspace_passes_and_round_trips():
    assert P.validate_manifest(with_ws(WS)) == []
    m = Manifest.from_dict(with_ws(WS))
    assert m.workspace == WS and m.to_dict()["workspace"] == WS
    assert Manifest.from_dict(copy.deepcopy(MANIFEST)).workspace is None


@pytest.mark.parametrize("change, needle", [
    ({"id": "other"}, "workspace.id"),
    ({"entry": ["process_started"]}, "workspace.entry"),
    ({"entry": []}, "workspace.entry"),
    ({"interruptible": "да"}, "workspace.interruptible"),
    ({"signals_in": "telegram.*"}, "workspace.signals_in"),
    ({"safe_points": [3]}, "workspace.safe_points"),
    ({"done_when": ""}, "workspace.done_when"),
    ({"mood": "x"}, "неизвестные поля"),
])
def test_bad_workspace_fields_are_reported(change, needle):
    problems = P.validate_manifest(with_ws({**WS, **change}))
    assert any(needle in p for p in problems), problems


def test_workspace_needs_a_title_and_an_object():
    assert any("workspace.title" in p for p in P.validate_manifest(with_ws({"id": "demo"})))
    assert any("workspace" in p for p in P.validate_manifest(with_ws("rimworld")))


def test_aliases_are_symmetric():
    for new, old in (("workspace.enter", "policy.activate"), ("workspace.exit", "focus.release"),
                     ("workspace.resume", "focus.resume")):
        assert P.WORKSPACE_ALIASES[new] == old and P.WORKSPACE_ALIASES[old] == new
        assert P.is_request(new) and P.is_request(old)


@pytest.mark.parametrize("registered, sent", [("workspace.enter", "policy.activate"), ("policy.activate", "workspace.enter"),
                                              ("workspace.enter", "workspace.enter")])
async def test_one_handler_serves_both_names(core, registered, sent):
    client = make_client(core)
    got = []

    @client.on_request(registered)
    async def enter(body):
        got.append(body)
        return {"ok": True, "policy": "working"}

    async with running(client, core):
        res = await core.request(sent, {"policy": "working", "goal": "g"})
        assert res == {"ok": True, "policy": "working"} and got == [{"policy": "working", "goal": "g"}]


async def test_fakecore_helper_sends_new_and_legacy_names(core):
    client = make_client(core)
    seen = []

    for name in ("workspace.exit", "focus.resume"):
        @client.on_request(name)
        async def handler(body, _n=name):
            seen.append(_n)
            return {"ok": True}

    async with running(client, core):
        await core.workspace("exit", {"checkpoint": True})
        await core.workspace("exit", {"checkpoint": True}, legacy=True)
        await core.workspace("resume", {"task_id": "t1"}, legacy=True)
        await core.workspace("resume", {"task_id": "t1"})
        assert seen == ["workspace.exit"] * 2 + ["focus.resume"] * 2
        with pytest.raises(ValueError):
            await core.workspace("sleep")


async def test_module_without_handler_answers_unknown(core):
    from gits_sdk import ToolError
    client = make_client(core)
    async with running(client, core):
        with pytest.raises(ToolError) as e:
            await core.request("workspace.enter", {})
        assert e.value.code == "unknown"
