"""Переключение персонажа (ТЗ 05, CHR-7): порядок шагов, память и группа графа, тема character, отказ без изменений."""
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import character
import prompts
import settings
from character_switch import CharacterSwitch

REAL = Path(__file__).resolve().parents[2] / "resources"


class Recorder:
    def __init__(self):
        self.calls: list[tuple] = []


class FakeCompanion(Recorder):
    def __init__(self, speaking_polls=0):
        super().__init__()
        self._polls = speaking_polls

    def is_speaking(self):
        if self._polls > 0:
            self._polls -= 1
            return True
        return False

    def rebind_memory(self, memory_dir):
        self.calls.append(("memory", str(memory_dir)))


class FakeRel(Recorder):
    def rebind(self, memory_dir):
        self.calls.append(("rel", str(memory_dir)))


class FakeRules(Recorder):
    def rebind(self, path):
        self.calls.append(("rules", str(path)))


class FakeAttention(Recorder):
    def __init__(self, log):
        super().__init__()
        self.rules = FakeRules()
        self._log = log

    async def reset_for_character(self):
        self._log.append("reset")
        return 2


class FakeGraph(Recorder):
    def set_character(self, char_id):
        self.calls.append(("graph", char_id))


class FakeHub(Recorder):
    async def broadcast(self, topic, data):
        self.calls.append(("broadcast", topic, data))


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "characters"
    shutil.copytree(REAL / "GlaDOS", root / "glados")
    shutil.copytree(REAL / "GlaDOS", root / "tester")        # второй персонаж: карточка с id tester
    (root / "tester" / "character.yaml").write_text(
        (REAL / "GlaDOS" / "character.yaml").read_text(encoding="utf-8").replace("id: glados", "id: tester")
        .replace("name: ГЛаДОС", "name: Тестерша").replace("aliases: [ГЛаДОС, гладос, GLaDOS, глэдос]",
                                                         "aliases: [Тестерша]"),
        encoding="utf-8")
    monkeypatch.setattr(character, "CHARACTERS_DIR", root)
    character.init()
    log: list = []
    parts = dict(companion=FakeCompanion(), rel=FakeRel(), graph=FakeGraph(), attention=FakeAttention(log), hub=FakeHub())
    switch = CharacterSwitch(mem_root=tmp_path / "memory", speech_wait_s=2.0, **parts)
    return SimpleNamespace(switch=switch, parts=parts, log=log, mem=tmp_path / "memory")



async def test_switch_moves_memory_group_setting_and_prompts(world):
    res = await world.switch.switch("tester")
    assert res == {"changed": True, "id": "tester", "name": "Тестерша"}
    assert character.active().id == "tester"
    assert settings.load()["character"] == "tester"
    assert ("memory", str(world.mem / "characters" / "tester")) in world.parts["companion"].calls
    assert ("rel", str(world.mem / "characters" / "tester")) in world.parts["rel"].calls
    assert ("rules", str(world.mem / "characters" / "tester" / "attention_rules.json")) in world.parts["attention"].rules.calls
    assert ("graph", "tester") in world.parts["graph"].calls
    assert world.log == ["reset"], "очередь речи и стек фокуса сброшены"
    assert "Тестерша" in prompts.persona_card()


async def test_topic_character_goes_to_modules_with_the_card(world):
    await world.switch.switch("tester")
    topics = [c for c in world.parts["hub"].calls if c[0] == "broadcast"]
    assert len(topics) == 1 and topics[0][1] == "character"
    card = topics[0][2]
    assert card["id"] == "tester" and card["name"] == "Тестерша" and card["aliases"] == ["Тестерша"]


async def test_switching_back_to_glados_returns_its_memory_at_the_root(world):
    await world.switch.switch("tester")
    await world.switch.switch("glados")
    assert ("memory", str(world.mem)) in world.parts["companion"].calls
    assert ("graph", "glados") in world.parts["graph"].calls


async def test_same_character_changes_nothing(world):
    res = await world.switch.switch("glados")
    assert res["changed"] is False
    assert world.parts["hub"].calls == [] and world.parts["companion"].calls == []


async def test_unknown_character_fails_before_any_change(world):
    with pytest.raises(character.CharacterError):
        await world.switch.switch("nobody")
    assert character.active().id == "glados"
    assert settings.load()["character"] == "glados"
    assert world.log == [] and world.parts["companion"].calls == []


async def test_switch_waits_for_the_current_utterance_to_end(world):
    world.switch.companion = FakeCompanion(speaking_polls=3)
    await world.switch.switch("tester")
    assert world.switch.companion._polls == 0, "смена дождалась конца реплики"
    assert world.log == ["reset"]


async def test_tool_answers_in_plain_words(world):
    assert await world.switch.tool("tester") == "персонаж переключён: Тестерша"
    assert await world.switch.tool("tester") == "Тестерша уже активна"
    assert "не переключён" in await world.switch.tool("nobody")
