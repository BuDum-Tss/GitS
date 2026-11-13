"""Пиктограммы (STY-3): набор из регламента §4.6, единый контур 6 % размера, один цвет, без градиентов и внешних ссылок."""
from layout import DEFAULT
import re
import xml.etree.ElementTree as ET

import pytest

NAMES = ["cube", "button", "person", "hazard_water", "laser", "exit", "warning", "clock", "gear", "eye", "speaker",
         "camera"]
SVG = "{http://www.w3.org/2000/svg}"


@pytest.mark.parametrize("name", NAMES)
def test_pictogram_is_simple_single_colour_outline(root, name):
    path = DEFAULT.assets / "pictograms" / f"{name}.svg"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    svg = ET.fromstring(text)
    assert svg.tag == SVG + "svg"
    vb = [float(v) for v in svg.attrib["viewBox"].split()]
    assert vb == [0, 0, 48, 48]
    assert float(svg.attrib["stroke-width"]) == pytest.approx(0.06 * 48, abs=0.01)         # контур 6 % размера
    assert svg.attrib["stroke"] == "currentColor" and svg.attrib["fill"] == "none"
    for forbidden in ("gradient", "<image", "href", "<text", "<style", "filter", "opacity"):
        assert forbidden not in text, forbidden
    colours = set(re.findall(r'(?:fill|stroke)="([^"]+)"', text))
    assert colours <= {"currentColor", "none"}                                              # один цвет
    shapes = [e for e in svg.iter() if e.tag.removeprefix(SVG) in {"path", "circle", "rect", "polygon", "line"}]
    assert 1 <= len(shapes) <= 4                                                            # без мелких деталей
    for e in svg.iter():                                                                    # внутри поля 48 x 48
        if e.tag == SVG + "circle":
            cx, cy, r = (float(e.attrib[k]) for k in ("cx", "cy", "r"))
            assert cx - r >= 0 and cx + r <= 48 and cy - r >= 0 and cy + r <= 48
        if e.tag == SVG + "path":
            nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", e.attrib["d"])]
            assert nums and min(nums) >= 0 and max(nums) <= 48


def test_no_extra_pictograms(root):
    found = sorted(p.stem for p in (DEFAULT.assets / "pictograms").glob("*.svg"))
    assert found == sorted(NAMES)
