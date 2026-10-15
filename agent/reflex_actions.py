"""Простые команды, которые быстрый слой отдаёт игре без участия Claude.

Набор намеренно узкий: только то, что безопасно и обратимо.
  draft_colonists — призвать всех свободных колонистов к оружию;
  undraft         — отпустить их обратно к работе.

Каждая команда проходит проверки по реальному состоянию игры (а не только
по тому, что сказала локальная модель): draft только при наличии врагов,
undraft только когда врагов нет, плюс пауза между повторами.
Отключить: переменная окружения GITS_REFLEX_ACTIONS=off.
"""
import asyncio
import logging
import time

import httpx
from envcompat import env

logger = logging.getLogger("reflex")

BASE     = env("GITS_RIMWORLD_URL", "http://localhost:8765")   # мод отвечает только на имя localhost
ENABLED  = env("GITS_REFLEX_ACTIONS", "on").lower() != "off"
COOLDOWN_SECS = 30

EXECUTABLE = ("draft_colonists", "undraft")
_last_run: dict[str, float] = {}


def _pawn_ids(state: dict, drafted: bool) -> list[str]:
    pawns = (state.get("colony") or {}).get("pawns") or []
    return [p["id"] for p in pawns
            if p.get("id") and not p.get("is_prisoner")
            and bool(p.get("is_drafted")) == drafted]


def check(action: str, state: dict) -> tuple[bool, str]:
    """(можно ли выполнить, причина отказа)."""
    if not ENABLED:
        return False, "рефлекс-команды выключены"
    if action not in EXECUTABLE:
        return False, "нет такой простой команды"
    if time.time() - _last_run.get(action, 0) < COOLDOWN_SECS:
        return False, "уже выполняла недавно"
    threats = state.get("threats") or []
    if action == "draft_colonists":
        if not threats:
            return False, "врагов на карте нет"
        if not _pawn_ids(state, drafted=False):
            return False, "некого призывать"
    if action == "undraft":
        if threats:
            return False, "враги ещё на карте"
        if not _pawn_ids(state, drafted=True):
            return False, "некого отпускать"
    return True, ""


def _post(command: dict) -> str:
    r = httpx.post(f"{BASE}/command", json=command, timeout=6)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        return f"ERROR: {data.get('message', 'unknown')}"
    return "OK"


async def execute(action: str, state: dict) -> str:
    """Выполнить команду, если проходит проверки. Возвращает текст-итог для лога."""
    ok, why = check(action, state)
    if not ok:
        return f"пропущено ({why})"
    if action == "draft_colonists":
        ids = _pawn_ids(state, drafted=False)
        cmd = {"type": "draft", "pawn_ids": ids}
    else:
        ids = _pawn_ids(state, drafted=True)
        cmd = {"type": "undraft", "pawn_ids": ids}
    _last_run[action] = time.time()
    try:
        result = await asyncio.to_thread(_post, cmd)
    except Exception as e:
        return f"ошибка: {e}"
    return f"{result}: {action} × {len(ids)}"
