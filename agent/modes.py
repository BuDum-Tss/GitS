"""Профили окружений: ассистент в центре, вокруг него профили, между которыми он переключается сам.

Профиль = набор модулей (окружения реестра и внешние источники): RimWorld даёт инструменты игры, «Сон» не даёт
Claude-инструментов, остальные профили (Minecraft, программирование, монтаж) пока заглушки. Без профиля агент
просто ассистент: разговор и управление компьютером. Память, характер и контекст от профиля не зависят.

Профиль выбирается сам: нет заряда Claude (claude_battery.py) -> сон; запущена игра -> RimWorld; иначе ассистент.
Вручную: `set_override`. Граф для дашборда собирает `graph()`: узел-центр, профили, подграфы активных профилей
(RimWorld отдаёт свой по HTTP), заряд батарейки.
"""
import logging
from dataclasses import dataclass
from typing import Callable

import httpx

logger = logging.getLogger("modes")

HUB = "assistant"


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    description: str
    modules: tuple[str, ...] = ("core",)  # модули, включённые в профиле (поле `module` окружения в манифесте)
    planned: bool = False                 # профиль ещё не написан: на графе виден, включить нельзя
    subgraph_url: str | None = None       # агент профиля отдаёт свой граф: {"nodes": [...], "edges": [...]}


PROFILES: dict[str, Profile] = {p.id: p for p in (
    Profile(HUB, "Ассистент", "Обычное состояние: разговор, память, управление компьютером, Telegram."),
    Profile("rimworld", "RimWorld", "Игровой агент: мод, MCP-сервер, контуры хода, планирования и тактики.",
            ("core", "rimworld"), subgraph_url="http://localhost:8767/api/graph"),
    Profile("minecraft", "Minecraft", "Игровой агент Minecraft.", ("core", "minecraft"), planned=True),
    Profile("programming", "Программирование", "Работа с кодом: проекты, правки, проверки.", ("core", "code"),
            planned=True),
    Profile("editing", "Монтаж", "Монтаж готового ролика: нарезка, склейка, озвучка, выгрузка.", ("core", "content"),
            planned=True),
    Profile("sleep", "Сон", "Токены Claude кончились: никаких Claude-задач, только лёгкий разговор. "
                            "Просыпается сама, когда окно лимита освободится.", ("core",)),
)}


# Структура подграфа, пока агент профиля не отвечает: войти в подграф можно всегда. Живой граф берётся у агента.
_DEFAULT_SUBGRAPHS = {
    "rimworld": {
        "nodes": [{"id": "agent", "label": "Агент RimWorld", "root": True}, {"id": "observe", "label": "наблюдает"},
                  {"id": "act", "label": "действует"}, {"id": "plan", "label": "планирует"},
                  {"id": "tactic", "label": "в тактике (бой)"}],
        "edges": [{"from": "observe", "to": "act", "label": "пришёл ход"}, {"from": "act", "to": "observe", "label": "ход закончен"},
                  {"from": "observe", "to": "plan", "label": "пора раскладывать цели"},
                  {"from": "plan", "to": "observe", "label": "задачи расписаны"},
                  {"from": "observe", "to": "tactic", "label": "враги на карте"},
                  {"from": "act", "to": "tactic", "label": "враги на карте"},
                  {"from": "tactic", "to": "observe", "label": "бой окончен"}, {"from": "agent", "to": "observe", "label": "старт"}],
    },
}


@dataclass
class ModeManager:
    game_running: Callable[[], bool]
    battery: Callable[[], dict]           # claude_battery.status
    override: str | None = None
    _last: str = HUB

    def auto(self) -> str:
        if self.battery().get("sleeping"):
            return "sleep"
        return "rimworld" if self.game_running() else HUB

    def current(self) -> str:
        mode = "sleep" if self.battery().get("sleeping") else (self.override if self.override in PROFILES else self.auto())
        if mode != self._last:
            logger.info(f"Профиль: {self._last} -> {mode}" + (" (вручную)" if self.override == mode else ""))
            self._last = mode
        return mode

    def set_override(self, mode: str | None) -> str | None:
        """None или «auto»: вернуть автоматический выбор. Возвращает причину отказа или None."""
        if mode in (None, "", "auto"):
            self.override = None
            return None
        if mode not in PROFILES:
            return f"нет такого профиля: {mode}"
        if PROFILES[mode].planned:
            return f"профиль «{PROFILES[mode].title}» ещё не написан"
        self.override = mode
        return None

    def module_active(self, module: str | None) -> bool:
        """Для реестра окружений: окружение без модуля относится к ядру."""
        return (module or "core") in PROFILES[self.current()].modules

    async def graph(self) -> dict:
        """Узлы для дашборда: центр и профили; подграф активного профиля приходит от его агента."""
        cur = self.current()
        nodes = [{"id": p.id, "label": p.title, "title": p.description, "hub": p.id == HUB,
                  "planned": p.planned, "current": p.id == cur, "drill": True,
                  "modules": ["core"] if p.id == HUB else [m for m in p.modules if m != "core"]}
                 for p in PROFILES.values()]
        edges = [{"from": HUB, "to": p.id} for p in PROFILES.values() if p.id != HUB]
        subs = {}
        for p in PROFILES.values():
            if p.subgraph_url:
                live = await _fetch_subgraph(p)
                sub = live or _DEFAULT_SUBGRAPHS.get(p.id)
                subs[p.id] = {**sub, "live": bool(live)} if sub else None
        return {"current": cur, "auto": self.auto(), "override": self.override, "battery": self.battery(),
                "nodes": nodes, "edges": edges, "subgraphs": subs}


async def _fetch_subgraph(profile: Profile) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(profile.subgraph_url)
            r.raise_for_status()
            data = r.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None
