"""Пространство модуля RimWorld в дашборде (ТЗ 15, TAB-5, TAB-9): ссылки на оверлей и пульт оценок, блоки состояния, колонистов,
решений, оценок и записи забега; отправка из StateReporter только при изменении. Сеть и игра не нужны."""
import json
import time
from types import SimpleNamespace

from glados_module import protocol as P

import game_panel
import module_manifest
import state_reporter


def snapshot(connected=True, phase="colony"):
    return {"connected": connected, "colony_name": "Дом" if connected else None, "tick": 100 if connected else None,
            "pawns": [{"name": "<b>Борис</b>", "job": "idle", "mood_label": "happy 80%", "health_pct": 0.9}]
            if connected else [],
            "ambition": {"title": "Построить базу"} if connected else None,
            "mode": {"mode": "observe", "label": "наблюдает"},
            "recent_decisions": [{"tick": 90, "directive": "строить стену", "actions": ["build_wall"]}],
            "appraisals": [{"id": "a1", "text": "набег", "outcome": "верно", "label": "верно"}],
            "game_phase": phase, "game_running": phase == "colony"}


def test_manifest_has_the_two_links_and_is_valid():
    m = module_manifest.build_manifest([])
    warnings: list[str] = []
    assert P.validate_manifest(m, warnings) == [] and warnings == []
    spec = P.Manifest.from_dict(m)
    assert spec.panel is not None and spec.panel.title == "RimWorld"
    assert [(link.id, link.url) for link in spec.panel.links] == [
        ("overlay", "http://127.0.0.1:8767/overlay"), ("scores", "http://127.0.0.1:8766/")]
    assert all(link.kind == "url" for link in spec.panel.links)


def test_panel_blocks_with_colony_and_recording():
    rec = {"file": "raw_101010.mp4", "minutes": 12, "colony": "Дом"}
    blocks = game_panel.panel_blocks(snapshot(), rec)
    assert [b["type"] for b in blocks] == ["stats", "table", "list", "list", "text"]
    items = {i["label"]: i["value"] for i in blocks[0]["items"]}
    assert items == {"Игра": "колония загружена", "Колония": "Дом", "Режим агента": "наблюдает",
                     "Цель": "Построить базу"}
    table = blocks[1]
    assert table["columns"] == ["Имя", "Занятие", "Настроение", "Здоровье"]
    assert table["rows"][0]["cells"] == ["<b>Борис</b>", "idle", "happy 80%", "90 %"]   # HTML остаётся строкой (экранирует дашборд)
    assert blocks[2]["items"][0]["text"] == "строить стену" and blocks[2]["items"][0]["badge"] == "90"
    assert blocks[4]["text"] == "Идёт запись: raw_101010.mp4, 12 мин."


def test_panel_blocks_without_game_and_without_recording():
    blocks = game_panel.panel_blocks(snapshot(connected=False, phase="none"), None)
    items = {i["label"]: i["value"] for i in blocks[0]["items"]}
    assert items["Игра"] == "не запущена" and items["Колония"] == "нет" and items["Цель"] == "не поставлена"
    assert blocks[1]["type"] == "text" and "не загружена" in blocks[1]["text"]
    assert blocks[-1] == {"type": "text", "title": "Запись забега", "text": "Запись сейчас не ведётся."}


def test_panel_blocks_pass_the_protocol_validation():
    blocks = game_panel.panel_blocks(snapshot(), {"file": "x.mp4", "minutes": 1, "colony": "Дом"})
    assert P.validate_panel_set({"blocks": blocks}, set()) == []


def test_recording_info_reads_the_recorder_fields():
    rec = SimpleNamespace(run_id="r1", colony="Дом", raw="C:/videos/raw_101010.mp4", started=time.time() - 130)
    info = game_panel.recording_info(rec)
    assert info["file"] == "raw_101010.mp4" and info["minutes"] == 2 and info["colony"] == "Дом"
    assert game_panel.recording_info(None) is None


class FakeClient:
    def __init__(self):
        self.states, self.panels = [], []
        self.panel = SimpleNamespace(set=self._set)

    async def state(self, summary, data):
        self.states.append((summary, data))

    async def _set(self, blocks):
        self.panels.append(blocks)
        return True


async def test_state_reporter_sends_panel_only_when_it_changes(monkeypatch):
    data = snapshot()

    async def fake_build():
        return ("игра: колония", data)

    monkeypatch.setattr(state_reporter, "build_state", fake_build)
    client = FakeClient()
    reporter = state_reporter.StateReporter(client, recording=lambda: None)
    await reporter.send_once()
    await reporter.send_once()
    assert len(client.states) == 2 and len(client.panels) == 1           # одинаковые блоки второй раз не шлём
    data["mode"] = {"mode": "act", "label": "действует"}
    await reporter.send_once()
    assert len(client.panels) == 2
    assert json.dumps(client.panels[-1], ensure_ascii=False).count("действует") == 1
