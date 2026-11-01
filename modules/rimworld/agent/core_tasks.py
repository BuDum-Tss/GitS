"""Цель и задачи игры из ядра (`core.tasks`, контракт 1.9): ими ведает единый агент, модуль только показывает их в оверлее, панели и
статусе фокуса. Свои файлы целей и задач прежнего игрового агента не читаются."""
import asyncio
import logging

logger = logging.getLogger("core_tasks")

INTERVAL_S = 10.0
_snapshot: dict = {}


def set_snapshot(snap: dict | None) -> None:
    global _snapshot
    _snapshot = snap if isinstance(snap, dict) else {}


def _active() -> dict | None:
    act = _snapshot.get("active")
    return act if isinstance(act, dict) else None


def _done_pct(task: dict) -> int:
    done, total = (list(task.get("progress") or [0, 0]) + [0, 0])[:2]
    return int(100 * done / total) if total else 0


def ambition() -> dict | None:
    """Цель игры: активная задача среды (для оверлея `{title, progress}`)."""
    act = _active()
    if not act or not act.get("goal"):
        return None
    mine = next((t for t in _snapshot.get("tasks") or [] if t.get("id") == act.get("id")), act)
    return {"title": str(act["goal"]), "progress": _done_pct(mine)}


def tree() -> list[dict]:
    """Дерево задач для оверлея: задача и её шаги (`text`, `status`, `children`, `progress`)."""
    out = []
    for t in _snapshot.get("tasks") or []:
        steps = [{"text": str(s.get("text", "")), "status": "done" if s.get("status") == "done" else "pending",
                  "children": [], "progress": 0} for s in t.get("steps") or []]
        for s in steps:
            if s["status"] == "pending":
                s["status"] = "in_progress"        # первый невыполненный шаг идёт сейчас
                break
        status = "done" if t.get("status") == "done" else "in_progress" if t.get("id") == (_active() or {}).get("id") else "pending"
        out.append({"text": str(t.get("goal", "")), "status": status, "children": steps, "progress": _done_pct(t)})
    return out


def steps_for_focus() -> list[dict]:
    """Шаги активной задачи в прежнем виде для статуса фокуса и строки хода: `{description, status}`."""
    act = _active()
    if not act:
        return []
    rows, marked = [], False
    for s in act.get("steps") or []:
        status = "done" if s.get("status") == "done" else "pending"
        if status == "pending" and not marked:
            status, marked = "in_progress", True
        rows.append({"description": str(s.get("text", "")), "status": status})
    return rows


def goal_title() -> str:
    a = ambition()
    return a["title"] if a else ""


async def run(client) -> None:
    """Раз в `INTERVAL_S` спрашивает у ядра цель и задачи среды модуля; сбой (ядро ещё не готово) не страшен."""
    while True:
        try:
            set_snapshot(await client.tasks("rimworld"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"core.tasks: {e}")
        await asyncio.sleep(INTERVAL_S)
