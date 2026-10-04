"""Мост к агенту RimWorld (репозиторий rimworld-agent, отдельный процесс).

Агент игры слушает свой порт и отдаёт состояние (/api/game) и принимает приказы игрока (/api/order). Если он не запущен,
мост молчит: ассистентка работает как раньше, просто без игровой части.
"""
import logging
import os

import httpx

logger = logging.getLogger("game_bridge")
URL = os.getenv("RIMWORLD_AGENT_URL", "http://127.0.0.1:8767")


async def order(text: str) -> bool:
    """Приказ игроку-агенту. True, если агент принял фразу как приказ по игре."""
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.post(f"{URL}/api/order", json={"text": text})
            r.raise_for_status()
            return bool(r.json().get("accepted"))
    except Exception:
        return False


async def game_state() -> dict:
    """Игровая часть состояния для дашборда: колония, колонисты, цели, задачи, режим. Пусто, если агента нет."""
    try:
        async with httpx.AsyncClient(timeout=2.5) as c:
            r = await c.get(f"{URL}/api/game")
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}
