"""Аренда общих пространств (`ownership: shared`, внешний контейнер с интерфейсом управления): адаптер ядра над `LeaseClient`.

Пространство это целый контейнер, который агент берёт и возвращает как семафор. Выход агента не отменяет задачи: пока у
сервиса есть активные, аренда остаётся за хозяином (`mine`, `releasing: true`, `jobs`), он может вернуться (`acquire`), а
по окончании задач сервис освобождает её сам. Состояние аренды хранит сам сервис пространства;
ядро его не дублирует, а читает (`GET /info`, он же отдаёт описание: для чего пространство и когда в него переходить, и это
известно ДО аренды, пока сервис не подключён к шине). У аренды нет срока и нет продления: она держится до `release` (выход
агента из пространства) или до обрыва WebSocket участника (тогда сервис отпускает её сам). Таймеров здесь нет: кэш сведений
обновляется при старте ядра и по запросу (`refresh`: вкладка дашборда, инструмент `workspace.status`).

`acquire` даёт `{"ok": True, "lease_id"}`, `{"ok": False, "busy": True, "holder": {...}}` (занято) или
`{"ok": False, "reason": ...}` (сервис недоступен, нет токена управления). Токен ядра уходит сервису при `acquire`, в журнал и в
ответы дашборда не попадает.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from gits_sdk.control import LeaseBusy, LeaseClient, LeaseError

logger = logging.getLogger("lease")

REFRESH_TIMEOUT_S = 3.0                               # сведения для карточки не должны подвешивать вкладку
HOLDER_PUBLIC = ("character", "since", "purpose", "releasing", "jobs")      # что дашборд и агент узнают о хозяине (без адреса ядра и токенов)


@dataclass
class _Space:
    workspace: str
    url: str
    client: LeaseClient | None                 # None: токен управления не задан, аренда отключена
    lease_id: str | None = None
    purpose: str = ""
    info: dict | None = None                   # последний ответ GET /info
    error: str = ""                            # почему сервис недоступен (последний сбой)
    checked: float = 0.0
    problems: list = field(default_factory=list)


def public_holder(holder: dict | None) -> dict | None:
    return {k: holder[k] for k in HOLDER_PUBLIC if k in holder} if isinstance(holder, dict) else None


class LeaseAdapter:
    def __init__(self, *, character: Callable[[], str], core_url: Callable[[], str], core_token: Callable[[], str],
                 client_factory: Callable[[str, str], Any] = LeaseClient, clock: Callable[[], float] = time.time,
                 on_event: Callable[[str, dict], None] | None = None):
        """character() - id персонажа-хозяина; core_url() - адрес шины ядра для сервиса (`ws://.../ws`); core_token() - токен шины;
        client_factory(url, token) - клиент аренды (в тестах подменяется); on_event(kind, данные) - журнал (необязательно)."""
        self._character, self._core_url, self._core_token = character, core_url, core_token
        self._factory, self._clock, self._on_event = client_factory, clock, on_event
        self.spaces: dict[str, _Space] = {}

    # ── настройка ───────────────────────────────────────────────────────────

    def register(self, workspace: str, control_url: str, control_token: str = "") -> None:
        """Пространство с интерфейсом управления. Нет токена: запись есть (карточка), аренда отключена."""
        client = self._factory(control_url, control_token) if control_token else None
        self.spaces[workspace] = _Space(workspace, control_url, client)

    def manages(self, workspace: str) -> bool:
        return workspace in self.spaces

    def holds(self, workspace: str) -> bool:
        sp = self.spaces.get(workspace)
        return sp is not None and sp.lease_id is not None

    def _event(self, kind: str, **data) -> None:
        if self._on_event is not None:
            try:
                self._on_event(kind, data)
            except Exception as e:
                logger.warning("журнал аренды: %s", e)

    # ── аренда ──────────────────────────────────────────────────────────────

    async def acquire(self, workspace: str, purpose: str = "") -> dict:
        sp = self.spaces.get(workspace)
        if sp is None:
            return {"ok": False, "reason": f"для «{workspace}» аренды нет"}
        if sp.client is None:
            return {"ok": False, "reason": "токен управления (GITS_CONTROL_TOKEN) не задан: аренда отключена"}
        try:
            got = await sp.client.acquire(character=self._character(), core_url=self._core_url(), token=self._core_token(),
                                          purpose=purpose[:200])
        except LeaseBusy as e:
            sp.info = {**(sp.info or {}), "state": "busy", "holder": e.holder}
            self._event("busy", workspace=workspace, holder=public_holder(e.holder))
            return {"ok": False, "busy": True, "holder": public_holder(e.holder)}
        except LeaseError as e:
            sp.error = str(e)
            self._event("unavailable", workspace=workspace, reason=str(e))
            return {"ok": False, "reason": f"пространство недоступно: {e}"}
        sp.lease_id, sp.purpose, sp.error = got["lease_id"], purpose, ""
        sp.info = {**(sp.info or {}), "state": "busy", "holder": {"character": self._character(), "since": self._clock(),
                                                                  "purpose": purpose}}
        self._event("acquired", workspace=workspace)
        return {"ok": True, "lease_id": sp.lease_id}

    async def release(self, workspace: str) -> dict:
        """Вернуть пространство. Сбой связи не мешает: сервис отпустит его сам при обрыве WebSocket участника."""
        sp = self.spaces.get(workspace)
        if sp is None or sp.lease_id is None or sp.client is None:
            return {"ok": True, "released": False}
        lease_id = sp.lease_id
        try:
            res = await sp.client.release(lease_id)
        except LeaseError as e:
            logger.warning("аренда «%s»: release не дошёл: %s", workspace, e)
            res = {"released": False}
        if res.get("pending"):
            # у пространства остались задачи: аренда моя, пока они не кончатся (сервис закроет её сам); вернуться можно acquire
            holder = {**((sp.info or {}).get("holder") or {}), "releasing": True}
            sp.info = {**(sp.info or {}), "state": "busy", "holder": holder}
            self._event("releasing", workspace=workspace)
            return {"ok": True, "released": False, "pending": res["pending"]}
        sp.lease_id, sp.purpose = None, ""
        sp.info = {**(sp.info or {}), "state": "free", "holder": None}
        self._event("released", workspace=workspace)
        return {"ok": True, "released": bool(res.get("released"))}

    async def release_all(self) -> None:
        await asyncio.gather(*[self.release(ws) for ws in list(self.spaces)], return_exceptions=True)

    # ── сведения о пространствах (кэш без таймеров) ─────────────────────────

    async def _fetch(self, sp: _Space) -> None:
        if sp.client is None:
            sp.error = "токен управления (GITS_CONTROL_TOKEN) не задан: аренда отключена"
            return
        try:
            try:
                info = await asyncio.wait_for(sp.client.info(), REFRESH_TIMEOUT_S)
            except LeaseError as e:
                if e.status != 404:
                    raise
                info = {**(sp.info or {}), **await asyncio.wait_for(sp.client.status(), REFRESH_TIMEOUT_S)}   # без /info: хотя бы состояние
        except asyncio.TimeoutError:
            sp.error = "сервис пространства не ответил"
            sp.info = {**(sp.info or {}), "state": "offline"}
            return
        except LeaseError as e:
            sp.error = str(e)
            sp.info = {**(sp.info or {}), "state": "offline"}
            return
        sp.info, sp.error, sp.checked = info, "", self._clock()
        if info.get("state") != "busy":
            sp.lease_id = None                                              # сервис отпустил (обрыв связи): метка устарела
        elif sp.lease_id and (info.get("holder") or {}).get("character") != self._character():
            sp.lease_id = None

    async def refresh(self, workspace: str | None = None) -> None:
        """Обновить сведения у сервисов (при старте ядра и по запросу). Недоступный сервис не ошибка: состояние offline."""
        targets = [self.spaces[workspace]] if workspace in self.spaces else list(self.spaces.values())
        await asyncio.gather(*[self._fetch(sp) for sp in targets], return_exceptions=True)

    def info(self, workspace: str) -> dict | None:
        """Описание пространства из кэша (`GET /info`) или None, если сервис ещё ни разу не ответил."""
        sp = self.spaces.get(workspace)
        return dict(sp.info) if sp is not None and sp.info and sp.info.get("title") else None

    def view(self, workspace: str) -> dict | None:
        """Состояние аренды для дашборда и агента: `{"state": free|busy|offline|mine, "holder", "since", "purpose", "reason"}`."""
        sp = self.spaces.get(workspace)
        if sp is None:
            return None
        info = sp.info or {}
        holder = public_holder(info.get("holder"))
        state = str(info.get("state") or "offline")
        if sp.client is None:
            state = "offline"
        if state == "busy" and (sp.lease_id or (holder or {}).get("character") == self._character()):
            state = "mine"
        if state == "offline" and not sp.error and sp.info is None:
            sp.error = "сервис пространства ещё не отвечал"
        return {"state": state, "holder": holder, "since": (holder or {}).get("since"),
                "purpose": (holder or {}).get("purpose", ""), "reason": sp.error if state == "offline" else "",
                "releasing": bool((holder or {}).get("releasing")), "jobs": int((holder or {}).get("jobs") or 0)}

    def catalog(self) -> list[dict]:
        """Каталог пространств: что это, когда переходить, свободно ли (из `/info`; пусто у ещё не отвечавших)."""
        rows = []
        for ws, sp in self.spaces.items():
            info = sp.info or {}
            rows.append({"id": ws, "title": info.get("title") or ws, "purpose": info.get("purpose") or "",
                         "when_to_use": info.get("when_to_use") or "", "keywords": list(info.get("keywords") or []),
                         "tools": list(info.get("tools") or []), "lease": self.view(ws)})
        return rows

    def manifest_of(self, workspace: str) -> dict | None:
        """Манифест-подобное описание для менеджера сред, пока сервис не подключён к шине (поле `workspace` из `/info`)."""
        info = self.info(workspace)
        if info is None:
            return None
        ws = {"id": workspace, "title": info["title"]}
        for key in ("purpose", "when_to_use", "done_when"):
            if info.get(key):
                ws[key] = info[key]
        for key in ("entry", "safe_points", "keywords"):
            if info.get(key):
                ws[key] = list(info[key])
        if isinstance(info.get("interruptible"), bool):
            ws["interruptible"] = info["interruptible"]
        return {"id": workspace, "workspace": ws, "tools": []}

    def busy_reason(self, workspace: str, holder: dict | None) -> str:
        """Факт для агента: кто держит пространство, с какого времени и зачем."""
        info = self.info(workspace) or {}
        title = info.get("title") or workspace
        holder = holder or {}
        since = holder.get("since")
        when = time.strftime("%H:%M %d.%m", time.localtime(since)) if isinstance(since, (int, float)) else "неизвестно когда"
        goal = holder.get("purpose") or "не указана"
        if holder.get("releasing"):
            goal += f" (вышла, доделывает задачи: {holder.get('jobs', 0)})"
        return (f"пространство «{title}» занято: персонаж {holder.get('character') or 'неизвестен'} с {when}, цель: {goal}; "
                "попробуй позже или займись другим")
