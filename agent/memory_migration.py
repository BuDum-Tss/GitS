"""Разовый перенос старой памяти (`memories.json`, вид user/self/plan/episode) в новую структуру.

user, self и episode -> профиль владельца в графе (пожелания к ней и договорённости тоже о нём), plan -> задача `plan` в `tasks.db`. Берётся только важное
(user/self/episode с важностью 4-5, планы от 3 и не устаревшие), остальное отбрасывается. Файл не удаляется: он переименовывается
в `memories.json.bak` (откат: вернуть имя). Повторный запуск ничего не делает, пока нет нового `memories.json`.
"""
import json
import logging
import os
import time
from pathlib import Path

from adapters.memory import MIN_PLAN_IMPORTANCE
from domain import plan_due

logger = logging.getLogger("memory_migration")

FILE = "memories.json"
BACKUP = FILE + ".bak"
STALE_PLAN_S = 12 * 3600       # план, срок которого прошёл больше чем на полдня назад, не переносится


async def migrate(folder: Path, graph, tasks, owner: str = "", clock=time.time) -> dict:
    """Перенести `folder/memories.json`. Возвращает счётчики {owner, plans, dropped} (пусто, если переносить нечего)."""
    path = Path(folder) / FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"Старая память не читается ({e}): файл оставлен как есть")
        return {}
    done = {"owner": 0, "plans": 0, "dropped": 0}
    now = clock()
    for it in data.get("items", []):
        kind, text = it.get("kind"), str(it.get("text") or "").strip()
        try:
            importance = int(it.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        ok = False
        if kind in ("user", "self", "episode") and graph is not None:
            ok = graph.remember(text, importance, subject=owner)
            done["owner"] += ok
        elif kind == "plan" and tasks is not None and importance >= MIN_PLAN_IMPORTANCE:
            due = it.get("due") or plan_due.due_of_text(text, float(it.get("ts") or now))
            if due > now - STALE_PLAN_S:
                await tasks.upsert_plan("m-" + str(it.get("id") or abs(hash(text))), text[:300], float(due))
                done["plans"] += 1
                ok = True
        if not ok:
            done["dropped"] += 1
    meta = data.get("meta") or {}
    if meta.get("consolidated_ts"):                              # когда последний раз осмысляли беседу: не осмыслять заново
        try:
            (Path(folder) / "memory_meta.json").write_text(json.dumps({"consolidated_ts": meta["consolidated_ts"]}), encoding="utf-8")
        except OSError:
            pass
    os.replace(path, Path(folder) / BACKUP)
    return done
