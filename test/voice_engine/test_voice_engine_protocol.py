"""Проводной протокол движка голоса: кадры, сообщения, отказ на мусор и слишком большое."""
import json

import pytest

from gits_sdk.voice import protocol as V


def hello(**over):
    msg = {"type": "hello", "version": 1, "adapter": "discord", "session": "g1:c2",
           "capabilities": {"echo_free": True}, "format": V.audio_format()}
    msg.update(over)
    return json.dumps(msg)


def test_frame_roundtrip():
    pcm = bytes(range(64)) * 10
    data = V.pack_frame(V.KIND_MIC, 7, 0, 123456, pcm)
    assert len(data) == V.HEADER_SIZE + len(pcm)
    f = V.unpack_frame(data)
    assert (f.kind, f.speaker, f.stream, f.seq, f.pcm) == (V.KIND_MIC, 7, 0, 123456, pcm)


def test_frame_format_constants():
    assert (V.SAMPLE_RATE, V.FRAME_MS, V.FRAME_SAMPLES, V.FRAME_BYTES) == (16000, 20, 320, 640)
    assert V.HEADER_SIZE == 12


@pytest.mark.parametrize("data", [
    b"", b"\x01" * 12,                                        # без звука
    V.HEADER.pack(9, 0, 1, 0, 0) + bytes(640),                # чужой тип
    V.HEADER.pack(1, 1, 1, 0, 0) + bytes(640),                # флаги
    V.HEADER.pack(1, 0, 1, 0, 0) + bytes(641),                # нечётная длина
    V.HEADER.pack(1, 0, 1, 0, 0) + bytes(V.MAX_PCM_BYTES + 2),  # слишком большой
])
def test_bad_frames_rejected(data):
    with pytest.raises(V.VoiceProtocolError):
        V.unpack_frame(data)


def test_pack_checks_too():
    with pytest.raises(V.VoiceProtocolError):
        V.pack_frame(V.KIND_MIC, 70000, 0, 0, bytes(640))
    with pytest.raises(V.VoiceProtocolError):
        V.pack_frame(V.KIND_MIC, 1, 0, 0, bytes(V.MAX_PCM_BYTES + 2))


def test_hello_ok_normalizes_capabilities():
    msg = V.decode(hello(), allowed=V.TO_ENGINE)
    assert msg["capabilities"] == {"echo_free": True, "can_barge_in": False, "speakers_labelled": True, "direct": False}


@pytest.mark.parametrize("over", [
    {"version": 2}, {"adapter": "Bad Name"}, {"session": ""}, {"session": "a b"},
    {"capabilities": []}, {"capabilities": {"echo_free": "yes"}},
    {"format": {"codec": "opus", "rate": 48000, "channels": 2, "frame_ms": 20}},
])
def test_bad_hello_rejected(over):
    with pytest.raises(V.VoiceProtocolError):
        V.decode(hello(**over), allowed=V.TO_ENGINE)


@pytest.mark.parametrize("text", ["не json", "[]", '{"type": 5}', '{"type": "nope"}', "{" * 10])
def test_garbage_rejected(text):
    with pytest.raises(V.VoiceProtocolError):
        V.decode(text, allowed=V.TO_ENGINE)


def test_direction_enforced():
    # welcome адаптер не имеет права слать движку, play.ack движок не имеет права слать адаптеру
    with pytest.raises(V.VoiceProtocolError):
        V.decode(json.dumps({"type": "welcome", "version": 1, "session": "s"}), allowed=V.TO_ENGINE)
    with pytest.raises(V.VoiceProtocolError):
        V.decode(json.dumps({"type": "play.ack", "utterance_id": "u", "spoken_upto": 0, "reason": "stop"}), allowed=V.TO_ADAPTER)


def test_oversize_control_rejected():
    big = json.dumps({"type": "bye", "x": "я" * V.MAX_CONTROL_BYTES})
    with pytest.raises(V.VoiceProtocolError):
        V.decode(big, allowed=V.TO_ENGINE)
    with pytest.raises(V.VoiceProtocolError):
        V.encode("error", code="x", message="я" * V.MAX_CONTROL_BYTES)


def test_speaker_and_ack_validation():
    ok = V.decode(json.dumps({"type": "speaker.join", "id": "u1", "name": "Маша", "idx": 3, "is_owner": True}), allowed=V.TO_ENGINE)
    assert ok["idx"] == 3
    for bad in ({"type": "speaker.join", "id": "u1", "idx": 0}, {"type": "speaker.join", "id": "u1", "idx": True},
                {"type": "speaker.join", "id": "", "idx": 1}, {"type": "speaker.join", "id": "u1", "idx": 1, "is_owner": 1},
                {"type": "play.ack", "utterance_id": "u", "spoken_upto": -1, "reason": "stop"},
                {"type": "play.ack", "utterance_id": "u", "spoken_upto": 5, "reason": "weird"}):
        with pytest.raises(V.VoiceProtocolError):
            V.decode(json.dumps(bad), allowed=V.TO_ENGINE)


def test_encode_drops_none():
    assert json.loads(V.encode("play.stop", utterance_id=None, reason="stop")) == {"type": "play.stop", "reason": "stop"}


def test_hello_title_is_optional_and_bounded():
    assert V.decode(hello(title="Aperture Science / Игры"), allowed=V.TO_ENGINE)["title"] == "Aperture Science / Игры"
    assert "title" not in V.decode(hello(), allowed=V.TO_ENGINE)                  # версия протокола не меняется: поле необязательное
    for bad in ("", "я" * (V.MAX_TITLE_LEN + 1), 5):
        with pytest.raises(V.VoiceProtocolError):
            V.decode(hello(title=bad), allowed=V.TO_ENGINE)
