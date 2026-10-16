"""Прогон ядра на устойчивость и хвосты ТЗ 17 (FLOW-1..FLOW-3, трасса Claude, Telegram, питомца, ошибки GLiNER). Без сети,
моделей, Claude и настоящих модулей."""
import asyncio
from types import SimpleNamespace

import flow_trace
import pytest
from aiohttp.test_utils import TestClient, TestServer
from tools import soak_core

import dashboard
import decide
import settings
from conversation_io import Channel, ConversationIO
from module_outbound import Outbound
from supervisor import ModuleSpec, Supervisor


@pytest.fixture(autouse=True)
def _clean():
    flow_trace.clear()
    yield
    flow_trace.clear()


async def test_short_soak_runs_core_without_modules_and_closes_cleanly(tmp_path):
    rep = await soak_core.run_soak(0.15, 1.0, memory_dir=str(tmp_path / "soak"))
    assert rep["mode"] == "core-only"
    assert rep["load"]["chat"] > 0 and rep["load"]["events"] > 0 and rep["load"]["connects"] > 0
    assert rep["samples"] and {"rss_mb", "tasks", "q_speech", "q_attention", "q_graph", "trace_len"} <= set(rep["samples"][0])
    checks = rep["checks"]
    assert checks["нет необработанных исключений и ERROR"], rep["errors"]
    assert checks["ядро закрылось за 30 с"] and checks["после закрытия нет оставшихся задач"]
    assert checks["очередь речи ограничена"] and checks["очередь графа ограничена"]
    md = soak_core.to_markdown(rep)
    assert "| критерий | итог |" in md and "RSS" in md


async def test_soak_does_not_leave_patches_behind(tmp_path):
    import fast_brain
    import os
    before = (fast_brain.publish, fast_brain.react_chat, decide.decide)
    env = dict(os.environ)
    await soak_core.run_soak(0.05, 1.0, memory_dir=str(tmp_path / "soak2"))
    assert (fast_brain.publish, fast_brain.react_chat, decide.decide) == before
    assert dict(os.environ) == env                      # GITS_NO_MODULES и прочие переменные прогона возвращены


def test_percentile_and_stub_graph_queue_is_bounded():
    assert soak_core.percentile([], 0.99) is None
    assert soak_core.percentile(list(range(1, 101)), 0.99) == 99
    g = soak_core.StubGraph()
    for i in range(1000):
        g.enqueue("u", str(i))
    assert len(g.queue) == 500
    g.drain(5)
    assert len(g.queue) == 495


# ── ошибки GLiNER отдельно ──────────────────────────────────────────────────────────

async def test_gliner_failure_is_traced_as_error_with_reason():
    async def failing(text, questions, timeout=None, down_on_timeout=True):
        decide.last_failure[0] = "timeout"
        return None
    old = decide.decide
    decide.decide = failing
    try:
        d = decide.Decider()
        assert await d.classify("привет", {"q": {"type": "noul"}}) is None
        assert d.last_error == "timeout"
    finally:
        decide.decide = old
        decide.last_failure[0] = None
    errs = flow_trace.node_errors("gliner")
    assert errs["total"] == 1 and errs["last_label"] == "error:timeout" and errs["reasons"] == {"error:timeout": 1}
    assert "gliner" in flow_trace.error_nodes()
    assert flow_trace.node_info("gliner")["error_info"]["total"] == 1


async def test_gliner_answer_none_without_failure_is_not_an_error():
    async def backend(text, questions, timeout):
        return None
    d = decide.Decider(backend)
    assert await d.classify("привет", {"q": {"type": "noul"}}) is None
    assert d.last_error is None
    assert flow_trace.node_errors("gliner")["total"] == 0 and flow_trace.error_nodes() == {}


async def test_gliner_backend_exception_is_traced_as_error():
    async def backend(text, questions, timeout):
        raise RuntimeError("сбой")
    d = decide.Decider(backend)
    assert await d.classify("привет", {"q": {"type": "noul"}}) is None
    assert flow_trace.node_errors("gliner")["last_label"] == "error:backend"


# ── выходы: Telegram, питомец, Claude ───────────────────────────────────────────────

async def test_deliver_to_telegram_is_traced_ok_and_error_without_text():
    class Conn:
        manifest = {"capabilities": ["chat.channel"]}

        async def request(self, name, body, timeout=0):
            return {"delivered": True}

    hub = SimpleNamespace(connection=lambda mid: Conn())
    io = ConversationIO(SimpleNamespace(), hub)
    assert await io.deliver(Channel("module", "telegram", "1"), "секретный текст ответа")
    rec = flow_trace.records()[-1]
    assert rec["node"] == "out_telegram" and rec["from"] == "speech_queue" and rec["label"] == "telegram:ok"
    assert "секретный" not in str(flow_trace.records())

    io2 = ConversationIO(SimpleNamespace(), None)
    assert not await io2.deliver(Channel("module", "telegram", "1"), "x")
    err = flow_trace.records()[-1]
    assert err["kind"] == "error" and flow_trace.is_error(err)
    assert flow_trace.node_errors("out_telegram")["total"] == 1


async def test_presence_to_connected_pet_is_traced():
    sent = []

    class Hub:
        _monotonic = staticmethod(lambda: 1000.0)

        def is_connected(self, mid):
            return mid == "pet"

        def _spawn(self, coro):
            coro.close()
            sent.append(1)

    ob = Outbound(Hub())
    ob.broadcast_presence({"speaking": True})
    recs = [r for r in flow_trace.records() if r["node"] == "out_pet"]
    assert recs and recs[0]["from"] == "speech_queue" and sent


async def test_presence_without_pet_is_not_traced():
    class Hub:
        _monotonic = staticmethod(lambda: 1000.0)

        def is_connected(self, mid):
            return False

        def _spawn(self, coro):
            coro.close()

    Outbound(Hub()).broadcast_presence({"speaking": True})
    assert not [r for r in flow_trace.records() if r["node"] == "out_pet"]


async def test_web_search_and_analysis_trace_claude_without_text(monkeypatch):
    """Claude SDK заглушен: сеть и настоящий Claude не вызываются."""
    import web_search

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, q):
            raise RuntimeError("нет сети")

        async def receive_response(self):
            if False:
                yield None

    monkeypatch.setattr(web_search, "ClaudeSDKClient", FakeClient)
    out = await web_search.search("секретный запрос про погоду")
    assert out == "Поиск не удался."
    rec = [r for r in flow_trace.records() if r["node"] == "claude"][-1]
    assert rec["kind"] == "error" and rec["label"] == "web:error" and "секретный" not in str(rec)
    assert flow_trace.node_errors("claude")["total"] == 1


# ── режим «только ядро»: настройка ──────────────────────────────────────────────────

def test_only_core_setting_blocks_modules_like_the_env_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("GITS_NO_MODULES", raising=False)
    assert settings.DEFAULTS["only_core"] is False
    spec = ModuleSpec(id="m", cwd=str(tmp_path), command="x", args=[])
    svc = ModuleSpec(id="decider", cwd=str(tmp_path), command="x", args=[], kind="service")
    flag = {"on": False}
    sup = Supervisor([spec, svc], token="t", core_url="ws://127.0.0.1:1/module/ws", gits_root=tmp_path, agent_dir=tmp_path,
                     logs_dir=tmp_path / "logs", only_core=lambda: flag["on"])
    assert sup.should_run("m") and sup.should_run("decider")
    flag["on"] = True
    assert not sup.should_run("m") and sup.should_run("decider")


async def test_api_flow_reports_errors_and_only_core(monkeypatch):
    monkeypatch.delenv("GITS_NO_MODULES", raising=False)
    c = TestClient(TestServer(dashboard.build_app()))
    await c.start_server()
    try:
        flow_trace.step("t1", "gliner", "bus", "gliner", "error", label="error:down")
        body = await (await c.get("/api/flow")).json()
        assert body["errors"]["gliner"]["last_label"] == "error:down" and body["only_core"] is False
        settings.update(only_core=True)
        assert (await (await c.get("/api/flow")).json())["only_core"] is True
        n = await (await c.get("/api/flow/node/gliner")).json()
        assert n["error_info"]["total"] == 1
    finally:
        await c.close()
    await asyncio.sleep(0)
