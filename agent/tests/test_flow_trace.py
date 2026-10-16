"""Поток данных (ТЗ 17): шина трассы, топология, эндпоинты дашборда. Без сети, моделей и Claude."""
import asyncio
import time

import flow_spec
import flow_trace
import pytest
from aiohttp.test_utils import TestClient, TestServer

import dashboard


@pytest.fixture(autouse=True)
def _clean():
    flow_trace.clear()
    yield
    flow_trace.clear()


def test_topology_has_no_node_without_edge_and_edges_point_to_nodes():
    t = flow_spec.topology(["telegram", "voice", "rimworld"])
    ids = {n["id"] for n in t["nodes"]}
    linked = {e["from"] for e in t["edges"]} | {e["to"] for e in t["edges"]}
    assert ids <= linked
    assert all(e["from"] in ids and e["to"] in ids for e in t["edges"])
    assert {"module:telegram", "module:voice", "module:rimworld"} <= ids
    assert {n["layer"] for n in t["nodes"]} <= {layer["id"] for layer in t["layers"]}
    assert {s["node"] for s in t["states"]} <= ids
    states = {s["id"] for s in t["states"]}
    assert all(x["from"] in states and x["to"] in states for x in t["transitions"])


def test_trace_keeps_no_message_text():
    secret = "СЕКРЕТНЫЙ ТЕКСТ СООБЩЕНИЯ " * 5
    flow_trace.step("t1", "bus", "module:x", "bus", "event", size=len(secret), label=secret)
    rec = flow_trace.records()[0]
    assert rec["size"] == len(secret)
    assert len(rec["label"]) <= flow_trace.LABEL_MAX
    assert "\n" not in rec["label"]


def test_ring_buffer_is_bounded():
    for i in range(flow_trace.CAPACITY + 50):
        flow_trace.step("t", "bus")
    assert len(flow_trace.records()) == flow_trace.CAPACITY


def test_step_overhead_is_small():
    n = 5000
    t0 = time.perf_counter()
    for _ in range(n):
        flow_trace.step("t", "bus", "a", "b", "event", ms=1.0, size=5, label="x")
    per = (time.perf_counter() - t0) / n
    assert per < 100e-6


def test_edge_stats_windows_errors_and_p95():
    now = time.time()
    flow_trace.step("t", "gliner", "bus", "gliner", "classify", ms=10)
    flow_trace.step("t", "gliner", "bus", "gliner", "classify", ms=30, label="x:error")
    # старая запись (5 минут назад): в окне 10 минут, но не в минуте
    flow_trace.step("t", "gliner", "bus", "gliner", "classify", ms=50)
    flow_trace._buf[-1]["t"] = now - 300
    s = flow_trace.edge_stats(now)["bus->gliner"]
    assert s["per_min"] == 2 and s["per_10min"] == 3
    assert s["last_ms"] == 50 and s["p95_ms"] == 50
    assert s["error_rate"] == pytest.approx(1 / 3, abs=0.01)


def test_turns_outcome_and_node_info():
    flow_trace.step("a", "bus", "module:m", "bus", "event", size=3)
    flow_trace.step("a", "attention", "gliner", "attention", "decision", label="D2")
    flow_trace.step("b", "out_voice", "speech_queue", "out_voice", "speak", size=9)
    outs = {t["turn"]: t["outcome"] for t in flow_trace.turns()}
    assert outs == {"a": "silence", "b": "answer"}
    info = flow_trace.node_info("attention")
    assert info["count"] == 1 and info["labels"] == {"D2": 1}


def test_latency_row_becomes_path_with_step_delays():
    row = {"ts": 1.0, "data": {"trace_id": "abc", "source": "voice", "end_to_sound_ms": 90,
                               "marks": {"mic_end": 0, "core_in": 10, "classify_done": 25, "play_start": 90}}}
    turn = flow_trace.turn_from_latency_row(row)
    assert [s["node"] for s in turn["steps"]] == ["module:voice", "bus", "gliner", "out_voice"]
    assert [s["ms"] for s in turn["steps"]] == [None, 10, 15, 65]
    assert flow_trace.turn_from_latency_row({"data": {"marks": {}}}) is None


async def test_thousand_records_per_second_do_not_stall_the_loop():
    lag = []

    async def ticker():
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            lag.append(time.perf_counter() - t0 - 0.01)

    task = asyncio.create_task(ticker())
    for _ in range(10):
        for _ in range(100):
            flow_trace.step("load", "bus", "a", "b", "event")
        await asyncio.sleep(0.1)
    task.cancel()
    assert len(flow_trace.records()) == 1000
    assert max(lag) < 0.2


async def test_flow_endpoints():
    c = TestClient(TestServer(dashboard.build_app()))
    await c.start_server()
    try:
        flow_trace.step("tt", "bus", "module:m", "bus", "event", size=2, label="m.x")
        body = await (await c.get("/api/flow")).json()
        assert body["nodes"] and body["edges"] and "bus->gliner" in {e["id"] for e in body["edges"]}
        assert any(t["turn"] == "tt" for t in body["turns"])
        d = await (await c.get("/api/flow/turn/tt")).json()
        assert d["steps"][0]["node"] == "bus"
        assert (await c.get("/api/flow/turn/nope")).status == 404
        n = await (await c.get("/api/flow/node/bus")).json()
        assert n["count"] == 1
    finally:
        await c.close()


def test_core_only_mode_blocks_modules_but_keeps_services(monkeypatch):
    import supervisor

    class S:
        def __init__(self, kind):
            self.kind, self.autostart, self.skip_when_env = kind, True, {}

    sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
    sup.specs = {"m": S("module"), "decider": S("service")}
    sup._enabled = lambda mid: True
    monkeypatch.setenv("GITS_NO_MODULES", "1")
    assert not sup.should_run("m") and sup.should_run("decider")
    monkeypatch.setenv("GITS_NO_DECIDER", "1")
    assert not sup.should_run("decider")
    monkeypatch.delenv("GITS_NO_MODULES")
    assert sup.should_run("m")
