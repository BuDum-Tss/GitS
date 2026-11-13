"""Запись забегов, ТЗ 11b: звук кусками (REC-1), перезапуск звука (REC-2), нарезка перемоткой (REC-3), звук не обрезает
видео (REC-4), повторная сборка записи (REC-5), предел длины видео (REC-6). Без микрофона и игры; ffmpeg настоящий
только там, где это сказано."""
import json
import shutil
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import pytest

import audio_capture as ac
import events_log
import game_watchdog as gw
import module_manifest
import run_recorder as rr
from audio_capture import RATE, AudioPart, LoopbackRecorder

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="нужен ffmpeg")
BLOCK = ac.CHUNK / RATE


class Clock:
    t = 0.0


class FakeSource:
    """Подставка вместо WASAPI: каждый блок двигает общие часы на свою длину, как настоящее устройство."""

    def __init__(self, clock, blocks, jumps=None, fail_at=None, sleep=0.0):
        self.clock, self.blocks, self.jumps, self.fail_at, self.sleep = clock, blocks, jumps or {}, fail_at, sleep
        self.n = 0
        self.recorder = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes):
        self.n += 1
        time.sleep(self.sleep)
        self.clock.t += self.jumps.get(self.n, 0.0) + numframes / RATE
        if self.fail_at == self.n:
            raise RuntimeError("Error 0x100000001")
        if self.n >= self.blocks and self.recorder is not None:
            self.recorder._stop.set()
        return np.full((numframes, 2), 0.3, dtype="float32")


def make(tmp_path, blocks, part_seconds=2.0, **kw):
    clock = Clock()
    src = FakeSource(clock, blocks, **kw)
    rec = LoopbackRecorder(tmp_path / "raw_000912.wav", part_seconds=part_seconds, source=lambda: src,
                           wall=lambda: 1_000_000.0, mono=lambda: clock.t)
    src.recorder = rec
    return rec, src


def wav_frames(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes()


# ── REC-1: звук кусками с точным временем начала ──

def test_three_parts_have_exact_offsets_and_sidecars(tmp_path):
    rec, _ = make(tmp_path, blocks=98)                      # 44 блока на кусок (2,04 с), третий кусок неполный
    rec._run()
    assert [p.path.name for p in rec.parts] == ["raw_000912_a01.wav", "raw_000912_a02.wav", "raw_000912_a03.wav"]
    per = 44 * ac.CHUNK
    assert [p.frames for p in rec.parts] == [per, per, 10 * ac.CHUNK]
    assert [round(p.started_at - 1_000_000.0, 4) for p in rec.parts] == [0.0, round(per / RATE, 4), round(2 * per / RATE, 4)]
    assert [wav_frames(p.path) for p in rec.parts] == [p.frames for p in rec.parts]
    meta = json.loads(rec.parts[1].path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["started_at"] == rec.parts[1].started_at and meta["frames"] == per
    assert rec.has_sound() and not rec.error and rec.started_at == 1_000_000.0


def test_lost_frames_are_padded_with_silence_and_long_gap_starts_new_part(tmp_path):
    rec, _ = make(tmp_path, blocks=20, part_seconds=60, jumps={10: 1.2})        # потеряно 1,2 с: тишина в том же куске
    rec._run()
    assert len(rec.parts) == 1 and abs(rec.parts[0].frames - (20 * ac.CHUNK + 1.2 * RATE)) <= 2
    (tmp_path / "b").mkdir()
    rec2, _ = make(tmp_path / "b", blocks=20, part_seconds=60, jumps={10: 8.0})        # провал 8 с: новый кусок
    rec2._run()
    assert len(rec2.parts) == 2 and rec2.parts[0].frames == 9 * ac.CHUNK
    assert abs(rec2.parts[1].started_at - (1_000_000.0 + 9 * BLOCK + 8.0)) < 1e-6


def test_stream_failure_keeps_written_audio_and_reports_error(tmp_path):
    rec, _ = make(tmp_path, blocks=100, fail_at=5)
    assert rec.start() in (True, False)
    rec._thread.join(5)
    assert not rec.ok and "0x100000001" in rec.error and not rec.stopping
    assert wav_frames(rec.parts[0].path) == 4 * ac.CHUNK                # записанное до сбоя читается


def test_recorder_without_soundcard_does_not_start(tmp_path):
    def broken():
        raise ImportError("нет soundcard")
    rec = LoopbackRecorder(tmp_path / "raw_1.wav", source=broken)
    assert rec.start() is False and "ImportError" in rec.error and rec.parts == []


# ── REC-2: сбой звука не молчит, запись перезапускается ──

def test_ensure_audio_logs_and_starts_next_part(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(rr.events_log, "log", lambda kind, text="", **d: logged.append((kind, text)))
    dead, _ = make(tmp_path, blocks=100, fail_at=3)
    dead.start()
    dead._thread.join(5)
    created = []

    def fake_cls(base, first_index=1):
        clock = Clock()
        src = FakeSource(clock, 10**6, sleep=0.01)
        r = LoopbackRecorder(base, first_index=first_index, source=lambda: src, mono=lambda: clock.t)
        src.recorder = r
        created.append(r)
        return r
    monkeypatch.setattr(rr, "LoopbackRecorder", fake_cls)
    recorder = rr.RunRecorder(str(tmp_path), lambda: {})
    recorder.current = rr.Recording("r", "К", tmp_path / "raw_000912.mp4", time.time(), audio=dead)
    try:
        assert recorder.ensure_audio() is True
        rec = recorder.current
        assert rec.audio is created[0] and rec.audio_old == [dead] and created[0].next_index == 3     # кусок a02 занят
        assert [k for k, _ in logged] == ["video", "video"] and "оборвалась" in logged[0][1] and "возобновлена" in logged[1][1]
        assert recorder.ensure_audio() is False                          # звук идёт: ничего не делаем
        rec.audio.error = "RuntimeError: снова"                           # новая запись тоже оборвалась
        assert recorder.ensure_audio() is False and len(logged) == 2     # слишком рано для новой попытки (30 с)
        rec.audio_retry_at = 0
        assert recorder.ensure_audio() is True and len(created) == 2
    finally:
        for r in created:
            r.stop()


def test_watchdog_restarts_audio_only_when_video_is_alive():
    base = {"game_running": True, "launch_ok": True, "mod_ready": True, "in_game": True, "menu_for_s": 0,
            "window": "front", "need_front": False, "recording_wanted": True, "recording": "alive",
            "camera_wanted": False, "camera_mode": "auto"}
    assert gw.plan_actions({**base, "audio_dead": True}) == ["restart_audio"]
    assert gw.plan_actions({**base, "audio_dead": False}) == []
    assert gw.plan_actions({**base, "recording": "dead", "audio_dead": True}) == ["restart_recording"]
    assert gw.plan_actions({**base, "audio_dead": True, "recording_wanted": False}) == []


# ── REC-6: предел длины видео ──

class AliveProc:
    def poll(self):
        return None


def test_recording_rolls_over_to_next_part_after_max_hours(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(rr.events_log, "log", lambda kind, text="", **d: logged.append((kind, text)))
    hours = [6.0]
    recorder = rr.RunRecorder(str(tmp_path), lambda: {"record_runs": True}, max_hours=lambda: hours[0])
    started = []
    monkeypatch.setattr(recorder, "start", lambda run_id, colony, why="": started.append((run_id, colony)))
    recorder.current = rr.Recording("r1", "Колония", tmp_path / "raw_1.mp4", time.time() - 5 * 3600, AliveProc())
    assert not recorder.overdue() and recorder.ensure_alive() is None
    hours[0] = 4.0
    assert recorder.overdue()
    old = recorder.ensure_alive()
    assert old is not None and old.outcome == "part" and started == [("r1", "Колония")] and recorder.current is None
    assert any("следующему куску" in t for _, t in logged)
    assert rr.RunRecorder(str(tmp_path), lambda: {}).overdue() is False       # без записи


def test_watchdog_plans_restart_when_recording_is_overdue():
    base = {"game_running": True, "launch_ok": True, "mod_ready": True, "in_game": True, "menu_for_s": 0,
            "window": "front", "need_front": False, "recording_wanted": True, "recording": "alive",
            "camera_wanted": False, "camera_mode": "auto"}
    assert gw.plan_actions({**base, "recording_overdue": True}) == ["restart_recording"]


async def test_max_record_hours_is_a_module_setting():
    manifest = await module_manifest.build()
    spec = manifest["settings_schema"]["max_record_hours"]
    assert spec["type"] == "number" and spec["default"] == 6 and spec["min"] > 0 and spec["max"] >= 6


# ── REC-1/REC-4: склейка кусков звука ──

def test_audio_timeline_places_parts_by_start_time():
    # кусок 0 на 0..100 с, пауза, кусок 1 с 130 с; видео 300 с
    assert rr.audio_timeline([(0.0, 100.0), (130.0, 100.0)], 300.0) == [
        ("part", 0, 0.0, 100.0, 1.0), ("silence", 30.0), ("part", 1, 0.0, 100.0, 1.0)]
    # звук начался раньше видео: начало отрезается; за концом видео звук не берётся
    assert rr.audio_timeline([(-2.0, 50.0)], 30.0) == [("part", 0, 2.0, 30.0, 1.0)]
    # кусок с опозданием: перед ним тишина
    assert rr.audio_timeline([(10.0, 5.0)], 60.0) == [("silence", 10.0), ("part", 0, 0.0, 5.0, 1.0)]


def test_audio_timeline_stretches_small_clock_drift_and_trims_big_overlap():
    tl = rr.audio_timeline([(0.0, 1802.5), (1800.0, 600.0)], 2400.0)
    assert tl[0][0] == "part" and tl[0][3] == 1802.5 and abs(tl[0][4] - 1802.5 / 1800.0) < 1e-9     # дрейф 0,14 %: темп
    tl = rr.audio_timeline([(0.0, 1900.0), (1800.0, 600.0)], 2400.0)
    assert tl[0] == ("part", 0, 0.0, 1800.0, 1.0)                                                  # заметный хвост: обрезка
    assert rr.audio_timeline([], 100.0) == [] and rr.audio_timeline([(500.0, 10.0)], 100.0) == []


def test_audio_windows_are_cut_from_part_files_with_silence_to_window_end():
    track = rr.AudioTrack([(Path("a1.wav"), 0.0, 100.0), (Path("a2.wav"), 130.0, 50.0)], 300.0)
    assert track.has_sound()
    # окно через конец первого куска, паузу и начало второго
    assert track.pieces(90.0, 140.0) == [("part", Path("a1.wav"), 90.0, 10.0, 1.0, 10.0), ("silence", 30.0),
                                         ("part", Path("a2.wav"), 0.0, 10.0, 1.0, 10.0)]
    assert track.pieces(200.0, 250.0) == []                                  # звука там нет: окно целиком тишина
    inputs, chains = rr.audio_window(track, 90.0, 140.0, 3, "a0")
    assert inputs == ["-ss", "90.000", "-t", "10.000", "-i", "a1.wav", "-ss", "0.000", "-t", "10.000", "-i", "a2.wav"]
    graph = ";".join(chains)
    assert "[3:a]" in graph and "[4:a]" in graph and "anullsrc" in graph and "concat=n=3:v=0:a=1" in graph
    assert "apad=whole_dur=50.000,atrim=end=50.000" in graph                 # REC-4: окно ровно по длине видео
    inputs, chains = rr.audio_window(track, 200.0, 250.0, 3, "a0")
    assert inputs == [] and chains[0].startswith("anullsrc=") and chains[0].endswith("[a0]")


def test_audio_window_uses_stretch_for_clock_drift():
    track = rr.AudioTrack([(Path("a1.wav"), 0.0, 1802.5), (Path("a2.wav"), 1800.0, 600.0)], 2400.0)
    (_, _, off, dur, tempo, out_len), = track.pieces(0.0, 1800.0)
    assert off == 0.0 and out_len == pytest.approx(1800.0) and dur == pytest.approx(1802.5) and tempo == pytest.approx(1802.5 / 1800)
    inputs, chains = rr.audio_window(track, 0.0, 1800.0, 1, "a0")
    assert "atempo=1.001389" in ";".join(chains)


def test_montage_and_clip_take_audio_from_files_without_shortest():
    track = rr.AudioTrack([(Path("a.wav"), 0.0, 600.0)], 600.0)
    scenes = [{"start": 0, "end": 25}, {"start": 570, "end": 600}]
    cut = rr.montage_args("ff", Path("v.mp4"), Path("o.mp4"), scenes, 600, track)
    graph = cut[cut.index("-filter_complex") + 1]
    assert cut.count("-i") == 4 and "-shortest" not in cut and "concat=n=2:v=1:a=1" in graph and "[aout]" in cut
    assert cut[cut.index("a.wav") - 5:cut.index("a.wav") + 1] == ["-ss", "0.000", "-t", "25.000", "-i", "a.wav"]
    clip = rr.clip_args("ff", Path("v.mp4"), Path("o.mp4"), 10, 40, track)
    assert "-shortest" not in clip and "[araw]afade" in clip[clip.index("-filter_complex") + 1] and "[aout]" in clip
    assert "[1:a]" in clip[clip.index("-filter_complex") + 1]
    assert "[aout]" not in rr.montage_args("ff", Path("v"), Path("o"), [{"start": 0, "end": 600}], 600, track)    # таймлапс без звука


@needs_ffmpeg
def test_short_audio_does_not_cut_the_video(tmp_path, monkeypatch):
    raw, wav = tmp_path / "raw_1.mp4", tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=5:duration=20",
                    "-pix_fmt", "yuv420p", str(raw)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
                    "-ac", "2", "-ar", "44100", str(wav)], check=True)
    monkeypatch.setattr(rr, "OUT_SIZE", "320:180")
    track = rr.AudioTrack([(wav, 2.0, 8.0)], 20.0)                     # звук с 2-й по 10-ю секунду, видео 20 с
    out = tmp_path / "m.mp4"
    assert rr.RunRecorder._run(rr.montage_args(shutil.which("ffmpeg"), raw, out,
                                               [{"start": 0, "end": 8}, {"start": 10, "end": 20}], 20, track), out)
    assert abs(duration_of(out) - 18.0) < 0.5                           # видео не обрезано по звуку
    audio = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    assert abs(float(audio) - 18.0) < 0.5                               # звук дополнен тишиной до конца


# ── REC-3: нарезка с перемоткой ──

def legacy_montage_args(ffmpeg, raw, out, scenes):
    """Прежний способ (фильтр trim из одного входа): эталон длительности."""
    scale = "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30"
    parts = [f"[0:v]trim={s['start']:.2f}:{s['end']:.2f},setpts=PTS-STARTPTS,{scale}[c{i}]" for i, s in enumerate(scenes)]
    graph = ";".join(parts) + ";" + "".join(f"[c{i}]" for i in range(len(scenes))) + f"concat=n={len(scenes)}:v=1:a=0[v]"
    return [ffmpeg, "-y", "-loglevel", "error", "-i", str(raw), "-filter_complex", graph, "-map", "[v]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)]


def duration_of(path):
    return float(rr.probe_duration(shutil.which("ffprobe"), Path(path)))


@pytest.mark.slow
@needs_ffmpeg
def test_five_scenes_from_five_hour_file_are_cut_fast_and_match_old_method(tmp_path, monkeypatch):
    raw = tmp_path / "raw_5h.mp4"
    t0 = time.time()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=5:duration=18000",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-movflags", "+frag_keyframe+empty_moov",
                    str(raw)], check=True)
    print(f"файл на 5 часов создан за {time.time() - t0:.0f} с")
    monkeypatch.setattr(rr, "OUT_SIZE", "320:180")                       # цель проверки: скорость перемотки, а не кодирования
    scenes = [{"start": s, "end": s + 20} for s in (30.0, 4000.0, 9000.0, 13000.0, 17900.0)]
    new = tmp_path / "new.mp4"
    t0 = time.time()
    assert rr.RunRecorder._run(rr.montage_args(shutil.which("ffmpeg"), raw, new, scenes, 18000), new)
    took = time.time() - t0
    print(f"монтаж 5 сцен из 5 часов: {took:.1f} с")
    assert took < 120
    # эталон прежним способом на сценах в начале файла (иначе trim декодирует часы видео)
    early = [{"start": s, "end": s + 20} for s in (30.0, 100.0, 200.0, 300.0, 400.0)]
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    assert rr.RunRecorder._run(rr.montage_args(shutil.which("ffmpeg"), raw, a, early, 18000), a)
    assert rr.RunRecorder._run(legacy_montage_args(shutil.which("ffmpeg"), raw, b, early), b)
    assert abs(duration_of(a) - duration_of(b)) <= 0.2 and abs(duration_of(new) - 100) <= 0.5
    clip = tmp_path / "clip.mp4"
    t0 = time.time()
    assert rr.RunRecorder._run(rr.clip_args(shutil.which("ffmpeg"), raw, clip, 16000.0, 16030.0), clip)
    assert time.time() - t0 < 60 and abs(duration_of(clip) - 30) <= 0.5


@needs_ffmpeg
def test_seek_cut_matches_old_method_on_short_file(tmp_path, monkeypatch):
    raw = tmp_path / "raw_1.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=5:duration=90",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-movflags", "+frag_keyframe+empty_moov",
                    str(raw)], check=True)
    monkeypatch.setattr(rr, "OUT_SIZE", "320:180")
    scenes = [{"start": 3.3, "end": 15.7}, {"start": 40.0, "end": 52.5}, {"start": 70.4, "end": 80.0}]
    new, old = tmp_path / "n.mp4", tmp_path / "o.mp4"
    assert rr.RunRecorder._run(rr.montage_args(shutil.which("ffmpeg"), raw, new, scenes, 90), new)
    assert rr.RunRecorder._run(legacy_montage_args(shutil.which("ffmpeg"), raw, old, scenes), old)
    assert abs(duration_of(new) - duration_of(old)) <= 0.2


# ── REC-5: повторная сборка записанного забега ──

def test_process_uses_given_events_out_dir_and_ignores_later_events(tmp_path, monkeypatch):
    raw = tmp_path / "run" / "raw_100000.mp4"
    raw.parent.mkdir()
    raw.write_bytes(b"0")
    out = tmp_path / "out"
    started = 1_000_000.0
    events = [{"ts": started + 100, "kind": "game", "text": "изменение: налёт", "data": {"type": "incident"}},
              {"ts": started + 900, "kind": "game", "text": "изменение: пожар", "data": {"type": "incident"}}]    # уже после конца
    cmds = []
    monkeypatch.setattr(rr, "MIN_RAW_BYTES", 1)
    monkeypatch.setattr(rr, "find_ffmpeg", lambda: "ff")
    monkeypatch.setattr(rr, "probe_duration", lambda *a: 600.0)
    monkeypatch.setattr(rr.events_log, "log", lambda *a, **k: None)
    monkeypatch.setattr(rr.RunRecorder, "_run", staticmethod(lambda cmd, o: (cmds.append(cmd), o.write_bytes(b"x"))[0] is None))
    wav = tmp_path / "run" / "raw_100000_a01.wav"
    rec = rr.Recording("r", "К", raw, started, duration=600, events=events, out_dir=out,
                       audio_extra=[AudioPart(wav, started + 1, 100 * RATE)], audio_sound=True)
    result = rr.RunRecorder(str(tmp_path), lambda: {}).process(rec)
    labels = [s["label"] for s in result["scenario"]["scenes"]]
    assert "изменение: налёт" in labels and "изменение: пожар" not in labels
    assert Path(result["video"]).parent == out and (out / "scenario_100000.json").is_file()
    assert not any(raw.parent.glob("montage*")) and result["audio"] is True
    montage = cmds[0]
    assert "-shortest" not in montage and str(wav) in montage and Path(montage[-1]).parent == out and "[aout]" in montage


def make_db(path, rows):
    events_log.init(str(path.parent))
    for kind, text, ts in rows:
        conn = events_log._connect()
        conn.execute("INSERT INTO events(ts, kind, text) VALUES (?,?,?)", (ts, kind, text))
        conn.commit()
        conn.close()


def test_produce_run_helpers(tmp_path):
    from tools import produce_run as pr
    folder = tmp_path / "colony_x"
    folder.mkdir()
    raw = folder / "raw_235500.mp4"
    raw.write_bytes(b"0")
    big = folder / "raw_000100.mp4"
    big.write_bytes(b"0" * 100)
    assert pr.pick_raw(folder, None) == big and pr.pick_raw(folder, "raw_235500.mp4") == raw
    (folder / "raw_000100_av.mp4").write_bytes(b"0" * 500)
    assert pr.pick_raw(folder, None) == big                              # склеенный со звуком файл не считается записью
    # забег перешёл через полночь: имя 23:55:00, файл дописан в 01:00 следующих суток, длительность 3900 с
    end = time.mktime((2026, 10, 12, 1, 0, 0, 0, 0, -1))
    import os
    os.utime(raw, (end, end))
    start = pr.guess_start(raw, 3900)
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start - 0.5)) == "2026-10-11 23:55:00"
    # уточнение по протоколу событий и название колонии
    db = tmp_path / "mem" / "events.db"
    make_db(db, [("video", "запись забега началась: Новая заря", start + 0.8), ("video", "запись забега началась: Не та", start + 500)])
    at, colony = pr.refine_start(db, start)
    assert colony == "Новая заря" and abs(at - (start + 0.3)) < 1e-6
    assert pr.refine_start(tmp_path / "нет.db", start) == (start, None)


def test_produce_run_finds_audio_parts_with_start_times(tmp_path):
    from tools import produce_run as pr
    raw = tmp_path / "raw_100000.mp4"
    raw.write_bytes(b"0")
    for i, at in ((1, 5000.5), (2, 6800.25)):
        wav = tmp_path / f"raw_100000_a{i:02d}.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"\0" * 4 * RATE)
        wav.with_suffix(".json").write_text(json.dumps({"started_at": at}), encoding="utf-8")
    parts = pr.audio_parts_for(raw, 5000.0)
    assert [(p.path.name, p.started_at, p.frames) for p in parts] == [
        ("raw_100000_a01.wav", 5000.5, RATE), ("raw_100000_a02.wav", 6800.25, RATE)]
    legacy = tmp_path / "raw_200000.mp4"
    legacy.write_bytes(b"0")
    (tmp_path / "raw_200000.wav").write_bytes(b"\0" * (44 + 4 * 3 * RATE))
    only = pr.audio_parts_for(legacy, 100.0)
    assert len(only) == 1 and only[0].started_at == pytest.approx(100.3) and only[0].frames == 3 * RATE
    assert pr.audio_parts_for(tmp_path / "raw_300000.mp4", 0) == []


@pytest.mark.slow
@needs_ffmpeg
def test_produce_run_on_recorded_folder_writes_only_to_out(tmp_path):
    from tools import produce_run as pr
    mem = tmp_path / "memory"
    folder = mem / "videos" / "colony_1"
    folder.mkdir(parents=True)
    raw = folder / "raw_tmp.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=5:duration=120",
                    "-pix_fmt", "yuv420p", "-movflags", "+frag_keyframe+empty_moov", str(raw)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=60", "-ac", "2",
                    "-ar", "44100", str(folder / "raw_tmp.wav")], check=True)
    end = raw.stat().st_mtime
    start = end - 120
    name = "raw_" + time.strftime("%H%M%S", time.localtime(start))            # имя файла несёт время начала записи
    raw = raw.rename(folder / f"{name}.mp4")
    (folder / "raw_tmp.wav").rename(folder / f"{name}.wav")
    # события основного протокола: налёт на 50-й секунде и запись о старте записи
    make_db(mem / "events.db", [("video", "запись забега началась: Заря", start + 0.5),
                                ("game", "изменение: налёт", start + 50)])
    events_log.init(str(tmp_path / "other"))
    before = sorted(p.name for p in folder.iterdir())
    out = tmp_path / "result"
    code = pr.main([str(folder), "--out", str(out), "--memory", str(mem)])
    assert code == 0 and sorted(p.name for p in folder.iterdir()) == before          # папка забега не тронута
    assert list(out.glob("montage_*.mp4")) and (out / "clips").is_dir() and list(out.glob("scenario_*.json"))
    plan = json.loads(next(out.glob("scenario_*.json")).read_text(encoding="utf-8"))
    assert plan["colony"] == "Заря" and any(s["label"] == "изменение: налёт" for s in plan["scenes"])
