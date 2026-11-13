"""Совместимость переменных окружения (ТЗ 05, CHR-1, CHR-2): новое имя, старое GLADOS_* с предупреждением, копии файла."""
import logging
import os
import re
from pathlib import Path

from gits_sdk import envcompat

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ("agent", "workspaces", "agent/interfaces/workspace/gits_sdk", "services")


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


def test_decider_copy_of_envcompat_is_identical_to_the_sdk_file():
    """Участники и ядро берут `gits_sdk.envcompat`; единственная копия нужна сервису decider (его образ без SDK) и обязана совпадать."""
    canon = (ROOT / "agent" / "interfaces" / "workspace" / "gits_sdk" / "envcompat.py").read_bytes()
    assert (ROOT / "services" / "decider" / "envcompat.py").read_bytes() == canon
    import subprocess
    tracked = subprocess.run(["git", "ls-files", "*envcompat.py"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert sorted(f for f in tracked if not f.startswith("test/")) == [
        "agent/interfaces/workspace/gits_sdk/envcompat.py", "services/decider/envcompat.py"]       # других копий нет


def test_no_old_names_left_in_code():
    """После ТЗ 05 старое имя GLADOS_* живёт только в envcompat.py (таблица переименования) и в его тесте."""
    pattern = re.compile(r"GLADOS_[A-Z]")
    allowed = {ROOT / "agent" / "interfaces" / "workspace" / "gits_sdk" / "envcompat.py", ROOT / "services" / "decider" / "envcompat.py", ROOT / "test" / "test_envcompat.py"}
    hits = []
    for folder in CODE_DIRS:
        for path in (ROOT / folder).rglob("*.py"):
            if "__pycache__" in path.parts or ".venv" in path.parts or path in allowed or path.name == "envcompat.py":
                continue                                  # .venv: чужой код установленных пакетов (у служб своё окружение внутри репозитория)
            if pattern.search(path.read_text(encoding="utf-8")):
                hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"старые имена переменных в коде: {hits}"


def test_env_helper_reads_os_environ_not_a_copy(monkeypatch):
    monkeypatch.setenv("GITS_TEST_VALUE_ENVCOMPAT", "1")
    assert os.environ["GITS_TEST_VALUE_ENVCOMPAT"] == envcompat.env("GITS_TEST_VALUE_ENVCOMPAT")
