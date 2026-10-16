"""Документ контракта создан из кода и актуален (ТЗ 31, MC-9)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_contract_doc", ROOT / "sdk" / "tools" / "gen_contract_doc.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generated_files_are_current():
    gen = _gen()
    files = gen.build()
    stale = [p.name for p, text in files.items()
             if not p.exists() or p.read_text(encoding="utf-8").replace("\r\n", "\n") != text]
    assert not stale, "запустите python sdk/tools/gen_contract_doc.py: устарели " + ", ".join(stale)
    existing = {p.name for p in gen.SCHEMA_DIR.glob("*.json")}
    assert existing == {p.name for p in files if p.suffix == ".json"}, "в docs/contract лишние или отсутствующие схемы"


def test_modules_guide_links_to_the_generated_contract():
    assert "MODULE_CONTRACT.md" in (ROOT / "docs" / "MODULES.md").read_text(encoding="utf-8")
