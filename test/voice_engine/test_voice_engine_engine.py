"""Движок голоса без сети: говорящие вперемешку, события для ядра, речь в темпе 20 мс, очередь, перебивание, обрыв."""
import pytest

from gits_sdk.voice.testing import silence, tone
from services.voice_engine.config import EngineConfig
from services.voice_engine.engine import VoiceEngine
from services.voice_engine.session import Capabilities, SessionLimitError
from services.voice_engine.stt import FakeStt, SttTimeout
from services.voice_engine.testing import FakeClock, RecordingTransport
from services.voice_engine.tts import FakeTts, TtsServerError

ECHO_FREE = Capabilities(echo_free=True, can_barge_in=True)
PC = Capabilities(echo_free=False, can_barge_in=True, speakers_labelled=False)


class Rig:
    def __init__(self, texts=None, caps=ECHO_FREE, tts=None, cfg=None, **kw):
        self.clock = FakeClock()
        self.events: list[dict] = []
        self.journal: list[tuple] = []
        self.speech: list[dict] = []
        self.stt = FakeStt(texts or [])
        self.tts = tts or FakeTts(ms_per_char=40)

        async def emit(event):
            self.events.append(event)

        self.engine = VoiceEngine(stt=self.stt, tts=self.tts, config=cfg or EngineConfig(barge_in_ms=400), clock=self.clock, emit_event=emit,
                                  journal=lambda k, t, d: self.journal.append((k, t, d)), on_speech=self.speech.append,
                                  names=lambda: ("Глэдос", "Эйч"), **kw)
        self.transport = RecordingTransport(self.clock)
        self.caps = caps
        self.sess = None

    async def open(self, sid="s1", adapter="fake"):
        self.sess = await self.engine.open_session(sid, adapter, self.transport, self.caps)
        return self.sess

    def join(self, sid, name, idx, **kw):
        self.engine.add_speaker(self.sess.id, sid, name, idx, **kw)

    def ended(self):
        return [s for s in self.speech if s["phase"] == "ended"]


@pytest.fixture
async def rig():
    r = Rig()
    await r.open()
    yield r
    await r.engine.aclose()


async def say(r: Rig, speaker, ms=600):
    """Говорящий произносит ms мс и замолкает: реплика закрыта."""
    r.engine.feed(r.sess.id, speaker, tone(ms) + silence(800))
    await r.engine.settle()


# ── вход: звук -> событие внимания ───────────────────────────────────────────────

async def test_two_speakers_interleaved_become_two_events():
    r = Rig(["а я просто мимо", "Глэдос, привет"])         # короткая фраза второго закрывается раньше
    await r.open()
    r.join("u1", "Маша", 1, is_owner=True)
    r.join("u2", "Петя", 2)
    a, b = tone(1000) + silence(800), tone(700) + silence(800)
    step = 640 * 5
    for i in range(0, max(len(a), len(b)), step):          # куски по 100 мс вперемешку
        r.engine.feed("s1", "u1", a[i:i + step])
        r.engine.feed("s1", 2, b[i:i + step])               # второй говорящий задан номером кадра
    await r.engine.settle()
    assert [(e["sender"]["id"], e["text"]) for e in r.events] == [("u2", "а я просто мимо"), ("u1", "Глэдос, привет")]
    second, first = r.events
    assert first["kind"] == "voicechat.utterance" and first["sender"] == {"id": "u1", "name": "Маша", "is_owner": True}
    assert first["addressed_to_me"] is True and first["data"]["needs_reply"] is True and first["data"]["addressed_reason"] == "name"
    assert second["addressed_to_me"] is False and second["data"]["needs_reply"] is False
    assert first["data"]["session"] == "s1" and first["data"]["adapter"] == "fake" and first["data"]["duration_ms"] > 0
    assert first["reply_channel"]["tool"] == "voice.say" and first["expires_in_s"] > 0
    assert len(r.stt.calls) == 2
    await r.engine.aclose()


@pytest.mark.parametrize("text,addressed", [("эй, Глэдоса позови", True), ("ГЛЭДОС", True), ("глэдосовщина не считается", False),
                                           ("эйч, слышишь", True), ("просто разговор", False)])
async def test_addressing_by_name_and_alias(text, addressed):
    r = Rig([text])
    await r.open()
    r.join("u1", "Маша", 1)
    r.join("u2", "Петя", 2)                                 # двое: «один на один» не действует
    await say(r, "u1")
    assert r.events[0]["addressed_to_me"] is addressed
    await r.engine.aclose()


async def test_one_to_one_is_addressed_without_name():
    r = Rig(["просто фраза", "ещё фраза"])
    await r.open()
    r.join("u1", "Маша", 1)
    await say(r, "u1")
    assert r.events[0]["addressed_to_me"] is True and r.events[0]["data"]["addressed_reason"] == "one_to_one"
    r.join("u2", "Петя", 2)
    await say(r, "u1")
    assert r.events[1]["addressed_to_me"] is False
    await r.engine.aclose()


async def test_direct_adapter_is_always_one_to_one():
    r = Rig(["раз"], caps=Capabilities(direct=True))
    await r.open()
    r.join("u1", "Маша", 1)
    r.join("u2", "Петя", 2)
    await say(r, "u1")
    assert r.events[0]["addressed_to_me"] is True
    await r.engine.aclose()


async def test_noise_text_and_stt_errors_make_no_event(rig):
    rig.join("u1", "Маша", 1)
    rig.stt._texts.extend(["э", " . ", "нормальная фраза"])
    for _ in range(3):
        await say(rig, "u1")
    assert [e["text"] for e in rig.events] == ["нормальная фраза"]
    rig.stt.fail = SttTimeout("долго")
    await say(rig, "u1")
    assert len(rig.events) == 1 and rig.sess.stats.stt_errors == 1


async def test_speaker_leaving_flushes_unfinished_phrase(rig):
    rig.stt._texts.append("недоговорил")
    rig.join("u1", "Маша", 1)
    rig.engine.feed("s1", "u1", tone(500))
    rig.engine.remove_speaker("s1", "u1")
    await rig.engine.settle()
    assert [e["text"] for e in rig.events] == ["недоговорил"]
    assert not rig.engine.feed("s1", "u1", tone(100))      # ушедшего больше не слышно


async def test_unknown_speaker_audio_is_dropped_and_counted(rig):
    assert not rig.engine.feed("s1", 9, tone(100))
    assert not rig.engine.feed("нет-такой", "u1", tone(100))
    assert rig.sess.stats.bad_frames == 1


async def test_limits():
    r = Rig(cfg=EngineConfig(max_sessions=1, max_speakers=3))
    await r.open()
    r.join("a", "A", 1)
    r.join("b", "B", 2)
    with pytest.raises(ValueError):
        r.engine.add_speaker("s1", "b2", "B2", 2)                  # номер 2 уже занят
    r.join("c", "C", 3)
    with pytest.raises(SessionLimitError):
        r.join("d", "D", 4)
    with pytest.raises(SessionLimitError):
        await r.engine.open_session("s2", "x", RecordingTransport(), ECHO_FREE)
    await r.engine.aclose()


async def test_same_session_id_replaces_old_one():
    r = Rig()
    await r.open()
    old = r.transport
    r.transport = RecordingTransport(r.clock)
    await r.open()
    assert old.closed and old.close_reason == "replaced"
    assert len(r.engine.sessions()) == 1
    assert not await r.engine.close_session("s1", "late", transport=old)      # старое соединение новую не закроет
    assert len(r.engine.sessions()) == 1
    await r.engine.aclose()


async def test_stt_overload_drops_phrases():
    r = Rig(cfg=EngineConfig(stt_pending_per_session=1))
    r.stt.delay_s = 0.05
    await r.open()
    r.join("u1", "Маша", 1)
    r.engine.feed("s1", "u1", (tone(500) + silence(800)) * 3)
    await r.engine.settle()
    assert len(r.events) == 1 and r.sess.stats.dropped_utterances == 2
    await r.engine.aclose()


# ── выход: речь ─────────────────────────────────────────────────────────────────

async def test_speech_frames_every_20ms_and_ack(rig):
    text = "а" * 25                                         # FakeTts: 40 мс на символ = 1.0 с = 50 кадров
    ticket = rig.engine.speak("s1", text)
    assert ticket.position == 0
    await rig.clock.advance(0.001)
    t0 = rig.clock.monotonic()
    start = rig.transport.of("play.start")[0]
    assert start["utterance_id"] == ticket.uid and start["total_ms"] == 0 and start["text"] == text
    await rig.clock.advance(1.2)
    frames = rig.transport.frames
    assert len(frames) == 50 and {f[1] for f in frames} == {start["play_id"]} and [f[2] for f in frames] == list(range(50))
    assert all(f[3] == 640 for f in frames)
    lead = 3                                                 # lead_ms 60 = три кадра сразу
    gaps = [round(frames[i + 1][0] - frames[i][0], 6) for i in range(lead, 49)]
    assert gaps and all(abs(g - 0.02) < 1e-6 for g in gaps)
    assert frames[0][0] == frames[lead][0] and t0 > 0
    assert rig.transport.types()[-1] == "play.end"
    assert rig.sess.playback.audible
    rig.engine.ack("s1", ticket.uid, 1000, "completed")
    await rig.clock.settle()
    ended = rig.ended()
    assert len(ended) == 1 and ended[0]["completed"] is True and ended[0]["spoken_upto"] == 1 and ended[0]["spoken_ms"] == 1000
    assert "reason" not in ended[0] and ended[0]["spoken_text"] == text
    assert not rig.sess.playback.audible


async def test_no_ack_falls_back_to_clock(rig):
    ticket = rig.engine.speak("s1", "а" * 25)
    await rig.clock.advance(1.0)
    assert not rig.ended()
    await rig.clock.advance(2.0)                             # хвост и время ожидания подтверждения
    ended = rig.ended()
    assert len(ended) == 1 and ended[0]["completed"] and ended[0]["spoken_ms"] == 1000 and ended[0]["utterance_id"] == ticket.uid


async def test_spoken_event_goes_to_journal_as_module_out(rig):
    ticket = rig.engine.speak("s1", "а" * 25)
    await rig.clock.advance(1.1)
    rig.engine.ack("s1", ticket.uid, 1000, "completed")
    await rig.clock.settle()
    kind, text, data = rig.journal[-1]
    assert kind == "module_out" and text == "а" * 25
    assert data["event_kind"] == "voicechat.spoken" and data["module"] == "voicechat" and data["to"] == "s1" and data["own"] is True
    assert data["tool"] == "voice.say" and data["completed"] is True


async def test_stop_now_reports_spoken_upto_and_partial_text(rig):
    text = "раз два три четыре пять шесть семь"             # 34 символа
    ticket = rig.engine.speak("s1", text)
    await rig.clock.advance(0.5)
    rig.engine.stop("s1", "now")
    await rig.clock.settle()
    stop = rig.transport.of("play.stop")[0]
    assert stop["utterance_id"] == ticket.uid and stop["reason"] == "stop"
    rig.engine.ack("s1", ticket.uid, 480, "stop")
    await rig.clock.settle()
    ended = rig.ended()[0]
    assert ended["completed"] is False and ended["reason"] == "stop" and ended["spoken_ms"] == 480 and ended["spoken_upto"] == 0
    assert 0 < len(ended["spoken_text"]) < len(text) and text.startswith(ended["spoken_text"])
    assert rig.journal[-1][2]["completed"] is False


async def test_stop_without_adapter_ack_uses_clock_estimate(rig):
    rig.engine.speak("s1", "а" * 25)
    await rig.clock.advance(0.5)
    rig.engine.stop("s1", "now")
    await rig.clock.advance(1.5)
    ended = rig.ended()[0]
    assert ended["reason"] == "stop" and 400 <= ended["spoken_ms"] <= 600


async def test_queue_overflow_drops_oldest_waiting(rig):
    first = rig.engine.speak("s1", "а" * 25)
    await rig.clock.advance(0.1)                             # первая звучит, остальные ждут
    waiting = [rig.engine.speak("s1", f"реплика номер {i}") for i in range(4)]
    assert [t.position for t in waiting[:3]] == [1, 2, 3]
    ended = rig.ended()
    assert [(e["utterance_id"], e["reason"]) for e in ended] == [(waiting[0].uid, "replaced")]
    assert rig.sess.playback.waiting_uids() == [t.uid for t in waiting[1:]] and first.uid == rig.sess.playback.current_uid


async def test_replace_interrupts_current_and_clears_queue(rig):
    a = rig.engine.speak("s1", "а" * 25)
    await rig.clock.advance(0.3)
    b = rig.engine.speak("s1", "бб бб")
    c = rig.engine.speak("s1", "вв вв")
    d = rig.engine.speak("s1", "срочно", replace=True)
    await rig.clock.settle()
    rig.engine.ack("s1", a.uid, 240, "replaced")
    await rig.clock.advance(0.1)
    reasons = {e["utterance_id"]: e["reason"] for e in rig.ended()}
    assert reasons == {a.uid: "replaced", b.uid: "replaced", c.uid: "replaced"}
    await rig.clock.advance(3.0)
    assert rig.transport.of("play.start")[-1]["utterance_id"] == d.uid
    assert [e["utterance_id"] for e in rig.ended() if e.get("completed")] == [d.uid]


async def test_queue_plays_in_order_without_replace(rig):
    a = rig.engine.speak("s1", "а" * 5)
    b = rig.engine.speak("s1", "б" * 5)
    await rig.clock.advance(5.0)
    assert [s["utterance_id"] for s in rig.transport.of("play.start")] == [a.uid, b.uid]
    assert [e["utterance_id"] for e in rig.ended()] == [a.uid, b.uid]
    assert all(e["completed"] for e in rig.ended())


async def test_segment_end_lets_current_finish_but_clears_queue(rig):
    a = rig.engine.speak("s1", "а" * 10)
    await rig.clock.advance(0.1)
    b = rig.engine.speak("s1", "б" * 10)
    rig.engine.stop("s1", "segment_end")
    await rig.clock.advance(2.0)
    by = {e["utterance_id"]: e for e in rig.ended()}
    assert by[a.uid]["completed"] and by[b.uid]["reason"] == "stop"


async def test_tts_failure_ends_with_error_and_next_one_plays():
    r = Rig(tts=FakeTts(fail=TtsServerError(500)))
    await r.open()
    a = r.engine.speak("s1", "привет")
    await r.clock.advance(0.1)
    assert [(e["utterance_id"], e["reason"]) for e in r.ended()] == [(a.uid, "error")]
    r.tts.fail = None
    b = r.engine.speak("s1", "а" * 5)
    await r.clock.advance(2.0)
    assert r.ended()[-1]["utterance_id"] == b.uid and r.ended()[-1]["completed"]
    await r.engine.aclose()


async def test_speak_validation(rig):
    with pytest.raises(ValueError):
        rig.engine.speak("s1", "  **  ")
    with pytest.raises(KeyError):
        rig.engine.speak("нет-такой", "привет")


# ── перебивание ─────────────────────────────────────────────────────────────────

async def start_speech(r: Rig, text="а" * 50):
    ticket = r.engine.speak("s1", text)
    await r.clock.advance(0.2)
    assert r.sess.playback.audible
    return ticket


async def talk_over(r: Rig, ms: int, speaker="u1"):
    for _ in range(ms // 20):
        r.engine.feed("s1", speaker, tone(20))
    await r.clock.settle()


@pytest.mark.parametrize("who", ["u1", "u2"])
async def test_barge_in_any_speaker_echo_free(who):
    r = Rig()
    await r.open()
    r.join("u1", "Маша", 1)
    r.join("u2", "Петя", 2)
    ticket = await start_speech(r)
    r.engine.speak("s1", "ждёт в очереди")
    await talk_over(r, 300, who)
    assert r.transport.of("play.stop") == []                 # 300 мс речи ещё не перебивание
    await talk_over(r, 160, who)
    stop = r.transport.of("play.stop")
    assert len(stop) == 1 and stop[0]["reason"] == "barge_in" and stop[0]["utterance_id"] == ticket.uid
    r.engine.ack("s1", ticket.uid, 200, "barge_in")
    await r.clock.settle()
    reasons = [(e["utterance_id"], e["reason"]) for e in r.ended()]
    assert len(reasons) == 2 and {r_[1] for r_ in reasons} == {"barge_in"} and ticket.uid in {r_[0] for r_ in reasons}   # и звучащая, и очередь
    assert r.sess.stats.barge_ins == 1
    assert r.journal[0][2]["interrupted"] is True            # в журнале её реплика помечена как прерванная
    # следующая фраза перебившего помечена: перебил её
    r.engine.feed("s1", who, silence(800))
    r.stt._texts.append("замолчи")
    await r.engine.settle()
    assert r.events[-1]["data"]["interrupted_me"] is True
    await r.engine.aclose()


async def test_barge_in_does_not_fire_twice():
    r = Rig()
    await r.open()
    r.join("u1", "Маша", 1)
    await start_speech(r)
    await talk_over(r, 1000)
    assert len(r.transport.of("play.stop")) == 1 and r.sess.stats.barge_ins == 1
    await r.engine.aclose()


async def test_barge_in_off_policy_keeps_talking():
    r = Rig(cfg=EngineConfig(barge_in="off"))
    await r.open()
    r.join("u1", "Маша", 1)
    assert r.sess.barge_in_policy == "off"
    await start_speech(r, "а" * 25)
    await talk_over(r, 800)
    assert r.transport.of("play.stop") == []
    await r.clock.advance(3.0)
    assert r.ended()[0]["completed"]
    await r.engine.aclose()


async def test_barge_in_refused_without_echo_free():
    r = Rig(caps=PC, cfg=EngineConfig(barge_in="any_speaker"))        # явно просили, но у адаптера нет подавления эха
    await r.open()
    r.join("u1", "Маша", 1)
    assert r.sess.barge_in_policy == "off"
    await start_speech(r, "а" * 25)
    await talk_over(r, 800)
    assert r.transport.of("play.stop") == []
    await r.engine.aclose()


def test_default_policy_by_capabilities():
    eng = VoiceEngine(stt=FakeStt(), tts=FakeTts())
    assert eng.resolve_barge_in(ECHO_FREE) == "any_speaker"
    assert eng.resolve_barge_in(PC) == "off"
    assert eng.resolve_barge_in(Capabilities(echo_free=True, can_barge_in=False)) == "off"
    assert eng.resolve_barge_in(ECHO_FREE, "off") == "off"


async def test_silence_does_not_barge_in():
    r = Rig()
    await r.open()
    r.join("u1", "Маша", 1)
    await start_speech(r, "а" * 25)
    r.engine.feed("s1", "u1", silence(2000))
    await r.clock.settle()
    assert r.transport.of("play.stop") == []
    await r.engine.aclose()


# ── закрытие ────────────────────────────────────────────────────────────────────

async def test_close_session_ends_speech_and_forgets_it():
    r = Rig()
    await r.open()
    r.join("u1", "Маша", 1)
    a = r.engine.speak("s1", "а" * 25)
    r.engine.speak("s1", "в очереди")
    await r.clock.advance(0.3)
    assert await r.engine.close_session("s1", "disconnect", transport=r.transport)
    reasons = [(e["utterance_id"], e["reason"]) for e in r.ended()]
    assert reasons[0] == (a.uid, "closed") and reasons[1][1] == "closed"
    assert r.engine.sessions() == [] and not r.engine.feed("s1", "u1", tone(100))
    assert not await r.engine.close_session("s1")
    with pytest.raises(KeyError):
        r.engine.speak("s1", "привет")
    await r.engine.aclose()


async def test_transport_failure_ends_utterance_with_error():
    r = Rig()
    await r.open()
    r.transport.fail = True
    a = r.engine.speak("s1", "привет")
    await r.clock.advance(0.2)
    assert [(e["utterance_id"], e["reason"]) for e in r.ended()] == [(a.uid, "error")]
    await r.engine.aclose()


async def test_sessions_are_independent():
    r = Rig(["раз", "два"])
    await r.open("s1")
    t2 = RecordingTransport(r.clock)
    s2 = await r.engine.open_session("s2", "other", t2, PC)
    r.engine.add_speaker("s1", "u1", "Маша", 1)
    r.engine.add_speaker("s2", "u1", "Маша", 1)
    r.engine.speak("s2", "а" * 5)
    await r.clock.advance(0.5)
    assert t2.of("play.start") and not r.transport.of("play.start")
    r.engine.feed("s1", "u1", tone(500) + silence(800))
    r.engine.feed("s2", "u1", tone(500) + silence(800))
    await r.engine.settle()
    assert sorted(e["data"]["session"] for e in r.events) == ["s1", "s2"] and s2.adapter == "other"
    await r.engine.aclose()
