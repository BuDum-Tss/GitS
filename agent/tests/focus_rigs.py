"""Заглушки для тестов менеджера фокуса (ТЗ 18, FOC-10): модули без процессов, Claude и LM Studio не вызываются.

FakeModule ведёт себя как модуль из ТЗ 18: отвечает на focus.release своим checkpoint, на policy.activate переходит в
политику и пишет её в статус (`active_policy`), на focus.resume возвращает задачу. Поведение меняется флагами
(refuse_release, refuse_activate, blocked, unknown_resume). Время подменяется: World.now двигает тесты без sleep."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import focus
from module_hub import ModuleError

GAME_POLICIES = {
    "default_policy": "afk",
    "policies": [
        {"id": "afk", "title": "Вне игры", "focus": "background", "interruptible": True, "transitions": []},
        {"id": "observe", "title": "Наблюдение", "focus": "active", "interruptible": True, "transitions": []},
        {"id": "combat", "title": "Бой", "focus": "active", "interruptible": True, "transitions": []},
    ],
}

TELEGRAM_POLICIES = {
    "default_policy": "available",
    "policies": [
        {"id": "available", "title": "Доступна", "focus": "background", "transitions": ["do_not_disturb", "replying"]},
        {"id": "do_not_disturb", "title": "Не беспокоить", "focus": "background", "quiet": True,
         "transitions": ["available"]},
        {"id": "replying", "title": "Отвечает", "focus": "active", "transitions": ["available"],
         "interruptible": True},
    ],
}

MONTAGE_POLICIES = {
    "default_policy": "idle",
    "policies": [
        {"id": "idle", "title": "Простой", "focus": "background", "transitions": []},
        {"id": "working", "title": "Монтаж", "focus": "active", "transitions": [], "interruptible": True},
        {"id": "waiting_for_materials", "title": "Нет исходников", "focus": "background", "transitions": []},
    ],
}


def policy_table(spec: dict) -> dict:
    return {"default_policy": spec["default_policy"], "policies": copy.deepcopy(spec["policies"])}


class FakeModule:
    """Модуль с политиками: соединение (request), статус и журнал вызовов."""

    def __init__(self, world: "World", module_id: str, spec: dict, *, active: str | None = None):
        self.world = world
        self.id = module_id
        self.spec = spec
        self.calls: list[tuple[str, dict]] = []
        self.refuse_release = False
        self.refuse_activate = False
        self.unknown_resume = False
        self.refuse_resume = False
        self.checkpoint_payload: dict | None = {"step": 3, "goal_text": module_id}
        self.status: dict = {"active_policy": active or spec["default_policy"], "interruptible": True}

    @property
    def active(self) -> str:
        return self.status["active_policy"]

    async def request(self, msg_type: str, body: dict | None = None, timeout: float = 30.0) -> Any:
        body = dict(body or {})
        self.calls.append((msg_type, body))
        self.world.log_call(self.id, msg_type, body)
        if msg_type == "focus.release":
            if self.refuse_release:
                return {"ok": False, "error": "небезопасная точка: продолжаю"}
            self.status["active_policy"] = self.spec["default_policy"]
            self.status["interruptible"] = True
            return {"ok": True, "checkpoint": copy.deepcopy(self.checkpoint_payload), "policy": self.active}
        if msg_type == "policy.activate":
            if self.refuse_activate:
                raise ModuleError("denied", "переход отклонён модулем")
            self.status["active_policy"] = body["policy"]
            return {"ok": True, "policy": body["policy"]}
        if msg_type == "focus.resume":
            if self.unknown_resume:
                raise ModuleError("unknown", "ядро не знает запрос «focus.resume»")
            if self.refuse_resume:
                return {"ok": False, "error": "задача потеряла актуальность"}
            self.status["active_policy"] = body["policy"]
            return {"ok": True, "policy": body["policy"]}
        raise ModuleError("unknown", f"нет запроса {msg_type}")


class World:
    """Ядро в памяти: модули, часы, журнал вызовов в порядке поступления."""

    def __init__(self, *, now: float = 1_000_000.0):
        self.now = now
        self.modules: dict[str, FakeModule] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.busy = False
        self.llm_reply: Any = None
        self.llm_error: Exception | None = None
        self.llm_calls: list[dict] = []
        self.recheck_result: Any = None
        self.recheck_calls = 0

    def add(self, module_id: str, spec: dict, **kw) -> FakeModule:
        mod = FakeModule(self, module_id, policy_table(spec), **kw)
        self.modules[module_id] = mod
        return mod

    def log_call(self, module_id: str, msg_type: str, body: dict) -> None:
        self.calls.append((module_id, msg_type, body))

    def types(self, module_id: str | None = None) -> list[str]:
        return [t for m, t, _ in self.calls if module_id is None or m == module_id]

    def disconnect(self, module_id: str) -> None:
        self.modules.pop(module_id, None)

    async def _llm(self, ctx: dict) -> Any:
        self.llm_calls.append(ctx)
        if self.llm_error is not None:
            raise self.llm_error
        return self.llm_reply

    async def _recheck(self, ev: dict) -> Any:
        self.recheck_calls += 1
        return self.recheck_result

    def manager(self, tmp_path=None, *, llm=True, recheck=False, state=False) -> focus.FocusManager:
        state_path = (tmp_path / "focus.json") if (state and tmp_path is not None) else None
        return focus.FocusManager(
            connection_of=lambda mid: self.modules.get(mid),
            status_of=lambda mid: (self.modules[mid].status if mid in self.modules else None),
            policies_of=lambda mid: (self.modules[mid].spec if mid in self.modules else None),
            clock=lambda: self.now,
            state_path=state_path,
            llm=self._llm if llm else None,
            busy_of=lambda: self.busy,
            recheck=self._recheck if recheck else None,
            title_of=lambda mid: mid,
        )


def item(module: str, kind: str, text: str, priority: float, *, urgency: int | None = None, category: str | None = None,
         conf: float | None = None, expires: float | None = None, dedupe: str | None = None) -> SimpleNamespace:
    """Элемент внимания (attention.Item) с полями, которые читает менеджер фокуса."""
    data = {}
    if category is not None:
        data["category"] = category
    if conf is not None:
        data["category_conf"] = conf
    return SimpleNamespace(id=f"a-{kind}-{priority}", module=module, kind=kind, text=text, data=data,
                           urgency=urgency, expires=expires, dedupe_key=dedupe, priority=priority)


def decide(mgr: focus.FocusManager, it: SimpleNamespace, decision: str = "D3", rule: str | None = None) -> None:
    """Имитация хука внимания: решение и приоритет уже посчитаны."""
    mgr.on_decided(it, decision, it.priority, rule)


async def run(mgr: focus.FocusManager, world: World, steps: int = 1, dt: float = 0.0) -> None:
    for _ in range(steps):
        await mgr.tick()
        world.now += dt


def journal_types(mgr: focus.FocusManager) -> list[str]:
    return [r["type"] for r in mgr._journal]


def journal_reasons(mgr: focus.FocusManager, typ: str) -> list[str]:
    return [r.get("reason", "") for r in mgr._journal if r["type"] == typ]
