"""Состояние модуля для ядра (ТЗ 11, RW-8): сообщение `state` раз в 5 секунд и сразу при смене фазы игры.

`summary` читает дашборд и планировщик: «игра: <нет | меню | колония «имя»>, режим агента: <режим>, цель: <главная цель>».
`data` это то, что отдаёт `GET /api/game` (колония, колонисты, ресурсы, цели, задачи, режим), плюс `game_running` - порт мода
открыт И колония загружена. По `game_running` ядро включает профиль RimWorld (`module_state` профиля) и показывает игровую
часть дашборда вместо прежнего `game_bridge.game_state()`.
"""
import asyncio
import logging

import agent_mode
import game_api
import gamenet

logger = logging.getLogger("state_reporter")

POLL_S = 5.0


def phase_of(port_open: bool, snapshot: dict) -> str:
    """none - игра не запущена, menu - мод отвечает, но колонии нет (главное меню, загрузка), colony - колония загружена."""
    if not port_open:
        return "none"
    return "colony" if snapshot.get("connected") else "menu"


def summary_of(phase: str, snapshot: dict, mode_label: str) -> str:
    if phase == "colony":
        game = f"колония «{snapshot.get('colony_name') or 'без имени'}»"
    else:
        game = "меню" if phase == "menu" else "нет"
    goal = (snapshot.get("ambition") or {}).get("title") or "не поставлена"
    return f"игра: {game}, режим агента: {mode_label}, цель: {goal}"


async def build_state() -> tuple[str, dict]:
    """(summary, data) модуля сейчас. Состояние игры берётся так же, как для оверлея (game_api.game_snapshot)."""
    port_open = await asyncio.to_thread(gamenet.port_open)
    snapshot = await game_api.game_snapshot()           # без игры возвращает пустую колонию (connected: false)
    phase = phase_of(port_open, snapshot)
    data = {**snapshot, "game_running": phase == "colony", "game_phase": phase}
    return summary_of(phase, snapshot, agent_mode.get()["label"]), data


class StateReporter:
    def __init__(self, client, interval: float = POLL_S, recording=None, policy=None):
        """recording() -> dict | None: статус записи забега для пространства в дашборде (TZ 15); None - без записи.
        policy() -> dict: политика, правила наблюдения и ручное управление (ТЗ 11e, POL-7), в state и на панели."""
        self.client = client
        self.interval = interval
        self._wake = asyncio.Event()
        self._phase: str | None = None
        self._recording = recording or (lambda: None)
        self._policy = policy or (lambda: {})
        self._panel_key: str | None = None

    def poke(self) -> None:
        """Отправить состояние сейчас (игра загрузилась или забег закончился), не дожидаясь пяти секунд."""
        self._wake.set()

    async def send_once(self) -> None:
        summary, data = await build_state()
        data = {**data, **(self._policy() or {})}
        if data["game_phase"] != self._phase:
            self._phase = data["game_phase"]
            logger.info(f"Игра: {summary}")
        await self.client.state(summary, data)
        await self._send_panel(data)

    async def _send_panel(self, data: dict) -> None:
        """Пространство «RimWorld» (TZ 15): блоки уходят только при изменении (SDK не чаще раза в секунду)."""
        import json
        import game_panel
        blocks = game_panel.panel_blocks(data, self._recording())
        key = json.dumps(blocks, ensure_ascii=False, sort_keys=True)
        if key != self._panel_key and await self.client.panel.set(blocks):
            self._panel_key = key

    async def run(self) -> None:
        while True:
            try:
                await self.send_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Состояние не собрано: {e}")
            try:
                await asyncio.wait_for(self._wake.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
