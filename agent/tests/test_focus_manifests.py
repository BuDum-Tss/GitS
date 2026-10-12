"""Манифесты модулей GitS объявляют политики фокуса (ТЗ 18, FOC-1; ТЗ 11e, POL-13) и проходят проверку SDK."""
import importlib
import sys
from pathlib import Path

import pytest

from glados_module.protocol import Manifest

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "telegram": ("available", {"available", "do_not_disturb"}),
    "voice": ("ambient", {"ambient"}),
    "pet": ("ambient", {"ambient"}),
    "desktop": ("idle", {"idle"}),
}


def load_manifest(module: str) -> dict:
    """manifest.py каждого модуля лежит в своей папке и имеет то же имя (common.py тоже): грузим из папки модуля,
    а прежние записи sys.modules возвращаем на место."""
    folder = str(ROOT / "modules" / module)
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


@pytest.mark.parametrize("module", sorted(EXPECTED))
def test_module_manifest_declares_background_policies(module):
    manifest = load_manifest(module)
    parsed = Manifest.from_dict(manifest)                        # бросает ManifestError при нарушении FOC-1
    default, ids = EXPECTED[module]
    assert parsed.default_policy == default
    assert {p.id for p in parsed.policies} == ids
    assert all(p.focus == "background" for p in parsed.policies)  # ни один из этих модулей фокус сам не держит


def test_telegram_do_not_disturb_is_quiet():
    parsed = Manifest.from_dict(load_manifest("telegram"))
    assert parsed.policy("do_not_disturb").quiet is True
    assert parsed.policy("available").quiet is False
