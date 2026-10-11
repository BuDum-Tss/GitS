"""Модуль голоса против FakeCore (VOICE-20): протокол речи, tts.synthesize, события, инструменты, настройки."""
import asyncio
import os
import time
import wave

import pytest
from glados_module.protocol import ToolError, validate_manifest

import common
import opus_encoder
from manifest import MANIFEST, build_manifest
from voice_rig import FakePlayer, FakeSTT, Rig, until, vec


def test_manifest_is_valid_and_matches_the_spec():
    m = build_manifest()
    assert validate_manifest(m) == []
    assert (m["id"], m["title"], m["capabilities"], m["subscribe"], m["state"]) == \
        ("voice", "Голос", ["voice.output", "voice.input"], ["settings", "module"], True)
    assert m["shared_settings"] == ["voice_muted", "volume", "mic", "hearing", "dnd", "quiet_until"]
    assert m["settings_schema"]["barge_in"]["default"] is False
    assert m["settings_schema"]["echo_tail_ms"] == {"type": "integer", "title": m["settings_schema"]["echo_tail_ms"]["title"],
                                                      "default": 600, "min": 0, "max": 3000}
    assert [t["name"] for t in m["tools"]] == ["recent_audio", "name_voice"]
    assert all(t["risk"] == "auto" and t["availability"] == "always" for t in m["tools"])
    assert {e["kind"] for e in m["emits"]} >= {"voice.utterance", "voice.speech", "voice.user_speaking", "voice.heard"}
    assert next(e for e in m["emits"] if e["kind"] == "voice.heard")["default_urgency"] == 1
    assert build_manifest() == MANIFEST and build_manifest() is not MANIFEST


async def test_speak_gives_started_segment_ended_events(tmp_path):
    async with Rig(tmp_path) as rig:
        await rig.speak("u-1a2b3c4d", "Первое предложение. Второе предложение.", emotion="angry")
        end = await rig.wait_ended("u-1a2b3c4d")
        await until(lambda: len(rig.core.events) >= 3)
        assert [(d["phase"], d.get("seq")) for d in rig.speech()] == [("started", 0), ("segment", 1), ("ended", None)]
        assert rig.speech("started")[0]["segment"] == "Первое предложение."
        assert end == {"phase": "ended", "utterance_id": "u-1a2b3c4d", "completed": True, "spoken_upto": 2}
        assert all(e["kind"].startswith("voice.") for e in rig.core.events)
        assert all(e["text"] for e in rig.core.events)


async def test_replace_queue_and_stop_over_the_protocol(tmp_path):
    async with Rig(tmp_path, hold=True) as rig:
        await rig.speak("u-1", "Первая реплика. Её продолжение.")
        await until(lambda: len(rig.player.played) == 1)
        await rig.speak("u-2", "Следующая.")                                  # replace по умолчанию false: в очередь
        await rig.speak("u-3", "Срочная.", replace=True)
        assert (await rig.wait_ended("u-1"))["reason"] == "replaced"
        assert (await rig.wait_ended("u-2"))["reason"] == "replaced"
        await until(lambda: len(rig.player.played) == 2)
        await rig.stop(None, "now")
        assert (await rig.wait_ended("u-3"))["reason"] == "stop"


async def test_stop_segment_end_over_the_protocol(tmp_path):
    async with Rig(tmp_path, hold=True) as rig:
        await rig.speak("u-1", "Раз. Два.")
        await until(lambda: len(rig.player.played) == 1)
        await rig.stop("u-1", "segment_end")
        await asyncio.sleep(0.05)
        assert rig.speech("ended") == []
        rig.player.finish()
        end = await rig.wait_ended("u-1")
        assert end["completed"] is False and end["spoken_upto"] == 1 and end["reason"] == "stop"


async def test_muted_setting_gives_instant_events_without_sound(tmp_path):
    async with Rig(tmp_path, settings={"voice_muted": True}) as rig:
        await rig.speak("u-1", "Тихо.")
        end = await rig.wait_ended("u-1")
        assert end["reason"] == "muted" and end["completed"] is False
        assert rig.speech("started")[0]["segment"] == "Тихо."
        assert rig.player.played == []


async def test_muting_in_the_middle_aborts_speech(tmp_path):
    async with Rig(tmp_path, hold=True) as rig:
        await rig.speak("u-1", "Звучит. И продолжает.")
        await until(lambda: len(rig.player.played) == 1)
        await rig.core.update_settings({"voice_muted": True})
        assert (await rig.wait_ended("u-1"))["reason"] == "muted"


async def test_volume_setting_is_applied_to_playback(tmp_path):
    async with Rig(tmp_path, settings={"volume": 40}) as rig:
        await rig.speak("u-1", "Тише.")
        await rig.wait_ended("u-1")
        assert rig.player.volumes == [0.4]
        await rig.core.update_settings({"volume": 100})
        await until(lambda: rig.client.settings["volume"] == 100)
        await rig.speak("u-2", "Громче.")
        await rig.wait_ended("u-2")
        assert rig.player.volumes == [0.4, 1.0]


async def test_disabling_the_module_stops_speech(tmp_path):
    async with Rig(tmp_path, hold=True) as rig:
        await rig.speak("u-1", "Звучит.")
        await until(lambda: len(rig.player.played) == 1)
        await rig.core.set_enabled(False)
        await until(lambda: not rig.mod.speaker.is_speaking())
        await rig.speak("u-2", "Выключена.")                                  # выключенный модуль не говорит
        await asyncio.sleep(0.05)
        assert rig.mod.speaker.waiting_uids() == []


async def test_bad_speak_messages_are_ignored(tmp_path):
    async with Rig(tmp_path) as rig:
        await rig.core.send("speak", {"text": "без id"})
        await rig.core.send("speak", {"utterance_id": "u-1"})
        await rig.speak("u-2", "Нормальная.")
        assert (await rig.wait_ended("u-2"))["completed"]
        assert [d["utterance_id"] for d in rig.speech("ended")] == ["u-2"]


# ── tts.synthesize (VOICE-5) ──────────────────────────────────────────────────

def make_wav(path, ms=500):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * (16 * ms))
    return str(path)


async def test_tts_synthesize_wav_goes_to_the_temp_dir_with_duration(tmp_path):
    async with Rig(tmp_path) as rig:
        src = make_wav(tmp_path / "raw.wav", 750)
        rig.mod._synth_blocking = lambda text, directory: src
        res = await rig.core.request("tts.synthesize", {"text": "Привет", "emotion": None, "format": "wav"})
        assert res["duration_ms"] == 750
        assert res["path"].startswith(str(rig.tmp / "voice_tmp")) and res["path"].endswith(".wav") and os.path.exists(res["path"])
        assert not os.path.exists(src)                                         # исходник забран
        assert os.path.basename(res["path"]).startswith("tts_")


async def test_tts_synthesize_ogg_uses_the_opus_encoder_and_removes_wav(tmp_path, monkeypatch):
    async def fake_ogg(path):
        assert path.endswith(".wav") and os.path.exists(path)
        return b"OggS-test"
    monkeypatch.setattr(opus_encoder, "audio_file_to_ogg_opus", fake_ogg)
    async with Rig(tmp_path) as rig:
        rig.mod._synth_blocking = lambda text, directory: make_wav(tmp_path / "r.wav", 300)
        res = await rig.core.request("tts.synthesize", {"text": "Привет", "format": "ogg"})
        assert res["path"].endswith(".ogg") and open(res["path"], "rb").read() == b"OggS-test" and res["duration_ms"] == 300
        assert not [f for f in os.listdir(rig.tmp / "voice_tmp") if f.endswith(".wav")]


async def test_tts_synthesize_mp3_is_converted_to_wav_first(tmp_path, monkeypatch):
    mp3 = tmp_path / "edge.mp3"
    mp3.write_bytes(b"ID3")

    async def fake_wav(src, dst):
        assert src == str(mp3)
        make_wav(dst, 200)
    monkeypatch.setattr(opus_encoder, "audio_file_to_wav_file", fake_wav)
    async with Rig(tmp_path) as rig:
        rig.mod._synth_blocking = lambda text, directory: str(mp3)
        res = await rig.core.request("tts.synthesize", {"text": "Привет"})
        assert res["path"].endswith(".wav") and res["duration_ms"] == 200 and not mp3.exists()


async def test_tts_synthesize_errors(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.mod._synth_blocking = lambda text, directory: None
        with pytest.raises(ToolError) as e:
            await rig.core.request("tts.synthesize", {"text": "Привет", "format": "wav"})
        assert e.value.code == "unavailable"                                   # ошибка синтеза
        for body in ({"text": "  "}, {"text": "x", "format": "mp3"}, {"format": "wav"}, {"text": "я" * 6000}):
            with pytest.raises(ToolError) as e:
                await rig.core.request("tts.synthesize", body)
            assert e.value.code == "bad_request"


def test_sweep_removes_only_files_older_than_an_hour(tmp_path):
    d = tmp_path / "glados_voice"
    d.mkdir()
    old, fresh = d / "tts_old.wav", d / "tts_fresh.wav"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    past = time.time() - 4000
    os.utime(old, (past, past))
    assert common.sweep_dir(d) == 1
    assert not old.exists() and fresh.exists()
    assert common.sweep_dir(tmp_path / "нет-папки") == 0


async def test_start_sweeps_the_temp_dir(tmp_path):
    d = tmp_path / "voice_tmp"
    d.mkdir()
    old = d / "tts_old.ogg"
    old.write_bytes(b"x")
    past = time.time() - 4000
    os.utime(old, (past, past))
    async with Rig(tmp_path) as rig:
        rig.mod.fake = True                       # микрофон и слух в тесте не нужны
        rig.mod.start()
        await until(lambda: not old.exists())
        await rig.mod.stop()


# ── входящая речь (VOICE-6, VOICE-8, VOICE-9) ─────────────────────────────────────

async def test_utterance_becomes_a_voice_utterance_event_with_the_speaker(tmp_path):
    me, stranger = vec(1), vec(2)
    stt = FakeSTT("включи свет", me)
    async with Rig(tmp_path, stt=stt) as rig:
        pid, _ = rig.mod.voices.resolve(me)
        rig.mod.voices.set_owner(pid)
        rig.mod.voices.set_name(pid, "Аня")
        await rig.mod.mic.handle_utterance(b"\0" * 100)
        ev = await rig.core.wait_message("event", lambda b: b["kind"] == "voice.utterance")
        body = ev.body
        assert body["text"] == "включи свет" and body["addressed_to_me"] is True
        assert body["data"]["speaker"] == {"id": pid, "name": "Аня", "new": False, "owner": True, "has_owner": True}
        assert body["sender"] == {"id": f"voice:{pid}", "name": "Аня", "is_owner": True}
        stt.t.text, stt.t.speaker_embedding = "кто здесь", stranger
        await rig.mod.mic.handle_utterance(b"\0" * 100)
        second = (await rig.core.wait_message("event", lambda b: b["data"].get("speaker", {}).get("new"), timeout=5)).body
        assert second["sender"]["is_owner"] is False and "name" not in second["sender"]


async def test_own_voice_and_junk_give_no_events(tmp_path):
    own = vec(3)
    stt = FakeSTT("это мой голос из колонок", own)
    async with Rig(tmp_path, stt=stt) as rig:
        rig.mod.voices.register_self(own)
        await rig.mod.mic.handle_utterance(b"\0" * 100)
        stt.t.text = "Продолжение следует..."
        stt.t.speaker_embedding = vec(4)
        await rig.mod.mic.handle_utterance(b"\0" * 100)
        await rig.mod.outbox.drain()
        assert [e for e in rig.core.events if e["kind"] == "voice.utterance"] == []


async def test_user_speaking_events_go_out_in_order(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.mod._on_user_speaking(True)
        rig.mod._on_user_speaking(False)
        await until(lambda: len([e for e in rig.core.events if e["kind"] == "voice.user_speaking"]) == 2)
        assert [e["data"]["speaking"] for e in rig.core.events if e["kind"] == "voice.user_speaking"] == [True, False]


async def test_own_voice_sample_is_synthesized_inside_the_module(tmp_path, monkeypatch):
    async def fake_pcm(path):
        assert os.path.exists(path)
        return b"PCM"
    monkeypatch.setattr(opus_encoder, "audio_file_to_pcm16k", fake_pcm)
    async with Rig(tmp_path) as rig:
        rig.mod._synth_blocking = lambda t, d: None
        import tts_engine
        calls = []
        monkeypatch.setattr(tts_engine, "synth_file", lambda text, directory=None: calls.append(text) or make_wav(tmp_path / "s.wav"))
        assert await rig.mod._own_voice_sample() == b"PCM"
        assert calls and "ГЛаДОС" in calls[0] and not (tmp_path / "s.wav").exists()
        rig.mod.fake = True
        assert await rig.mod._own_voice_sample() == b""


# ── перебивание (VOICE-10) ───────────────────────────────────────────────────

async def barge(rig, frames=40):
    """Подать кадры речи пока она говорит; вернуть, когда проверка голоса закончилась."""
    for _ in range(frames):
        await rig.mod.mic.handle_frame(b"\x01" * 960)
    await until(lambda: not rig.mod.mic._tasks, 3)


def loud_listener(rig, enabled=True):
    rig.client.module_settings["barge_in"] = enabled
    rig.mod.mic.prepare(vad=lambda frame: frame[0] == 1)
    return rig.mod.mic


async def test_barge_in_by_the_owner_stops_speech_and_keeps_recording(tmp_path):
    me = vec(5)
    stt = FakeSTT("стой", me)
    async with Rig(tmp_path, stt=stt, hold=True) as rig:
        pid, _ = rig.mod.voices.resolve(me)
        rig.mod.voices.set_owner(pid)
        rig.mod.voices.register_self(vec(6))
        mic = loud_listener(rig)
        await rig.speak("u-1", "Длинная речь. Ещё предложение.")
        await until(lambda: rig.mod.speaker.audible)
        await barge(rig)
        end = await rig.wait_ended("u-1")
        assert end["reason"] == "barge_in" and end["completed"] is False
        assert stt.calls == 1
        assert mic._seg.active and mic._barge_state in (None, "confirmed")        # запись реплики продолжается
        assert mic._tail_until == 0.0


async def test_barge_in_ignores_her_own_voice(tmp_path):
    own = vec(7)
    stt = FakeSTT("её эхо", own)
    async with Rig(tmp_path, stt=stt, hold=True) as rig:
        rig.mod.voices.register_self(own)
        loud_listener(rig)
        await rig.speak("u-1", "Говорю. Дальше.")
        await until(lambda: rig.mod.speaker.audible)
        await barge(rig)
        assert stt.calls == 1 and rig.speech("ended") == [] and rig.mod.speaker.audible
        assert not rig.mod.mic._seg.active


async def test_barge_in_by_a_stranger_is_ignored_when_the_owner_is_known(tmp_path):
    me, stranger = vec(8), vec(9)
    stt = FakeSTT("телевизор", stranger)
    async with Rig(tmp_path, stt=stt, hold=True) as rig:
        pid, _ = rig.mod.voices.resolve(me)
        rig.mod.voices.set_owner(pid)
        loud_listener(rig)
        await rig.speak("u-1", "Говорю.")
        await until(lambda: rig.mod.speaker.audible)
        await barge(rig)
        assert rig.speech("ended") == []


async def test_barge_in_by_anyone_when_there_is_no_owner(tmp_path):
    stt = FakeSTT("эй", vec(10))
    async with Rig(tmp_path, stt=stt, hold=True) as rig:
        loud_listener(rig)
        await rig.speak("u-1", "Говорю.")
        await until(lambda: rig.mod.speaker.audible)
        await barge(rig)
        assert (await rig.wait_ended("u-1"))["reason"] == "barge_in"


async def test_barge_in_off_by_default_keeps_the_gate_closed(tmp_path):
    stt = FakeSTT("эй", vec(11))
    async with Rig(tmp_path, stt=stt, hold=True) as rig:
        loud_listener(rig, enabled=False)
        await rig.speak("u-1", "Говорю.")
        await until(lambda: rig.mod.speaker.audible)
        await barge(rig)
        for _ in range(20):
            await rig.mod.mic.handle_frame(b"\x00" * 960)                      # реплика закончилась, но пересеклась с её речью
        await asyncio.sleep(0.05)
        assert stt.calls == 0 and rig.speech("ended") == [] and rig.mod.speaker.audible


async def test_state_is_reported_for_the_dashboard(tmp_path):
    async with Rig(tmp_path, settings={"mic": True}) as rig:
        await rig.mod.report_state(force=True)
        state = (await rig.core.wait_message("state")).body
        assert "микрофон: вкл" in state["summary"] and state["data"]["mic"] is True and state["data"]["hearing"] is False
        assert state["data"]["muted"] is False and "tts" in state["data"]


# ── инструменты ──────────────────────────────────────────────────────────────

async def test_recent_audio_tool(tmp_path):
    async with Rig(tmp_path) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("recent_audio")
        assert "слух выключен" in e.value.message
        await rig.core.update_settings({"hearing": True})
        await until(lambda: rig.client.settings.get("hearing"))
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("recent_audio")
        assert "речи в звуке не было" in e.value.message
        rig.mod.hearing._buf.append((time.time(), "в фильме говорят про сыр"))
        assert await rig.core.call_tool("recent_audio") == "в фильме говорят про сыр"


async def test_name_voice_tool(tmp_path):
    me = vec(12)
    stt = FakeSTT("привет", me)
    async with Rig(tmp_path, stt=stt) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("name_voice", {"name": "Аня"})
        assert "голос не узнан" in e.value.message
        await rig.mod.mic.handle_utterance(b"\0" * 100)
        pid = rig.mod.mic.last_info["id"]
        res = await rig.core.call_tool("name_voice", {"name": " Аня ", "is_owner": True})
        assert "тебя зовут Аня" in res
        assert rig.mod.voices.name_of(pid) == "Аня" and rig.mod.voices.owner() == pid
        ev = await rig.core.wait_message("event", lambda b: b["kind"] == "voice.named")
        assert ev.body["data"] == {"voice_id": pid, "name": "Аня", "is_owner": True}
        res = await rig.core.call_tool("name_voice", {"name": "Вася"})
        assert "по имени Вася" in res and rig.mod.voices.owner() == pid
        with pytest.raises(ToolError):
            await rig.core.call_tool("name_voice", {"name": "  "})
        rig.mod.mic.last_info = {"id": "SPEAKER_00"}                           # метка диаризации, а не человек
        with pytest.raises(ToolError):
            await rig.core.call_tool("name_voice", {"name": "Кто-то"})


async def test_gist_is_sent_as_voice_heard(tmp_path):
    async with Rig(tmp_path) as rig:
        await rig.mod._on_gist("играет джаз")
        ev = (await rig.core.wait_message("event", lambda b: b["kind"] == "voice.heard")).body
        assert ev["text"] == "В колонках: играет джаз" and ev["memorable"] is False and ev["data"] == {"summary": "играет джаз"}


async def test_player_param_is_replaceable(tmp_path):
    pl = FakePlayer(seconds=0)
    async with Rig(tmp_path, player=pl) as rig:
        assert rig.mod.player is pl
