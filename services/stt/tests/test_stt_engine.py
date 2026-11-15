"""Движки распознавания без весов: PCM -> float, фильтр мусора, выбор движка и запасной, имена файлов весов."""
import numpy as np
import pytest

from stt.config import SttConfig
from stt.engine import (EngineUnavailable, FakeEngine, FallbackEngine, GigaAmEngine, WhisperEngine, build_engine,
                        gigaam_encoder_name, is_junk, pcm_to_float)
from stt.fetch import gigaam_files, whisper_repo


def test_pcm_to_float():
    pcm = np.array([0, 16384, -32768, 32767], dtype="<i2").tobytes()
    out = pcm_to_float(pcm)
    assert out.dtype == np.float32
    assert out.tolist() == pytest.approx([0.0, 0.5, -1.0, 32767 / 32768])
    assert len(pcm_to_float(pcm + b"\x01")) == 4          # нечётный хвост отбрасывается


@pytest.mark.parametrize("text", ["Субтитры сделал DimaTorzok", "Продолжение следует...", "Спасибо за просмотр!", "Редактор субтитров А.Семкин"])
def test_junk_phrases(text):
    assert is_junk(text)


def test_normal_text_is_not_junk():
    assert not is_junk("включи свет на кухне")


class _Seg:
    def __init__(self, text, no_speech=0.0, logprob=-0.2):
        self.text, self.no_speech_prob, self.avg_logprob = text, no_speech, logprob


class _FakeWhisper:
    def __init__(self, segments):
        self.segments, self.kwargs = segments, None

    def transcribe(self, audio, **kw):
        self.kwargs = kw
        return iter(self.segments), None


def test_whisper_filters_segments_and_passes_language():
    model = _FakeWhisper([_Seg(" привет "), _Seg("шум", no_speech=0.9), _Seg("неуверенно", logprob=-2.0),
                          _Seg("Субтитры сделал DimaTorzok"), _Seg("мир")])
    eng = WhisperEngine(SttConfig(), loader=lambda name, threads: model)
    eng.load()
    assert eng.transcribe(np.zeros(16000, np.float32), "ru") == "привет мир"
    assert model.kwargs["language"] == "ru" and model.kwargs["vad_filter"] is True


def test_gigaam_junk_gives_empty_and_only_russian():
    class M:
        def recognize(self, audio, sample_rate):
            return "Спасибо за просмотр"
    eng = GigaAmEngine(SttConfig(), loader=lambda *a: M())
    eng.load()
    assert eng.transcribe(np.zeros(16000, np.float32), "ru") == ""
    assert eng.supports("ru") and not eng.supports("en")


def test_transcribe_before_load_raises():
    with pytest.raises(EngineUnavailable):
        GigaAmEngine(SttConfig(), loader=lambda *a: None).transcribe(np.zeros(10, np.float32), "ru")


def _boom(*a):
    raise FileNotFoundError("нет весов")


class _GigaStub:
    def recognize(self, audio, sample_rate):
        return "гигаам"


def test_build_engine_default_is_gigaam_with_whisper_fallback():
    eng = build_engine(SttConfig(), gigaam_loader=lambda *a: _GigaStub(), whisper_loader=_boom)
    assert isinstance(eng, FallbackEngine) and not eng.loaded
    eng.load()
    assert eng.loaded and eng.name == "gigaam" and eng.model == "gigaam-v3-e2e-rnnt"
    assert eng.transcribe(np.zeros(16000, np.float32), "ru") == "гигаам"


def test_fallback_to_whisper_when_gigaam_fails():
    eng = build_engine(SttConfig(), gigaam_loader=_boom,
                       whisper_loader=lambda name, threads: _FakeWhisper([_Seg("запасной")]))
    eng.load()
    assert eng.name == "whisper" and eng.model == "small" and eng.languages is None
    assert eng.transcribe(np.zeros(16000, np.float32), "ru") == "запасной"


def test_both_engines_fail_reports_both_reasons():
    eng = build_engine(SttConfig(), gigaam_loader=_boom, whisper_loader=_boom)
    with pytest.raises(EngineUnavailable) as e:
        eng.load()
    assert "gigaam" in str(e.value) and "whisper" in str(e.value)
    assert not eng.loaded


def test_whisper_engine_selected_by_env():
    cfg = SttConfig.from_env({"GITS_STT_ENGINE": "whisper", "GITS_STT_MODEL": "base", "GITS_STT_THREADS": "3"})
    eng = build_engine(cfg, gigaam_loader=_boom, whisper_loader=lambda name, threads: _FakeWhisper([]))
    assert isinstance(eng, WhisperEngine) and eng.model == "base" and cfg.threads == 3
    eng.load()
    assert eng.loaded


def test_config_defaults_and_bad_values():
    cfg = SttConfig.from_env({})
    assert (cfg.engine, cfg.language, cfg.threads, cfg.workers, cfg.queue) == ("gigaam", "ru", 2, 2, 8)
    bad = SttConfig.from_env({"GITS_STT_ENGINE": "xxx", "STT_WORKERS": "много", "STT_QUEUE": "-5", "STT_TIMEOUT_S": "0"})
    assert bad.engine == "gigaam" and bad.workers == 2 and bad.queue == 0 and bad.timeout_s == 1.0


def test_gigaam_file_names():
    assert gigaam_encoder_name("gigaam-v3-e2e-rnnt") == "v3_e2e_rnnt_encoder.int8.onnx"
    assert "v3_e2e_rnnt_vocab.txt" in gigaam_files("gigaam-v3-e2e-rnnt")
    assert gigaam_files("gigaam-v3-e2e-ctc")[1] == "v3_e2e_ctc.int8.onnx"
    assert whisper_repo("small") == "Systran/faster-whisper-small" and whisper_repo("a/b") == "a/b"


def test_fake_engine_load_failure():
    with pytest.raises(EngineUnavailable):
        FakeEngine(fail_load="нет").load()
