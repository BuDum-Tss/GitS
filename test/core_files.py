"""Список исходных файлов ядра (без данных, внешнего SDK, клиентов и тестов) для проверок, которые смотрят в код."""
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1] / "agent"
_SKIP = {"memory", "models", "resources", "clients", "logs", "node_modules", ".venv", "__pycache__", "tests"}


def core_sources() -> list[Path]:
    out = []
    for p in sorted(AGENT.rglob("*.py")):
        rel = p.relative_to(AGENT)
        if _SKIP & set(rel.parts) or rel.parts[:2] == ("interfaces", "workspace"):
            continue
        out.append(p)
    return out
