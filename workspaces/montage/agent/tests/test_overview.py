"""Обзор модуля на начальной странице: краткое описание первым, дальше процесс и всё, что модуль добавляет (из манифеста)."""
from manifest import MANIFEST
from overview import SUMMARY, overview_html


def test_overview_starts_with_summary_and_lists_everything_from_the_manifest():
    page = overview_html()
    assert page.index(SUMMARY[:40]) < page.index("Как идёт работа") < page.index("Инструменты агента")
    for tool in MANIFEST["tools"]:
        assert f"<code>{tool['name']}</code>" in page
    for command in MANIFEST["workspace"]["commands"]:
        assert f"<code>run {command['id']}</code>" in page
    for skill in MANIFEST["workspace"]["skills"]:
        assert skill["name"] in page
    for event in MANIFEST["emits"]:
        assert event["kind"] in page
    assert "run compose" in page and "work scenes" in page
