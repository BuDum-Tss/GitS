"""Проверки голосовой части: сегментация речи, узнавание голосов, поток «реплика → разговор» без микрофона и сервера."""
import asyncio
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mic_listener as ml
import voice_id as vid
from stt_client import Transcript, dominant_speaker


def frame(speech: bool) -> bytes:
    return (np.full(ml.FRAME, 8000 if speech else 0, dtype=np.int16)).tobytes()


def test_segmenter_returns_utterance_after_silence():
    seg = ml.UtteranceSegmenter(lambda f: np.frombuffer(f, dtype=np.int16).max() > 100)
    out = None
    for _ in range(20):
        assert seg.feed(frame(True)) is None
    for _ in range(ml.SILENCE_FRAMES):
        out = seg.feed(frame(False)) or out
    assert out is not None and len(out[0]) > 20 * ml.FRAME_BYTES and out[1] is False


def test_segmenter_drops_clicks_and_marks_overlap():
    seg = ml.UtteranceSegmenter(lambda f: np.frombuffer(f, dtype=np.int16).max() > 100)
    for _ in range(3):
        seg.feed(frame(True))
    res = None
    for _ in range(ml.SILENCE_FRAMES):
        res = seg.feed(frame(False)) or res
    assert res is None                                   # слишком короткий звук: не речь
    for _ in range(10):
        seg.feed(frame(True), agent_speaking=True)
    for _ in range(ml.SILENCE_FRAMES):
        res = seg.feed(frame(False)) or res
    assert res is not None and res[1] is True            # перекрылась с её речью


def test_hallucinations():
    assert ml.is_hallucination("Продолжение следует...")
    assert ml.is_hallucination("Субтитры сделал DimaTorzok")
    assert not ml.is_hallucination("поставь таймер на пять минут")


def test_dominant_speaker():
    segs = [{"speaker": "A", "start": 0, "end": 1}, {"speaker": "B", "start": 1, "end": 4}]
    assert dominant_speaker(segs) == "B" and dominant_speaker([]) is None


def test_voice_identity_recognises_and_separates(tmp_path):
    v = vid.VoiceIdentity(str(tmp_path))
    rng = np.random.default_rng(1)
    a, b = rng.normal(size=192), rng.normal(size=192)
    pa, new = v.resolve(list(a))
    assert new and pa.startswith("person-")
    again, new2 = v.resolve(list(a + rng.normal(scale=0.05, size=192)))       # тот же голос с шумом
    assert again == pa and not new2
    pb, new3 = v.resolve(list(b))
    assert pb != pa and new3
    assert v.resolve(None) == (None, False) and v.resolve([float("nan")] * 4) == (None, False)


def test_voice_names_owner_and_self(tmp_path):
    v = vid.VoiceIdentity(str(tmp_path))
    rng = np.random.default_rng(2)
    e = rng.normal(size=64)
    pid, _ = v.resolve(list(e))
    assert v.set_name(pid, "Аня") and v.name_of(pid) == "Аня" and v.set_owner(pid) and v.owner() == pid
    assert not v.has_self()
    v.register_self(list(rng.normal(size=64)))
    assert v.has_self() and not v.set_owner(vid.SELF_PERSON_ID)
    assert all(p["id"] != vid.SELF_PERSON_ID for p in v.people())
    # состояние переживает перезапуск
    assert vid.VoiceIdentity(str(tmp_path)).name_of(pid) == "Аня"


class FakeSTT:
    def __init__(self, text, emb):
        self.t = Transcript(text=text, speaker="SPEAKER_00", speaker_embedding=emb)

    async def transcribe(self, *a, **k):
        return self.t


def test_listener_passes_known_voice_and_drops_self_and_junk(tmp_path):
    v = vid.VoiceIdentity(str(tmp_path))
    rng = np.random.default_rng(3)
    me, own = list(rng.normal(size=64)), list(rng.normal(size=64))
    pid, _ = v.resolve(me)
    v.set_owner(pid)
    v.register_self(own)
    got = []

    async def on_voice(text, info):
        got.append((text, info))

    async def run(text, emb):
        lis = ml.MicListener(FakeSTT(text, emb), v, on_voice, lambda: False)
        await lis.handle_utterance(b"\0" * 100)

    asyncio.run(run("включи свет", me))
    asyncio.run(run("это мой голос из колонок", own))
    asyncio.run(run("Продолжение следует...", me))
    stranger = list(rng.normal(size=64))
    asyncio.run(run("кто здесь", stranger))
    assert [t for t, _ in got] == ["включи свет", "кто здесь"]
    assert got[0][1]["owner"] is True and got[1][1]["owner"] is False and got[1][1]["new"] is True
