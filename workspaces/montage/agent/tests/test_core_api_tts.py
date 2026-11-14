"""Озвучка в монтаже: звук лежит в общем томе персонажа, путь для контейнера собирается из volume_path."""
from types import SimpleNamespace

import pytest
from core_api import ClientCore


class FakeClient:
    def __init__(self, result):
        self.result = result

    async def tts(self, text, emotion, fmt, timeout=0):
        return self.result


async def test_volume_path_is_resolved_under_this_containers_volume(tmp_path):
    f = tmp_path / "shared" / "tts" / "tts_a.wav"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"RIFF")
    core = ClientCore(FakeClient(SimpleNamespace(path="C:/Users/x/Temp/a.wav", duration_ms=800, volume_path="shared/tts/tts_a.wav")),
                      volume=lambda: tmp_path)
    assert await core.tts("Привет") == {"path": str(f), "duration_ms": 800}


@pytest.mark.parametrize("rel", [None, "shared/tts/нет.wav", "../etc/passwd"])
async def test_without_a_visible_file_the_host_path_is_kept(tmp_path, rel):
    core = ClientCore(FakeClient(SimpleNamespace(path="/x/a.wav", duration_ms=500, volume_path=rel)), volume=lambda: tmp_path)
    assert await core.tts("Привет") == {"path": "/x/a.wav", "duration_ms": 500}
