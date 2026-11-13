"""Журнал действий: что пишется, как сворачивается, как попадает в подсказку."""
import time

import action_journal as aj


def entry(typ, text, ok=True, ago=0, **kw):
    return {"ts": time.time() - ago, "type": typ, "text": text, "ok": ok, **kw}


def test_only_mutating_commands_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(aj, "FILE", tmp_path / "actions.jsonl")
    aj.record({"type": "get_items"}, "OK")
    aj.record({"type": "build", "def_name": "Wall", "material": "WoodLog", "x": 5, "z": 6}, "OK: build")
    aj.record({"type": "build", "def_name": "Wall", "x": 5, "z": 6}, "ERROR: уже стоит")
    items = aj._load()
    assert [e["type"] for e in items] == ["build", "build"] and items[0]["text"] == "чертёж Wall из WoodLog на (5,6)"
    assert items[1]["ok"] is False and "уже стоит" in items[1]["error"]


def test_repeats_collapse_and_failures_are_marked():
    text = aj.recent_text(entries=[entry("build", "чертёж Wall на (1,1)"), entry("build", "чертёж Wall на (2,1)"),
                                   entry("build", "чертёж Door на (3,1)", ok=False, error="нельзя")])
    assert "и ещё 1 таких подряд" in text and "НЕ УДАЛОСЬ: нельзя" in text


def test_old_entries_and_empty_block(tmp_path, monkeypatch):
    monkeypatch.setattr(aj, "FILE", tmp_path / "actions.jsonl")
    assert aj.block() == ""
    assert aj.recent_text(minutes=10, entries=[entry("build", "старое", ago=3600)]) == ""
    aj.note("склад у входа", "рядом с мастерской")
    assert "склад у входа, потому что рядом с мастерской" in aj.block() and "сама" in aj.block()
