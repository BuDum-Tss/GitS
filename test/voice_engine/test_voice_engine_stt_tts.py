"""Клиенты моделей против локального тестового HTTP-сервера: успех, 500, таймаут, мусор, недоступность."""
import asyncio
import io
import json
import struct
import wave

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from services.voice_engine import stt as S
from services.voice_engine import tts as T

PCM = b"\x10\x00" * 16000       # 1 с


async def serve(handlers: dict):
    app = web.Application()
    for (method, path), handler in handlers.items():
        app.router.add_route(method, path, handler)
    server = TestServer(app)
    await server.start_server()
    return server


def url(server) -> str:
    return f"http://127.0.0.1:{server.port}"


async def test_stt_ok_and_request_shape():
    seen = {}

    async def transcribe(request):
        seen["lang"], seen["ctype"], seen["len"] = request.query.get("lang"), request.content_type, len(await request.read())
        return web.json_response({"text": " Привет, мир ", "language": "ru", "duration_ms": 1000})

    server = await serve({("POST", "/v1/transcribe"): transcribe})
    client = S.HttpSttClient(url(server), timeout_s=2)
    try:
        res = await client.transcribe(PCM)
    finally:
        await client.aclose()
        await server.close()
    assert res == S.Transcript("Привет, мир", "ru", 1000)
    assert seen == {"lang": "ru", "ctype": "application/octet-stream", "len": len(PCM)}


async def test_stt_server_error_is_typed():
    async def boom(request):
        return web.Response(status=500, text="модель упала")

    server = await serve({("POST", "/v1/transcribe"): boom})
    client = S.HttpSttClient(url(server), timeout_s=2)
    try:
        with pytest.raises(S.SttServerError) as err:
            await client.transcribe(PCM)
    finally:
        await client.aclose()
        await server.close()
    assert err.value.status == 500


async def test_stt_timeout_is_typed_and_no_retry():
    calls = []

    async def slow(request):
        calls.append(1)
        await asyncio.sleep(2)
        return web.json_response({"text": "поздно"})

    server = await serve({("POST", "/v1/transcribe"): slow})
    client = S.HttpSttClient(url(server), timeout_s=0.2)
    try:
        with pytest.raises(S.SttTimeout):
            await client.transcribe(PCM)
    finally:
        await client.aclose()
        await server.close()
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"not json", json.dumps({"nope": 1}).encode(), json.dumps({"text": 5}).encode()])
async def test_stt_bad_response(body):
    async def bad(request):
        return web.Response(body=body, content_type="application/json")

    server = await serve({("POST", "/v1/transcribe"): bad})
    client = S.HttpSttClient(url(server), timeout_s=2)
    try:
        with pytest.raises(S.SttBadResponse):
            await client.transcribe(PCM)
    finally:
        await client.aclose()
        await server.close()


async def drop(request):
    request.transport.close()                       # сервис оборвал соединение (упал посреди запроса)
    await asyncio.sleep(1)


async def test_stt_unavailable_and_input_checks():
    server = await serve({("POST", "/v1/transcribe"): drop})
    client = S.HttpSttClient(url(server), timeout_s=2)
    try:
        with pytest.raises(S.SttUnavailable):
            await client.transcribe(PCM)
        with pytest.raises(S.SttError):
            await client.transcribe(b"")
    finally:
        await client.aclose()
        await server.close()


def make_wav(rate=24000, ms=500, freq=800, float_fmt=False, channels=1) -> bytes:
    t = np.arange(rate * ms // 1000) / rate
    sig = np.sin(2 * np.pi * freq * t) * 0.5
    if float_fmt:
        data = sig.astype("<f4")
        fmt_tag, bits = 3, 32
    else:
        data = (sig * 32767).astype("<i2")
        fmt_tag, bits = 1, 16
    if channels == 2:
        data = np.repeat(data, 2)
    body = data.tobytes()
    header = (b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, fmt_tag, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
        + b"data" + struct.pack("<I", len(body)))
    return header + body


@pytest.mark.parametrize("kwargs", [{}, {"float_fmt": True}, {"channels": 2}, {"rate": 16000}, {"rate": 44100}])
def test_wav_to_pcm16k(kwargs):
    pcm = T.wav_to_pcm16k(make_wav(**kwargs))
    assert abs(len(pcm) - 16000) <= 4
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    assert 0.2 * 32767 < np.abs(x).max() < 0.6 * 32767


def test_wav_from_stdlib_writer():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x10" * 2400)
    assert abs(len(T.wav_to_pcm16k(buf.getvalue())) - 3200) <= 4


@pytest.mark.parametrize("data", [b"", b"not wav at all" * 5, b"RIFF" + bytes(40)])
def test_bad_wav(data):
    with pytest.raises(T.TtsBadAudio):
        T.decode_wav(data)


async def test_tts_client_posts_reference_and_resamples(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(make_wav(ms=100))
    seen = {}

    async def synth(request):
        seen.update(await request.json())
        return web.Response(body=make_wav(24000, 500), content_type="audio/wav")

    server = await serve({("POST", "/tts"): synth})
    client = T.HttpTtsClient(T.TtsSettings(url=url(server), ref_audio=str(ref), ref_text="текст", speed=0.9, voice="v", timeout_s=3))
    try:
        pcm = await client.synthesize("**Привет**, мир")
    finally:
        await client.aclose()
        await server.close()
    assert abs(len(pcm) - 16000) <= 4
    assert seen["text"] == "Привет, мир" and seen["ref_text"] == "текст" and seen["speed"] == 0.9 and seen["voice"] == "v"
    assert len(seen["ref_audio_b64"]) > 100


async def test_tts_errors_are_typed(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(make_wav())
    async def boom(request):
        return web.Response(status=503)

    async def slow(request):
        await asyncio.sleep(2)
        return web.Response(body=make_wav())

    server = await serve({("POST", "/tts"): boom})
    c = T.HttpTtsClient(T.TtsSettings(url=url(server), ref_audio=str(ref), ref_text="т", timeout_s=1))
    with pytest.raises(T.TtsServerError):
        await c.synthesize("привет")
    with pytest.raises(T.TtsError):
        await c.synthesize("   ")
    await c.aclose()
    await server.close()
    server = await serve({("POST", "/tts"): slow})
    c = T.HttpTtsClient(T.TtsSettings(url=url(server), ref_audio=str(ref), ref_text="т", timeout_s=0.2))
    with pytest.raises(T.TtsTimeout):
        await c.synthesize("привет")
    await c.aclose()
    await server.close()
    server = await serve({("POST", "/tts"): drop})
    c = T.HttpTtsClient(T.TtsSettings(url=url(server), ref_audio=str(ref), ref_text="т", timeout_s=2))
    with pytest.raises(T.TtsUnavailable):
        await c.synthesize("привет")
    await c.aclose()
    await server.close()


async def test_reference_comes_from_the_character_resolver_and_missing_reference_is_a_config_error(tmp_path):
    """Референс по умолчанию берётся у активного персонажа (резолвер ядра); без него ESpeech не говорит, и это честная ошибка."""
    ref = tmp_path / "espeech_ref.wav"
    ref.write_bytes(make_wav())
    seen = {}

    async def ok(request):
        seen.update(await request.json())
        return web.Response(body=make_wav())

    server = await serve({("POST", "/tts"): ok})
    c = T.HttpTtsClient(T.TtsSettings(url=url(server), ref_resolver=lambda: (ref, "дословный текст"), timeout_s=3))
    await c.synthesize("привет")
    assert seen["ref_text"] == "дословный текст" and len(seen["ref_audio_b64"]) > 100
    await c.aclose()
    c = T.HttpTtsClient(T.TtsSettings(url=url(server), ref_resolver=lambda: None, timeout_s=3))
    with pytest.raises(T.TtsError, match="референса"):                 # не TtsServerError: до сервиса дело не доходит
        await c.synthesize("привет")
    await c.aclose()
    await server.close()


async def test_tts_body_arriving_in_several_network_chunks_is_read_completely(tmp_path):
    """Регрессия: `content.read(n)` возвращал только первую пришедшую часть ответа, и реплика озвучивалась обрывком (0,15..2 с
    вместо 3 с). Звук приходит кусками с паузами, как по реальной сети: клиент обязан дочитать тело до конца."""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(make_wav())
    wav = make_wav(ms=3000)

    async def chunked(request):
        resp = web.StreamResponse(headers={"Content-Type": "audio/wav"})
        await resp.prepare(request)
        for i in range(0, len(wav), 9000):
            await resp.write(wav[i:i + 9000])
            await asyncio.sleep(0.02)                              # следующая часть приходит позже: read() вернул бы только первую
        await resp.write_eof()
        return resp

    server = await serve({("POST", "/tts"): chunked})
    c = T.HttpTtsClient(T.TtsSettings(url=url(server), ref_audio=str(ref), ref_text="т", timeout_s=5))
    pcm = await c.synthesize("привет")
    assert len(pcm) == 3000 * 32                                   # 3 с при 16 кГц моно s16: целиком, а не первый кусок
    await c.aclose()
    await server.close()


def test_clean_text():
    assert T.clean_text("# Заголовок\n**жирный** `код` [ссылка](http://x)") == "Заголовок жирный ссылка"
    long = "Раз. " * 400
    assert len(T.clean_text(long, 100)) <= 100 and T.clean_text(long, 100).endswith(".")
