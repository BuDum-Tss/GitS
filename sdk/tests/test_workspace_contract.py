"""Среда агента в контракте модулей (ТЗ 32, WS-4): поле манифеста `workspace`, запросы `workspace.*`."""
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


def test_workspace_requests_are_the_only_environment_names():
    for name in ("workspace.enter", "workspace.exit", "workspace.resume"):
        assert P.is_request(name)
    for old in ("policy.activate", "focus.release", "focus.resume"):
        assert not P.is_request(old)
    assert not hasattr(P, "WORKSPACE_ALIASES") and not hasattr(P, "PolicySpec")


async def test_enter_without_policy_and_helper_sends_new_names(core):
    client = make_client(core)
    got = []

    @client.on_request("workspace.enter")
    async def enter(body):
        got.append(body)
        return {"ok": True}

    seen = []
    for name in ("workspace.exit", "workspace.resume"):
        @client.on_request(name)
        async def handler(body, _n=name):
            seen.append(_n)
            return {"ok": True}

    async with running(client, core):
        assert await core.request("workspace.enter", {"workspace": "demo", "goal": "g"}) == {"ok": True}
        assert got == [{"workspace": "demo", "goal": "g"}]
        await core.workspace("exit", {"checkpoint": True})
        await core.workspace("resume", {"task_id": "t1"})
        assert seen == ["workspace.exit", "workspace.resume"]
        with pytest.raises(ValueError):
            await core.workspace("sleep")


def test_legacy_policies_in_manifest_are_ignored():
    legacy = {**copy.deepcopy(MANIFEST), "policies": [{"id": "x"}], "default_policy": "nope"}
    assert P.validate_manifest(legacy) == []
    m = Manifest.from_dict(legacy)
    assert "policies" not in m.to_dict() and not hasattr(m, "policies")


async def test_module_without_handler_answers_unknown(core):
    from gits_sdk import ToolError
    client = make_client(core)
    async with running(client, core):
        with pytest.raises(ToolError) as e:
            await core.request("workspace.enter", {})
        assert e.value.code == "unknown"
