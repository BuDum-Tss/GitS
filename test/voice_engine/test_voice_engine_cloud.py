"""Облачный синтез (OpenRouter) с локальным запасным: запрос, формат ответа, ошибки, предохранитель, бюджет, приватность, секреты."""
import asyncio
import base64
import json
import logging

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from services.voice_engine import cloud_tts as C
from services.voice_engine.config import EngineConfig
from services.voice_engine.engine import VoiceEngine
from services.voice_engine.session import Capabilities
from services.voice_engine.stt import FakeStt
from services.voice_engine.testing import ClockTts, FakeClock, RecordingTransport
from services.voice_engine.tts import FakeTts, HttpTtsClient, TtsBadAudio, TtsServerError, TtsSettings, TtsTimeout, TtsUnavailable
from services.voice_engine.wiring import VoiceService

KEY = "sk-or-v1-СЕКРЕТНЫЙКЛЮЧ1234567890"
REF = b"RIFF....WAVEfmt  fake reference bytes" * 20


def sine_pcm(rate, ms=1000, channels=1, freq=500):
    t = np.arange(rate * ms // 1000) / rate
    mono = (np.sin(2 * np.pi * freq * t) * 0.4 * 32767).astype("<i2")
    return (np.repeat(mono, channels) if channels == 2 else mono).tobytes()


@pytest.fixture
def ref(tmp_path):
    wav = tmp_path / "ref.wav"
    wav.write_bytes(REF)
    return TtsSettings(ref_audio=str(wav), ref_text="дословный текст референса")


class Cloud:
    """Тестовый «OpenRouter»: записывает запросы, отвечает заданным образом."""

    def __init__(self, handler=None):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.handler = handler or self.ok
        app = web.Application()
        app.router.add_post("/audio/speech", self._route)
        self.server = TestServer(app)

    async def _route(self, request):
        self.requests.append(await request.json())
        self.headers.append(dict(request.headers))
        return await self.handler(request)

    async def ok(self, request):
        return web.Response(body=sine_pcm(44100), headers={"Content-Type": "audio/pcm;rate=44100;channels=1", "X-Generation-Id": "g1"})

    async def start(self):
        await self.server.start_server()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def client(self, settings, **kw):
        return C.OpenRouterTtsClient(KEY, settings, base_url=self.base, **kw)


@pytest.fixture
async def cloud():
    c = Cloud()
    await c.start()
    yield c
    await c.server.close()


async def test_request_shape_and_pcm_16k_result(cloud, ref):
    client = cloud.client(ref)
    pcm = await client.synthesize("**Привет**, мир")
    await client.aclose()
    body = cloud.requests[0]
    assert body["model"] == "fish-audio/s2.1-pro" and body["input"] == "Привет, мир" and body["response_format"] == "pcm"
    audio, text = body["input_references"]
    assert audio["type"] == "input_audio" and text == {"type": "text", "text": "дословный текст референса"}
    uri = audio["input_audio"]["data"]
    assert uri.startswith("data:audio/wav;base64,") and base64.b64decode(uri.split(",", 1)[1]) == REF
    assert cloud.headers[0]["Authorization"] == f"Bearer {KEY}" and cloud.headers[0]["Content-Type"] == "application/json"
    assert KEY not in json.dumps(body)                                   # ключ только в заголовке
    assert abs(len(pcm) - 32000) <= 8                                    # 1 с 44.1 кГц -> 16 кГц моно


@pytest.mark.parametrize("rate,channels", [(44100, 1), (24000, 1), (48000, 2)])
async def test_rate_and_channels_read_from_header(ref, rate, channels):
    async def handler(request):
        return web.Response(body=sine_pcm(rate, 1000, channels), headers={"Content-Type": f"audio/pcm;rate={rate};channels={channels}"})

    c = Cloud(handler)
    await c.start()
    client = c.client(ref)
    pcm = await client.synthesize("тест")
    await client.aclose()
    await c.server.close()
    assert abs(len(pcm) - 32000) <= 8
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    assert abs(np.argmax(np.abs(np.fft.rfft(x * np.hanning(len(x))))) - 500) <= 2             # 500 Гц, 1 с


async def test_tempo_shortens_cloud_audio(cloud, ref):
    client = cloud.client(ref, tempo=1.25)
    pcm = await client.synthesize("тест")
    await client.aclose()
    assert abs(len(pcm) / 32000 - 0.8) < 0.03


async def test_chunked_body_with_pauses_is_read_entirely(ref):
    data = sine_pcm(44100, 1000)

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "audio/pcm;rate=44100;channels=1"})
        await resp.prepare(request)
        for i in range(0, len(data), 5000):
            await resp.write(data[i:i + 5000])
            await asyncio.sleep(0.01)
        return resp

    c = Cloud(handler)
    await c.start()
    client = c.client(ref)
    pcm = await client.synthesize("тест")
    await client.aclose()
    await c.server.close()
    assert abs(len(pcm) - 32000) <= 8


@pytest.mark.parametrize("status", [402, 404, 429, 500])
async def test_http_errors_are_typed_with_status(ref, status):
    async def handler(request):
        return web.Response(status=status, text=f"ошибка {KEY} Bearer {KEY}")

    c = Cloud(handler)
    await c.start()
    client = c.client(ref)
    with pytest.raises(TtsServerError) as err:
        await client.synthesize("тест")
    await client.aclose()
    await c.server.close()
    assert err.value.status == status and KEY not in str(err.value)


async def test_timeout_unavailable_and_bad_audio(ref):
    async def slow(request):
        await asyncio.sleep(2)

    async def drop(request):
        request.transport.close()
        await asyncio.sleep(1)

    async def json_ok(request):
        return web.json_response({"oops": 1})

    async def no_rate(request):
        return web.Response(body=b"\x00\x00" * 100, headers={"Content-Type": "audio/pcm"})

    async def empty(request):
        return web.Response(body=b"", headers={"Content-Type": "audio/pcm;rate=44100;channels=1"})

    for handler, kind in ((slow, TtsTimeout), (drop, TtsUnavailable), (json_ok, TtsBadAudio), (no_rate, TtsBadAudio), (empty, TtsBadAudio)):
        c = Cloud(handler)
        await c.start()
        client = c.client(ref, timeout_s=0.3)
        with pytest.raises(kind):
            await client.synthesize("тест")
        await client.aclose()
        await c.server.close()


def test_parse_pcm_format():
    assert C.parse_pcm_format("audio/pcm;rate=44100;channels=1") == (44100, 1)
    assert C.parse_pcm_format("audio/pcm; rate=24000") == (24000, 1)
    for bad in ("application/json", "audio/pcm;rate=5", "audio/pcm;rate=44100;channels=6"):
        with pytest.raises(TtsBadAudio):
            C.parse_pcm_format(bad)


# ── запасной локальный, предохранитель, бюджет, приватность ─────────────────────────

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def fallback(primary=None, local=None, *, daily=200000, leaks=None, clock=None, path=None, now=None):
    budget = C.CloudBudget(path, daily, 0.000015, now=now or (lambda: 1_700_000_000.0))
    return C.FallbackTtsClient(primary or FakeTts(), local or FakeTts(ms_per_char=20), budget=budget, leaks=leaks, clock=clock or Clock())


async def test_cloud_error_falls_back_to_local_for_that_sentence(cloud, ref):
    async def too_many(request):
        return web.Response(status=429, text="rate limit")

    cloud.handler = too_many
    local = FakeTts(ms_per_char=20)
    fb = fallback(cloud.client(ref), local)
    pcm, who = await fb.synthesize_ex("Привет, мир")
    assert who == "local" and pcm and local.calls == ["Привет, мир"]
    st = fb.state()
    assert st["fallbacks"] == 1 and st["local"] == 1 and st["cloud"] == 0 and "TtsServerError 429" in st["last_error"]
    await fb.aclose()


async def test_cloud_success_counts_and_reports_openrouter():
    fb = fallback()
    pcm, who = await fb.synthesize_ex("Привет, мир")
    assert who == "openrouter" and fb.state()["cloud"] == 1 and fb.state()["chars_today"] == len("Привет, мир")
    assert fb.state()["usd_today"] == round(len("Привет, мир") * 0.000015, 4)


async def test_breaker_two_failures_disable_cloud_then_recover():
    clock = Clock()
    primary = FakeTts(fail=TtsTimeout("долго"))
    fb = fallback(primary, clock=clock)
    for _ in range(4):
        assert (await fb.synthesize_ex("предложение"))[1] == "local"
    assert len(primary.calls) == 2                                   # после двух неудач облако не опрашивается
    assert "предохранитель" in fb.disabled_reason() and not fb.cloud_active
    clock.t += 59
    await fb.synthesize_ex("ещё")
    assert len(primary.calls) == 2
    clock.t += 2                                                     # 60 с прошло: одна проба
    await fb.synthesize_ex("проба")
    assert len(primary.calls) == 3 and fb.backoff_s == 120           # неудача пробы: пауза вдвое дольше
    clock.t += 121
    primary.fail = None
    assert (await fb.synthesize_ex("снова"))[1] == "openrouter"
    assert fb.disabled_reason() == "" and fb.failures == 0 and fb.backoff_s == 0 and fb.cloud_active


async def test_breaker_backoff_is_capped():
    clock = Clock()
    fb = fallback(FakeTts(fail=TtsTimeout("долго")), clock=clock)
    for _ in range(12):
        clock.t += 1000
        await fb.synthesize_ex("предложение")
    assert fb.backoff_s == C.BREAKER_MAX_S


async def test_daily_limit_switches_to_local_and_counter_survives_restart(tmp_path):
    path = tmp_path / "usage.json"
    day = [1_700_000_000.0]
    fb = fallback(daily=30, path=path, now=lambda: day[0])
    assert (await fb.synthesize_ex("а" * 20))[1] == "openrouter"
    assert (await fb.synthesize_ex("б" * 20))[1] == "openrouter"       # 40 >= 30: лимит достигнут
    assert (await fb.synthesize_ex("в" * 20))[1] == "local" and "лимит" in fb.disabled_reason()
    assert json.loads(path.read_text(encoding="utf-8"))["chars"] == 40
    again = C.CloudBudget(path, 30, 0.000015, now=lambda: day[0])      # перезапуск ядра в тот же день
    assert again.chars == 40 and again.exhausted()
    day[0] += 86400                                                    # после 00:00 UTC счёт заново
    assert not again.exhausted() and not fb.budget.exhausted() and fb.budget.chars == 0
    assert (await fb.synthesize_ex("г" * 20))[1] == "openrouter"
    unlimited = C.CloudBudget(None, 0, 0.1)
    await unlimited.add(10 ** 7)
    assert not unlimited.exhausted()


async def test_corrupt_usage_file_is_ignored(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("{не json", encoding="utf-8")
    assert C.CloudBudget(path, 100, 0.1).chars == 0


@pytest.mark.parametrize("sync", [True, False])
async def test_private_words_never_go_to_cloud(sync):
    primary = FakeTts()

    def leaks_sync(text):
        return "аня" in text.lower()

    async def leaks_async(text):
        return leaks_sync(text)

    fb = fallback(primary, leaks=leaks_sync if sync else leaks_async)
    assert (await fb.synthesize_ex("Привет, Аня, как дела"))[1] == "local"
    assert primary.calls == [] and fb.state()["privacy_local"] == 1 and fb.budget.chars == 0
    assert (await fb.synthesize_ex("Просто привет"))[1] == "openrouter" and primary.calls == ["Просто привет"]


async def test_broken_privacy_filter_means_local():
    def boom(text):
        raise RuntimeError("нет данных")

    primary = FakeTts()
    fb = fallback(primary, leaks=boom)
    assert (await fb.synthesize_ex("Привет"))[1] == "local" and primary.calls == []


# ── провайдеры, состояние, секреты ──────────────────────────────────────────────

def build(environ, **kw):
    async def submit(peer, event):
        pass

    base = {"GITS_STT_URL": "http://127.0.0.1:1", "GITS_TTS_URL": "http://127.0.0.1:2"}
    return VoiceService.build({**base, **environ}, submit=submit, token=lambda: "t", allow=lambda r: True, names=lambda: (),
                              journal=lambda *a: None, **kw)


async def test_provider_modes(tmp_path):
    svc = build({"OPENROUTER_API_KEY": KEY, "GITS_TTS_PROVIDER": "local"})
    assert isinstance(svc.engine.tts, HttpTtsClient)                      # local: облака нет совсем
    await svc.stop()
    svc = build({})
    assert isinstance(svc.engine.tts, HttpTtsClient)                      # auto без ключа
    await svc.stop()
    svc = build({"GITS_TTS_PROVIDER": "openrouter"})
    assert isinstance(svc.engine.tts, HttpTtsClient)                      # openrouter без ключа: локально, не падаем
    await svc.stop()
    for mode in ("auto", "openrouter"):
        svc = build({"OPENROUTER_API_KEY": KEY, "GITS_TTS_PROVIDER": mode}, memory_dir=tmp_path)
        assert isinstance(svc.engine.tts, C.FallbackTtsClient) and svc.engine.tts.provider == mode
        await svc.stop()


async def test_local_speed_takes_tempo_and_cloud_gets_it_too(tmp_path):
    svc = build({}, memory_dir=tmp_path)
    assert svc.engine.tts.settings.speed == pytest.approx(1.08)
    await svc.stop()
    svc = build({"GITS_TTS_TEMPO": "1.0", "GITS_TTS_SPEED": "1.2"})
    assert svc.engine.tts.settings.speed == pytest.approx(1.2)
    await svc.stop()
    svc = build({"OPENROUTER_API_KEY": KEY, "GITS_TTS_PROVIDER": "auto", "GITS_TTS_TEMPO": "1.2"}, memory_dir=tmp_path)
    assert svc.engine.tts.primary.tempo == 1.2 and svc.engine.tts.fallback.settings.speed == pytest.approx(1.2)
    await svc.stop()
    assert EngineConfig.from_env({"GITS_TTS_TEMPO": "9"}).tts_tempo == 1.5 and EngineConfig.from_env({}).tts_tempo == 1.08


def test_cloud_config_defaults_and_env():
    d = EngineConfig.from_env({})
    assert (d.tts_provider, d.openrouter_model, d.cloud_daily_chars, d.cloud_usd_per_char, d.tts_first_max_cloud) == (
        "local", "fish-audio/s2.1-pro", 200000, 0.000015, 45)     # облако только по явному выбору (приватность, деньги, сеть)
    assert d.cloud_connect_timeout_s == 5 and d.cloud_timeout_s == 12 and not d.has_openrouter_key
    cfg = EngineConfig.from_env({"OPENROUTER_API_KEY": KEY, "GITS_TTS_PROVIDER": "bogus", "GITS_TTS_CLOUD_DAILY_CHARS": "0",
                                 "GITS_TTS_OPENROUTER_MODEL": "x/y", "GITS_TTS_FIRST_MAX_CLOUD": "60"})
    assert cfg.has_openrouter_key and cfg.tts_provider == "local" and cfg.cloud_daily_chars == 0      # неизвестное значение = локально
    assert cfg.openrouter_model == "x/y" and cfg.tts_first_max_cloud == 60
    assert KEY not in json.dumps(cfg.public())


async def test_state_in_api_and_key_never_visible(tmp_path, caplog, ref):
    caplog.set_level(logging.DEBUG)

    async def leaky(request):
        return web.Response(status=401, text=f"bad key {KEY}")

    c = Cloud(leaky)
    await c.start()
    svc = build({"OPENROUTER_API_KEY": KEY, "GITS_TTS_PROVIDER": "auto"}, memory_dir=tmp_path)
    fb = svc.engine.tts
    fb.primary.url, fb.primary.settings = f"{c.base}/audio/speech", ref
    fb.fallback = FakeTts()
    await fb.synthesize_ex("Привет, мир")
    snap = json.dumps(svc.state(), ensure_ascii=False)
    assert KEY not in snap and KEY not in caplog.text and "ключ" not in fb.last_error
    assert svc.state()["tts"]["provider"] == "auto" and svc.state()["tts"]["fallbacks"] == 1
    assert (tmp_path / C.USAGE_FILE).exists() and KEY not in (tmp_path / C.USAGE_FILE).read_text(encoding="utf-8")
    assert KEY not in repr(fb.primary.settings)
    await svc.stop()
    await c.server.close()


def test_scrub():
    assert KEY not in C.scrub(f"ошибка {KEY}", (KEY,)) and "***" in C.scrub("Authorization: Bearer abc.def-123")
    assert "sk-or-v1-abcdef123456" not in C.scrub("токен sk-or-v1-abcdef123456 утёк")


async def test_local_engine_state_when_no_cloud():
    eng = VoiceEngine(stt=FakeStt(), tts=FakeTts())
    assert eng.snapshot()["tts"] == {"provider": "local", "active": "local"}


# ── разбиение и пометка провайдера в памяти ─────────────────────────────────────────

TEXT_88 = "Ну смотри, это довольно длинное первое предложение, которое тянется без конца и края."


async def make_engine(tts, journal):
    clock = FakeClock()
    eng = VoiceEngine(stt=FakeStt(), tts=tts(clock), config=EngineConfig(), clock=clock, journal=lambda k, t, d: journal.append(d))
    await eng.open_session("s1", "fake", RecordingTransport(clock), Capabilities(echo_free=True, can_barge_in=True))
    return eng, clock


async def test_first_chunk_is_shorter_for_cloud_than_for_local():
    journal: list[dict] = []
    eng, clock = await make_engine(lambda c: C.FallbackTtsClient(ClockTts(c, latency_s=0.1), ClockTts(c, latency_s=0.1),
                                                                 budget=C.CloudBudget(None, 0, 0.1), clock=c.monotonic), journal)
    eng.speak("s1", TEXT_88)
    await clock.advance(0.3)
    assert eng.tts.primary.calls[0] == "Ну смотри, это довольно длинное первое предложение,"     # облако: граница 45
    await eng.aclose()
    eng, clock = await make_engine(lambda c: ClockTts(c, latency_s=0.1), journal)
    eng.speak("s1", TEXT_88)
    await clock.advance(0.3)
    assert eng.tts.calls[0] == TEXT_88                                                              # локально: до 90 не режется
    await eng.aclose()


async def test_spoken_event_marks_provider():
    for fail, expect in ((None, "openrouter"), (TtsServerError(500), "local")):
        journal: list[dict] = []
        eng, clock = await make_engine(lambda c, f=fail: C.FallbackTtsClient(
            ClockTts(c, latency_s=0.1, fail_on=("а",) if f else ()), ClockTts(c, latency_s=0.1),
            budget=C.CloudBudget(None, 0, 0.1), clock=c.monotonic), journal)
        ticket = eng.speak("s1", "Привет, это проверка голоса.")
        await clock.advance(5.0)
        eng.ack("s1", ticket.uid, 1500, "completed")
        await clock.advance(0.1)
        assert journal and journal[-1]["tts"] == expect
        await eng.aclose()
