"""Связь со средой модуля: вход, выход и возобновление по протоколу модулей (вместо менеджера фокуса ТЗ 18).

Слот один: модуль, в чью среду агент сейчас вошёл. `activate` отправляет модулю `workspace.enter` (модулю без этого запроса
прежний `policy.activate`), `release` просит `workspace.exit` (прежний `focus.release`) и забирает checkpoint, `resume` возвращает
приостановленную задачу (`workspace.resume`, прежний `focus.resume`). Очереди событий, дребезга, закрепления и второго эшелона
решений нет: среду выбирает только `WorkspaceManager` по просьбе пользователя или по самоимпульсу. Checkpoint живёт в памяти.
"""
import itertools
import logging
import time
from typing import Any, Callable

from module_hub import ModuleError

logger = logging.getLogger("module_link")

RELEASE_TIMEOUT_S = 15.0
ACTIVATE_TIMEOUT_S = 20.0
RESUME_TIMEOUT_S = 20.0

RPC_NAMES = {"activate": ("workspace.enter", "policy.activate"), "release": ("workspace.exit", "focus.release"),
             "resume": ("workspace.resume", "focus.resume")}

_ids = itertools.count(1)


def _confirmed(value: Any, policy: str | None) -> bool:
    """Модуль подтвердил переход: ok true и, если назвал политику, то именно запрошенную."""
    return isinstance(value, dict) and value.get("ok") is True and value.get("policy") in (None, policy)


class ModuleLink:
    def __init__(self, *, connection_of: Callable[[str], Any], policies_of: Callable[[str], dict | None],
                 clock: Callable[[], float] = time.time):
        """connection_of(id) - соединение модуля или None; policies_of(id) - политики из манифеста или None."""
        self.connection_of, self.policies_of, self.clock = connection_of, policies_of, clock
        self.holder: dict | None = None             # {"module", "task_id", "policy", "goal", "since"}
        self.checkpoints: dict[str, dict] = {}      # модуль -> приостановленная задача

    def _table(self, module: str) -> dict[str, dict]:
        spec = self.policies_of(module) or {}
        return {p["id"]: p for p in spec.get("policies") or [] if isinstance(p, dict) and "id" in p}

    def default_active(self, module: str) -> str | None:
        return next((pid for pid, p in self._table(module).items() if p.get("focus") == "active"), None)

    def is_background(self, module: str, policy: str | None) -> bool:
        """Фоновая ли политика модуля (возврат в неё значит, что задача кончилась)."""
        return policy in self._table(module) and self._table(module)[policy].get("focus") == "background"

    async def _rpc(self, conn: Any, action: str, body: dict, timeout: float) -> Any:
        new, old = RPC_NAMES[action]
        try:
            return await conn.request(new, body, timeout=timeout)
        except ModuleError as e:
            if e.code != "unknown":
                raise
            return await conn.request(old, body, timeout=timeout)

    async def activate(self, module: str, goal: str = "") -> dict:
        """Войти в среду модуля с новой целью. {"ok": True, "policy": ...} или {"ok": False, "reason": ...}."""
        conn = self.connection_of(module)
        if conn is None:
            return {"ok": False, "reason": f"модуль «{module}» не подключён"}
        policy = self.default_active(module)
        if policy is None:
            return {"ok": False, "reason": f"у модуля «{module}» нет активной политики"}
        task_id = f"t{next(_ids)}"
        try:
            value = await self._rpc(conn, "activate", {"policy": policy, "goal": goal[:200], "task_id": task_id,
                                                       "workspace": module, "reason": "вход в среду"}, ACTIVATE_TIMEOUT_S)
        except ModuleError as e:
            return {"ok": False, "reason": f"{e.code}: {e.message}"}
        if not _confirmed(value, policy):
            return {"ok": False, "reason": f"модуль не подтвердил политику «{policy}»"}
        self.holder = {"module": module, "task_id": task_id, "policy": policy, "goal": goal[:200], "since": self.clock()}
        return {"ok": True, "policy": policy}

    async def release(self, reason: str = "user", force: bool = False) -> dict:
        """Выйти из среды текущего модуля: он отдаёт checkpoint. Отказ модуля оставляет среду за ним (кроме force)."""
        slot = self.holder
        if slot is None:
            return {"ok": True, "already": True}
        conn = self.connection_of(slot["module"])
        payload = None
        if conn is not None:
            try:
                value = await self._rpc(conn, "release", {"reason": reason, "task_id": slot["task_id"], "force": force,
                                                          "checkpoint": True}, RELEASE_TIMEOUT_S)
            except ModuleError as e:
                if not force:
                    return {"ok": False, "reason": f"{e.code}: {e.message}"}
                value = {"ok": True}
            if not isinstance(value, dict) or value.get("ok") is False:
                if not force:
                    err = value.get("error") if isinstance(value, dict) else None
                    return {"ok": False, "reason": str(err or "модуль не освободил среду")}
            else:
                payload = value.get("checkpoint") if isinstance(value.get("checkpoint"), dict) else None
        self.checkpoints[slot["module"]] = {**slot, "payload": payload, "reason": reason, "created": self.clock()}
        self.holder = None
        return {"ok": True}

    async def resume(self, module: str) -> dict:
        """Вернуть модулю его приостановленную задачу. {"ok": False}, если возобновлять нечего или модуль её не вернул."""
        cp = self.checkpoints.get(module)
        conn = self.connection_of(module)
        if cp is None or conn is None:
            return {"ok": False, "reason": "нет приостановленной задачи"}
        body = {"task_id": cp["task_id"], "policy": cp["policy"], "goal": cp["goal"], "checkpoint": cp["payload"], "subgoals": []}
        try:
            value = await self._rpc(conn, "resume", body, RESUME_TIMEOUT_S)
        except ModuleError as e:
            return {"ok": False, "reason": f"{e.code}: {e.message}"}
        if not _confirmed(value, cp["policy"]):
            return {"ok": False, "reason": "модуль не вернул задачу"}
        self.holder = {"module": module, "task_id": cp["task_id"], "policy": cp["policy"], "goal": cp["goal"],
                       "since": self.clock()}
        del self.checkpoints[module]
        return {"ok": True, "resumed": True, "policy": cp["policy"], "goal": cp["goal"]}

    def forget(self, module: str) -> None:
        """Модуль ушёл: слот освобождён (checkpoint остаётся)."""
        if self.holder is not None and self.holder["module"] == module:
            self.checkpoints[module] = {**self.holder, "payload": None, "reason": "disconnect", "created": self.clock()}
            self.holder = None
