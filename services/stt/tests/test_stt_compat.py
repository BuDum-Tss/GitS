"""Совместимость с клиентом ядра: приложение STT на FakeEngine поднято на локальном порту, к нему идёт НАСТОЯЩИЙ HttpSttClient
(agent/services/voice_engine/stt.py). Ловит расхождения контракта: путь, параметры, тело, формат ответа, типы ошибок."""
import asyncio
import threading

import numpy as np
import pytest
from aiohttp.test_utils import TestServer

from services.voice_engine.stt import (HttpSttClient, SttError, SttServerError, SttTimeout, SttUnavailable, Transcript)
from stt.app import STT_KEY, create_app
from stt.config import SttConfig
from stt.engine import FakeEngine


def pcm(seconds: float) -> bytes:
    return (np.ones(int(16000 * seconds), dtype="<i2") * 1000).tobytes()


async def serve(engine, **cfg) -> TestServer:
    cfg.setdefault("warmup", False)
    server = TestServer(create_app(SttConfig(**cfg), engine))
    await server.start_server()
    await asyncio.wait_for(server.app[STT_KEY].ready.wait(), 5)
    return server


@pytest.fixture
async def stack():
    server = await serve(FakeEngine("включи свет"))
    client = HttpSttClient(f"http://127.0.0.1:{server.port}", timeout_s=5.0)
    yield server, client
    await client.aclose()
    await server.close()


async def test_client_gets_transcript(stack):
    _, client = stack
    result = await client.transcribe(pcm(1.5))
    assert result == Transcript(text="включи свет", language="ru", duration_ms=1500)


async def test_client_language_param(stack):
    server, client = stack
    result = await client.transcribe(pcm(1.0), lang="en")
    assert result.language == "en"
    assert server.app[STT_KEY].engine.calls == [(16000, "en")]


async def test_too_short_is_400_server_error(stack):
    _, client = stack
    with pytest.raises(SttServerError) as e:
        await client.transcribe(pcm(0.1))
    assert e.value.status == 400


async def test_overload_is_503_server_error():
    gate = threading.Event()
    server = await serve(FakeEngine(gate=gate), workers=1, queue=0, timeout_s=20)
    client = HttpSttClient(f"http://127.0.0.1:{server.port}", timeout_s=10.0)
    try:
        busy = asyncio.create_task(client.transcribe(pcm(1)))
        for _ in range(200):
            if server.app[STT_KEY].running == 1:
                break
            await asyncio.sleep(0.02)
        with pytest.raises(SttServerError) as e:
            await client.transcribe(pcm(1))
        assert e.value.status == 503
        gate.set()
        assert (await busy).text == "привет"
    finally:
        gate.set()
        await client.aclose()
        await server.close()


async def test_model_not_loaded_is_503():
    server = await serve(FakeEngine("x"))
    server.app[STT_KEY].ready.clear()                      # как будто модель ещё грузится
    client = HttpSttClient(f"http://127.0.0.1:{server.port}", timeout_s=5.0)
    try:
        with pytest.raises(SttServerError) as e:
            await client.transcribe(pcm(1))
        assert e.value.status == 503
    finally:
        await client.aclose()
        await server.close()


async def test_client_timeout_is_typed():
    gate = threading.Event()
    server = await serve(FakeEngine(gate=gate), timeout_s=20)
    client = HttpSttClient(f"http://127.0.0.1:{server.port}", timeout_s=0.3)
    try:
        with pytest.raises(SttTimeout):
            await client.transcribe(pcm(1))
    finally:
        gate.set()
        await client.aclose()
        await server.close()


async def test_server_timeout_504_is_server_error():
    gate = threading.Event()
    server = await serve(FakeEngine(gate=gate), timeout_s=1.0)
    client = HttpSttClient(f"http://127.0.0.1:{server.port}", timeout_s=10.0)
    try:
        with pytest.raises(SttServerError) as e:
            await client.transcribe(pcm(1))
        assert e.value.status == 504
    finally:
        gate.set()
        await client.aclose()
        await server.close()


async def test_unreachable_service_is_unavailable():
    """Сервис оборвал соединение (закрытый порт на Windows отвечает с задержкой, поэтому обрыв имитируем сами)."""
    async def drop(reader, writer):
        writer.close()
    srv = await asyncio.start_server(drop, "127.0.0.1", 0)
    client = HttpSttClient(f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}", timeout_s=5.0)
    try:
        with pytest.raises(SttUnavailable):
            await client.transcribe(pcm(1))
    finally:
        await client.aclose()
        srv.close()
        await srv.wait_closed()


async def test_client_side_limit_matches_server():
    """Клиент сам не шлёт звук длиннее минуты: лимит клиента (MAX_STT_BYTES) не больше лимита сервера."""
    from services.voice_engine.stt import MAX_STT_BYTES
    from stt.config import MAX_BODY_BYTES
    assert MAX_STT_BYTES <= MAX_BODY_BYTES
    client = HttpSttClient("http://127.0.0.1:9")
    with pytest.raises(SttError):
        await client.transcribe(b"\x00" * (MAX_STT_BYTES + 2))
