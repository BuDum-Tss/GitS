"""Запись забега: сценарий, команды ffmpeg, клипы, метки. Без игры и сети."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import run_recorder as rr
import scenario as sc
from audio_capture import AudioPart


def ev(t, kind, text, **data):
    return {"ts": 1000 + t, "kind": kind, "text": text, "data": data}


def test_moments_from_protocol_and_markers():
    events = [ev(5, "game", "погиб Ева", type="pawn_died"), ev(6, "game", "изменение: пожар"),
              ev(7, "game", "письмо", type="letter"), ev(8, "say", "Ну вот"), ev(-5, "game", "до старта")]
    m = sc.collect_moments(events, 1000, [(30, "метка", 2)])
    assert [(x["kind"], x["importance"]) for x in m] == [("pawn_died", 3), ("change", 2), ("letter", 1), ("say", 0),
                                                         ("marker", 2)]


def test_scenario_clusters_moments_into_scenes_with_said():
    moments = [{"t": 100, "kind": "incident", "label": "налёт", "importance": 2},
               {"t": 120, "kind": "pawn_died", "label": "погиб Ева", "importance": 3},
               {"t": 125, "kind": "say", "label": "Печально", "importance": 0},
               {"t": 400, "kind": "incident", "label": "пожар", "importance": 2}]
    plan = sc.build(moments, 600, "r1", "Колония", "colony_died", {"title": "Выжить"})
    events = [s for s in plan["scenes"] if s["kind"] == "event"]
    assert [s["label"] for s in events] == ["погиб Ева", "пожар"] and events[0]["said"] == ["Печально"]
    assert plan["scenes"][0]["kind"] == "opening" and plan["scenes"][-1]["kind"] == "ending"
    assert plan["goal"]["title"] == "Выжить" and [s["id"] for s in plan["scenes"]] == [1, 2, 3, 4]


def test_scenario_without_events_is_opening_and_ending():
    plan = sc.build([], 600, "r", "К", "", None)
    assert [(s["start"], s["end"]) for s in plan["scenes"]] == [(0.0, 25), (570.0, 600)]


def test_scenario_respects_max_length_and_keeps_important():
    moments = [{"t": t, "kind": "marker", "label": "x", "importance": 2} for t in range(100, 5000, 100)]
    moments.append({"t": 2500, "kind": "pawn_died", "label": "главное", "importance": 3})
    plan = sc.build(moments, 6000, "r", "К", "", None)
    assert sum(s["end"] - s["start"] for s in plan["scenes"]) <= sc.MAX_TOTAL_S
    assert any(s["start"] <= 2500 <= s["end"] for s in plan["scenes"])


def test_clip_windows_are_short_and_limited():
    moments = [{"t": 100 * i, "kind": "marker", "label": f"m{i}", "importance": 2} for i in range(1, 12)]
    plan = sc.build(moments, 2000, "r", "К", "", None)
    clips = sc.clip_windows(plan)
    assert 0 < len(clips) <= sc.MAX_CLIPS and all(c["end"] - c["start"] <= sc.MAX_CLIP_S for c in clips)


def test_read_goal_prefers_active_ambition(tmp_path):
    (tmp_path / "goals.json").write_text(json.dumps([
        {"category": "ambition", "status": "done", "title": "Старая"},
        {"category": "ambition", "status": "active", "title": "Новая", "progress_pct": 40},
        {"category": "tactical", "status": "active", "title": "Не та"}]), encoding="utf-8")
    assert sc.read_goal(tmp_path)["title"] == "Новая" and sc.read_goal(tmp_path / "нет") is None


def test_ffmpeg_commands():
    whole = rr.montage_args("ff", Path("a"), Path("b"), [{"start": 0, "end": 900}], 900)
    assert "setpts=PTS/1.875" in whole[whole.index("-filter_complex") + 1]
    cut = rr.montage_args("ff", Path("a"), Path("b"), [{"start": 0, "end": 25}, {"start": 570, "end": 600}], 600)
    graph = cut[cut.index("-filter_complex") + 1]
    assert "concat=n=2" in graph and "trim=570" not in graph and "atrim" not in graph                       # REC-3: сцены не режутся фильтром trim
    assert cut.count("-i") == 2 and ["-ss", "570.00", "-to", "600.00", "-i", "a"] == cut[cut.index("570.00") - 1:][:6]
    vert = rr.clip_args("ff", Path("a"), Path("b"), 10, 40)
    assert "crop=1080:1920" in vert[vert.index("-filter_complex") + 1]


def test_recorder_disabled_or_without_ffmpeg_does_nothing(tmp_path, monkeypatch):
    off = rr.RunRecorder(str(tmp_path), lambda: {"record_runs": False})
    off.start("r1", "К")
    off.mark("x")
    assert off.current is None and off.close("x") is None
    monkeypatch.setattr(rr, "find_ffmpeg", lambda: None)
    on = rr.RunRecorder(str(tmp_path), lambda: {"record_runs": True})
    on.start("r1", "К")
    assert on.current is None


def test_process_skips_empty_recording(tmp_path):
    raw = tmp_path / "raw_1.mp4"
    raw.write_bytes(b"0")
    assert rr.RunRecorder(str(tmp_path), lambda: {}).process(rr.Recording("r", "К", raw, 0, duration=500)) is None


@pytest.mark.slow                  # настоящий ffmpeg, около минуты: по умолчанию пропускается (.\check.ps1 -All запускает)
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="нужен ffmpeg")
def test_full_pipeline_on_real_ffmpeg(tmp_path, monkeypatch):
    raw = tmp_path / "raw_1.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=70",
                    "-pix_fmt", "yuv420p", str(raw)], check=True)
    monkeypatch.setattr(rr, "MIN_RAW_BYTES", 1)
    monkeypatch.setattr(rr.events_log, "tail", lambda **kw: [])
    rec = rr.Recording("r", "Колония", raw, 0, duration=70, markers=[(35, "налёт", 2)])
    result = rr.RunRecorder(str(tmp_path), lambda: {}).process(rec)
    assert Path(result["video"]).stat().st_size > 0 and len(result["clips"]) == 1
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", result["clips"][0]], capture_output=True, text=True).stdout.strip()
    assert probe == "1080,1920"
    assert (tmp_path / "scenario_1.json").is_file() and (tmp_path / "clips" / "clips_1.json").is_file()


def test_audio_commands_mux_voice_into_montage_and_clip():
    track = rr.AudioTrack([(Path("a.wav"), 0.0, 600.0)], 600.0)
    cut = rr.montage_args("ff", Path("a"), Path("b"), [{"start": 0, "end": 25}, {"start": 570, "end": 600}], 600, track)
    graph = cut[cut.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1" in graph and "[aout]" in cut and "-shortest" not in cut
    vert = rr.clip_args("ff", Path("a"), Path("b"), 10, 40, track)
    assert vert[vert.index("-ss") + 1] == "10.00" and "[aout]" in vert


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="нужен ffmpeg")
def test_voice_reaches_final_video(tmp_path, monkeypatch):
    raw = tmp_path / "raw_1.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=70",
                    "-pix_fmt", "yuv420p", str(raw)], check=True)
    wav = tmp_path / "raw_1.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=70",
                    "-ac", "2", str(wav)], check=True)
    monkeypatch.setattr(rr, "MIN_RAW_BYTES", 1)
    monkeypatch.setattr(rr.events_log, "tail", lambda **kw: [])
    rec = rr.Recording("r", "К", raw, 0, duration=70, markers=[(35, "налёт", 2)],
                       audio_extra=[AudioPart(wav, 0.2, 70 * 44100)], audio_sound=True)
    result = rr.RunRecorder(str(tmp_path), lambda: {}).process(rec)
    assert result["audio"] is True
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                              result["clips"][0]], capture_output=True, text=True).stdout.split()
    assert "audio" in streams and "video" in streams


def test_capture_uses_window_capture_by_default(monkeypatch):
    monkeypatch.delenv("GITS_RECORD_SOURCE", raising=False)
    cmd = rr.capture_args("ff", Path("o.mp4"))
    assert "gfxcapture=window_exe=RimWorldWin64" in cmd[cmd.index("-i") + 1] and "hwdownload" in cmd[cmd.index("-i") + 1]
    monkeypatch.setenv("GITS_RECORD_SOURCE", "desktop")
    assert "gdigrab" in rr.capture_args("ff", Path("o.mp4")) and "desktop" in rr.capture_args("ff", Path("o.mp4"))


def test_windowed_launch_args_follow_screen_size(monkeypatch):
    import game_launcher
    args = game_launcher.windowed_args()
    assert args[0] == "-popupwindow" and (len(args) == 1 or args[1] == "-screen-width")
