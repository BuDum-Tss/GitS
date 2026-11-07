"""Фильтр голоса (voice_fx): выключен, ошибка ffmpeg не портит файл, настоящий ffmpeg меняет аудио."""
import shutil
import subprocess
import wave

import pytest

import voice_fx


def _wav(path, seconds=0.5):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x10\x00\xf0\xff" * int(12000 * seconds))
    return str(path)


def test_unknown_and_off_mean_disabled(tmp_path):
    for name in (None, "", "off", "nonsense", 5):
        voice_fx.set_preset(name)
        assert voice_fx.preset() == voice_fx.OFF
    src = _wav(tmp_path / "a.wav")
    before = open(src, "rb").read()
    assert voice_fx.apply(src) == src and open(src, "rb").read() == before


def test_ffmpeg_failure_keeps_original(tmp_path, monkeypatch):
    src = _wav(tmp_path / "a.wav")
    before = open(src, "rb").read()
    monkeypatch.setattr(voice_fx, "find_ffmpeg", lambda: (_ for _ in ()).throw(FileNotFoundError("нет ffmpeg")))
    assert voice_fx.apply(src, "glados") == src
    assert open(src, "rb").read() == before
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("gits_fx_")]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_all_presets_run_with_real_ffmpeg(tmp_path):
    for name in voice_fx.PRESETS:
        src = _wav(tmp_path / f"{name}.wav")
        before = open(src, "rb").read()
        assert voice_fx.apply(src, name) == src
        assert open(src, "rb").read() != before, name
        subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-f", "null", "-"], check=True)
