"""Workspace: среды агента (ТЗ 32, WS-1, WS-2, WS-3, WS-5, WS-9). Модулей и моделей нет: заглушки activate/release."""
import aiohttp
from aiohttp import web
from test_core_wiring import JSON, free_port

import dashboard
from app.workspace_manager import WorkspaceManager
from domain import workspace as dom
from domain.workspace import ASSISTANT, SLEEP, Reason


def make(**kw):
    calls = []

    async def activate(ws, goal):
        calls.append(("activate", ws, goal))
        return {"ok": True}

    async def release(ws, reason, force):
        calls.append(("release", ws, reason, force))
        return {"ok": True}

    clock = {"t": 100.0}
    mgr = WorkspaceManager(activate=kw.pop("activate", activate), release=kw.pop("release", release),
                           now=lambda: clock["t"], **kw)
    mgr.register_module("rimworld", "RimWorld")
    mgr.register_module("montage", "Монтаж")
    return mgr, calls, clock


# ── домен ────────────────────────────────────────────────────────────────────

def test_module_workspace_is_entered_only_by_impulse():
    spec = dom.module_spec("rimworld", "RimWorld")
    assert dom.can_enter(spec, Reason.USER)[0] and dom.can_enter(spec, Reason.SELF)[0] and dom.can_enter(spec, Reason.CRITICAL)[0]
    for reason in (Reason.SCHEDULE, Reason.DONE, Reason.DISCONNECT, "process_started", "game_running"):
        assert not dom.can_enter(spec, reason)[0], reason


def test_sleep_is_entered_only_by_the_sleep_scheduler_and_assistant_by_any_reason():
    assert dom.can_enter(dom.BUILTIN[SLEEP], Reason.SCHEDULE)[0]
    assert not dom.can_enter(dom.BUILTIN[SLEEP], Reason.USER)[0]
    assert all(dom.can_enter(dom.BUILTIN[ASSISTANT], r)[0] for r in Reason.ALL)


def test_exit_waits_for_a_safe_point_unless_the_reason_is_forced():
    spec = dom.module_spec("montage", "Монтаж")
    assert not dom.can_exit(spec, False, Reason.SELF)[0]
    assert not dom.can_exit(spec, False, Reason.CRITICAL)[0]
    for reason in (Reason.DONE, Reason.BLOCKED, Reason.DISCONNECT, Reason.USER):
        assert dom.can_exit(spec, False, reason)[0], reason
    assert dom.can_exit(spec, True, Reason.SELF)[0]


# ── менеджер ─────────────────────────────────────────────────────────────────

async def test_starts_in_assistant():
    mgr, _, _ = make()
    assert mgr.current == ASSISTANT and mgr.reason == Reason.START


async def test_user_message_enters_a_module_workspace_and_done_returns_to_assistant():
    mgr, calls, clock = make()
    clock["t"] = 200.0
    res = await mgr.enter("rimworld", Reason.USER, "зайти в колонию")
    assert res == {"ok": True, "workspace": "rimworld"}
    assert (mgr.current, mgr.reason, mgr.goal, mgr.since) == ("rimworld", Reason.USER, "зайти в колонию", 200.0)
    assert calls == [("activate", "rimworld", "зайти в колонию")]
    res = await mgr.leave(Reason.DONE)
    assert res["ok"] and mgr.current == ASSISTANT and mgr.reason == Reason.DONE
    assert calls[-1] == ("release", "rimworld", Reason.DONE, True)
    assert [(r["from"], r["to"], r["reason"]) for r in mgr.history] == [
        (ASSISTANT, "rimworld", Reason.USER), ("rimworld", ASSISTANT, Reason.DONE)]


async def test_self_impulse_enters_too():
    mgr, _, _ = make()
    assert (await mgr.enter("montage", Reason.SELF, "собрать ролик"))["ok"]
    assert mgr.current == "montage"


async def test_game_start_or_schedule_does_not_enter_a_module_workspace():
    """WS-5: запуск игры не включает среду, расписание тоже."""
    mgr, calls, _ = make()
    res = await mgr.enter("rimworld", "process_started")
    assert not res["ok"] and mgr.current == ASSISTANT and calls == []
    assert not (await mgr.enter("rimworld", Reason.SCHEDULE))["ok"]
    assert mgr.current == ASSISTANT


async def test_unknown_or_disconnected_workspace_is_refused_and_nothing_changes():
    mgr, calls, _ = make(connected=lambda ws: ws != "rimworld")
    assert not (await mgr.enter("nowhere", Reason.USER))["ok"]
    res = await mgr.enter("rimworld", Reason.USER)
    assert not res["ok"] and "не подключён" in res["error"]
    assert mgr.current == ASSISTANT and calls == []


async def test_module_refusal_keeps_the_current_workspace():
    async def refuse(ws, goal):
        return {"ok": False, "reason": "небезопасная точка"}

    mgr, _, _ = make(activate=refuse)
    res = await mgr.enter("rimworld", Reason.USER)
    assert res == {"ok": False, "error": "небезопасная точка"} and mgr.current == ASSISTANT


async def test_critical_signal_waits_for_a_safe_point_but_the_user_does_not():
    safe = {"v": False}
    mgr, _, _ = make(interruptible=lambda ws: safe["v"])
    await mgr.enter("montage", Reason.USER)
    refused = await mgr.enter("rimworld", Reason.CRITICAL, "набег")
    assert not refused["ok"] and mgr.current == "montage"
    assert (await mgr.enter("rimworld", Reason.USER))["ok"] and mgr.current == "rimworld"


async def test_refused_release_keeps_the_module_unless_forced():
    async def refuse(ws, reason, force):
        return {"ok": False, "reason": "не могу"}

    mgr, _, _ = make(release=refuse)
    await mgr.enter("rimworld", Reason.USER)
    assert not (await mgr.leave(Reason.SELF))["ok"] and mgr.current == "rimworld"
    assert (await mgr.leave(Reason.BLOCKED))["ok"] and mgr.current == ASSISTANT       # блокировку модуль не отменяет


async def test_module_disconnect_returns_to_assistant():
    connected = {"rimworld": True}
    mgr, _, _ = make(connected=lambda ws: connected.get(ws, True))
    await mgr.enter("rimworld", Reason.USER)
    mgr.check()
    assert mgr.current == "rimworld"
    connected["rimworld"] = False
    mgr.check()
    assert mgr.current == ASSISTANT and mgr.reason == Reason.DISCONNECT and mgr.history[-1]["note"] == "rimworld"


async def test_sleep_follows_the_scheduler_and_wake_returns_to_assistant():
    seen = []
    mgr, _, _ = make(on_change=seen.append)
    mgr.on_sleep_state("sleeping")
    assert mgr.current == SLEEP and mgr.reason == Reason.SCHEDULE
    mgr.on_sleep_state("awake")
    assert mgr.current == ASSISTANT and [r["to"] for r in seen] == [SLEEP, ASSISTANT]


async def test_sleep_does_not_pull_the_agent_out_of_a_module_workspace():
    mgr, _, _ = make()
    await mgr.enter("rimworld", Reason.USER)
    mgr.on_sleep_state("sleeping")
    assert mgr.current == "rimworld"
    mgr.on_sleep_state("awake")
    assert mgr.current == "rimworld"


async def test_snapshot_lists_workspaces_and_history():
    mgr, _, _ = make(connected=lambda ws: ws == "montage")
    await mgr.enter("montage", Reason.USER, "ролик")
    snap = mgr.snapshot()
    assert snap["current"] == "montage" and snap["goal"] == "ролик" and snap["kind"] == dom.KIND_MODULE
    avail = {a["id"]: a for a in snap["available"]}
    assert set(avail) == {ASSISTANT, SLEEP, "rimworld", "montage"}
    assert avail["rimworld"]["connected"] is False and avail["montage"]["connected"] is True
    assert snap["history"][-1]["to"] == "montage"


# ── дашборд ──────────────────────────────────────────────────────────────────

async def test_dashboard_workspace_endpoints():
    mgr, _, _ = make()
    dashboard.set_workspace(mgr)
    port = free_port()
    runner = web.AppRunner(dashboard.build_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(base + "/api/workspace") as r:
                assert (await r.json())["current"] == ASSISTANT
            async with http.post(base + "/api/workspace/enter", json={"workspace": "rimworld", "goal": "играть"}, headers=JSON) as r:
                assert r.status == 200 and (await r.json())["ok"]
            async with http.post(base + "/api/workspace/enter", json={"workspace": SLEEP}, headers=JSON) as r:
                assert r.status == 409                                  # сон включает только планировщик
            async with http.post(base + "/api/workspace/enter", json={}, headers=JSON) as r:
                assert r.status == 400
            async with http.post(base + "/api/workspace/exit", json={}, headers=JSON) as r:
                assert r.status == 200
            async with http.get(base + "/api/workspace") as r:
                assert (await r.json())["current"] == ASSISTANT
    finally:
        dashboard.set_workspace(None)
        await runner.cleanup()


async def test_core_wiring_has_workspaces_and_release_holder_without_slot():
    import core_service
    from environments.registry import Registry
    from test_core_wiring import WiredCompanion
    layer = core_service.wire_modules(WiredCompanion(), Registry(), port=free_port(),
                                      battery=lambda: {"level": 1.0, "sleeping": False}, specs=[])
    snap = layer.workspaces.snapshot()
    assert snap["current"] == ASSISTANT and {"rimworld", "montage"} <= {a["id"] for a in snap["available"]}
    assert (await layer.focus.release_holder()) == {"ok": True, "already": True}
