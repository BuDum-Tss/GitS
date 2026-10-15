"""Checkpoint задач модуля при потере фокуса (ТЗ 11e, POL-14, FOC-5 в ТЗ 18): цель, шаг плана, последние важные изменения,
ожидающие события, была ли игра поставлена на паузу модулем. Сохраняется на диск, чтобы возврат фокуса восстановил контур,
а не начинал с нуля. Записывает и читает контроллер."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

FILE = Path(__file__).parent / "memory" / "focus_checkpoint.json"
CHANGES_KEEP = 10


def build(*, policy: str, reason: str, goal: str, step: str, changes: list[str], pending: list[str],
          paused_by_module: bool, now: float | None = None) -> dict:
    return {"ts": round(now if now is not None else time.time()), "policy": policy, "reason": reason[:200], "goal": goal[:300],
            "step": step[:300], "changes": [c[:160] for c in changes[-CHANGES_KEEP:]], "pending": [p[:200] for p in pending[-5:]],
            "paused_by_module": bool(paused_by_module)}


def save(cp: dict, path: Path | None = None) -> bool:
    path = Path(path) if path is not None else FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load(path: Path | None = None) -> dict | None:
    path = Path(path) if path is not None else FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear(path: Path | None = None) -> None:
    path = Path(path) if path is not None else FILE
    try:
        path.unlink()
    except OSError:
        pass
