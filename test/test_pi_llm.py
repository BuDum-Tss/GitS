"""Быстрая модель pi (`pi_llm.py`): против подделки процесса на Python и против настоящего `llm.mjs` на поддельном поставщике.
Без сети и без входа. Тесты с настоящим процессом пропускаются без node или `npm ci` в `services/agent-runtime`."""
import asyncio
import logging
import shutil
import sys
from pathlib import Path

import pytest

import fast_brain
import pi_llm
import settings
from adapters.runtime_pi import BRIDGE_DIR

FAKE = Path(__file__).with_name("fake_pi_llm.py")
HAVE_NODE = bool(shutil.which("node")) and (BRIDGE_DIR / "node_modules").is_dir()
GOOD_IMAGE = "QUJDREVGR0hJSktMTU5PUA=="


@pytest.fixture
async def fake(tmp_path):
    c = pi_llm.PiLlmClient(command=[sys.executable, str(FAKE)], auth_file=str(tmp_path / "auth.json"))
    pi_llm.set_client(c)
    yield c
    await c.close()
    pi_llm.set_client(None)


async def test_ask_and_image(fake):
    assert await pi_llm.ask("система", "привет", 10) == "эхо: привет"
    assert await pi_llm.ask("система", "смотри", 10, image_b64=GOOD_IMAGE) == "эхо: смотри +img"


async def test_stream_yields_pieces(fake):
    pieces = [p async for p in pi_llm.ask_stream("система", "раз два", 10)]
    assert len(pieces) >= 2 and "".join(pieces).strip() == "эхо: раз два"


async def test_parallel_requests(fake):
    answers = await asyncio.gather(*(pi_llm.ask("с", f"вопрос {i}", 10) for i in range(5)))
    assert answers == [f"эхо: вопрос {i}" for i in range(5)]


async def test_errors_like_claude_llm(fake):
    with pytest.raises(RuntimeError, match="лимит исчерпан"):
        await pi_llm.ask("с", "fail", 10)
    with pytest.raises(RuntimeError, match="пустой"):
        await pi_llm.ask("с", "empty", 10)
    with pytest.raises(asyncio.TimeoutError):
        await pi_llm.ask("с", "slow", 0.3)
    assert await pi_llm.ask("с", "после", 10) == "эхо: после"      # процесс жив после тайм-аута


async def test_timeout_sends_cancel(fake, caplog):
    caplog.set_level(logging.INFO, logger="pi_llm")
    with pytest.raises(asyncio.TimeoutError):
        await pi_llm.ask("с", "slow", 0.3)
    await asyncio.sleep(0.3)
    assert any("cancel " in r.message for r in caplog.records)


async def test_task_cancel_and_generator_close_send_cancel(fake, caplog):
    caplog.set_level(logging.INFO, logger="pi_llm")
    task = asyncio.create_task(pi_llm.ask("с", "slow", 10))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gen = pi_llm.ask_stream("с", "slow", 10)
    nxt = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.4)
    nxt.cancel()
    await asyncio.gather(nxt, return_exceptions=True)
    await gen.aclose()
    await asyncio.sleep(0.3)
    assert len([r for r in caplog.records if "cancel " in r.message]) >= 2


async def test_respawn_after_death(fake):
    with pytest.raises(RuntimeError):
        await pi_llm.ask("с", "die", 10)
    assert await pi_llm.ask("с", "снова", 10) == "эхо: снова"


async def test_missing_runtime_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr("pi_llm.BRIDGE_DIR", tmp_path)              # нет llm.mjs
    monkeypatch.setattr("shutil.which", lambda name: "node")
    c = pi_llm.PiLlmClient()
    with pytest.raises(RuntimeError, match="llm.mjs"):
        await c.ask("с", "привет", 5)
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="node"):
        await c.ask("с", "привет", 5)


def test_image_ok():
    assert pi_llm.image_ok(GOOD_IMAGE) and not pi_llm.image_ok(None) and not pi_llm.image_ok("***")


# ── выбор поставщика в fast_brain ─────────────────────────────────

async def test_fast_brain_picks_provider(monkeypatch):
    import claude_llm
    calls = []

    async def fake_claude(system, user, timeout, image_b64=None, contour="fast"):
        calls.append("claude")
        return "от claude"

    async def fake_pi(system, user, timeout, image_b64=None, contour="fast"):
        calls.append("pi")
        return "от pi"

    async def stream_pi(system, user, timeout):
        yield "ку"
        yield "ку"

    async def stream_claude(system, user, timeout):
        yield "мяу"

    labels = []
    monkeypatch.setattr(claude_llm, "ask", fake_claude)
    monkeypatch.setattr(claude_llm, "ask_stream", stream_claude)
    monkeypatch.setattr(pi_llm, "ask", fake_pi)
    monkeypatch.setattr(pi_llm, "ask_stream", stream_pi)
    monkeypatch.setattr(fast_brain.flow_trace, "step", lambda *a, **k: labels.append(k.get("label")))

    assert await fast_brain._ask("с", "п", 5) == "от claude"
    assert ["".join([p async for p in fast_brain._ask_stream("с", "п", 5)])] == ["мяу"]
    settings.update(agent_runtime="pi")
    assert await fast_brain._ask("с", "п", 5) == "от pi"
    assert ["".join([p async for p in fast_brain._ask_stream("с", "п", 5)])] == ["куку"]
    assert calls == ["claude", "pi"] and labels == ["claude", "pi"]


# ── настоящий llm.mjs на faux ─────────────────────────────────────

@pytest.mark.skipif(not HAVE_NODE, reason="нет node или зависимостей моста")
async def test_real_llm_process_on_faux(tmp_path):
    c = pi_llm.PiLlmClient(provider="faux", faux_responses=[{"text": "Привет из pi."}, {"echo": True}, {"error": "сбой"}, {"text": "Жив."}])
    pi_llm.set_client(c)
    try:
        assert await pi_llm.ask("система", "скажи", 20) == "Привет из pi."
        out = await pi_llm.ask("система", "что тут?", 20, image_b64=GOOD_IMAGE)
        assert out.startswith("что тут?|image:image/jpeg:") and "sys=система" in out
        with pytest.raises(RuntimeError, match="сбой"):
            await pi_llm.ask("система", "ещё", 20)
        pieces = [p async for p in pi_llm.ask_stream("система", "ещё", 20)]
        assert "".join(pieces) == "Жив."
    finally:
        await c.close()
        pi_llm.set_client(None)


@pytest.mark.skipif(not HAVE_NODE, reason="нет node или зависимостей моста")
async def test_real_llm_process_without_login(tmp_path):
    c = pi_llm.PiLlmClient(provider="openai", auth_file=str(tmp_path / "нет.json"))
    try:
        with pytest.raises(RuntimeError, match="нет входа"):
            await c.ask("с", "привет", 20)
    finally:
        await c.close()
