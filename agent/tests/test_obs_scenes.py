"""Коллекция сцен OBS (tools/obs_scenes.py): структура, ссылки элементов на источники, страницы сцен существуют."""
import json
import re
from pathlib import Path

from tools import obs_scenes

STATIC = Path(__file__).resolve().parents[1] / "static" / "stream"
TEMPLATE = {"sources": [{"canvas_uuid": "c-1"}], "DesktopAudioDevice1": {"name": "Звук рабочего стола"}, "modules": {"x": 1}}


def test_three_scenes_with_items_pointing_to_real_sources():
    data = obs_scenes.build(TEMPLATE)
    by_uuid = {s["uuid"]: s for s in data["sources"]}
    assert len(by_uuid) == len(data["sources"])
    assert [s["name"] for s in data["scene_order"]] == ["Кабинет", "AFK", "Игра"]
    scenes = {s["name"]: s for s in data["sources"] if s["id"] == "scene"}
    game = scenes["Игра"]["settings"]["items"]
    assert [by_uuid[i["source_uuid"]]["id"] for i in game] == ["game_capture", "browser_source"]       # захват под рамкой
    assert [i["id"] for i in game] == [1, 2] and scenes["Игра"]["settings"]["id_counter"] == 2
    for sc in scenes.values():
        assert all(i["source_uuid"] in by_uuid for i in sc["settings"]["items"])
    assert data["DesktopAudioDevice1"] == TEMPLATE["DesktopAudioDevice1"] and data["modules"] == {"x": 1}
    assert all(s.get("canvas_uuid") == "c-1" for s in data["sources"])
    json.dumps(data, ensure_ascii=False)


def test_browser_sources_point_to_existing_stream_pages():
    data = obs_scenes.build()
    urls = [s["settings"]["url"] for s in data["sources"] if s["id"] == "browser_source"]
    assert len(urls) == 3
    for url in urls:
        page = STATIC / url.rsplit("/", 1)[1]
        assert page.exists(), url
        html = page.read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href)="([^"/][^"]*)"', html):
            if not ref.startswith("http"):
                assert (STATIC / ref).exists(), f"{page.name}: {ref}"
