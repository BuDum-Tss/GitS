"""Расход Claude для батарейки ядра: запрос core.claude_usage (ТЗ 11, RW-15)."""
import asyncio

import usage_report
from rigs import Rig


class _Msg:
    usage = {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 9999}


def test_push_result_counts_tokens_without_cache_reads(monkeypatch):
    sent = []
    monkeypatch.setattr(usage_report, "push", lambda tokens=0, source="game", limit_text="": sent.append((tokens, source)))
    usage_report.push_result(_Msg(), "turn")
    assert sent == [(160, "turn")]


def test_push_ignores_empty_and_missing_link(monkeypatch):
    monkeypatch.setattr(usage_report, "_client", None)
    usage_report.push(0)
    usage_report.push(500)                                    # связи нет: расход просто не учитывается, без исключений


def test_source_name_and_body():
    assert usage_report.source_name("turn") == "rimworld.turn"
    assert usage_report.source_name("rimworld.goals") == "rimworld.goals"
    assert usage_report.source_name("") == "rimworld.game"
    assert usage_report.body_of(120, "planning") == {"tokens": 120, "source": "rimworld.planning"}
    assert usage_report.body_of(0, "game", "лимит исчерпан") == \
        {"tokens": 0, "source": "rimworld.game", "limit_text": "лимит исчерпан"}


async def test_push_sends_core_claude_usage_from_any_thread(tmp_path, monkeypatch):
    async with Rig(tmp_path) as rig:
        usage_report.attach(rig.client, asyncio.get_running_loop())
        try:
            await asyncio.to_thread(usage_report.push, 321, "turn")                         # как claude_quota.note из потока
            usage_report.push(0, "game", "You've hit your session limit · resets 7:40pm")   # лимит: токенов нет, есть текст
            await rig.core.wait_for(lambda: len(rig.usage) == 2)
        finally:
            monkeypatch.setattr(usage_report, "_client", None)
        assert rig.usage[0] == {"tokens": 321, "source": "rimworld.turn"}
        assert rig.usage[1]["limit_text"].startswith("You've hit") and rig.usage[1]["tokens"] == 0
