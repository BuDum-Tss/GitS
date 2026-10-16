"""Выбор движка синтеза (ESpeech → Fish Speech → edge-tts): без сети и видеокарты, движки подменены."""
import pytest

import tts_engine


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    tts_engine.reset()
    monkeypatch.setattr(tts_engine, "ENGINE", "auto")
    yield
    tts_engine.reset()


def _rig(monkeypatch, tmp_path, *, espeech=True, fish=True, espeech_ok=True, fish_ok=True):
    """Подмена пингов и синтеза; вернуть журнал вызовов."""
    calls: list[str] = []
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"x")
    (tmp_path / "espeech_ref.wav").write_bytes(b"x")
    (tmp_path / "espeech_ref.txt").write_text("текст референса", encoding="utf-8")
    monkeypatch.setattr(tts_engine, "FISH_REFERENCE", ref)

    async def e_ping():
        return espeech

    async def f_ping():
        return fish

    async def e_synth(text, directory=None):
        calls.append("espeech")
        return "e.wav" if espeech_ok else None

    async def f_synth(text, directory=None):
        calls.append("fish")
        return "f.wav" if fish_ok else None

    async def edge(text, directory=None):
        calls.append("edge")
        return "edge.mp3"

    monkeypatch.setattr(tts_engine, "_espeech_ping", e_ping)
    monkeypatch.setattr(tts_engine, "_fish_ping", f_ping)
    monkeypatch.setattr(tts_engine, "_espeech_synth", e_synth)
    monkeypatch.setattr(tts_engine, "_fish_synth", f_synth)
    monkeypatch.setattr(tts_engine, "_edge_synth", edge)
    return calls


def test_espeech_first(monkeypatch, tmp_path):
    calls = _rig(monkeypatch, tmp_path)
    assert tts_engine.synth_file("Привет.") == "e.wav"
    assert calls == ["espeech"] and tts_engine.backend() == "espeech"


def test_falls_back_to_fish_then_edge(monkeypatch, tmp_path):
    calls = _rig(monkeypatch, tmp_path, espeech=False)
    assert tts_engine.synth_file("Привет.") == "f.wav" and calls == ["fish"] and tts_engine.backend() == "fish"
    tts_engine.reset()
    calls = _rig(monkeypatch, tmp_path, espeech=False, fish=False)
    assert tts_engine.synth_file("Привет.") == "edge.mp3" and calls == ["edge"] and tts_engine.backend() == "edge"


def test_single_failure_falls_through_to_next(monkeypatch, tmp_path):
    calls = _rig(monkeypatch, tmp_path, espeech_ok=False)
    assert tts_engine.synth_file("Привет.") == "f.wav"
    assert calls == ["espeech", "fish"]


def test_no_espeech_reference_files_skips_espeech(monkeypatch, tmp_path):
    calls = _rig(monkeypatch, tmp_path)
    (tmp_path / "espeech_ref.txt").unlink()
    assert tts_engine.synth_file("Привет.") == "f.wav" and calls == ["fish"]


@pytest.mark.parametrize("engine,expected", [("fish", ["fish"]), ("edge", ["edge"]), ("espeech", ["espeech"])])
def test_engine_setting_limits_the_chain(monkeypatch, tmp_path, engine, expected):
    calls = _rig(monkeypatch, tmp_path)
    monkeypatch.setattr(tts_engine, "ENGINE", engine)
    tts_engine.synth_file("Привет.")
    assert calls == expected
