"""Манифесты модулей GitS проходят проверку SDK и не объявляют политик фокуса (удалены в контракте 1.10)."""
import importlib
import sys
from pathlib import Path

import pytest

from gits_sdk.protocol import Manifest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("telegram", "voice", "pet", "desktop")


def load_manifest(module: str) -> dict:
    """manifest.py каждого модуля лежит в своей папке и имеет то же имя (common.py тоже): грузим из папки модуля,
    а прежние записи sys.modules возвращаем на место."""
    folder = str(ROOT / ("services" if module in ("voice", "pet") else "workspaces") / module)
    names = ("manifest", "common")
    saved = {k: sys.modules.pop(k) for k in names if k in sys.modules}
    sys.path.insert(0, folder)
    try:
        return importlib.import_module("manifest").build_manifest()
    finally:
        sys.path.remove(folder)
        for k in names:
            sys.modules.pop(k, None)
        sys.modules.update(saved)


@pytest.mark.parametrize("module", MODULES)
def test_module_manifest_is_valid_and_has_no_policies(module):
    manifest = load_manifest(module)
    Manifest.from_dict(manifest)                                 # бросает ManifestError при нарушении
    assert "policies" not in manifest and "default_policy" not in manifest
