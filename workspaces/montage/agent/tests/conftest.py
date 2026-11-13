"""Корень репозитория и пути для тестов (pythonpath задан в pyproject.toml)."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture()
def fast(monkeypatch):
    """Быстрые замены дорогих на этой машине шагов: копия шаблона, junction, loudnorm."""
    import media
    import pipeline
    from tests import stubs
    monkeypatch.setattr(pipeline, "prepare_work", stubs.fast_prepare_work)
    monkeypatch.setattr(media, "loudnorm", stubs.fast_loudnorm)
    monkeypatch.setattr(media, "extract_frame", stubs.fast_extract_frame)
