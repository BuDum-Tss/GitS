"""Свои ресурсы монтажа: инструмент resources и короткие имена в start.sources."""
from layout import Layout


def make(tmp_path):
    root = tmp_path / "characters"
    media = root / "glados" / "montage" / "design" / "assets" / "Intro"
    media.mkdir(parents=True)
    (media / "Aperture_Boot_dark.mp4").write_bytes(b"x" * 2048)
    (root / "glados" / "montage" / "design" / "components").mkdir()
    (root / "glados" / "montage" / "design" / "components" / "bundle.css").write_text("a")
    return Layout().for_owner("glados", root)


def test_short_names_resolve_to_the_resource(tmp_path):
    lay = make(tmp_path)
    expected = lay.character / "design" / "assets" / "Intro" / "Aperture_Boot_dark.mp4"
    for raw in ("design/assets/Intro/Aperture_Boot_dark.mp4", "montage/design/assets/Intro/Aperture_Boot_dark.mp4",
                "resources/GlaDOS/montage/design/assets/Intro/Aperture_Boot_dark.mp4"):
        assert lay.resolve_resource(raw) == expected, raw
    assert lay.resolve_resource("design/нет.mp4") is None and lay.resolve_resource("../x") is None
    other = lay.character.parent / "pet"
    other.mkdir()
    (other / "idle.png").write_bytes(b"image")
    (other / "settings.json").write_text("{}")
    assert lay.resolve_resource("pet/idle.png") == other / "idle.png"
    assert lay.resolve_resource("character/pet/idle.png") == other / "idle.png"
    assert lay.resolve_resource("character/pet/settings.json") is None


# ---------------------------------------------------------------- компактный каталог и группы

import pytest
from gits_sdk import ToolError

from layout import REQUIRED_FONTS
from tests.rig import Rig


def big_layout(tmp_path):
    root = tmp_path / "characters"
    base = root / "glados" / "montage"
    files = {"APERTURE_STYLE.md": 3000, "design/README.md": 900, "design/tokens.json": 1500,
             "design/assets/Room/glados-room.webp": 400000, "design/assets/Room/README.md": 300,
             "design/assets/Pet/README.md": 200, "design/assets/HUD/hud-frame.svg": 5000,
             "design/assets/Robots/atlas.svg": 9000, "design/assets/Robots/pbody.svg": 8000,
             "design/assets/Footage/rimworld-colony.jpg": 90000, "design/assets/Новая/x.png": 10,
             "design/components/Intro/a.tsx": 10, "out/preview.png": 10}
    for n in ("dark", "light", "vertical_dark", "vertical_light"):
        files[f"design/assets/Intro/Aperture_Boot_{n}.mp4"] = 5_000_000
    for n in ("angry", "happy", "idle", "question", "surprised", "thinking"):
        files[f"design/assets/Pet/pet-{n}.png"] = 70000
    for n in (*REQUIRED_FONTS, "fonts.json", "OFL.txt"):
        files[f"assets/fonts/{n}"] = 200000
    for n in ("beep", "buzz_error", "chime_down", "chime_up", "elevator_ding", "tick", "whoosh"):
        files[f"assets/sfx/{n}.wav"] = 40000
    for i in range(60):
        files[f"assets/pictograms/pict_{i:02d}.svg"] = 800
    for name, size in files.items():
        p = base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * min(size, 4096))
    assert len(files) > 90
    return Layout().for_owner("glados", root)


def make_mgr(tmp_path):
    rig = Rig(tmp_path)
    rig.manager.layout = big_layout(tmp_path)
    return rig


async def test_compact_catalog_fits_and_has_groups(tmp_path):
    rig = make_mgr(tmp_path)
    text = (await rig.tool("resources"))["text"]
    assert len(text) <= 1400, (len(text), text)
    for name in ("Регламент стиля", "Интро", "Комната", "Питомец", "Рамки/HUD", "Чертежи роботов", "Кадры игры", "Шрифты", "Звуки",
                 "Пиктограммы", "Новая"):
        assert name in text, name
    assert "resources/design/assets/Room/glados-room.webp" in text and "resources/APERTURE_STYLE.md" in text
    assert "resources/design/tokens.json" in text or "tokens.json" in text
    assert "C:" not in text and "Компоненты дизайна" in text and "preview.png" not in text
    catalog = (await rig.tool("resources", {"group": "Компоненты дизайна"}))["text"]
    assert "Intro" in catalog and "README.md" in catalog and "preview.html" in catalog


async def test_group_returns_full_list_with_sizes(tmp_path):
    rig = make_mgr(tmp_path)
    text = (await rig.tool("resources", {"group": "питомец"}))["text"]
    assert text.count("resources/design/assets/Pet/pet-") == 6 and "pet-idle.png (" in text and "КБ)" in text


async def test_unknown_group_lists_groups(tmp_path):
    rig = make_mgr(tmp_path)
    with pytest.raises(ToolError) as e:
        await rig.tool("resources", {"group": "Кухня"})
    assert e.value.code == "invalid" and "Комната" in str(e.value) and "Питомец" in str(e.value)


@pytest.mark.usefixtures("fast")
async def test_sources_accept_resources_form(tmp_path):
    rig = make_mgr(tmp_path)
    rig.manager.layout.character.joinpath("assets", "fonts").mkdir(parents=True, exist_ok=True)
    paths, req = rig.manager.preflight({"brief": "х", "sources": ["resources/design/assets/Room/glados-room.webp"]})
    expected = rig.manager.layout.character / "design" / "assets" / "Room" / "glados-room.webp"
    assert paths == [expected] and req["sources"] == [str(expected)]
