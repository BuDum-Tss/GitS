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


def test_listing_skips_code_and_previews(tmp_path):
    lay = make(tmp_path)
    names = [p.name for p, _ in lay.list_resources()]
    assert names == ["Aperture_Boot_dark.mp4"]
