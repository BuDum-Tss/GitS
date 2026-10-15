"""Передача забега модулю монтажа (ТЗ 20, MON-35, MON-36, MON-26a): сценарий, журнал речи, описание записи; вызовы montage.start;
запасной собственный монтаж. Без игры, ffmpeg и Claude."""
import json
import types
import wave

import pytest
from gits_sdk import ToolError

import run_recorder as rr
from audio_capture import RATE, AudioPart
from controller import Controller
from tests.rigs import Rig

STARTED = 1000.0


def ev(t, kind, text, **data):
    return {"ts": STARTED + t, "kind": kind, "text": text, "data": data}


def make_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "MIN_RAW_BYTES", 1)
    raw = tmp_path / "raw_120000.mp4"
    raw.write_bytes(b"video")
    wav = tmp_path / "raw_120000_a01.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x00\x00\x00\x00" * RATE)
    events = [ev(5, "game", "погиб Ева", type="pawn_died"), ev(6, "say", "Печально, но ожидаемо."), ev(70, "say", "Ещё одно наблюдение."),
              ev(500, "say", "после конца записи"), ev(8, "say", "")]
    return rr.Recording("r1", "Свалка", raw, STARTED, duration=120.0, outcome="colony_died", events=events,
                        audio_extra=[AudioPart(wav, STARTED + 2.5, RATE)], audio_sound=True)


def test_handoff_writes_scenario_speech_log_and_recording(tmp_path, monkeypatch):
    rec = make_recording(tmp_path, monkeypatch)
    result = rr.RunRecorder(str(tmp_path), lambda: {}).handoff(rec)
    assert result["raw"] == str(rec.raw) and result["audio"] == [str(tmp_path / "raw_120000_a01.wav")]
    scenario = json.loads((tmp_path / "scenario_120000.json").read_text(encoding="utf-8"))
    assert result["scenario_path"].endswith("scenario_120000.json") and scenario["scenes"]
    speech = json.loads((tmp_path / "speech_log_120000.json").read_text(encoding="utf-8"))
    assert speech == [{"ts": STARTED + 6, "kind": "say", "text": "Печально, но ожидаемо."},
                      {"ts": STARTED + 70, "kind": "say", "text": "Ещё одно наблюдение."}]          # только её реплики внутри окна записи
    recording = json.loads((tmp_path / "recording_120000.json").read_text(encoding="utf-8"))
    assert recording["started_at"] == STARTED and recording["duration_s"] == 120.0
    assert recording["audio_parts"] == [{"path": str(tmp_path / "raw_120000_a01.wav"), "started_at": STARTED + 2.5, "seconds": 1.0}]
    assert not list(tmp_path.glob("montage*.mp4")) and not (tmp_path / "clips").exists()                # своего монтажа нет


def test_handoff_skips_an_empty_recording(tmp_path):
    raw = tmp_path / "raw_1.mp4"
    raw.write_bytes(b"0")
    assert rr.RunRecorder(str(tmp_path), lambda: {}).handoff(rr.Recording("r", "К", raw, 0, duration=500)) is None


async def test_start_montage_makes_two_calls_with_the_spec_arguments(tmp_path):
    async with Rig(tmp_path, module_settings={"auto_montage": True}) as rig:
        handoff = {"raw": "C:/v/raw.mp4", "audio": ["C:/v/a1.wav"], "scenario_path": "C:/v/scenario.json",
                   "speech_log_path": "C:/v/speech_log.json", "recording_path": "C:/v/recording.json", "colony": "Свалка",
                   "outcome": "colony_died", "duration": 1800, "scenario": {"goal": {"title": "Выжить"}, "scenes": [1, 2, 3]}}
        accepted = await rig.link.start_montage(handoff)
        assert len(accepted) == 2
        calls = [c for c in rig.tool_calls if c["tool"] == "montage.start"]
        assert [(c["args"]["platform"], c["args"]["format"]) for c in calls] == [("youtube", "horizontal"), ("shorts", "vertical")]
        args = calls[0]["args"]
        assert args["source_kind"] == "rimworld_run" and args["voiceover"] == "glados"
        assert args["sources"] == ["C:/v/raw.mp4", "C:/v/a1.wav", "C:/v/scenario.json", "C:/v/speech_log.json", "C:/v/recording.json"]
        assert "Свалка" in args["brief"] and "colony_died" in args["brief"] and "Выжить" in args["brief"] and "сцен в сценарии 3" in args["brief"]


async def test_start_montage_returns_nothing_when_the_module_is_missing(tmp_path):
    async with Rig(tmp_path) as rig:
        def refuse(body):
            raise ToolError("инструмента «montage.start» нет", code="unknown")
        rig.core.handlers["core.tool.call"] = refuse
        assert await rig.link.start_montage({"raw": "r", "audio": [], "scenario_path": "s", "speech_log_path": "l", "recording_path": "c"}) == []


class FakeRecorder:
    def __init__(self):
        self.calls = []

    async def produce_for_montage(self, rec):
        self.calls.append("handoff")
        return {"run_id": "r1", "raw": "raw.mp4", "scenario_path": "s.json", "audio": [], "speech_log_path": "l.json", "recording_path": "c.json"}

    def process(self, rec):
        self.calls.append("process")
        return {"run_id": "r1", "video": "montage.mp4", "raw": "raw.mp4", "scenario_path": "s.json", "clips": []}

    async def produce(self, rec):
        self.calls.append("produce")
        return self.process(rec)


class FakeLink:
    def __init__(self, auto, accept):
        self.online = True
        self.module_settings = {"auto_montage": auto}
        self.accept = accept
        self.events = []

    async def start_montage(self, handoff):
        return ["ok", "ok"] if self.accept else []

    async def event(self, kind, data, text=None, urgency=None):
        self.events.append((kind, data, text))


def controller_with(auto, accept):
    ctl = types.SimpleNamespace(recorder=FakeRecorder(), link=FakeLink(auto, accept))
    ctl._auto_montage_on = lambda: Controller._auto_montage_on(ctl)
    return ctl


@pytest.mark.parametrize(("auto", "accept", "calls", "event_text"), [
    (True, True, ["handoff"], "передана модулю монтажа"),
    (True, False, ["handoff", "process"], "Видео забега готово"),            # модуль не принял: монтирует сама
    (False, True, ["produce", "process"], "Видео забега готово"),            # настройка выключена: как раньше
])
async def test_produce_video_routes_to_the_montage_module_or_to_itself(auto, accept, calls, event_text):
    ctl = controller_with(auto, accept)
    await Controller._produce_video(ctl, object())
    assert ctl.recorder.calls == calls and event_text in ctl.link.events[-1][2]
    if calls == ["handoff"]:
        assert ctl.link.events[-1][1]["montage_jobs"] == ["ok", "ok"]
