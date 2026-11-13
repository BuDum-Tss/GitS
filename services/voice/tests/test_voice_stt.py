"""Распознавание речи (ТЗ 12): GigaAM как основной движок, faster-whisper как запасной, всё локально. Без сети и без реальных
моделей: faster-whisper и GigaAM подменены заглушками (loader=...), часы подменены.
"""
import asyncio
import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

import voice_module as app
import hearing as h
import local_stt
import mic_listener as ml
from local_stt import LocalSTTUnavailable, LocalWhisper
from stt_client import LABEL_LOCAL, MODE_LOCAL, STTClient
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


def make_client(eng, seen=None):
    return STTClient(local=eng, on_change=(seen.append if seen is not None else None))


# ── клиент: только локальное распознавание ───────────────────────────────────

async def test_client_recognizes_locally_and_returns_only_text():
    clock, seen = Clock(), []
    eng, calls = make_engine(clock=clock)
    client = make_client(eng, seen)
    assert client.mode == "unknown" and client.label().endswith("ещё не проверено")

    t1 = await client.transcribe(PCM)
    t2 = await client.transcribe(PCM)

    assert t1.text == t2.text == "привет из локальной модели"
    assert not hasattr(t1, "speaker") and not hasattr(t1, "speaker_embedding")      # диаризации и отпечатков нет
    assert client.mode == MODE_LOCAL and client.label().startswith(LABEL_LOCAL)
    assert calls["n"] == 1                                          # модель открыта один раз и переиспользуется
    assert [s["mode"] for s in seen] == [MODE_LOCAL]                # переход записан один раз
    assert "diarized" not in client.status()


async def test_client_has_no_network_path():
    import stt_client
    assert not hasattr(stt_client, "httpx") and not hasattr(stt_client, "URL")


async def test_probe_checks_the_model_without_recognizing():
    eng, calls = make_engine()
    client = make_client(eng)
    assert await client.probe() == MODE_LOCAL and calls["n"] == 1 and client.short() == "локально (CPU)"


async def test_gigaam_is_the_main_engine_and_falls_back_to_whisper_through_the_client():
    whisper, _ = make_engine()

    def broken(n, t):
        raise OSError("нет весов")

    eng = local_stt.LocalGigaAM("gigaam-v3-e2e-rnnt", threads=2, loader=broken, clock=Clock(), fallback=whisper)
    seen = []
    client = make_client(eng, seen)
    assert (await client.transcribe(PCM)).text == "привет из локальной модели"      # ответил запасной faster-whisper
    assert seen[-1]["model"] == "small" and seen[-1]["model_state"] == "загружена"

    giga = FakeGiga()
    ok = local_stt.LocalGigaAM("gigaam-v3-e2e-rnnt", threads=2, loader=lambda n, t: giga, clock=Clock(), fallback=whisper)
    assert (await make_client(ok).transcribe(PCM)).text == "привет от гигаам"       # основной: GigaAM


# ── запасная модель: состояние, без скачивания, без повторов ───────────────────

async def test_missing_model_is_reported_not_downloaded_and_retried_only_after_a_minute():
    clock, seen = Clock(), []
    eng, calls = make_engine(fail=True, clock=clock)
    client = make_client(eng, seen)

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
    clock = Clock()
    model = FakeModel()
    flags = {"fail": True}

    def loader(name, threads):
        if flags["fail"]:
            raise OSError("весов нет")
        return model

    eng = LocalWhisper("small", threads=2, loader=loader, clock=clock)
    client = make_client(eng)
    with pytest.raises(LocalSTTUnavailable):
        await client.transcribe(PCM)

    flags["fail"] = False                                           # владелец скачал веса вручную
    clock.t += 61
    t = await client.transcribe(PCM)
    assert t.text == "привет из локальной модели" and client.label().startswith(LABEL_LOCAL)


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
    monkeypatch.delenv("GITS_STT_THREADS", raising=False)
    assert local_stt._threads() == 2
    monkeypatch.setenv("GITS_STT_THREADS", "4")
    assert local_stt._threads() == 4
    monkeypatch.setenv("GITS_STT_THREADS", "0")
    assert local_stt._threads() == 1
    monkeypatch.setenv("GITS_STT_THREADS", "много")
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

    client = make_client(eng)
    assert (await client.transcribe(PCM)).text == "привет из локальной модели"
    assert calls["n"] == 1


# ── микрофон: говорящий всегда владелец ───────────────────────────────────────

async def test_mic_reports_utterance_from_the_owner_without_speaker_identity():
    client = make_client(make_engine()[0])
    got = []

    async def on_voice(text, info, trace=None):
        got.append((text, info))

    lis = ml.MicListener(client, on_voice, lambda: False)
    await lis.handle_utterance(PCM)

    (text, info), = got
    assert text == "привет из локальной модели"
    assert info == {"id": "owner", "name": None, "owner": True}


async def test_mic_failure_message_does_not_mention_docker(monkeypatch):
    logged = []
    monkeypatch.setattr(ml.host, "log_nowait", lambda kind, text="", **d: logged.append((kind, text)))

    class Broken:
        async def transcribe(self, *a, **k):
            raise RuntimeError("сбой")

    lis = ml.MicListener(Broken(), lambda t, i: None, lambda: False)
    await lis.handle_utterance(PCM)
    assert logged and all("docker" not in text for _, text in logged)


# ── стенд модуля: состояние, панель и журнал при смене состояния ─────────────────

async def test_module_shows_state_in_panel_and_journals_each_change_once(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(app.host, "log_nowait", lambda kind, text="", **d: logged.append((kind, text)))
    eng, _ = make_engine(clock=Clock())
    stt = STTClient(local=eng)

    async with Rig(tmp_path, stt=stt) as rig:
        stt._on_change = rig.mod._stt_changed                      # модуль подписан на смену состояния
        await stt.probe()
        await stt.probe()                                          # без изменения: строка в журнал не повторяется
        await asyncio.sleep(0.05)

        assert len(logged) == 1 and "локально (CPU)" in logged[0][1] and "WhisperX" not in logged[0][1]
        panel = rig.mod.panel_blocks()
        assert panel[-1]["title"] == "Распознавание речи" and "локально (CPU)" in panel[-1]["text"]
        stats = {i["label"]: i["value"] for i in panel[0]["items"]}
        assert stats["Распознавание"] == "локально (CPU)"
        assert any(k == "stt" and "локально (CPU)" in v for k, v in rig.mod._last_state)


def test_local_model_gets_explicit_language():
    """Язык задан явно (по умолчанию ru): автоопределение на коротких фразах выдаёт корейский и прочие выдумки."""
    import numpy as np
    model = FakeModel()
    eng, _ = make_engine(model=model)
    eng.transcribe_array(np.zeros(1600, dtype=np.float32))
    assert model.calls[0][1].get("language") == "ru"


# ── GigaAM v3 как основной локальный движок, Whisper как запасной ─────────────────

class FakeGiga:
    def __init__(self, text="привет от гигаам"):
        self.text, self.calls = text, []

    def recognize(self, audio, sample_rate=16000):
        self.calls.append((len(audio), sample_rate))
        return self.text


def test_gigaam_recognizes_and_filters_junk():
    giga = FakeGiga()
    eng = local_stt.LocalGigaAM("gigaam-v3-e2e-rnnt", threads=2, loader=lambda n, t: giga, clock=Clock())
    assert eng.transcribe_pcm(PCM) == "привет от гигаам" and eng.state == "загружена"
    assert giga.calls and giga.calls[0][1] == 16000
    giga.text = "Субтитры сделал DimaTorzok"
    assert eng.transcribe_pcm(PCM) == ""


def test_gigaam_falls_back_to_whisper_when_it_does_not_open():
    whisper, _ = make_engine()
    def broken(n, t):
        raise OSError("нет весов")
    eng = local_stt.LocalGigaAM("gigaam-v3-e2e-rnnt", threads=2, loader=broken, clock=Clock(), fallback=whisper)
    assert eng.transcribe_pcm(PCM) == "привет из локальной модели"
    assert eng.state == "загружена" and eng.active_model == "small"


def test_gigaam_without_fallback_reports_unavailable():
    def broken(n, t):
        raise OSError("нет весов")
    eng = local_stt.LocalGigaAM("gigaam-v3-e2e-rnnt", threads=2, loader=broken, clock=Clock())
    with pytest.raises(LocalSTTUnavailable):
        eng.transcribe_pcm(PCM)
    assert eng.state == "не загружена"


def test_shared_engine_choice_follows_setting(monkeypatch):
    monkeypatch.setattr(local_stt, "_shared", None)
    monkeypatch.setenv("GITS_STT_ENGINE", "whisper")
    assert type(local_stt.shared()) is LocalWhisper
    monkeypatch.setattr(local_stt, "_shared", None)
    monkeypatch.delenv("GITS_STT_ENGINE")
    assert isinstance(local_stt.shared(), local_stt.LocalGigaAM)
