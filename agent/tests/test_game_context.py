"""Игровая среда: ассистентский шум не отвлекает, реплики не повторяются, Gemma видит, что реально делают исполнители."""
import time

import pytest
from test_app_attention import Rig, _item

import tool_trace
from app.context_builder import ContextBuilder
from domain import repeat, scoring


def test_assistant_noise_is_dropped_only_while_she_is_in_a_module_environment():
    assert scoring.is_assistant_noise("rimworld", "desktop", "desktop.app_changed")
    assert scoring.is_assistant_noise("rimworld", "desktop", "desktop.activity")
    assert not scoring.is_assistant_noise("assistant", "desktop", "desktop.activity")
    assert not scoring.is_assistant_noise(None, "desktop", "desktop.activity")
    assert not scoring.is_assistant_noise("sleep", "desktop", "desktop.activity")
    assert not scoring.is_assistant_noise("rimworld", "rimworld", "rimworld.raid")         # события самой игры не шум
    assert not scoring.is_assistant_noise("rimworld", "desktop", "desktop.launch")


def test_normalizer_makes_window_and_tab_events_urgency_zero_in_the_game(tmp_path):
    assign = Rig(tmp_path).att.normalizer.assign_urgency
    item = _item(urgency=None, urgency_hint=3, module="desktop", kind="desktop.app_changed")
    assign(item, None, "rimworld")
    assert item.urgency == 0
    other = _item(urgency=None, urgency_hint=3, module="desktop", kind="desktop.app_changed")
    assign(other, None, "assistant")
    assert other.urgency == 3                                                       # в обычной среде как прежде


def test_similar_replies_are_repeats_and_short_or_different_ones_are_not():
    said = ["Нажата кнопка «Продолжить». Видишь? Я просто повторяю действия."]
    assert repeat.is_repeat("Я нажала «Продолжить», подопытный. Видишь? Просто повторяю.", said)
    assert not repeat.is_repeat("Стены растут, колонисты заняты укреплениями.", said)
    assert not repeat.is_repeat("Хорошо.", ["Хорошо."])                                # слишком коротко, чтобы быть повтором
    assert not repeat.is_repeat("Что-то новое про колонию и её запасы дерева.", [])


@pytest.fixture
def clean_trace():
    tool_trace.clear()
    yield
    tool_trace.clear()


def test_actions_block_lists_what_executors_really_do_and_says_when_nothing_is_running(clean_trace):
    assert "ничего не запущено" in ContextBuilder.actions_block()
    tool_trace.note("claude", "place_building", "кровать")
    tool_trace.note("claude", "place_building", "кровать")
    eid = tool_trace.start("fast", "desktop.menu_click", "text=Продолжить")
    tool_trace.finish(eid, False, "пользователь за компьютером")
    text = ContextBuilder.actions_block()
    assert "Claude: place_building ×2 (сделано)" in text
    assert "планировщик: desktop.menu_click (не удалось): пользователь за компьютером" in text


def test_actions_block_forgets_old_entries(clean_trace):
    tool_trace.note("claude", "old_tool", ts=time.time() - 3600)
    assert "old_tool" not in ContextBuilder.actions_block()


def test_in_module_env_follows_the_workspace():
    class Host:
        env = "assistant"

        def workspace_of(self):
            return self.env

    host = Host()
    builder = ContextBuilder(host)
    assert not builder.in_module_env()
    host.env = "rimworld"
    assert builder.in_module_env()
    host.env = "sleep"
    assert not builder.in_module_env()
