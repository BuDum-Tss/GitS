"""Разовый перенос старых напоминаний `reminders.json` (ТЗ 30) в общие задачи `tasks.db` (ТЗ 32, WS-8).

Выполняется при первом открытии базы задач в папке памяти персонажа. Файл остаётся на месте (ничего не теряется), повторного
переноса нет: отметка `migrated_reminders_json` лежит в той же базе. Записи сохраняют прежние id, сроки, источник и недоверенность.
"""
import json
import logging
from pathlib import Path

from domain import tasks as dom

logger = logging.getLogger("tasks")

REMINDERS_FILE = "reminders.json"
MARK = "migrated_reminders_json"
_STATUS = {"pending": dom.PENDING, "fired": dom.DONE, "cancelled": dom.CANCELLED}


def migrate_reminders(store, folder: Path) -> int:
    """Перенести напоминания из `folder/reminders.json` в `store` (TaskStore). Возвращает число перенесённых записей."""
    if store.get_meta(MARK):
        return 0
    path = Path(folder) / REMINDERS_FILE
    items: list = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else []
        except (OSError, ValueError) as e:
            logger.warning(f"reminders.json не прочитан, перенос отложен: {e}")
            return 0
    n = 0
    for r in items:
        if not isinstance(r, dict) or not r.get("id") or r.get("due") is None or store.get(str(r["id"])) is not None:
            continue
        src = r.get("source") or {}
        t = dom.new_task(str(r["id"]), "assistant", str(r.get("text") or ""), float(r.get("created") or 0.0),
                         origin=dom.USER if src.get("kind") == "plan" else dom.SELF, due=float(r["due"]), kind=dom.KIND_REMINDER,
                         meta={"source": src, "depth": int(r.get("depth") or 0), "untrusted": bool(r.get("untrusted")),
                               "ttl_s": r.get("ttl_s")})
        d = t.to_dict()
        d["status"] = _STATUS.get(r.get("status"), dom.PENDING)
        store.save(d)
        n += 1
    store.set_meta(MARK, {"count": n, "file": str(path)})
    if n:
        logger.info(f"Напоминания перенесены в задачи: {n}")
    return n
