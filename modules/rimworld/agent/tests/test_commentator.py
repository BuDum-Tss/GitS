"""Комментатор игры: когда говорит, когда молчит, режим стримера, повторы. Модель и речь подменены."""
import asyncio
import time

import pytest

import settings
from commentator import Commentator


@pytest.fixture
def mem(tmp_path, monkeypatch):
    import action_journal
    monkeypatch.setattr(action_journal, "FILE", tmp_path / "actions.jsonl")      # не читать настоящий журнал
    settings.init(str(tmp_path))
    return tmp_path


class Rig:
    def __init__(self, mem, answer="Sean лежит. Прекрасно.", user_quiet=True, speaking=False):
        self.said, self.asked = [], []
        self.answer = answer

        async def ask(system, user):
            self.asked.append((system, user))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        async def say(text):
            self.said.append(text)

        self.c = Commentator(str(mem), say, lambda: 0.0 if user_quiet else time.time(), lambda: speaking, ask=ask)
        self.c.update_state("Поселение", "Sean: Wait; Vicky: GotoWander")


def run(rig):
    return asyncio.run(rig.c.step())


def test_important_event_gets_a_reaction(mem):
    r = Rig(mem)
    r.c.feed("Sean упал(а) и лежит", True)
    assert run(r) == "Sean лежит. Прекрасно." and r.said == ["Sean лежит. Прекрасно."]
    assert "Sean упал" in r.asked[0][1] and "Колония «Поселение»" in r.asked[0][1]


def test_quiet_without_events_and_without_stream_mode(mem):
    r = Rig(mem)
    assert run(r) is None and r.said == []


def test_stream_mode_talks_on_a_timer_and_rotates_topics(mem):
    settings.update(streamer=True)
    r = Rig(mem, answer="Я думаю о колонии.")
    assert run(r) == "Я думаю о колонии."
    r.c._last_comment = 0.0
    r.answer = "Теперь о плане."
    run(r)
    assert r.asked[0][1] != r.asked[1][1] and "Тема этой реплики" in r.asked[0][1]


def test_does_not_interrupt_conversation_speech_dnd_or_missing_game(mem):
    for kwargs in ({"user_quiet": False}, {"speaking": True}):
        r = Rig(mem, **kwargs)
        r.c.feed("враг появился", True)
        assert run(r) is None
    settings.update(dnd=True)
    r = Rig(mem)
    r.c.feed("враг появился", True)
    assert run(r) is None
    settings.update(dnd=False)
    r = Rig(mem)
    r.c.state_ts = 0.0                                                     # игру давно не видели
    r.c.feed("враг появился", True)
    assert run(r) is None


def test_silence_marker_repeat_and_model_failure_say_nothing(mem):
    for answer in ("<silent>", Exception("модель недоступна")):
        r = Rig(mem, answer=answer)
        r.c.feed("колонист упал", True)
        assert run(r) is None and r.said == []
    r = Rig(mem)
    r.c._repeats = lambda text: True
    r.c.feed("колонист упал", True)
    assert run(r) is None


def test_goals_and_plans_go_into_the_prompt(mem):
    import json
    (mem / "goals.json").write_text(json.dumps([{"title": "Форпост автономии", "category": "ambition", "status": "active", "progress_pct": 10}]), encoding="utf-8")
    (mem / "tasks.json").write_text(json.dumps([{"description": "Построить три кровати", "status": "pending"}]), encoding="utf-8")
    settings.update(streamer=True)
    r = Rig(mem)
    run(r)
    assert "Форпост автономии" in r.asked[0][1] and "Построить три кровати" in r.asked[0][1]


def test_battle_outranks_construction_noise_and_skips_the_gap(mem):
    r = Rig(mem)
    r.c._last_comment = time.time() - 10                    # 10 с назад: для обычного события рано, для боя нет
    r.c.feed("построено: Wall ×4", True)
    r.c.feed("появился враг: Рейдер (человек, здоровье 100%)", True)
    assert run(r) == "Sean лежит. Прекрасно."
    prompt = r.asked[0][1]
    assert "появился враг" in prompt and "Wall" not in prompt
    r2 = Rig(mem)
    r2.c._last_comment = time.time() - 10
    r2.c.feed("построено: Wall ×4", True)
    assert run(r2) is None


def test_clean_speech_drops_stage_directions_and_cut_off_tail():
    from commentator import clean_speech
    raw = ("(Голос ГЛаДОС звучит ровно, в кадре виден интерфейс RimWorld.)\n\nНу что ж, **план** выглядит отдалённым. "
           "Стены будут построены. Посмотрим, как эти наши ресурсы распреде")
    assert clean_speech(raw) == "Ну что ж, план выглядит отдалённым. Стены будут построены."
    assert clean_speech("Короткая реплика без точки") == "Короткая реплика без точки"


def test_letters_are_cleaned_and_get_their_own_reaction(mem):
    import fast_brain
    line = fast_brain.describe({"type": "letter", "data": {
        "label": "{'RawText': 'Скоро свадьба!', 'Length': 14, 'StrippedLength': 14}",
        "text": "<color=#D09B61FF>Corgi</color> предлагает <color=#D09B61FF>Erika</color> жить вместе."}})
    assert line == "Письмо: Скоро свадьба!. Corgi предлагает Erika жить вместе."
    r = Rig(mem)
    r.c.feed(line, True)
    r.c.feed("ресурсы WoodLog: 100 → 90", False)
    assert run(r) and "свадьба" in r.asked[0][1] and "WoodLog" not in r.asked[0][1]
