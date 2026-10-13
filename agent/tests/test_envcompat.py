"""Совместимость переменных окружения (ТЗ 05, CHR-1, CHR-2): новое имя, старое GLADOS_* с предупреждением, копии файла."""
import logging
import os
import re
from pathlib import Path

import envcompat

ROOT = Path(__file__).resolve().parents[2]
CODE_DIRS = ("agent", "modules", "sdk/gits_sdk", "services")


def test_new_name_wins_over_old(monkeypatch):
    monkeypatch.setenv("GITS_CORE_URL", "ws://new")
    monkeypatch.setenv("GLADOS_CORE_URL", "ws://old")
    assert envcompat.env("GITS_CORE_URL", "x") == "ws://new"


def test_old_name_is_read_with_one_warning(monkeypatch, caplog):
    monkeypatch.delenv("GITS_STT_MODEL", raising=False)
    monkeypatch.setenv("GLADOS_STT_MODEL", "medium")
    monkeypatch.setattr(envcompat, "_warned", set())
    with caplog.at_level(logging.WARNING, logger="gits.env"):
        assert envcompat.env("GITS_STT_MODEL", "small") == "medium"
        assert envcompat.env("GITS_STT_MODEL", "small") == "medium"
    warnings = [r for r in caplog.records if "GLADOS_STT_MODEL" in r.getMessage()]
    assert len(warnings) == 1
    assert "GITS_STT_MODEL" in warnings[0].getMessage()


def test_default_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("GITS_LM_URL", raising=False)
    monkeypatch.delenv("GLADOS_LM_URL", raising=False)
    assert envcompat.env("GITS_LM_URL", "http://d") == "http://d"
    assert envcompat.env("GITS_LM_URL") is None


def test_character_names_fall_back_to_old_tail(monkeypatch):
    monkeypatch.delenv("CHARACTER_FAST_MODEL", raising=False)
    monkeypatch.setenv("GLADOS_FAST_MODEL", "gemma")
    assert envcompat.env("CHARACTER_FAST_MODEL") == "gemma"


def test_explicit_rename_for_core_http_url(monkeypatch):
    monkeypatch.delenv("GITS_CORE_HTTP_URL", raising=False)
    monkeypatch.setenv("GLADOS_ASSISTANT_URL", "http://127.0.0.1:8766")
    assert envcompat.legacy_name("GITS_CORE_HTTP_URL") == "GLADOS_ASSISTANT_URL"
    assert envcompat.env("GITS_CORE_HTTP_URL") == "http://127.0.0.1:8766"


def test_names_without_prefix_have_no_fallback():
    assert envcompat.legacy_name("RIMWORLD_MODULE_DIR") is None


def test_copies_of_envcompat_are_identical():
    """Модули, SDK и сервис decider не импортируют agent/: их копии обязаны совпадать с каноническим файлом."""
    canon = (ROOT / "agent" / "envcompat.py").read_bytes()
    copies = sorted(ROOT.glob("modules/*/envcompat.py")) + [ROOT / "sdk" / "gits_sdk" / "envcompat.py",
                                                            ROOT / "services" / "decider" / "envcompat.py"]
    assert len(copies) >= 6
    for copy in copies:
        assert copy.read_bytes() == canon, f"копия не совпадает с agent/envcompat.py: {copy.relative_to(ROOT)}"


def test_no_old_names_left_in_code():
    """После ТЗ 05 старое имя GLADOS_* живёт только в envcompat.py (таблица переименования) и в его тесте."""
    pattern = re.compile(r"GLADOS_[A-Z]")
    allowed = {ROOT / "agent" / "envcompat.py", ROOT / "agent" / "tests" / "test_envcompat.py"}
    hits = []
    for folder in CODE_DIRS:
        for path in (ROOT / folder).rglob("*.py"):
            if "__pycache__" in path.parts or path in allowed or path.name == "envcompat.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"старые имена переменных в коде: {hits}"


def test_env_helper_reads_os_environ_not_a_copy(monkeypatch):
    monkeypatch.setenv("GITS_TEST_VALUE_ENVCOMPAT", "1")
    assert os.environ["GITS_TEST_VALUE_ENVCOMPAT"] == envcompat.env("GITS_TEST_VALUE_ENVCOMPAT")
