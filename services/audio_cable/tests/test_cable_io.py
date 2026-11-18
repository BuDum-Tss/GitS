"""Поиск устройств, проверка петли и буферизованный выход (MemoryOut) без звуковых устройств."""
import asyncio
from types import SimpleNamespace

import pytest

from cable_io import (FRAME_BYTES, DeviceError, MemoryIn, MemoryOut, cable_base, find_device, same_cable)

DEVICES = [SimpleNamespace(name=n) for n in (
    "CABLE-A Input (VB-Audio Cable A)", "CABLE-B Input (VB-Audio Cable B)", "Динамики (Realtek Audio)", "CABLE Input (VB-Audio Virtual Cable)")]


# ── петля ──

@pytest.mark.parametrize("a,b,expected", [
    ("CABLE Input (VB-Audio Virtual Cable)", "CABLE Output (VB-Audio Virtual Cable)", True),
    ("CABLE-A Input (VB-Audio Cable A)", "CABLE-A Output (VB-Audio Cable A)", True),
    ("CABLE-A Input", "CABLE-A Output", True),
    ("Кабель Вход (VB-Audio)", "Кабель Выход (VB-Audio)", True),
    ("CABLE-A Output (VB-Audio Cable A", "CABLE-A Input (VB-Audio Cable A)", True),      # имя обрезано Windows: скобка не закрыта
    ("Один и тот же", "один и тот же", True),
    ("CABLE-A Input", "CABLE-B Output", False),
    ("CABLE-A Output (VB-Audio Cable A)", "CABLE-B Input (VB-Audio Cable B)", False),
    ("CABLE Output (VB-Audio Virtual Cable)", "Динамики (Realtek Audio)", False),
    ("Микрофон (Realtek Audio)", "Динамики (Realtek Audio)", False),
])
def test_same_cable(a, b, expected):
    assert same_cable(a, b) is expected
    assert same_cable(b, a) is expected


def test_cable_base_strips_words_and_vendor():
    assert cable_base("CABLE Input (VB-Audio Virtual Cable)") == "cable"
    assert cable_base("CABLE-B Output") == "cable-b"
    assert cable_base("Inputs Mixer") == "inputs mixer"             # слово Input внутри другого слова не трогаем


# ── поиск устройств ──

def test_find_device_case_insensitive_substring():
    assert find_device("speaker", "cable-b input", DEVICES).name.startswith("CABLE-B Input")
    assert find_device("speaker", "реалтек".replace("реалтек", "realtek"), DEVICES).name.startswith("Динамики")


def test_find_device_not_found_lists_available():
    with pytest.raises(DeviceError) as err:
        find_device("microphone", "нет-такого", DEVICES)
    assert "CABLE-A Input" in str(err.value) and "не найдено" in str(err.value)


def test_find_device_ambiguous_asks_to_refine():
    with pytest.raises(DeviceError) as err:
        find_device("speaker", "CABLE", DEVICES)
    assert "несколько" in str(err.value)


def test_find_device_exact_name_wins_over_substring():
    devices = [SimpleNamespace(name="CABLE"), SimpleNamespace(name="CABLE-B Input")]
    assert find_device("speaker", "cable", devices).name == "CABLE"


def test_find_device_empty_name_rejected():
    with pytest.raises(DeviceError):
        find_device("speaker", "  ", DEVICES)


# ── выход ──

async def test_memory_out_plays_whole_frames_and_pads_tail():
    out = MemoryOut()
    await out.start()
    out.begin()
    out.feed(b"\x01\x00" * (FRAME_BYTES // 2) * 3 + b"\x02\x00" * 100)         # три кадра и неполный хвост
    await out.drained()
    assert len(out.audio) == FRAME_BYTES * 4 and out.played_ms == 80
    assert out.audio[-FRAME_BYTES:][:200] == b"\x02\x00" * 100 and not any(out.audio[-FRAME_BYTES + 200:])
    assert out.buffered_ms == 0
    await out.stop()


async def test_memory_out_abort_clears_within_one_frame():
    out = MemoryOut(frame_delay_s=0.02)
    await out.start()
    out.begin()
    out.feed(bytes(FRAME_BYTES * 100))                                          # 2 с
    await asyncio.sleep(0.1)
    drained = asyncio.create_task(out.drained())
    out.abort()
    await asyncio.wait_for(drained, 1)                                           # drained завершается по abort
    seen = len(out.audio)
    await asyncio.sleep(0.1)
    assert len(out.audio) - seen <= FRAME_BYTES and out.buffered_ms == 0 and out.aborts == 1
    assert 0 < out.played_ms < 2000
    await out.stop()


async def test_memory_out_writes_silence_when_empty_and_begin_resets_played():
    out = MemoryOut()
    await out.start()
    await asyncio.sleep(0.05)
    assert out.silence_frames > 0 and out.audio == b""                           # поток не обрывается, тишина не копится как речь
    out.begin()
    out.feed(bytes(FRAME_BYTES * 2))
    await out.drained()
    assert out.played_ms == 40
    out.begin()
    assert out.played_ms == 0
    await out.stop()


async def test_memory_in_yields_frames_then_ends_and_push_keeps_open():
    frames = [bytes(FRAME_BYTES)] * 3
    assert [f async for f in MemoryIn(frames)] == frames
    live = MemoryIn(keep_open=True)
    live.push(b"a")
    live.close()
    assert [f async for f in live] == [b"a"]
