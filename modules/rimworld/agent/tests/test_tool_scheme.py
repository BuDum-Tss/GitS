"""Схема инструментов (docs/TOOLS_SCHEME.html) создана из манифеста и не расходится с кодом."""
import asyncio
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
import build_tool_scheme as bts  # noqa: E402
import module_manifest  # noqa: E402


def test_scheme_matches_code():
    assert bts.OUT.exists() and bts.OUT.read_text(encoding="utf-8") == bts.render(), \
        "схема устарела: python modules/rimworld/tools/build_tool_scheme.py"


def test_every_manifest_tool_in_scheme():
    manifest = asyncio.run(module_manifest.build())
    data = bts.collect(manifest)
    assert {t["name"] for t in data["tools"]} == {t["name"] for t in manifest["tools"]}


def test_screen_regions_point_to_real_nodes_and_images():
    manifest = asyncio.run(module_manifest.build())
    nodes = {n["path"] for n in manifest["nodes"]}
    for sid, sc in bts.UI_SCREENS.items():
        assert (bts.OUT.parent / sc["image"]).exists(), sc["image"]
        for node, *_rest, go in sc["regions"] + bts.COMMON:
            assert node in nodes, f"{sid}: узла {node} нет в манифесте"
            assert go is None or go in bts.UI_SCREENS
