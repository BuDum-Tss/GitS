"""Режим закадра (ТЗ 05, CHR-4): прежнее имя glados читается как character, схема принимает оба."""
from manifest import MANIFEST, VOICEOVERS, normalize_voiceover


def test_old_mode_name_is_read_as_character():
    assert normalize_voiceover("glados") == "character"
    assert normalize_voiceover("character") == "character"
    assert normalize_voiceover("keep") == "keep"
    assert normalize_voiceover(None) == ""


def test_schema_accepts_both_names_and_the_new_list_is_the_canonical_one():
    enum = next(t for t in MANIFEST["tools"] if t["name"] == "start")["params"]["voiceover"]["enum"]
    assert {"none", "character", "keep", "glados"} <= set(enum)
    assert "glados" not in VOICEOVERS
