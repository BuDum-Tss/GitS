"""HTTP-API сервиса распознавания на FakeEngine: успех, ошибки, лимиты, очередь, таймаут, /health и /v1/info."""
import asyncio
import threading

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from stt.app import STT_KEY, create_app
from stt.config import MAX_BODY_BYTES, SttConfig
from stt.engine import FakeEngine


def pcm(seconds: float) -> bytes:
    return (np.ones(int(16000 * seconds), dtype="<i2") * 1000).tobytes()


async def start(engine, **cfg) -> TestClient:
    cfg.setdefault("warmup", False)
    client = TestClient(TestServer(create_app(SttConfig(**cfg), engine)))
    await client.start_server()
    await asyncio.wait_for(client.app[STT_KEY].ready.wait(), 5)
    return client


@pytest.fixture
async def client():
    c = await start(FakeEngine("привет мир"))
    yield c
    await c.close()


async def test_transcribe_ok(client):
    r = await client.post("/v1/transcribe?lang=ru", data=pcm(1.0))
    assert r.status == 200
    body = await r.json()
    assert set(body) == {"text", "language", "duration_ms", "engine", "took_ms"}
    assert body["text"] == "привет мир" and body["duration_ms"] == 1000 and body["engine"] == "fake"
    assert body["language"] == "ru"


async def test_default_language_from_config(client):
    r = await client.post("/v1/transcribe", data=pcm(0.5))
    assert (await r.json())["language"] == "ru"


async def test_audio_reaches_engine_as_float(client):
    await client.post("/v1/transcribe", data=pcm(0.5))
    assert client.app[STT_KEY].engine.calls == [(8000, "ru")]


@pytest.mark.parametrize("data,code", [(b"", "bad_pcm"), (b"\x00" * 3, "bad_pcm"), (pcm(0.1), "too_short")])
async def test_bad_input_400(client, data, code):
    r = await client.post("/v1/transcribe?lang=ru", data=data)
    assert r.status == 400 and (await r.json())["error"] == code


async def test_bad_lang_400(client):
    r = await client.post("/v1/transcribe?lang=../x", data=pcm(1))
    assert r.status == 400 and (await r.json())["error"] == "bad_lang"


async def test_unsupported_lang_400():
    c = await start(FakeEngine(languages=frozenset({"ru"})))
    try:
        r = await c.post("/v1/transcribe?lang=en", data=pcm(1))
        assert r.status == 400 and (await r.json())["error"] == "unsupported_lang"
    finally:
        await c.close()


async def test_limits_boundaries(client):
    assert (await client.post("/v1/transcribe", data=pcm(0.2))).status == 200
    assert (await client.post("/v1/transcribe", data=pcm(60.0))).status == 200
    assert len(pcm(60.0)) == MAX_BODY_BYTES


async def test_too_long_413(client):
    r = await client.post("/v1/transcribe", data=pcm(60.5))
    assert r.status == 413 and (await r.json())["error"] == "too_long"


async def test_too_long_413_chunked(client):
    async def gen():
        for _ in range(3):
            yield pcm(21.0)                                  # без Content-Length: режет лимит aiohttp
    r = await client.post("/v1/transcribe", data=gen())
    assert r.status == 413


async def test_engine_failure_500():
    c = await start(FakeEngine(fail=ValueError("x")))
    try:
        r = await c.post("/v1/transcribe", data=pcm(1))
        assert r.status == 500 and (await r.json())["error"] == "engine_error"
    finally:
        await c.close()


async def test_health_and_info(client):
    r = await client.get("/health")
    assert r.status == 200
    assert await r.json() == {"ok": True, "engine": "fake", "model_loaded": True, "queue": 0}
    info = await (await client.get("/v1/info")).json()
    assert info["engine"] == "fake" and info["language"] == "ru" and info["threads"] == 2
    assert info["workers"] == 2 and info["queue_limit"] == 8 and info["max_audio_s"] == 60.0


async def test_health_503_until_model_loaded():
    engine = FakeEngine()
    gate = threading.Event()
    real_load = engine.load
    engine.load = lambda: (gate.wait(5), real_load())[1]
    c = TestClient(TestServer(create_app(SttConfig(warmup=False), engine)))
    await c.start_server()
    try:
        r = await c.get("/health")
        assert r.status == 503 and (await r.json())["model_loaded"] is False
        r = await c.post("/v1/transcribe", data=pcm(1))
        assert r.status == 503 and r.headers["Retry-After"] and (await r.json())["error"] == "model_not_loaded"
        assert (await c.get("/v1/info")).status == 200       # info доступен и до загрузки
        gate.set()
        await asyncio.wait_for(c.app[STT_KEY].ready.wait(), 5)
        assert (await c.get("/health")).status == 200
        assert (await c.post("/v1/transcribe", data=pcm(1))).status == 200
    finally:
        gate.set()
        await c.close()


async def test_load_failure_reported_and_retried():
    engine = FakeEngine(fail_load="весов нет в кэше")
    c = TestClient(TestServer(create_app(SttConfig(warmup=False, retry_s=1.0), engine)))
    await c.start_server()
    try:
        await asyncio.sleep(0.1)
        r = await c.get("/health")
        assert r.status == 503 and "весов нет" in (await r.json())["error"]
        engine.fail_load = ""                                # веса появились (после stt.fetch)
        await asyncio.wait_for(c.app[STT_KEY].ready.wait(), 5)
        assert (await c.get("/health")).status == 200
    finally:
        await c.close()


async def test_concurrency_limit_and_queue_overflow():
    gate = threading.Event()
    engine = FakeEngine(gate=gate)
    c = await start(engine, workers=2, queue=2, timeout_s=20)
    svc = c.app[STT_KEY]
    try:
        tasks = [asyncio.create_task(c.post("/v1/transcribe", data=pcm(1))) for _ in range(4)]    # 2 работают, 2 в очереди
        for _ in range(200):
            if svc.running == 2 and svc.waiting == 2:
                break
            await asyncio.sleep(0.02)
        assert (svc.running, svc.waiting) == (2, 2)
        health = await (await c.get("/health")).json()
        assert health["queue"] == 2
        over = await c.post("/v1/transcribe", data=pcm(1))
        assert over.status == 503 and over.headers["Retry-After"] and (await over.json())["error"] == "overloaded"
        gate.set()
        results = await asyncio.gather(*tasks)
        assert [r.status for r in results] == [200] * 4
        assert engine.max_active == 2                           # параллелизм не выше STT_WORKERS
    finally:
        gate.set()
        await c.close()


async def test_zero_queue_rejects_when_busy():
    gate = threading.Event()
    c = await start(FakeEngine(gate=gate), workers=1, queue=0, timeout_s=20)
    svc = c.app[STT_KEY]
    try:
        t = asyncio.create_task(c.post("/v1/transcribe", data=pcm(1)))
        for _ in range(200):
            if svc.running == 1:
                break
            await asyncio.sleep(0.02)
        assert (await c.post("/v1/transcribe", data=pcm(1))).status == 503
        gate.set()
        assert (await t).status == 200
    finally:
        gate.set()
        await c.close()


async def test_timeout_504_and_worker_slot_kept_until_thread_done():
    gate = threading.Event()
    engine = FakeEngine(gate=gate)
    c = await start(engine, workers=1, queue=4, timeout_s=1.0)
    svc = c.app[STT_KEY]
    try:
        r = await c.post("/v1/transcribe", data=pcm(1))
        assert r.status == 504 and (await r.json())["error"] == "timeout"
        assert svc.running == 1                                 # поток ещё занят: слот не освобождён раньше времени
        gate.set()
        for _ in range(200):
            if svc.running == 0:
                break
            await asyncio.sleep(0.02)
        assert svc.running == 0
        assert (await c.post("/v1/transcribe", data=pcm(1))).status == 200
    finally:
        gate.set()
        await c.close()
