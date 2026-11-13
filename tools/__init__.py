"""Инструменты репозитория (сборка схемы архитектуры, выгрузки, оценки, прогоны). Запуск из корня репозитория:
`python -m tools.<имя>` или `python tools/<имя>.py`. Пакет подключает к путям импорта `agent/` (ядро) и `sdk/` (gits_sdk)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "agent", _ROOT / "sdk"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
