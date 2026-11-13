"""Обработчики игровых событий (ТЗ 11; вынесены из core_service в ТЗ 29, ARC-7).

Игрового кода тут нет: ядро знает события `rimworld.*` как данные. Приказы игре агент отдаёт инструментами среды."""
import logging

import journal

logger = logging.getLogger("core")

def make_colonists_handler(rel, graph):
    """Обработчик события rimworld.colonists (RW-9): `data` = {"pawns": [...], "colony": "имя"}. Раньше это делал канал
    /api/event агента игры (link_event)."""
    async def on_colonists(item) -> None:
        data = item.data or {}
        pawns, colony = data.get("pawns") or [], data.get("colony") or "колония"
        rel.sync_colonists(pawns)
        await graph.sync_colonists(pawns, colony)
    return on_colonists


def make_run_lost_hook():
    """Метка ошибки `run_lost` (ТЗ 24, TRC-5): забег проигран (колония погибла) - событие run_ended с исходом colony_died.
    Событие идёт обычным путём внимания; хук только пишет метку."""
    async def on_run_ended(item) -> None:
        data = item.data or {}
        if str(data.get("outcome") or "") == "colony_died":
            journal.mistake("run_lost", item.text[:200], refs=[item.created], outcome="colony_died",
                            trace_id=item.trace.id if item.trace is not None else None)
    return on_run_ended
