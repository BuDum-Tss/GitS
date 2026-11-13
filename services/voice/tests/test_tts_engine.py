"""Выбор движка синтеза (ESpeech → Fish Speech → edge-tts): без сети и видеокарты, движки подменены."""
import types
from pathlib import Path

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

    async def e_state():
        return "ready" if espeech else "down"

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

    monkeypatch.setattr(tts_engine, "_espeech_state", e_state)
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
    answers = iter(["down", "loading", "loading", "ready"])            # порт открылся, модель грузится, потом готово

    async def state():
        return next(answers)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(tts_engine, "_espeech_state", state)
    monkeypatch.setattr(tts_engine.asyncio, "sleep", no_sleep)
    assert await tts_engine._espeech_wait(60.0) is True


async def test_espeech_wait_gives_up_at_the_limit(monkeypatch):
    async def state():
        return "loading"
    monkeypatch.setattr(tts_engine, "_espeech_state", state)
    assert await tts_engine._espeech_wait(0.0) is False


async def test_espeech_wait_does_not_wait_for_a_service_that_is_not_running(monkeypatch):
    clock = iter([0.0, 0.0, 5.0, 25.0, 25.0, 25.0])                    # порт молчит дольше ESPEECH_DOWN_GRACE_S

    async def state():
        return "down"

    async def no_sleep(_):
        return None
    monkeypatch.setattr(tts_engine, "_espeech_state", state)
    monkeypatch.setattr(tts_engine.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(tts_engine, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))
    assert await tts_engine._espeech_wait(90.0) is False


def test_edge_pitch_is_clamped_to_what_the_service_accepts():
    assert tts_engine._shifted("-25Hz", 0.0) == "-20Hz"                 # -25Hz: сервис отвечает таймаутом
    assert tts_engine._shifted("-18Hz", -5.0) == "-20Hz"
    assert tts_engine._shifted("+18Hz", 5.0) == "+20Hz"
    assert tts_engine._shifted("-12%", -5.0) == "-17%"                  # проценты (скорость) не ограничиваются


def test_recheck_waits_for_a_loading_service_instead_of_fallback_voice(monkeypatch):
    """Живой запуск: ESpeech грузил модель дольше первой проверки, первые фразы ушли запасным голосом, следующие голосом персонажа.
    Повторная проверка, заставшая загрузку, ждёт сервис; молчащий порт не ждёт (запасной голос сразу)."""
    waits: list[float] = []
    state = {"now": "loading"}

    async def e_state():
        return state["now"]

    async def e_wait(limit):
        waits.append(limit)
        return state["now"] == "loading"          # загрузка закончится готовностью, молчащий порт нет

    monkeypatch.setattr(tts_engine, "_espeech_state", e_state)
    monkeypatch.setattr(tts_engine, "_espeech_wait", e_wait)
    monkeypatch.setattr(tts_engine, "_espeech_reference", lambda: ("ref.wav", "текст"))
    monkeypatch.setattr(tts_engine, "_espeech_available", False)
    monkeypatch.setattr(tts_engine, "_espeech_checked_at", -1e9)
    assert tts_engine._check_espeech() is True and waits == [tts_engine.ESPEECH_RELOAD_WAIT_S]
    state["now"] = "down"
    monkeypatch.setattr(tts_engine, "_espeech_available", False)
    monkeypatch.setattr(tts_engine, "_espeech_checked_at", -1e9)
    assert tts_engine._check_espeech() is False and waits[-1] == 0.0


def _fake_client(sent: list):
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
            sent.append(json)
            return Resp()

    return Client


def test_espeech_request_carries_reference_as_base64_and_path(monkeypatch, tmp_path):
    import asyncio
    import base64
    sent: list = []
    (tmp_path / "espeech_ref.wav").write_bytes(b"RIFF-wav-bytes")
    (tmp_path / "espeech_ref.txt").write_text("текст", encoding="utf-8")
    monkeypatch.setattr(tts_engine, "FISH_REFERENCE", tmp_path / "ref.wav")
    monkeypatch.setattr(tts_engine.httpx, "AsyncClient", _fake_client(sent))
    assert asyncio.run(tts_engine._espeech_synth("привет", tmp_path))
    body = sent[0]
    assert base64.b64decode(body["ref_audio_b64"]) == b"RIFF-wav-bytes"
    assert body["ref_audio"] == str(tmp_path / "espeech_ref.wav") and body["ref_text"] == "текст"


def test_espeech_reference_is_read_once_and_reread_when_replaced(monkeypatch, tmp_path):
    import asyncio
    import os
    sent: list = []
    wav = tmp_path / "espeech_ref.wav"
    wav.write_bytes(b"first")
    (tmp_path / "espeech_ref.txt").write_text("текст", encoding="utf-8")
    monkeypatch.setattr(tts_engine, "FISH_REFERENCE", tmp_path / "ref.wav")
    monkeypatch.setattr(tts_engine.httpx, "AsyncClient", _fake_client(sent))
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (reads.append(self.name), real(self))[1])
    asyncio.run(tts_engine._espeech_synth("раз", tmp_path))
    asyncio.run(tts_engine._espeech_synth("два", tmp_path))
    assert reads.count("espeech_ref.wav") == 1 and sent[0]["ref_audio_b64"] == sent[1]["ref_audio_b64"]
    wav.write_bytes(b"second!")
    os.utime(wav, ns=(1, 1))                                      # время изменения другое: референс заменили
    asyncio.run(tts_engine._espeech_synth("три", tmp_path))
    assert reads.count("espeech_ref.wav") == 2 and sent[2]["ref_audio_b64"] != sent[0]["ref_audio_b64"]
