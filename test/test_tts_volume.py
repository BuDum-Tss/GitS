"""core.tts: путь файла относительно папки персонажа доходит от голоса до участника."""
from gits_sdk import contract

from services.peers.requests import CoreRequests


class FakeVoice:
    def __init__(self, reply):
        self.reply = reply

    async def request(self, name, body, timeout=0):
        return self.reply


async def test_volume_path_comes_back_to_the_participant():
    core = CoreRequests(attention=None)
    core._voice_connection = lambda: FakeVoice({"path": "C:/t/a.wav", "duration_ms": 900, "volume_path": "shared/tts/a.wav"})
    assert await core._tts(None, {"text": "Привет"}) == {"path": "C:/t/a.wav", "duration_ms": 900, "volume_path": "shared/tts/a.wav"}
    core._voice_connection = lambda: FakeVoice({"path": "C:/t/a.wav", "duration_ms": 900})
    assert "volume_path" not in await core._tts(None, {"text": "Привет"})


def test_contract_has_the_optional_field():
    assert contract.validate_response("core.tts", {"path": "a", "duration_ms": 1, "volume_path": "shared/tts/a.wav"}) == []
    assert contract.validate_response("tts.synthesize", {"path": "a", "duration_ms": 1}) == []
