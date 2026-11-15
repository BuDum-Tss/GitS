"""Том персонажа (CHARACTER_HOME): раскладка, перенос export и проверка check."""
import sqlite3
from pathlib import Path

import pytest

from services.character import character
from host import settings
from tools import character_home


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / "vol"
    (home / "core").mkdir(parents=True)
    (home / "core" / "character.yaml").write_text("id: tester\nname: Тестерша\naliases: [Тестерша]\n", encoding="utf-8")
    (home / "memory").mkdir()
    return home


def test_home_layout(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setenv("CHARACTER_HOME", str(home))
    monkeypatch.delenv("MEMORY_DIR", raising=False)
    assert character.in_container()
    assert character.load("tester").folder == home
    assert character.memory_dir("tester", home / "memory") == home / "memory"
    assert settings.default_memory_dir() == str(home / "memory")
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "explicit"))
    assert settings.default_memory_dir() == str(tmp_path / "explicit")


def test_home_character_is_the_active_one_whatever_its_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARACTER_HOME", str(_make_home(tmp_path)))
    assert character.configured_id() == "tester"                  # настройки character нет, а в томе один персонаж
    monkeypatch.delenv("CHARACTER_HOME")
    assert character.configured_id() == character.DEFAULT_ID     # без тома как раньше


def _source(tmp_path):
    res = tmp_path / "res"
    folder = res / "Tester"
    (folder / "core").mkdir(parents=True)
    (folder / "core" / "character.yaml").write_text("id: tester\nname: Т\n", encoding="utf-8")
    (folder / "voice").mkdir()
    (folder / "voice" / "ref.wav").write_bytes(b"wav")
    (folder / "voice" / "__pycache__").mkdir()
    (folder / "voice" / "__pycache__" / "x.pyc").write_bytes(b"x")
    mem = tmp_path / "mem" / "characters" / "tester"
    (mem / "logs").mkdir(parents=True)
    (mem / "logs" / "a.txt").write_text("log", encoding="utf-8")
    (mem / "core.log").write_text("log", encoding="utf-8")
    (mem / "x.tmp").write_text("t", encoding="utf-8")
    (mem / "tool_embeddings.json").write_text("{}", encoding="utf-8")
    (mem / "settings.json").write_text("{}", encoding="utf-8")
    db = sqlite3.connect(mem / "graph.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("create table t (a)")
    db.execute("insert into t values (42)")
    db.commit()                       # живое соединение держит WAL: файл -wal существует
    return res, tmp_path / "mem", db


def test_export_and_check(tmp_path):
    res, mem_root, db = _source(tmp_path)
    target = tmp_path / "out"
    r = character_home.export("tester", target, res, mem_root)
    db.close()
    assert (target / "core" / "character.yaml").is_file() and (target / "voice" / "ref.wav").is_file()
    assert not (target / "voice" / "__pycache__").exists()
    m = target / "memory"
    assert (m / "settings.json").is_file()
    for gone in ("logs", "core.log", "x.tmp", "tool_embeddings.json", "graph.db-wal", "graph.db-shm"):
        assert not (m / gone).exists(), gone
    assert sqlite3.connect(m / "graph.db").execute("select a from t").fetchall() == [(42,)]
    assert r["memory"]["dbs"] == 1
    info = character_home.check(target)
    assert info["id"] == "tester" and info["memory_bytes"] > 0


def test_export_refuses_non_empty_target(tmp_path):
    res, mem_root, db = _source(tmp_path)
    db.close()
    target = tmp_path / "out"
    target.mkdir()
    (target / "x").write_text("1", encoding="utf-8")
    with pytest.raises(character_home.HomeError):
        character_home.export("tester", target, res, mem_root)
    assert character_home.main(["export", "tester", str(target), "--resources-root", str(res), "--memory-root", str(mem_root)]) == 1


def test_check_rejects_bad_home(tmp_path):
    home = tmp_path / "vol"           # в tmp_path фикстуры сами создают memory/
    home.mkdir()
    with pytest.raises(character_home.HomeError):
        character_home.check(home)
    (home / "core").mkdir()
    (home / "core" / "character.yaml").write_text("id: a\n", encoding="utf-8")
    with pytest.raises(character_home.HomeError, match="memory"):
        character_home.check(home)
