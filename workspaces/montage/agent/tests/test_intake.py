"""Стадия intake: проверка файлов, перекодирование в H.264, вид исходников, запись RimWorld со звуком из кусков (ТЗ 20, раздел 6)."""
import json
import subprocess
import wave

import pytest

import intake
import media


def ffmpeg_sync(*args):
    res = subprocess.run([media.find_ffmpeg(), "-hide_banner", "-y", *args], capture_output=True)
    assert res.returncode == 0, res.stderr[-300:]


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("src")
    ffmpeg_sync("-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=3", "-f", "lavfi", "-i", "sine=d=3", "-c:v", "mpeg4", "-c:a", "aac", str(d / "Мой ролик.mp4"))
    ffmpeg_sync("-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "raw_120000.mp4"))
    with wave.open(str(d / "raw_120000_a01.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x10\x10" * 8000 * 2)                              # 2 секунды звука
    (d / "scenario.json").write_text(json.dumps({"scenes": []}), encoding="utf-8")
    (d / "speech_log.json").write_text("[]", encoding="utf-8")
    (d / "recording.json").write_text(json.dumps({"started_at": 1000.0, "duration_s": 4.0, "audio_parts": [
        {"path": "C:/old/path/raw_120000_a01.wav", "started_at": 1001.0, "seconds": 2.0}]}), encoding="utf-8")
    return d


def test_kind_detection():
    from pathlib import Path
    vid = Path("a.mp4")
    sound, mute = media.MediaInfo("a", has_video=True, has_audio=True), media.MediaInfo("a", has_video=True, has_audio=False)
    assert intake.detect_kind([Path("scenario.json"), Path("raw.mp4")], "", {}) == "rimworld_run"
    assert intake.detect_kind([], "ролик про правила", {}) == "narrated"
    assert intake.detect_kind([vid], "", {str(vid): sound}) == "talking_head"
    assert intake.detect_kind([vid], "", {str(vid): mute}) == "narrated"
    assert intake.detect_kind([vid], "поверх пойдёт мой закадр", {str(vid): sound}) == "narrated"
    assert intake.safe_name("Мой ролик (1).MP4", set()) == "Мой_ролик_1.mp4"
    assert intake.safe_name("clip.mp4", {"clip.mp4"}) == "clip_2.mp4"


def test_expand_sources_walks_folders_and_dedupes(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.mp4").write_bytes(b"1")
    (tmp_path / "a" / "deep" / "deeper").mkdir(parents=True)
    (tmp_path / "a" / "deep" / "deeper" / "no.mp4").write_bytes(b"1")
    files = intake.expand_sources([str(tmp_path / "a"), str(tmp_path / "a" / "x.mp4")])
    assert [f.name for f in files] == ["x.mp4"]


async def test_non_h264_video_is_transcoded_and_talking_head_is_detected(tmp_path, sources):
    job = tmp_path / "j-aaaaaaaa"
    (job / "sources").mkdir(parents=True)
    info = await intake.run_intake(job, {"sources": [str(sources / "Мой ролик.mp4")], "brief": "шортс про куб"})
    f = info["files"][0]
    assert info["source_kind"] == "talking_head" and f["transcoded"] is True and f["name"].endswith("_h264.mp4")
    assert (job / "sources" / f["name"]).is_file() and f["vcodec"] == "h264" and f["has_audio"] and f["width"] == 64
    assert json.loads((job / "sources" / "info.json").read_text(encoding="utf-8"))["files"][0]["path"] == "sources/" + f["name"]


async def test_rimworld_recording_gets_its_audio_on_the_video_timeline(tmp_path, sources):
    job = tmp_path / "j-bbbbbbbb"
    (job / "sources").mkdir(parents=True)
    paths = [str(sources / n) for n in ("raw_120000.mp4", "raw_120000_a01.wav", "scenario.json", "speech_log.json", "recording.json")]
    info = await intake.run_intake(job, {"sources": paths, "brief": "забег"})
    assert info["source_kind"] == "rimworld_run"
    video = next(f for f in info["files"] if f["kind"] == "video")
    assert video["name"] == "raw_120000_h264.mp4" and video["has_audio"] and abs(video["duration_ms"] - 4000) < 400
    assert not (job / "sources" / "_audio_track.wav").exists()                     # временная дорожка убрана
    names = {f["name"] for f in info["files"]}
    assert {"scenario.json", "speech_log.json", "recording.json"} <= names
    # звук на месте: тишина первую секунду (кусок начинается на 1,0 с), потом сигнал
    quiet = await media.max_level(job / "sources" / video["name"], 0.0, 0.8)
    loud = await media.max_level(job / "sources" / video["name"], 1.3, 0.5)
    assert (quiet is None or quiet < -50) and loud is not None and loud > -40


def test_audio_timeline_filter_pads_gaps_and_trims_overlaps():
    chains, label = media.audio_timeline_filter([(10.0, 5.0), (20.0, 3.0), (22.0, 4.0)], 30.0)
    text = ";".join(chains)
    assert label == "[a]" and "anullsrc" in text and "apad=whole_dur=30.000" in text and "atrim=end=30.000" in text
    assert "atrim=start=1.000" in text                                           # третий кусок начинается внахлёст на 1 секунду
    assert text.count("concat=n=") == 1 and "concat=n=5" in text                 # тишина, кусок, тишина, кусок, кусок
