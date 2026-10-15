"""Трасса задержки в модуле голоса (ТЗ 24, TRC-1): реплика несёт трассу ядра, озвучка ставит метки tts_first_audio и
play_start и отдаёт их событием voice.trace; распознавание ставит mic_end и stt_done; без трассы ничего не меняется."""
import latency_trace as lt
from mic_listener import MicListener
from speaker import Speaker
from voice_rig import FakePlayer, FakeSynth, until


class Rig:
    def __init__(self):
        self.events: list[dict] = []
        self.traces: list[dict] = []
        self.sp = Speaker(synth=FakeSynth(), player=FakePlayer(), emit=self.events.append, volume=lambda: 0.5,
                          on_trace=self.traces.append)
        self.sp.start()


async def test_first_segment_marks_audio_and_play_and_sends_them_once():
    r = Rig()
    wire = lt.wire_new("chat", marks={"core_in": lt.now_ms()})
    await r.sp.speak("u-1", "Первое. Второе.", trace=wire)
    await until(lambda: any(e["phase"] == "ended" for e in r.events))
    assert len(r.traces) == 1                       # ушло один раз, на первом предложении
    marks = r.traces[0]["marks"]
    assert r.traces[0]["id"] == wire["id"]
    assert "tts_first_audio" in marks and "play_start" in marks and "core_in" in marks
    assert marks["tts_first_audio"] <= marks["play_start"]
    await r.sp.aclose()


async def test_speech_without_trace_sends_nothing():
    r = Rig()
    await r.sp.speak("u-2", "Одно предложение.")
    await until(lambda: any(e["phase"] == "ended" for e in r.events))
    assert r.traces == []
    await r.sp.aclose()


async def test_utterance_event_carries_trace_from_mic_to_handler():
    got = []

    async def on_voice(text, info, trace=None):
        got.append((text, trace))

    class Stt:
        async def transcribe(self, audio, diarize=False):
            from types import SimpleNamespace
            return SimpleNamespace(text="привет", speaker=None, speaker_embedding=None)

    class Voices:
        def resolve(self, emb):
            return None, False

        def name_of(self, pid):
            return None

        def owner(self):
            return None

    m = MicListener(Stt(), Voices(), on_voice, lambda: False, None)
    wire = lt.wire_new("voice", marks={"mic_end": lt.now_ms()})
    await m.handle_utterance(b"\x00\x00", wire)
    assert len(got) == 1
    text, trace = got[0]
    assert text == "привет"
    assert trace["id"] == wire["id"]
    assert "mic_end" in trace["marks"] and "stt_done" in trace["marks"]
    assert trace["marks"]["mic_end"] <= trace["marks"]["stt_done"]


async def test_muted_utterance_sends_trace_at_once_without_audio_marks():
    # без звука (голос выключен) метки уходят сейчас: ядро не ждёт таймер отметки звука (ТЗ 24, TRC-1)
    events, traces = [], []
    sp = Speaker(synth=FakeSynth(), player=FakePlayer(), emit=events.append, volume=lambda: 0.5,
                 muted=lambda: True, on_trace=traces.append)
    sp.start()
    wire = lt.wire_new("chat", marks={"core_in": lt.now_ms()})
    await sp.speak("u-3", "Тихо.", trace=wire)
    assert len(traces) == 1 and traces[0]["id"] == wire["id"]
    assert "play_start" not in traces[0]["marks"]
    await until(lambda: any(e["phase"] == "ended" for e in events))
    assert len(traces) == 1                         # при окончании второй раз не шлёт
    await sp.aclose()
