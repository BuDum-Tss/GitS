"""Моменты длинных исходников: ранжирование, речь (нарезка, stt, поиск), аргументы и команды scan, moments, speech, find, peek, clip.
ffmpeg настоящий (синтетическое видео 20 с: тишина, громкий тон посередине), stt и OCR заглушки (сети и контейнера нет)."""
import asyncio
import json
import subprocess

import pytest
from gits_sdk import ToolError

import commands as commands_mod
import media
import moments
import qa as qa_mod
import speech
import stt_client
from tests.rig import Rig

HAVE_FFMPEG = True
try:
    media.find_ffmpeg()
except FileNotFoundError:
    HAVE_FFMPEG = False
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="нет ffmpeg")
pytestmark = pytest.mark.usefixtures("fast")


# ---------------------------------------------------------------- чистые функции: моменты

def series(n=60, loud=(), base=-60.0, level=-20.0):
    rms = [base] * n
    for s in loud:
        rms[s] = level
    return {"rms_db": rms, "motion": [0.0] * n, "cuts": [0] * n, "silences": [], "has_audio": True}


def test_normalized_loudness_flat_audio_has_no_peaks():
    assert set(moments.normalized_loudness([-30.0] * 20)) == {0.0}
    norm = moments.normalized_loudness([-60.0] * 50 + [-20.0] * 3)
    assert max(norm) == 1.0 and min(norm) == 0.0


def test_rank_suppresses_neighbours_and_bounds_windows():
    data = series(60, loud=(10, 12, 13, 40))           # 10, 12 и 13 лежат рядом: из них нужен один момент
    found = moments.rank_moments(data, 60.0)
    ats = sorted(m["at_ms"] // 1000 for m in found)
    assert any(8 <= a <= 14 for a in ats) and any(a == 40 for a in ats)
    assert all(b - a >= 10 for a, b in zip(ats, ats[1:]))              # между пиками не меньше 10 с
    assert all(0 <= m["in_ms"] < m["at_ms"] < m["out_ms"] <= 60_000 and 0 <= m["score"] <= 1 for m in found)
    assert found[0]["score"] >= found[-1]["score"] and "громко" in found[0]["tags"]


def test_rank_clips_window_to_file_edges_and_handles_empty():
    near_start = moments.rank_moments(series(20, loud=(0,)), 20.0)
    assert near_start and near_start[0]["in_ms"] == 0
    near_end = moments.rank_moments(series(20, loud=(19,)), 20.0)
    assert near_end and near_end[0]["out_ms"] == 20_000
    assert moments.rank_moments({}, 20.0) == [] and moments.rank_moments(series(20), 0.0) == []
    assert len(moments.rank_moments(series(400, loud=range(5, 400, 11)), 400.0, top=5)) == 5


def test_rank_tags_cuts_quiet_before_loud_and_speech():
    data = series(40, loud=(20, 21))
    data["cuts"][20] = 2
    data["motion"][20] = 0.8
    data["silences"] = [[10.0, 19.5]]
    data["speech"] = [{"start_ms": 18000, "end_ms": 24000, "text": "одна две три четыре пять шесть семь восемь"}]
    top = moments.rank_moments(data, 40.0)[0]
    assert {"громко", "смена кадра", "резкая тишина", "речь", "реплика"} <= set(top["tags"])
    assert top["text"].startswith("одна две")
    plain = moments.rank_moments({**data, "speech": []}, 40.0)[0]
    assert top["score"] > plain["score"] and "речь" not in plain["tags"]


def test_rank_without_audio_uses_picture_only():
    data = {"rms_db": [], "motion": [0.0] * 30, "cuts": [0] * 30, "silences": [], "has_audio": False}
    data["cuts"][15] = 3
    data["motion"][15] = 0.7
    top = moments.rank_moments(data, 30.0)[0]
    assert abs(top["at_ms"] // 1000 - 15) <= 3 and top["tags"][0] == "смена кадра"


def test_select_moments_filters_and_limits():
    ms = moments.assign_ids([("a.mp4", [{"at_ms": 1000, "in_ms": 0, "out_ms": 5000, "score": 0.5, "tags": []},
                                        {"at_ms": 90000, "in_ms": 86000, "out_ms": 94000, "score": 0.9, "tags": ["громко"]}]),
                             ("b.mp4", [{"at_ms": 5000, "in_ms": 1000, "out_ms": 9000, "score": 0.7, "tags": ["речь"], "text": "привет"}])])
    assert [m["id"] for m in ms] == ["m1", "m2", "m3"]
    assert [m["id"] for m in moments.select_moments(ms)] == ["m2", "m3", "m1"]
    assert [m["id"] for m in moments.select_moments(ms, source="b.mp4")] == ["m3"]
    assert [m["id"] for m in moments.select_moments(ms, from_ms=2000, to_ms=80000)] == ["m3"]
    assert len(moments.select_moments(ms, limit=1)) == 1 and len(moments.select_moments(ms * 30, limit=999)) == moments.MAX_LIMIT
    assert moments.moment_line(ms[2]) == "m3 b.mp4 [0:01–0:09] in=1000 out=9000 at=5000 0.70 речь «привет»"
    assert moments.clock(3_725_000) == "1:02:05"


def test_merge_texts_drops_repeats():
    assert moments.merge_texts(["Привет, мир", "привет мир!", "", "Совсем другое", "совсем другое."]) == ["Привет, мир", "Совсем другое"]


def test_parse_scan_output():
    out = ("frame:0 pts:0 pts_time:0\nlavfi.scene_score=0.000000\nframe:0 pts:0 pts_time:0\nlavfi.astats.Overall.RMS_level=-inf\n"
           "frame:1 pts:4000 pts_time:0.5\nlavfi.scene_score=0.800000\nframe:1 pts:8000 pts_time:1\n"
           "lavfi.astats.Overall.RMS_level=-20.5\nframe:3 pts_time:1.5\nlavfi.scene_score=0.1\n")
    err = "[silencedetect @ 0x1] silence_start: 0\n[silencedetect @ 0x1] silence_end: 0.9 | silence_duration: 0.9\n" \
          "[silencedetect @ 0x1] silence_start: 1.5\n"
    d = media.parse_scan_output(out, err, 2.0)
    assert d["rms_db"] == [-90.0, -20.5] and d["cuts"] == [1, 0] and d["motion"] == [0.4, 0.1]
    assert d["silences"] == [[0.0, 0.9], [1.5, 2.0]]


# ---------------------------------------------------------------- чистые функции: речь

def test_speech_spans_and_chunks():
    assert speech.speech_spans([[0.0, 8.0], [12.0, 20.0]], 20.0) == [(8.0, 12.0)]
    assert speech.speech_spans([], 10.0) == [(0.0, 10.0)] and speech.speech_spans([[0.0, 10.0]], 10.0) == []
    # паузы остаются границами, близкие участки склеиваются до 25 с
    chunks = speech.split_chunks([(0, 5), (7, 12), (40, 42)], 60.0)
    assert [(a // 1000, b // 1000) for a, b in chunks] == [(0, 12), (39, 42)]
    # длинный участок без пауз режется на куски не длиннее 25 с (с запасом на отступы) в самом тихом месте
    rms = [-20.0] * 100
    rms[17] = -80.0
    long = speech.split_chunks([(0.0, 60.0)], 60.0, rms)
    assert all((b - a) <= 25_400 for a, b in long) and len(long) == 3
    assert abs(long[0][1] / 1000 - 17.5) <= 0.5                          # режет по тихой секунде
    assert speech.split_chunks([(5.0, 5.2)], 60.0) == []                  # короче 0,4 с не отправляется
    assert len(speech.split_chunks([(i * 30.0, i * 30.0 + 20) for i in range(900)], 30000.0)) == speech.MAX_CHUNKS
    assert speech.pcm_slice_bounds(1000, 3000) == (32000, 64000)


SPEECH = [{"start_ms": 8000, "end_ms": 12000, "text": "Робот взорвал ракету на складе"},
          {"start_ms": 30000, "end_ms": 34000, "text": "Ракеты закончились, роботов нет"},
          {"start_ms": 50000, "end_ms": 51000, "text": "Совсем про другое"}]


def test_stems_and_find():
    assert speech.stem("Ракету") == speech.stem("ракеты") == "раке" and speech.stem("кот") == "кот" and speech.stem("Ёлка") == "елка"
    hits = speech.find_in_speech(SPEECH, "РАКЕТА", 60.0)
    assert [h["start_ms"] for h in hits] == [8000, 30000]
    assert all(0 <= h["in_ms"] <= h["at_ms"] <= h["out_ms"] <= 60_000 and h["out_ms"] - h["in_ms"] <= 8000 for h in hits)
    assert 8000 <= hits[0]["at_ms"] <= 12000
    assert [h["start_ms"] for h in speech.find_in_speech(SPEECH, "робот ракета", 60.0)][0] == 8000
    assert speech.find_in_speech(SPEECH, "ра", 60.0) == [] and speech.find_in_speech(SPEECH, "самолёт", 60.0) == []
    assert speech.find_in_speech(SPEECH, "робот склад", 60.0)[0]["start_ms"] == 8000          # все слова в одной строке
    assert len(speech.find_in_speech(SPEECH * 20, "ракета", 60.0)) == speech.FIND_LIMIT


def test_speech_lines_limits():
    many = [{"start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": "х" * 300} for i in range(100)]
    lines, rest = speech.speech_lines(many)
    assert len(lines) == 60 and rest == 40 and all(len(ln) <= 215 for ln in lines) and lines[0].startswith("[0:00–0:00] ")
    lines, rest = speech.speech_lines(many, 10_000, 12_000)
    assert len(lines) == 3 and rest == 0 and lines[0].startswith("[0:10")


# ---------------------------------------------------------------- клиент stt (заглушка вместо сети)

def scripted(*answers):
    calls = []

    async def post(pcm):
        calls.append(len(pcm))
        a = answers[min(len(calls), len(answers)) - 1]
        if isinstance(a, Exception):
            raise a
        return a
    return post, calls


async def test_stt_client_retries_503_skips_400_and_reports_unavailable():
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
    post, calls = scripted((503, None, 1.5), (503, None, None), (200, {"text": " привет "}, None))
    assert await stt_client.HttpStt(post=post, sleep=sleep).transcribe(b"\0\0") == "привет"
    assert len(calls) == 3 and sleeps == [1.5, 2.0]
    post, _ = scripted((400, {"error": "garbage"}, None))
    assert await stt_client.HttpStt(post=post).transcribe(b"\0\0") == ""               # мусор не ошибка
    post, calls = scripted((503, None, None))
    with pytest.raises(stt_client.SttUnavailable):
        await stt_client.HttpStt(post=post, sleep=sleep, max_retries=2).transcribe(b"\0\0")
    assert len(calls) == 3
    post, _ = scripted(stt_client.SttUnavailable("нет сети"))
    with pytest.raises(stt_client.SttUnavailable):
        await stt_client.HttpStt(post=post).transcribe(b"\0\0")
    post, _ = scripted((500, None, None))
    with pytest.raises(stt_client.SttUnavailable):
        await stt_client.HttpStt(post=post).transcribe(b"\0\0")


async def test_stt_client_real_connection_failure_is_unavailable():
    with pytest.raises(stt_client.SttUnavailable):
        await stt_client.HttpStt("http://127.0.0.1:9", timeout_s=3).transcribe(b"\0\0" * 3200)


async def test_transcribe_chunks_limits_concurrency_and_stops_when_down():
    running, peak = [0], [0]

    async def tr(pcm):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.01)
        running[0] -= 1
        return "" if len(pcm) == 0 else f"текст {len(pcm)}"
    chunks = [(i * 1000, i * 1000 + 500) for i in range(8)]
    got, down = await speech.transcribe_chunks(chunks, lambda a, b: b"\0" * 64, tr, concurrency=2)
    assert not down and peak[0] == 2 and [g["start_ms"] for g in got] == [c[0] for c in chunks]
    calls = []

    async def dead(pcm):
        calls.append(1)
        raise stt_client.SttUnavailable("нет")
    got, down = await speech.transcribe_chunks(chunks, lambda a, b: b"\0", dead, concurrency=2)
    assert down and got == [] and len(calls) <= 2                            # после отказа остальные куски не шлются


# ---------------------------------------------------------------- аргументы

VIDEOS = ["a_h264.mp4", "b.mp4"]


def bad(command, *args):
    with pytest.raises(ToolError) as e:
        commands_mod.parse_args(command, list(args), VIDEOS)
    assert e.value.code == "invalid"


def test_parse_args_validation():
    p = commands_mod.parse_args
    assert p("scan", [], VIDEOS) == {} and p("scan", ["b.mp4"], VIDEOS) == {"source": "b.mp4"}
    assert p("scan", ["sources/b.mp4"], VIDEOS) == {"source": "b.mp4"} and p("scan", ["-"], VIDEOS) == {}
    bad("scan", "c.mp4")
    bad("scan", "../b.mp4")
    bad("scan", "b.mp4", "x")
    assert p("moments", ["-", "10", "1000", "5000"], VIDEOS) == {"limit": 10, "from_ms": 1000, "to_ms": 5000}
    assert p("moments", ["a_h264.mp4", "-", "-", "5000"], VIDEOS) == {"source": "a_h264.mp4", "to_ms": 5000}
    bad("moments", "-", "0")
    bad("moments", "-", "-1")
    bad("moments", "-", "x")
    bad("moments", "-", "5", "9000", "1000")
    assert p("speech", ["-", "-", "100"], VIDEOS) == {"to_ms": 100}
    assert p("peek", ["b.mp4", "12000"], VIDEOS) == {"source": "b.mp4", "t_ms": 12000}
    bad("peek", "b.mp4")
    bad("peek", "-", "10")
    bad("peek", "b.mp4", "1.5")
    assert p("clip", ["b.mp4", "1000", "5000"], VIDEOS) == {"source": "b.mp4", "in_ms": 1000, "out_ms": 5000}
    bad("clip", "b.mp4", "5000", "5000")
    bad("clip", "b.mp4", "5000", "1000")
    bad("clip", "b.mp4", "1000")
    bad("clip", "C:/x.mp4", "1", "2")
    assert p("find", ["ракета"], VIDEOS) == {"query": "ракета"}
    assert p("find", ["взорвал", "ракету", "b.mp4"], VIDEOS) == {"query": "взорвал ракету", "source": "b.mp4"}
    bad("find")
    bad("find", "я")
    bad("find", "x" * 61)
    bad("find", "ракета\x00")
    assert commands_mod.parse_args("still", ["5"]) == {"frame": "5"}
    with pytest.raises(ToolError):
        commands_mod.parse_args("peek", ["b.mp4", "1"], None)                    # без info.json источник не проверить: отказ


# ---------------------------------------------------------------- команды на настоящем ffmpeg

async def call(rig, *args):
    """Быстрая команда: результат команды из ответа инструмента run."""
    return (await rig.run(*args))["result"]


def make_video(path, with_audio=True):
    """20 с: видео testsrc2 (резкая смена на 10-й секунде), звук: тишина 8 с, тон 4 с, тишина 8 с."""
    ff = media.find_ffmpeg()
    args = [ff, "-hide_banner", "-y", "-f", "lavfi", "-i", "testsrc2=s=128x96:r=10:d=10", "-f", "lavfi", "-i", "testsrc=s=128x96:r=10:d=10"]
    if with_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=8", "-f", "lavfi", "-i", "sine=f=700:r=16000:d=4",
                 "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=8",
                 "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a][3:a][4:a]concat=n=3:v=0:a=1[a]", "-map", "[v]", "-map", "[a]",
                 "-c:a", "aac"]
    else:
        args += ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    res = subprocess.run(args, capture_output=True)
    assert res.returncode == 0, res.stderr[-400:]


class FakeStt:
    def __init__(self, text="Робот взорвал ракету на складе, всем отойти подальше", fail=False):
        self.text, self.fail, self.sizes = text, fail, []

    async def transcribe(self, pcm):
        self.sizes.append(len(pcm))
        if self.fail:
            raise stt_client.SttUnavailable("stt недоступен")
        return self.text

    async def aclose(self):
        pass


@pytest.fixture
async def unit(tmp_path):
    """Стенд с единицей, в sources которой лежат два синтетических видео и info.json (intake пропущен)."""
    rig = Rig(tmp_path)
    jid = await rig.create()
    job = rig.unit(jid)
    make_video(job.sources / "talk.mp4")
    make_video(job.sources / "mute.mp4", with_audio=False)
    files = []
    for name, audio in (("talk.mp4", True), ("mute.mp4", False)):
        mi = await media.probe(job.sources / name)
        files.append({"kind": "video", "name": name, "path": f"sources/{name}", "duration_ms": int(mi.duration_s * 1000),
                      "width": mi.width, "height": mi.height, "has_audio": mi.has_audio})
    (job.sources / "info.json").write_text(json.dumps({"source_kind": "generic", "files": files}), encoding="utf-8")
    rig.stt = FakeStt()
    rig.manager.cmds.stt_factory = lambda: rig.stt
    rig.manager.cmds.qa = type("Q", (), {"ocr": None})()
    rig.jid, rig.job = jid, job
    return rig


async def run_bg(rig, *args):
    started = await rig.run(*args, background=True)
    return await rig.wait_run(started["job_id"], 60)


@needs_ffmpeg
async def test_scan_moments_speech_find_peek_clip(unit, monkeypatch):
    rig, jid, job = unit, unit.jid, unit.job
    with pytest.raises(ToolError) as e:
        await call(rig, "moments", jid)
    assert e.value.code == "unavailable" and "run scan" in e.value.message
    with pytest.raises(ToolError) as e:
        await call(rig, "find", jid, "ракета")
    assert e.value.code == "unavailable"

    # scan: фон, у единицы одна исключительная операция
    started = await rig.run("scan", jid, background=True)
    assert "job_id" in started
    with pytest.raises(ToolError) as e:
        await rig.run("scan", jid, background=True)
    assert e.value.code == "busy"
    last = await rig.wait_run(started["job_id"], 60)
    assert last["state"] == "done" and "talk.mp4" in last["text"] and "mute.mp4" in last["text"]
    cache = json.loads((job.folder / "analysis" / "talk.mp4.json").read_text(encoding="utf-8"))
    assert len(cache["rms_db"]) == 20 and cache["has_audio"] and cache["speech_done"]
    assert cache["rms_db"][10] > cache["rms_db"][2] + 30                         # тон громче тишины
    assert any(a <= 1 and b >= 7 for a, b in cache["silences"]) and any(a >= 11 for a, _ in cache["silences"])
    assert sum(cache["cuts"]) >= 1                                              # смена кадра на 10-й секунде
    assert cache["speech"] and 7000 <= cache["speech"][0]["start_ms"] <= 8500 and cache["speech"][0]["end_ms"] <= 12500
    assert rig.stt.sizes == [rig.stt.sizes[0]] and 4000 * 32 <= rig.stt.sizes[0] <= 4400 * 32      # один кусок 4 с PCM 16 кГц
    mute = json.loads((job.folder / "analysis" / "mute.mp4.json").read_text(encoding="utf-8"))
    assert not mute["has_audio"] and mute["speech"] == []

    # moments
    out = await call(rig, "moments", jid)
    lines = out["text"].splitlines()[1:]
    assert out["moments"] and lines[0].split()[1] == "talk.mp4"
    assert "громко" in out["moments"][0]["tags"] and out["moments"][0]["source"] == "talk.mp4"
    assert 8000 <= out["moments"][0]["at_ms"] <= 12500 and "речь" in out["moments"][0]["tags"] and "«Робот" in lines[0]
    assert len(json.loads((job.folder / "analysis" / "moments.json").read_text(encoding="utf-8"))["moments"]) >= len(out["moments"])
    only = await call(rig, "moments", jid, "mute.mp4", "2")
    assert len(only["moments"]) <= 2 and all(m["source"] == "mute.mp4" for m in only["moments"])
    assert "нет" in (await call(rig, "moments", jid, "-", "5", "19000", "19500"))["text"]

    # кэш общий: файл не менялся, ни звук, ни речь повторно не считаются
    rig.stt.sizes.clear()
    real = media.analyze_video

    async def boom(*a, **k):
        raise AssertionError("повторный анализ")
    media.analyze_video = boom
    try:
        again = await run_bg(rig, "scan", jid)
    finally:
        media.analyze_video = real
    assert again["state"] == "done" and not rig.stt.sizes and "из кэша" in again["text"]

    # speech и find
    sp = await call(rig, "speech", jid)
    assert sp["lines"][0].startswith("[0:07") or sp["lines"][0].startswith("[0:08")
    assert "Робот взорвал" in sp["lines"][0]
    assert "нет" in (await call(rig, "speech", jid, "-", "15000", "19000"))["text"]
    hit = await call(rig, "find", jid, "ракеты")
    assert hit["matches"][0]["source"] == "talk.mp4" and hit["matches"][0]["in_ms"] < hit["matches"][0]["at_ms"] < hit["matches"][0]["out_ms"]
    assert "(~at=" in hit["text"]
    assert "не найдено" in (await call(rig, "find", jid, "самолёт"))["text"]
    assert (await call(rig, "find", jid, "ракету", "talk.mp4"))["matches"]

    # peek: без OCR честная пометка, уровни громкости и речь рядом из кэша
    monkeypatch.setattr(qa_mod, "ocr_available", lambda: False)                  # не зависеть от установленного RapidOCR
    peek = await call(rig, "peek", jid, "talk.mp4", "10000")
    assert len(peek["frames"]) == 3 and all((job.folder / f).is_file() for f in peek["frames"])
    assert peek["frames"][0] == "renders/peek_talk_10000_0.png" and not peek["ocr"]
    assert "OCR не установлен" in peek["text"] and "громкость по секундам" in peek["text"] and "рядом говорят" in peek["text"]
    texts = iter(["Опасность: утечка", "опасность утечка", ""])

    async def fake_ocr(path):
        return next(texts)
    rig.manager.cmds.qa = type("Q", (), {"ocr": staticmethod(fake_ocr)})()
    peek = await call(rig, "peek", jid, "mute.mp4", "19999")                       # без кэша уровней; время у самого конца
    assert peek["ocr"] and peek["texts"] == ["Опасность: утечка"] and "«Опасность: утечка»" in peek["text"] and "OCR не" not in peek["text"]
    with pytest.raises(ToolError) as e:
        await call(rig, "peek", jid, "talk.mp4", "99999999")
    assert e.value.code == "invalid"

    # clip
    clip = await call(rig, "clip", jid, "talk.mp4", "7000", "13000")
    assert clip["path"] == "clips/talk_7000_13000.mp4" and clip["duration_ms"] == 6000
    mi = await media.probe(job.folder / clip["path"])
    assert 5.0 <= mi.duration_s <= 7.0 and mi.has_video and mi.has_audio
    assert not list((job.folder / "clips").glob("*.part*"))
    for args in (("talk.mp4", "1000", "1500"), ("talk.mp4", "0", "125000"), ("talk.mp4", "15000", "21000")):
        with pytest.raises(ToolError) as e:
            await call(rig, "clip", jid, *args)
        assert e.value.code == "invalid"
    mclip = await call(rig, "clip", jid, "mute.mp4", "0", "2000")                  # без звука тоже режется
    assert (await media.probe(job.folder / mclip["path"])).has_video


@needs_ffmpeg
async def test_scan_survives_unavailable_stt_and_retries_speech_later(unit):
    rig, jid, job = unit, unit.jid, unit.job
    rig.stt.fail = True
    first = await run_bg(rig, "scan", jid, "talk.mp4")
    assert first["state"] == "done" and "речь не распознана: stt недоступен" in first["text"]
    cache = json.loads((job.folder / "analysis" / "talk.mp4.json").read_text(encoding="utf-8"))
    assert cache["rms_db"] and cache["speech"] == [] and not cache["speech_done"]
    with pytest.raises(ToolError) as e:
        await call(rig, "speech", jid)
    assert e.value.code == "unavailable" and "stt" in e.value.message
    assert (await call(rig, "moments", jid))["moments"]                              # моменты по звуку есть и без речи
    rig.stt.fail = False
    rig.stt.sizes.clear()
    second = await run_bg(rig, "scan", jid, "talk.mp4")
    assert "речь не распознана" not in second["text"] and rig.stt.sizes and (await call(rig, "speech", jid))["lines"]


@needs_ffmpeg
async def test_scan_rejects_unknown_source_and_missing_video(unit):
    rig, jid, job = unit, unit.jid, unit.job
    for args in (("scan", jid, "../talk.mp4"), ("peek", jid, "zzz.mp4", "5"), ("clip", jid, "C:/Windows/x.mp4", "1", "2000")):
        with pytest.raises(ToolError) as e:
            await call(rig, *args)
        assert e.value.code == "invalid"
    (job.sources / "info.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    with pytest.raises(ToolError) as e:
        await call(rig, "peek", jid, "talk.mp4", "5")
    assert e.value.code == "invalid" and "intake" in e.value.message


async def test_analysis_and_clips_are_readable_not_writable(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    job = rig.unit(jid)
    for name in ("analysis", "clips"):
        (job.folder / name).mkdir()
        (job.folder / name / "x.txt").write_text("данные", encoding="utf-8")
        assert (await rig.files("read", f"{jid}/{name}/x.txt"))["content"] == "данные"
        for op, kw in (("write", {"content": "взлом"}), ("delete", {})):
            with pytest.raises(ToolError) as e:
                await rig.files(op, f"{jid}/{name}/x.txt", **kw)
            assert e.value.code == "denied"
