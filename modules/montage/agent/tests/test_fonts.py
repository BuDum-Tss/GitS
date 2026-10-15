"""Шрифты (STY-2, MON-24): файлы из регламента есть, кириллица на месте, оси вариативного Plex Sans, копии в шаблоне."""
from layout import DEFAULT
import json

import pytest
from fontTools.ttLib import TTFont

TEXT = "Испытание № 07. ЙЁЖЩЪЫЭЮЯ ИЙ ийёжщ — «ёлка»"
REQUIRED = ["IBMPlexSansCondensed-SemiBold.ttf", "IBMPlexSansCondensed-Bold.ttf", "IBMPlexSans-Medium.ttf",
            "IBMPlexMono-Regular.ttf"]


@pytest.fixture(scope="module")
def fonts_dir(root):
    return DEFAULT.assets / "fonts"


def test_files_and_license_exist(fonts_dir):
    for name in REQUIRED + ["OFL.txt", "fonts.json"]:
        assert (fonts_dir / name).is_file(), name
    assert "SIL OPEN FONT LICENSE Version 1.1" in (fonts_dir / "OFL.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", REQUIRED)
def test_cyrillic_and_punctuation_glyphs_present(fonts_dir, name):
    cmap = TTFont(fonts_dir / name).getBestCmap()
    missing = sorted({c for c in TEXT if ord(c) not in cmap})
    assert missing == [], f"{name}: нет глифов {missing}"


def test_sans_files_are_variable_with_condensed_axis(fonts_dir):
    for name in REQUIRED[:3]:
        axes = {a.axisTag: (a.minValue, a.maxValue) for a in TTFont(fonts_dir / name)["fvar"].axes}
        assert axes["wght"][0] <= 500 and axes["wght"][1] >= 700
        assert axes["wdth"][0] <= 75                       # узкое начертание (Condensed)


def test_fonts_json_lists_the_required_files(fonts_dir):
    data = json.loads((fonts_dir / "fonts.json").read_text(encoding="utf-8"))
    assert [f["file"] for f in data["fonts"]] == REQUIRED
    assert {f["role"] for f in data["fonts"]} == {"caption", "heading", "service", "mono"}


def test_template_copies_match(root, fonts_dir):
    pub = root / "template" / "public" / "fonts"
    for name in REQUIRED + ["OFL.txt"]:
        assert (pub / name).read_bytes() == (fonts_dir / name).read_bytes(), name
