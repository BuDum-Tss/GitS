"""Цель и задачи игры из ядра (`core.tasks`, контракт 1.9): ими ведает единый агент, модуль только показывает их в оверлее, панели и
статусе фокуса. Свои файлы целей и задач прежнего игрового агента не читаются."""
import asyncio
import logging

logger = logging.getLogger("core_tasks")

INTERVAL_S = 10.0
_snapshot: dict = {}
_known = False             # снимок от ядра получен (ядро ответило): без него «цели нет» значило бы «ядро ещё не готово»
_last_goal: dict | None = None     # {id, goal} последней виденной активной цели
_closed_goal = ""          # название цели, которая пропала из открытых задач (выполнена, закрыта или отменена агентом)


def set_snapshot(snap: dict | None) -> None:
    """Принять снимок задач ядра. Заодно запоминает закрытую цель: активная цель была, а теперь её нет среди открытых задач
    (ядро отдаёт только открытые; агент закрывает цель `assistant.close_task`, и она исчезает из снимка)."""
    global _snapshot, _known, _last_goal, _closed_goal
    _snapshot = snap if isinstance(snap, dict) else {}
    _known = isinstance(snap, dict) and "tasks" in snap
    if not _known:
        _last_goal, _closed_goal = None, ""
        return
    act = _active()
    if act and act.get("goal"):
        _closed_goal = ""
        _last_goal = {"id": act.get("id"), "goal": str(act["goal"])}
        return
    if _last_goal is not None:
        still_open = any(t.get("id") == _last_goal["id"] for t in _snapshot.get("tasks") or [])
        if not still_open:
            _closed_goal = _last_goal["goal"]
        _last_goal = None


def known() -> bool:
    """Ядро уже отдало снимок задач среды (иначе об активной цели ничего не известно)."""
    return _known


def closed_goal() -> str:
    """Название цели, закрытой последней, пока новой активной нет; иначе пусто."""
    return _closed_goal


def active_progress() -> tuple[int, int]:
    """Шаги активной цели: (выполнено, всего); цели нет: (0, 0)."""
    act = _active()
    if not act:
        return 0, 0
    mine = next((t for t in _snapshot.get("tasks") or [] if t.get("id") == act.get("id")), act)
    done, total = (list(mine.get("progress") or [0, 0]) + [0, 0])[:2]
    return int(done), int(total)


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
