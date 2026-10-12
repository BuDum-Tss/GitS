"""Запасной распознаватель микрофона (ТЗ 12): WhisperX-сервер ↔ локальная модель на процессоре. Без сети, без реальной модели.

Сервер подменён httpx.MockTransport, модель faster-whisper подменена заглушкой (LocalWhisper(loader=...)), часы подменены.
"""
import asyncio
import sys
import types
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

import app
import hearing as h
import local_stt
import mic_listener as ml
from local_stt import LocalSTTUnavailable, LocalWhisper
from stt_client import LABEL_LOCAL, LABEL_SERVER, MODE_LOCAL, MODE_SERVER, STTClient
from voice_id import VoiceIdentity
from voice_rig import Rig

PCM = np.zeros(1600, dtype=np.int16).tobytes()
AUDIO = np.zeros(16000, dtype=np.float32)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def seg(text, no_speech=0.0, logprob=-0.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob)


class FakeModel:
    """Заглушка faster-whisper: отдаёт заранее заданные сегменты и помнит вызовы."""

    def __init__(self, segments=None):
        self.segments = segments if segments is not None else [seg("привет из локальной модели")]
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append((len(audio), kw))
        return list(self.segments), None


def make_engine(model=None, fail=False, clock=None):
    """LocalWhisper со считающим загрузчиком: fail=True имитирует «весов нет в кэше»."""
    calls = {"n": 0, "args": []}

    def loader(name, threads):
        calls["n"] += 1
        calls["args"].append((name, threads))
        if fail:
            raise OSError("LocalEntryNotFoundError: весов нет в кэше")
        return model if model is not None else FakeModel()

    eng = LocalWhisper("small", threads=2, loader=loader, clock=clock or Clock())
    return eng, calls


def server_state(up=True, status=None):
    return {"up": up, "status": status, "posts": 0}


def server(st):
    """Сервер WhisperX в памяти: st['up'] - доступен ли; st['status'] - HTTP-код ответа на распознавание."""

    def handler(request):
        if not st["up"]:
            raise httpx.ConnectError("All connection attempts failed", request=request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v1/audio/transcriptions":
            st["posts"] += 1
            if st["status"]:
                return httpx.Response(st["status"], text="ошибка сервера")
            return httpx.Response(200, json={
                "text": " привет из сервера ",
                "segments": [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0}],
                "speaker_embeddings": {"SPEAKER_00": [0.1, 0.2, 0.3]},
            })
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def make_client(st, eng, clock, seen=None):
    return STTClient(transport=server(st), local=eng, clock=clock,
                     on_change=(seen.append if seen is not None else None))


# ── переключение сервер / запасной ───────────────────────────────────────────

async def test_server_up_is_used_with_diarization_and_local_model_stays_unloaded():
    st, clock, seen = server_state(), Clock(), []
    eng, calls = make_engine(clock=clock)
    client = make_client(st, eng, clock, seen)

    t = await client.transcribe(PCM)

    assert t.text == "привет из сервера" and t.speaker == "SPEAKER_00" and t.speaker_embedding == [0.1, 0.2, 0.3]
    assert client.mode == MODE_SERVER and client.diarized and client.label() == LABEL_SERVER
    assert calls["n"] == 0                                   # модель не грузили: сервер отвечает
    assert [s["mode"] for s in seen] == [MODE_SERVER]


async def test_server_down_at_start_switches_to_local_and_says_so_once():
    st, clock, seen = server_state(up=False), Clock(), []
    eng, calls = make_engine(clock=clock)
    client = make_client(st, eng, clock, seen)

    t1 = await client.transcribe(PCM)
    t2 = await client.transcribe(PCM)

    assert t1.text == t2.text == "привет из локальной модели"
    assert t1.speaker is None and t1.speaker_embedding is None      # без диаризации и голосовых отпечатков
    assert client.mode == MODE_LOCAL and not client.diarized
    assert client.label() == LABEL_LOCAL
    assert st["posts"] == 0                                         # аудио на сервер не уходит, пока он недоступен
    assert calls["n"] == 1                                          # модель открыта один раз и переиспользуется
    assert [s["mode"] for s in seen] == [MODE_LOCAL]                # переход записан один раз


async def test_connection_error_during_utterance_falls_back_for_that_utterance():
    st, clock, seen = server_state(), Clock(), []
    eng, _ = make_engine(clock=clock)
    client = make_client(st, eng, clock, seen)
    assert (await client.transcribe(PCM)).text == "привет из сервера"

    st["up"] = False                                                # сервер упал между фразами
    t = await client.transcribe(PCM)

    assert t.text == "привет из локальной модели"
    assert client.mode == MODE_LOCAL
    assert [s["mode"] for s in seen] == [MODE_SERVER, MODE_LOCAL]


async def test_server_comes_back_after_the_recheck_interval():
    st, clock, seen = server_state(up=False), Clock(), []
    eng, _ = make_engine(clock=clock)
    client = make_client(st, eng, clock, seen)
    await client.probe()
    assert client.mode == MODE_LOCAL

    st["up"] = True
    clock.t += 30                                                   # меньше минуты: сервер не перепроверяем
    assert (await client.transcribe(PCM)).text == "привет из локальной модели"

    clock.t += 31                                                   # минута прошла: проверяем и возвращаемся
    t = await client.transcribe(PCM)
    assert t.text == "привет из сервера" and client.mode == MODE_SERVER
    assert [s["mode"] for s in seen] == [MODE_LOCAL, MODE_SERVER]


async def test_manual_probe_returns_to_server_at_once():
    st, clock = server_state(up=False), Clock()
    eng, _ = make_engine(clock=clock)
    client = make_client(st, eng, clock)
    await client.probe()
    st["up"] = True
    assert await client.probe() == MODE_SERVER and client.diarized


async def test_server_5xx_falls_back_but_4xx_is_an_error_not_a_switch():
    st, clock = server_state(status=503), Clock()
    eng, _ = make_engine(clock=clock)
    client = make_client(st, eng, clock)
    assert (await client.transcribe(PCM)).text == "привет из локальной модели"

    st["status"] = 400
    clock.t += 120
    with pytest.raises(httpx.HTTPStatusError):
        await client.transcribe(PCM)


async def test_unknown_mode_before_any_check_is_probed_on_first_use():
    st, clock = server_state(), Clock()
    client = make_client(st, make_engine(clock=clock)[0], clock)
    assert client.mode == "unknown" and client.label().endswith("ещё не проверено")
    assert (await client.transcribe(PCM)).text == "привет из сервера"


# ── запасная модель: состояние, без скачивания, без повторов ───────────────────

async def test_missing_model_is_reported_not_downloaded_and_retried_only_after_a_minute():
    st, clock, seen = server_state(up=False), Clock(), []
    eng, calls = make_engine(fail=True, clock=clock)
    client = make_client(st, eng, clock, seen)

    with pytest.raises(LocalSTTUnavailable):
        await client.transcribe(PCM)
    with pytest.raises(LocalSTTUnavailable):
        await client.transcribe(PCM)

    assert calls["n"] == 1                                          # повторно не пытаемся, пока не прошла минута
    assert client.status()["model_state"] == "не загружена"
    assert "модель small не загружена" in client.label()
    assert client.short() == "локально, модель не загружена"
    assert [s["model_state"] for s in seen] == ["не загружена"]     # переход в журнал один раз

    clock.t += 61
    with pytest.raises(LocalSTTUnavailable):
        await client.transcribe(PCM)
    assert calls["n"] == 2


async def test_model_appears_later_and_is_used_without_restart():
    st, clock = server_state(up=False), Clock()
    model = FakeModel()
    flags = {"fail": True}

    def loader(name, threads):
        if flags["fail"]:
            raise OSError("весов нет")
        return model

    eng = LocalWhisper("small", threads=2, loader=loader, clock=clock)
    client = make_client(st, eng, clock)
    with pytest.raises(LocalSTTUnavailable):
        await client.transcribe(PCM)

    flags["fail"] = False                                           # владелец скачал веса вручную
    clock.t += 61
    t = await client.transcribe(PCM)
    assert t.text == "привет из локальной модели" and client.label() == LABEL_LOCAL


def test_default_loader_is_cpu_int8_local_only_and_limited_threads(monkeypatch):
    opened = {}

    class WhisperModel:
        def __init__(self, name, **kwargs):
            opened["name"], opened["kwargs"] = name, kwargs

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    local_stt._default_loader("small", 2)

    assert opened["name"] == "small"
    assert opened["kwargs"] == {"device": "cpu", "compute_type": "int8", "cpu_threads": 2, "local_files_only": True}


def test_thread_limit_defaults_to_two_and_follows_env(monkeypatch):
    monkeypatch.delenv("GLADOS_STT_THREADS", raising=False)
    assert local_stt._threads() == 2
    monkeypatch.setenv("GLADOS_STT_THREADS", "4")
    assert local_stt._threads() == 4
    monkeypatch.setenv("GLADOS_STT_THREADS", "0")
    assert local_stt._threads() == 1
    monkeypatch.setenv("GLADOS_STT_THREADS", "много")
    assert local_stt._threads() == 2


def test_junk_and_unsure_segments_are_dropped_like_in_hearing():
    model = FakeModel([seg("хорошая фраза"), seg("Продолжение следует"), seg("тишина", no_speech=0.9),
                       seg("мутно", logprob=-1.5), seg("   ")])
    eng, _ = make_engine(model=model)
    assert eng.transcribe_array(AUDIO) == "хорошая фраза"


def test_local_path_takes_16k_pcm_and_refuses_other_rates():
    eng, _ = make_engine()
    assert eng.transcribe_pcm(PCM) == "привет из локальной модели"
    with pytest.raises(ValueError):
        eng.transcribe_pcm(PCM, sample_rate=8000)


# ── общий экземпляр: слух и микрофон делят одну модель ───────────────────────────

def test_shared_engine_is_one_instance_and_hearing_uses_it(monkeypatch):
    monkeypatch.setattr(local_stt, "_shared", None)
    assert local_stt.shared() is local_stt.shared()
    ear = h.Hearing(lambda: {})
    assert ear._engine is local_stt.shared()


async def test_hearing_and_mic_fallback_load_the_model_once(monkeypatch):
    eng, calls = make_engine()
    ear = h.Hearing(lambda: {}, engine=eng)
    assert ear._transcribe(AUDIO) == "привет из локальной модели"

    clock = Clock()
    client = make_client(server_state(up=False), eng, clock)
    assert (await client.transcribe(PCM)).text == "привет из локальной модели"
    assert calls["n"] == 1


# ── микрофон без голосовых отпечатков ─────────────────────────────────────────

async def test_mic_in_local_mode_reports_utterance_without_speaker_identity(tmp_path):
    clock = Clock()
    client = make_client(server_state(up=False), make_engine(clock=clock)[0], clock)
    voices = VoiceIdentity(str(tmp_path))
    got = []

    async def on_voice(text, info):
        got.append((text, info))

    lis = ml.MicListener(client, voices, on_voice, lambda: False, diarized=lambda: client.diarized)
    await lis.handle_utterance(PCM)

    (text, info), = got
    assert text == "привет из локальной модели"
    assert info["id"] is None and info["name"] is None and info["owner"] is False and info["has_owner"] is False


async def test_self_voice_is_not_registered_without_diarization(tmp_path):
    clock = Clock()
    client = make_client(server_state(up=False), make_engine(clock=clock)[0], clock)
    voices = VoiceIdentity(str(tmp_path))
    samples = []

    async def make_sample():
        samples.append(1)
        return PCM

    lis = ml.MicListener(client, voices, lambda t, i: None, lambda: False, make_self_sample=make_sample,
                         diarized=lambda: client.diarized)
    await lis._ensure_self_voice()
    assert samples == [] and not voices.has_self()


async def test_mic_failure_message_no_longer_tells_to_start_docker(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(ml.events_log, "log_nowait", lambda kind, text="", **d: logged.append((kind, text)))

    class Broken:
        async def transcribe(self, *a, **k):
            raise RuntimeError("сбой")

    lis = ml.MicListener(Broken(), VoiceIdentity(str(tmp_path)), lambda t, i: None, lambda: False)
    await lis.handle_utterance(PCM)
    assert logged and all("docker" not in text for _, text in logged)


# ── стенд модуля: состояние, панель и журнал при смене режима ───────────────────

async def test_module_shows_mode_in_panel_and_journals_each_change_once(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(app.events_log, "log_nowait", lambda kind, text="", **d: logged.append((kind, text)))
    st, clock = server_state(up=False), Clock()
    eng, _ = make_engine(clock=clock)
    stt = STTClient(transport=server(st), local=eng, clock=clock)

    async with Rig(tmp_path, stt=stt) as rig:
        stt._on_change = rig.mod._stt_changed                      # модуль подписан на смену режима
        await stt.probe()
        await stt.probe()                                          # без изменения: строка в журнал не повторяется
        await asyncio.sleep(0.05)

        assert len(logged) == 1 and "локально (CPU)" in logged[0][1]
        panel = rig.mod.panel_blocks()
        assert panel[-1]["title"] == "Распознавание речи" and "локально (CPU)" in panel[-1]["text"]
        stats = {i["label"]: i["value"] for i in panel[0]["items"]}
        assert stats["Распознавание"] == "локально (CPU)"
        assert any(k == "stt" and "локально (CPU)" in v for k, v in rig.mod._last_state)

        st["up"] = True
        await stt.probe()                                          # сервер вернулся
        await asyncio.sleep(0.05)
        assert len(logged) == 2 and "WhisperX отвечает" in logged[1][1]
        assert rig.mod.panel_blocks()[-1]["text"] == stt.label()
