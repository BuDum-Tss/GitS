"""Граница сервиса памяти (`agent/memory_service/`): остальное ядро берёт из него только имена, выставленные в `__init__.py`
(`memory_service.__all__`), и не импортирует внутренние файлы папки."""
import ast
from pathlib import Path

import memory_service

AGENT = Path(__file__).resolve().parents[1] / "agent"
SKIP_PARTS = {"memory_service", "tests", ".venv", "node_modules", "logs", "tools"}


def _production_files():
    for p in AGENT.rglob("*.py"):
        if SKIP_PARTS & set(p.relative_to(AGENT).parts):
            continue
        yield p, ast.parse(p.read_text(encoding="utf-8"))


def test_public_names_exist():
    assert all(hasattr(memory_service, n) for n in memory_service.__all__)


def test_nobody_outside_imports_memory_internals():
    public = set(memory_service.__all__)
    bad = []
    for p, tree in _production_files():
        rel = p.relative_to(AGENT)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                if n.module.startswith("memory_service."):
                    bad.append(f"{rel}: from {n.module}")
                elif n.module == "memory_service":
                    bad += [f"{rel}: {a.name}" for a in n.names if a.name not in public]
            elif isinstance(n, ast.Import):
                bad += [f"{rel}: import {a.name}" for a in n.names if a.name.startswith("memory_service.")]
    assert not bad, f"внутренние файлы памяти импортируются снаружи: {bad}"
