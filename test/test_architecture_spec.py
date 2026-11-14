"""Схема архитектуры: спецификация цела, сгенерированные файлы совпадают с ней, вкладка есть в GUI."""
from pathlib import Path

from tools.architecture import build, spec

ROOT = Path(__file__).resolve().parents[1]


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


def test_dashboard_has_resources_tab_without_architecture_tab():
    html = (ROOT / "agent" / "clients" / "gui" / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="resources"' in html and 'id="tab-resources"' in html
    assert 'data-tab="arch"' not in html
