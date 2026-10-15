"""Субтитры из журнала речи (MON-26a), слова по отрезку, расшифровка живой речи отложена (MON-29)."""
import json
import math
import struct
import wave

import pytest
from gits_sdk import ToolError

import transcribe
from transcribe import Utterance, load_speech_log, make_audible, words_between, words_from_speech_log


def test_say_events_become_utterances(tmp_path):
    path = tmp_path / "speech_log.json"
    path.write_text(json.dumps([{"ts": 1000.0, "kind": "say", "text": "Первая реплика."}, {"ts": 1010.5, "kind": "say", "text": "Вторая."},
                                {"ts": 1020.0, "kind": "game", "text": "не реплика"}]), encoding="utf-8")
    lines = load_speech_log(path)
    assert [(u.start_ts, u.text) for u in lines] == [(1000.0, "Первая реплика."), (1010.5, "Вторая.")]


def test_voice_speech_events_give_precise_sentence_times(tmp_path):
    rows = [{"ts": 100.0, "kind": "say", "text": "Раз. Два три."},
            {"ts": 103.0, "kind": "voice.speech", "data": {"phase": "segment", "utterance_id": "u1", "text": "Раз."}},
            {"ts": 104.2, "kind": "voice.speech", "data": {"phase": "segment", "utterance_id": "u1", "text": "Два три."}},
            {"ts": 105.9, "kind": "voice.speech", "data": {"phase": "ended", "utterance_id": "u1"}}]
    path = tmp_path / "log.json"
    path.write_text(json.dumps({"lines": rows}), encoding="utf-8")
    lines = load_speech_log(path)
    assert [(u.text, u.start_ts, u.end_ts) for u in lines] == [("Раз.", 103.0, 104.2), ("Два три.", 104.2, 105.9)]   # say с тем же текстом не дублируется


async def audible_all(start, dur):
    return True


async def test_words_are_timed_from_the_recording_start():
    lines = [Utterance(1005.0, "Подопытный нарушил протокол")]
    words, meta = await words_from_speech_log(lines, 1000.0, 60.0, audible_all)
    assert [w["text"] for w in words] == ["Подопытный", "нарушил", "протокол"] and [w["i"] for w in words] == [0, 1, 2]
    assert words[0]["start_ms"] == 5000 and words[0]["conf"] is None
    # время делится пропорционально длине слов (длина + 1)
    total = words[-1]["end_ms"] - words[0]["start_ms"]
    assert abs((words[0]["end_ms"] - words[0]["start_ms"]) - total * 11 / 28) < 30
    assert all(a["end_ms"] <= b["start_ms"] + 1 for a, b in zip(words, words[1:], strict=False))
    assert meta["origin"] == "speech_log" and meta["words"] == 3 and "source" not in words[0]


async def test_duration_uses_the_event_end_when_known_and_never_overlaps_the_next_line():
    lines = [Utterance(10.0, "Длинная фраза про испытание", end_ts=12.0), Utterance(11.0, "Перебила")]
    words, _ = await words_from_speech_log(lines, 0.0, 60.0, audible_all)
    first = [w for w in words if w["i"] < 4]
    assert first[-1]["end_ms"] <= 11000                                         # обрезана началом следующей реплики
    assert words[4]["start_ms"] == 11000


async def test_lines_outside_the_recording_and_not_in_the_audio_get_no_subtitle():
    async def only_second(start, dur):
        return start > 20
    lines = [Utterance(990.0, "до записи"), Utterance(1010.0, "нет в звуке"), Utterance(1030.0, "есть в звуке"), Utterance(1100.0, "после конца")]
    words, meta = await words_from_speech_log(lines, 1000.0, 60.0, only_second)
    assert [w["text"] for w in words] == ["есть", "в", "звуке"]
    assert meta["skipped"] == {"outside": 2, "silent": 1}


async def test_source_is_set_only_when_given():
    words, _ = await words_from_speech_log([Utterance(5.0, "Слово")], 0.0, 30.0, None, source="sources/raw_h264.mp4")
    assert words[0]["source"] == "sources/raw_h264.mp4"


def tone_wav(path, seconds, tone=(2.0, 4.0), rate=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            t = i / rate
            v = int(12000 * math.sin(2 * math.pi * 440 * t)) if tone[0] <= t < tone[1] else 0
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


async def test_audible_checks_the_real_audio_window(tmp_path):
    wav = tmp_path / "part.wav"
    tone_wav(wav, 8.0)
    check = make_audible([(wav, 10.0, 8.0)])                                   # кусок звука начинается на 10-й секунде видео
    assert await check(12.5, 1.0) is True                                       # тон в окне
    assert await check(15.0, 1.5) is False                                      # окно в тишине
    assert await check(30.0, 2.0) is False                                      # окна нет в звуке (сбой записи)


def test_words_between_returns_only_the_window():
    words = [{"i": k, "text": "с", "start_ms": k * 1000, "end_ms": k * 1000 + 500} for k in range(10)]
    got = words_between(words, 2500, 5200)
    assert [w["i"] for w in got] == [3, 4, 5]
    assert len(words_between(words * 100, 0, 10 ** 9)) == 400


def test_live_speech_transcription_is_unavailable():
    with pytest.raises(ToolError) as e:
        transcribe.transcribe_live("x.wav")
    assert e.value.code == "unavailable" and "WhisperX" in e.value.message
