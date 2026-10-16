"""Обработчики игровых событий и приказ игроку (ТЗ 11; вынесены из core_service в ТЗ 29, ARC-7, поведение прежнее).

Игрового кода тут нет: ядро знает только «модуль с capabilities game и инструментом order» и события `rimworld.*` как данные."""
import logging

import journal
from module_hub import ModuleError

logger = logging.getLogger("core")

ORDER_TIMEOUT_S = 15           # RW-11: сколько ждём, пока модуль игры примет приказ


def make_game_order(modules, hub, profiles_of):
    """RW-11: приказ игроку-агенту. Каждому модулю из focus_modules активного профиля (`profiles_of()` отдаёт менеджер
    профилей), у которого есть capabilities ["game"] и инструмент order, отправляется tool.call order (тайм-аут 15 с);
    accepted: true - обращение обработано игрой."""
    async def game_order(text: str) -> bool:
        for module_id in profiles_of().current().focus_modules:
            rec = modules.get(module_id)
            manifest = (rec.manifest if rec is not None else None) or {}
            if "game" not in (manifest.get("capabilities") or []) or not modules.is_enabled(module_id):
                continue
            if not any(isinstance(t, dict) and t.get("name") == "order" for t in manifest.get("tools") or []):
                continue
            conn = hub.connection(module_id)
            if conn is None:
                continue
            try:
                value = await conn.request("tool.call", {"tool": "order", "args": {"text": text}}, timeout=ORDER_TIMEOUT_S)
            except ModuleError as e:
                logger.info(f"Приказ модулю «{module_id}» не принят: {e.message}")
                continue
            if isinstance(value, dict) and value.get("accepted"):
                return True
        return False
    return game_order


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
