"""Связь со средой модуля: вход, выход и возобновление по протоколу модулей (вместо менеджера фокуса ТЗ 18).

Слот один: модуль, в чью среду агент сейчас вошёл. `activate` отправляет модулю `workspace.enter`, `release` просит
`workspace.exit` и забирает checkpoint, `resume` возвращает приостановленную задачу (`workspace.resume`). Войти можно в среду
любого подключённого модуля; общее пространство с интерфейсом управления (`LeaseAdapter`) сначала берётся в аренду, потом
ждём подключения сервиса к шине. Очереди событий, дребезга, закрепления и второго эшелона решений нет: среду выбирает только
`WorkspaceManager` по просьбе пользователя или по самоимпульсу. Checkpoint живёт в памяти.
"""
import asyncio
import itertools
import logging
import time
from typing import Any, Callable

from services.peers.hub import PeerError

logger = logging.getLogger("workspace_link")

RELEASE_TIMEOUT_S = 15.0
ACTIVATE_TIMEOUT_S = 20.0
RESUME_TIMEOUT_S = 20.0
CONNECT_WAIT_S = 15.0          # после аренды ждём, пока сервис пространства подключится к шине ядра

RPC_NAMES = {"activate": "workspace.enter", "release": "workspace.exit", "resume": "workspace.resume"}

_ids = itertools.count(1)


def _confirmed(value: Any) -> bool:
    """Модуль подтвердил переход: ok true."""
    return isinstance(value, dict) and value.get("ok") is True


class WorkspaceLink:
    def __init__(self, *, connection_of: Callable[[str], Any], clock: Callable[[], float] = time.time,
                 leases: Any = None, connect_wait_s: float = CONNECT_WAIT_S, poll_s: float = 0.1):
        """connection_of(id) - соединение модуля или None; leases - `LeaseAdapter` (аренда общих пространств) или None."""
        self.connection_of, self.clock = connection_of, clock
        self.leases, self.connect_wait_s, self.poll_s = leases, connect_wait_s, poll_s
        self.holder: dict | None = None             # {"module", "task_id", "goal", "since"}
        self.checkpoints: dict[str, dict] = {}      # модуль -> приостановленная задача

    async def _rpc(self, conn: Any, action: str, body: dict, timeout: float) -> Any:
        return await conn.request(RPC_NAMES[action], body, timeout=timeout)

    def _leased(self, module: str) -> bool:
        return self.leases is not None and self.leases.manages(module)

    async def _drop_lease(self, module: str) -> None:
        if self._leased(module):
            await self.leases.release(module)

    async def _wait_connection(self, module: str) -> Any:
        """Подключение сервиса к шине после аренды: до `connect_wait_s` секунд."""
        waited = 0.0
        while True:
            conn = self.connection_of(module)
            if conn is not None or waited >= self.connect_wait_s:
                return conn
            await asyncio.sleep(self.poll_s)
            waited += self.poll_s

    async def activate(self, module: str, goal: str = "") -> dict:
        """Войти в среду модуля с новой целью. {"ok": True} или {"ok": False, "reason": ...}; для занятого пространства ещё
        `busy: True` и `holder`."""
        if self._leased(module):
            got = await self.leases.acquire(module, goal)
            if got.get("busy"):
                return {"ok": False, "busy": True, "holder": got.get("holder"),
                        "reason": self.leases.busy_reason(module, got.get("holder"))}
            if not got.get("ok"):
                return {"ok": False, "reason": str(got.get("reason") or "пространство недоступно")}
            if await self._wait_connection(module) is None:
                await self.leases.release(module)
                return {"ok": False, "reason": f"пространство «{module}» не подключилось к ядру за {self.connect_wait_s:g} с"}
            res = await self._enter(module, goal)
            if not res.get("ok"):
                await self.leases.release(module)
            return res
        return await self._enter(module, goal)

    async def _enter(self, module: str, goal: str) -> dict:
        conn = self.connection_of(module)
        if conn is None:
            return {"ok": False, "reason": f"модуль «{module}» не подключён"}
        task_id = f"t{next(_ids)}"
        try:
            value = await self._rpc(conn, "activate", {"goal": goal[:200], "task_id": task_id,
                                                       "workspace": module, "reason": "вход в среду"}, ACTIVATE_TIMEOUT_S)
        except PeerError as e:
            return {"ok": False, "reason": f"{e.code}: {e.message}"}
        if not _confirmed(value):
            return {"ok": False, "reason": "модуль не принял среду"}
        self.holder = {"module": module, "task_id": task_id, "goal": goal[:200], "since": self.clock()}
        return {"ok": True}

    async def release(self, reason: str = "user", force: bool = False) -> dict:
        """Выйти из среды текущего модуля: он отдаёт checkpoint. Отказ модуля оставляет среду за ним (кроме force)."""
        slot = self.holder
        if slot is None:
            return {"ok": True, "already": True}
        res = await self._release_slot(slot, reason, force)
        if res.get("ok") and self._leased(slot["module"]):
            await self.leases.release(slot["module"])           # аренда кончается выходом, даже при force после отказа модуля
        return res

    async def _release_slot(self, slot: dict, reason: str, force: bool) -> dict:
        conn = self.connection_of(slot["module"])
        payload = None
        if conn is not None:
            try:
                value = await self._rpc(conn, "release", {"reason": reason, "task_id": slot["task_id"], "force": force,
                                                          "checkpoint": True}, RELEASE_TIMEOUT_S)
            except PeerError as e:
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
        if cp is not None and conn is None and self._leased(module):      # пространство вернули: берём снова и ждём подключения
            got = await self.leases.acquire(module, cp["goal"])
            if got.get("busy"):
                return {"ok": False, "busy": True, "holder": got.get("holder"),
                        "reason": self.leases.busy_reason(module, got.get("holder"))}
            if not got.get("ok"):
                return {"ok": False, "reason": str(got.get("reason") or "пространство недоступно")}
            conn = await self._wait_connection(module)
            if conn is None:
                await self.leases.release(module)
        if cp is None or conn is None:
            return {"ok": False, "reason": "нет приостановленной задачи"}
        body = {"task_id": cp["task_id"], "goal": cp["goal"], "checkpoint": cp["payload"], "subgoals": []}
        try:
            value = await self._rpc(conn, "resume", body, RESUME_TIMEOUT_S)
        except PeerError as e:
            await self._drop_lease(module)
            return {"ok": False, "reason": f"{e.code}: {e.message}"}
        if not _confirmed(value):
            await self._drop_lease(module)
            return {"ok": False, "reason": "модуль не вернул задачу"}
        self.holder = {"module": module, "task_id": cp["task_id"], "goal": cp["goal"],
                       "since": self.clock()}
        del self.checkpoints[module]
        return {"ok": True, "resumed": True, "goal": cp["goal"]}

    def forget(self, module: str) -> None:
        """Модуль ушёл: слот освобождён (checkpoint остаётся), аренда отпущена."""
        if self.holder is not None and self.holder["module"] == module:
            self.checkpoints[module] = {**self.holder, "payload": None, "reason": "disconnect", "created": self.clock()}
            self.holder = None
        if self._leased(module) and self.leases.holds(module):
            try:
                asyncio.get_running_loop().create_task(self.leases.release(module))
            except RuntimeError:                                 # цикла нет (тест): отпустить нечем
                pass
