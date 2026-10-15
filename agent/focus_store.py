"""Хранилище checkpoint и файл состояния фокуса (ТЗ 29, ARC-6; ТЗ 18, FOC-5): checkpoint задач в памяти (слот
`<модуль>/scratch` рабочей памяти), запись и чтение `focus.json`. Формат файла не менялся."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("focus")

CHECKPOINT_MAX_AGE_S = 6 * 3600.0
CHECKPOINT_MAX_BYTES = 16 * 1024
CHECKPOINTS_KEEP = 50
RESUME_RETRIES = 3
STATE_FILE = "focus.json"


def json_safe(payload: Any) -> dict | None:
    """Checkpoint модуля: только JSON и не больше CHECKPOINT_MAX_BYTES; иначе сохраняется пометка."""
    if not isinstance(payload, dict):
        return None
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"note": "checkpoint модуля не сериализуется"}
    if len(text.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
        return {"note": "checkpoint модуля больше лимита и не сохранён целиком"}
    return json.loads(text)


def _write_json(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


class CheckpointStore:
    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path
        self.checkpoints: dict[str, dict] = {}
        self.dirty = False

    def touch(self) -> None:
        self.dirty = True

    def create(self, slot: Any, payload: Any, status: str, reason: str, resume: dict, now: float) -> dict:
        """FOC-5: checkpoint задачи слота (module, task_id, policy, goal, priority, origin)."""
        cp = {"task_id": slot.task_id, "module": slot.module, "policy": slot.policy, "goal": slot.goal,
              "priority": slot.priority, "origin": slot.origin, "status": status, "reason": reason, "resume": resume,
              "created": now, "attempts": 0, "subgoals": [], "payload": json_safe(payload),
              "scratch": f"{slot.module}/scratch"}
        self.checkpoints[slot.task_id] = cp
        self.dirty = True
        return cp

    def prune(self) -> None:
        if len(self.checkpoints) > CHECKPOINTS_KEEP:
            done = sorted((c for c in self.checkpoints.values() if c["status"] in ("resumed", "stale")),
                          key=lambda c: c["created"])
            for cp in done[:len(self.checkpoints) - CHECKPOINTS_KEEP]:
                self.checkpoints.pop(cp["task_id"], None)

    def resumable(self) -> list[dict]:
        """Задачи на паузе или блокировке: сначала назначенные диспетчером, потом по приоритету и давности."""
        cands = [c for c in self.checkpoints.values() if c["status"] in ("paused", "blocked")]
        cands.sort(key=lambda c: (not c.get("resume_now"), -c["priority"], c["created"]))
        return cands

    def paused(self, limit: int = 5) -> list[dict]:
        return [c for c in self.checkpoints.values() if c["status"] in ("paused", "blocked")][:limit]

    async def save(self, state: dict) -> None:
        if self.state_path is None or not self.dirty:
            return
        text = json.dumps(state, ensure_ascii=False, default=str)
        try:
            await asyncio.to_thread(_write_json, self.state_path, text)
            self.dirty = False
        except OSError as e:                                    # checkpoint остаётся в памяти, запись повторится
            logger.warning(f"Фокус: состояние не записано на диск, повтор позже: {e}")

    def read(self) -> dict | None:
        """Содержимое файла состояния или None; checkpoint из него попадают в память."""
        path = self.state_path
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"Фокус: файл состояния не прочитан: {e}")
            return None
        if not isinstance(data, dict):
            return None
        for cp in data.get("checkpoints") or []:
            if isinstance(cp, dict) and isinstance(cp.get("task_id"), str):
                self.checkpoints[cp["task_id"]] = cp
        self.dirty = False
        return data
