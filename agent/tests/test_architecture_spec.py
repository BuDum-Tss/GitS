"""Схема архитектуры: спецификация цела, сгенерированные файлы совпадают с ней, вкладка есть в GUI."""
from pathlib import Path

from tools.architecture import build, spec

ROOT = Path(__file__).resolve().parents[2]


def test_spec_edges_point_to_known_nodes():
    d = spec.data()
    ids = {n["id"] for g in d["groups"] for n in g["nodes"]}
    ids |= {g["id"] for g in d["groups"]}
    for e in d["edges"]:
        assert e[0] in ids and e[1] in ids, e
    assert all(g["id"] in spec.ROW for g in d["groups"])


def test_generated_files_match_spec():
    assert build.DATA_JS.read_text(encoding="utf-8") == build.render_data_js()
    assert build.DOCS_HTML.read_text(encoding="utf-8") == build.render_docs_html()
    assert build.DOCS_DRAWIO.read_text(encoding="utf-8") == build.render_drawio()


def test_gui_has_scheme_tab():
    html = (ROOT / "agent" / "static" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="arch"' in html and 'id="tab-arch"' in html
    assert "architecture-data.js" in html and "architecture.js" in html
