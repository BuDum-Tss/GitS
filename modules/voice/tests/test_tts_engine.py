"""Выбор движка синтеза (ESpeech → Fish Speech → edge-tts): без сети и видеокарты, движки подменены."""
import pytest

import tts_engine


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    tts_engine.reset()
    monkeypatch.setattr(tts_engine, "ENGINE", "auto")
    monkeypatch.setattr(tts_engine, "ESPEECH_START_WAIT_S", 0.0)       # без ожидания загрузки сервиса; ожидание проверяет свой тест
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


@pytest.mark.parametrize("raw,expected", [(100, 1.0), (80, 0.8), (10, 0.5), (900, 1.5), ("85", 0.85), (None, 1.0), ("мусор", 1.0),
                                          (float("nan"), 1.0)])
def test_set_speed_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setattr(tts_engine, "_speed", 1.0)
    tts_engine.set_speed(raw)
    assert tts_engine._speed == pytest.approx(expected)


def test_speed_goes_to_espeech_request_and_edge_rate(monkeypatch, tmp_path):
    import asyncio
    sent = {}

    class Resp:
        content = b"RIFFdata"

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            sent.update(json)
            return Resp()

    (tmp_path / "espeech_ref.wav").write_bytes(b"x")
    (tmp_path / "espeech_ref.txt").write_text("текст", encoding="utf-8")
    monkeypatch.setattr(tts_engine, "FISH_REFERENCE", tmp_path / "ref.wav")
    monkeypatch.setattr(tts_engine.httpx, "AsyncClient", Client)
    monkeypatch.setattr(tts_engine, "_speed", 1.0)
    tts_engine.set_speed(85)
    assert asyncio.run(tts_engine._espeech_synth("привет", tmp_path)) and sent["speed"] == pytest.approx(0.85)
    assert tts_engine._shifted("-12%", (tts_engine._speed - 1.0) * 100.0) == "-27%"


async def test_espeech_wait_survives_loading_service(monkeypatch):
    answers = iter([False, False, True])                                # сервис грузит модель, потом отвечает 200

    async def ping():
        return next(answers)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(tts_engine, "_espeech_ping", ping)
    monkeypatch.setattr(tts_engine.asyncio, "sleep", no_sleep)
    assert await tts_engine._espeech_wait(60.0) is True


async def test_espeech_wait_gives_up_at_the_limit(monkeypatch):
    async def ping():
        return False
    monkeypatch.setattr(tts_engine, "_espeech_ping", ping)
    assert await tts_engine._espeech_wait(0.0) is False
