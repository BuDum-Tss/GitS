"""Реестр участников шины (ТЗ 02, раздел 6; workspace и service): кто подключён, что объявил, в каком состоянии и какие у него задачи.

Запись о модуле живёт и при отключении (модуль «офлайн»): дашборд показывает её, а супервизор по ней решает, что запускать.
Из манифеста подключившегося модуля реестр собирает окружение для планировщика (`environments.registry`): инструменты
модуля вызываются запросом `tool.call` через шину (`services/peers/hub.py`). Видимость инструментов по профилю решает
`Registry.focus` (CORE-10), состояние окружения - включён ли модуль и подключён ли он (CORE-9).
"""
import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from host import settings
from environments.base import Environment, Tool, ToolError
from gits_sdk import protocol as P
from services.peers.hub import PeerError

logger = logging.getLogger("modules")

EVENTS_KEPT = 100               # недавних событий модулей для `core.module_events`
FINISHED_JOBS_KEPT = 20          # завершённых задач модуля в реестре (ТЗ 01: job.list)
CHECK_TIMEOUT_S = 2.0            # tool.check: ответ не позже чем за 2 с (CORE-35)
ENABLED_CACHE_S = 0.5            # как долго помнится чтение настроек (is_enabled зовут часто)
ANNOUNCE_DESC_MAX, ANNOUNCE_ARG_MAX, ANNOUNCE_ARGS_MAX = 140, 40, 160     # AN-3: сколько описания и аргументов уходит в факт
PANEL_SET_MIN_GAP_S = 0.8        # TZ 15: panel.set не чаще раза в секунду; 0.2 с на задержку сети (SDK шлёт не чаще 1 с)
JOB_FINISHED = ("done", "failed", "cancelled")
JOB_ID_IN_TEXT = re.compile(r"задача (j-[0-9a-f]{8}) запущена")              # render() для задач модуля (ТЗ 14, DESK-11)


def announce_text(spec, args: dict) -> str:
    """Факт «запускаю <name>: <описание и короткие аргументы>» для инструмента с `announce: true` (AN-3). Формулирует модель."""
    desc = " ".join(str(spec.description).split())
    desc = desc.split(". ", 1)[0].rstrip(".")[:ANNOUNCE_DESC_MAX]
    skip = set(spec.core_fills)
    shown = "; ".join(f"{k}={' '.join(str(v).split())[:ANNOUNCE_ARG_MAX]}" for k, v in (args or {}).items()
                      if k not in skip and v not in (None, ""))[:ANNOUNCE_ARGS_MAX]
    return f"Запускаю {spec.name}: {desc}." + (f" Аргументы: {shown}." if shown else "")


@dataclass
class PlannedRecord:
    """Участник, которого ещё нет (peers.yaml, planned): только карточка во вкладке, процесса и записи в реестре нет."""
    id: str
    title: str
    tz: str = ""


@dataclass
class PeerRecord:
    id: str
    title: str = ""
    description: str = ""
    version: str = ""
    manifest: dict | None = None
    connected: bool = False
    instance: str | None = None
    pid: int | None = None
    connected_at: float | None = None
    last_disconnect_reason: str | None = None
    state: dict | None = None                       # {"summary": str, "data": dict, "ts": float}
    jobs: dict = field(default_factory=dict)        # job_id -> JobStatus (последний job.update + module + updated)
    default_enabled: bool = True                    # из peers.yaml, пока в настройках нет явного выбора
    declared_role: str | None = None                # роль из peers.yaml (раздел workspaces / services); манифест главнее
    declared_ownership: str | None = None           # ownership из peers.yaml (только workspace); манифест главнее
    external: bool = False                          # внешний сервис без WebSocket, жив по health_url (ТЗ 03, ATT-2)
    title_override: str = ""                        # title из peers.yaml: дашборд показывает его вместо названия из манифеста
    description_override: str = ""                  # description из peers.yaml (пояснение для вкладок «Окружения» и «Сервисы»)
    dev: bool = False                               # для разработчиков: во вкладках только при настройке dev_mode
    panel: dict | None = None                       # пространство модуля из манифеста (TZ 15): PanelSpec.to_dict()
    control_url: str = ""                           # общее пространство с интерфейсом управления: аренда вместо подключения при старте
    panel_blocks: dict | None = None                # последнее panel.set: {"blocks": [...], "ts": float} (TZ 15)

    @property
    def role(self) -> str:
        """workspace | service: `role` манифеста подключившегося участника, иначе роль из peers.yaml, иначе workspace."""
        declared = (self.manifest or {}).get("role")
        return declared if declared in P.ROLES else (self.declared_role or P.ROLE_WORKSPACE)

    @property
    def ownership(self) -> str | None:
        """shared | owned у workspace (манифест главнее peers.yaml, без обоих owned), у service None."""
        if self.role != P.ROLE_WORKSPACE:
            return None
        declared = (self.manifest or {}).get("ownership")
        return declared if declared in P.OWNERSHIPS else (self.declared_ownership or P.OWNERSHIP_OWNED)


class PeerRegistry:
    def __init__(self, envs, *, on_state: Callable[[], None] | None = None,
                 settings_source: Callable[[], dict] = settings.load):
        """envs - реестр окружений планировщика (environments.registry.Registry); on_state() - состояние какого-то
        модуля изменилось (ProfileManager.poke)."""
        self.envs = envs
        self.on_state = on_state
        self.hub = None                    # PeerHub, ставит ядро
        self.supervisor = None             # Supervisor, ставит ядро
        self.leases = None                 # LeaseAdapter (аренда общих пространств), ставит ядро
        self.records: dict[str, PeerRecord] = {}
        self.planned: dict[str, PlannedRecord] = {}
        self._settings_source = settings_source
        self._module_envs: set[str] = set()
        self._enabled_cache: tuple[float, dict] = (0.0, {})
        self.context_for: Callable[[str], str] = lambda query: ""   # core_fills: краткий контекст разговора (ставит ядро, DESK-12)
        self._job_waiters: dict[tuple[str, str], asyncio.Future] = {}
        self.events: deque = deque(maxlen=EVENTS_KEPT)           # недавние события модулей: в контекст не кладутся, читаются по запросу

    # ── записи ──────────────────────────────────────────────────────────────

    def declare(self, peer_id: str, *, title: str = "", default_enabled: bool = True, role: str = "workspace",
                ownership: str | None = None, external: bool = False, description: str = "",
                dev: bool = False, control_url: str = "") -> PeerRecord:
        """Окружение или сервис, который ядро знает заранее (peers.yaml): запись есть и пока он не подключился."""
        rec = self.records.setdefault(peer_id, PeerRecord(peer_id))
        rec.title = rec.title or title or peer_id
        rec.default_enabled = default_enabled
        rec.declared_role, rec.declared_ownership, rec.external = role, ownership, external
        rec.title_override, rec.description_override, rec.dev = title, description, dev
        rec.control_url = control_url
        return rec

    def declare_planned(self, peer_id: str, *, title: str, tz: str = "") -> PlannedRecord:
        """Запланированный участник (peers.yaml, planned): виден в дашборде серой карточкой, ядро его не запускает."""
        rec = self.planned[peer_id] = PlannedRecord(peer_id, title, tz)
        return rec

    def get(self, peer_id: str) -> PeerRecord | None:
        return self.records.get(peer_id)

    def _settings(self, cfg: dict | None = None) -> dict:
        if cfg is not None:
            return cfg
        ts, cached = self._enabled_cache
        now = time.monotonic()
        if now - ts > ENABLED_CACHE_S:
            cached = self._settings_source()
            self._enabled_cache = (now, cached)
        return cached

    def invalidate(self) -> None:
        self._enabled_cache = (0.0, {})

    def is_enabled(self, peer_id: str, cfg: dict | None = None) -> bool:
        """Включён ли модуль: явный выбор в настройках (peers.<id>.enabled), иначе значение по умолчанию (включён)."""
        entry = settings.peer_entry(self._settings(cfg), peer_id)
        if entry is not None and isinstance(entry.get("enabled"), bool):
            return entry["enabled"]
        rec = self.records.get(peer_id)
        return rec.default_enabled if rec is not None else True

    def module_settings(self, peer_id: str, cfg: dict | None = None) -> dict:
        """Собственные настройки модуля: значения по умолчанию из манифеста, поверх них сохранённые."""
        rec = self.records.get(peer_id)
        schema = ((rec.manifest or {}).get("settings_schema") or {}) if rec else {}
        stored = settings.peer_entry(self._settings(cfg), peer_id) or {}
        return {name: stored.get(name, spec["default"]) for name, spec in schema.items()}

    # ── подключение ─────────────────────────────────────────────────────────

    def on_connected(self, conn) -> PeerRecord:
        peer_id = conn.id
        rec = self.records.setdefault(peer_id, PeerRecord(peer_id))
        changed = rec.manifest != conn.manifest
        m = conn.manifest
        rec.title, rec.description, rec.version = m["title"], m["description"], m["version"]
        rec.manifest = m
        rec.panel = conn.spec.panel.to_dict() if conn.spec.panel is not None else None
        rec.connected, rec.instance, rec.pid, rec.connected_at = True, conn.instance, conn.pid, conn.connected_at
        rec.last_disconnect_reason = None
        if changed or peer_id not in self._module_envs:
            self._build_environment(rec, conn.spec)
        return rec

    def on_disconnected(self, peer_id: str, reason: str) -> None:
        rec = self.records.get(peer_id)
        if rec is None:
            return
        rec.connected = False
        rec.last_disconnect_reason = reason
        for key in [k for k in self._job_waiters if k[0] == peer_id]:       # ждавшие задачу модуля получают сбой
            fut = self._job_waiters.pop(key)
            if not fut.done():
                fut.set_result({"job_id": key[1], "state": "failed", "text": "",
                                "error": {"code": "unavailable", "message": "модуль отключился"}})

    def set_state(self, peer_id: str, summary: str, data: dict, node: str | None = None) -> None:
        rec = self.records.get(peer_id)
        if rec is None:
            return
        rec.state = {"summary": summary, "data": data, "ts": time.time()}
        if node is not None:
            rec.state["node"] = node
        if self.on_state is not None:
            try:
                self.on_state()
            except Exception as e:
                logger.warning(f"Реакция на state модуля: {e}")

    def set_panel(self, peer_id: str, body: dict) -> bool:
        """Блоки пространства модуля (`panel.set`, TZ 15): последнее значение целиком. Чаще раза в секунду - лишнее
        отбрасывается (с допуском на задержку сети). False, если модуль не объявил panel или сообщение отброшено."""
        rec = self.records.get(peer_id)
        if rec is None or rec.panel is None:
            return False
        now = time.time()
        if rec.panel_blocks is not None and now - rec.panel_blocks["ts"] < PANEL_SET_MIN_GAP_S:
            return False
        rec.panel_blocks = {"blocks": body["blocks"], "ts": now}
        return True

    def notes(self, with_state: bool = False) -> list[str]:
        """Строки о модулях: что умеет каждый подключённый включённый модуль с инструментами. Состояние (сводка state)
        в контекст не кладётся, его она узнаёт по запросу инструментом `core.modules_status` (with_state=True).
        Это факты для неё, не готовые реплики."""
        lines = []
        for rec in sorted(self.records.values(), key=lambda r: r.id):
            if rec.control_url and not rec.connected and self.leases is not None and self.is_enabled(rec.id):
                line = self._space_note(rec)                        # пространство не подключено, но его можно взять в аренду
                if line:
                    lines.append(line)
                continue
            if rec.external or not rec.connected or not rec.manifest or not (rec.manifest.get("tools")):
                continue
            if not self.is_enabled(rec.id):
                continue
            summary = ((rec.state or {}).get("summary") or "").strip() if with_state else ""
            lines.append(f"Модуль «{rec.title or rec.id}»: {rec.description}" + (f" Сейчас: {summary}." if summary else ""))
        return lines

    def _space_note(self, rec: PeerRecord) -> str:
        """Строка о пространстве, которое берётся в аренду: для чего, когда переходить, свободно ли (факты из `/info`)."""
        info = self.leases.info(rec.id)
        if info is None:
            return ""
        view = self.leases.view(rec.id) or {}
        state = {"free": "свободно", "mine": "занято мной", "offline": "недоступно"}.get(view.get("state"), "")
        if view.get("state") == "busy":
            state = f"занято персонажем {(view.get('holder') or {}).get('character') or '?'}"
        when = f" Когда переходить: {info['when_to_use']}." if info.get("when_to_use") else ""
        return f"Пространство «{info.get('title') or rec.id}»: {info.get('purpose') or ''}.{when} Сейчас: {state}."

    def status_report(self) -> str:
        """Состояние модулей по запросу (инструмент `core.modules_status`): подключён ли, включён ли, сводка state."""
        lines = []
        for rec in sorted(self.records.values(), key=lambda r: r.id):
            if rec.external:
                continue
            if not self.is_enabled(rec.id):
                lines.append(f"- {rec.title or rec.id}: выключен.")
            elif not rec.connected:
                lines.append(f"- {rec.title or rec.id}: не подключён.")
            else:
                summary = ((rec.state or {}).get("summary") or "").strip()
                lines.append(f"- {rec.title or rec.id}: подключён" + (f", {summary}." if summary else "."))
        return ("Состояние модулей:\n" + "\n".join(lines)) if lines else "модулей нет"

    is_noise = staticmethod(lambda module, kind: False)       # (модуль, вид) -> событие чужого занятия, пока она в среде модуля (ставит ядро)

    def note_event(self, peer_id: str, event: dict) -> None:
        self.events.append({"ts": time.time(), "module": peer_id, "kind": str(event.get("kind") or ""),
                            "text": " ".join(str(event.get("text") or "").split())[:200]})

    def events_report(self, module: str | None = None, limit: int = 15) -> str:
        """Недавние события модулей (инструмент `core.module_events`): свежие снизу."""
        # что видит на своём экране пользователь (описания вкладок и окон) не относится к тому, чем она занята сейчас: пока она играет,
        # такие события в отчёт не попадают (раньше она читала там нашу переписку и «вспоминала» чужие слова как свои)
        rows = [e for e in self.events if (not module or e["module"] == module) and not self.is_noise(e["module"], e["kind"])]
        rows = rows[-max(1, min(int(limit or 15), EVENTS_KEPT)):]
        if not rows:
            return "событий модулей нет" + (f" ({module})" if module else "")
        now = time.time()
        return "Недавние события модулей:\n" + "\n".join(
            f"- {max(0, round((now - e['ts']) / 60))} мин назад, {e['module']} {e['kind']}: {e['text']}" for e in rows)

    def node(self, peer_id: str) -> str | None:
        """Узел рабочего места, о котором модуль сообщил в `state.node` (ТЗ 33), или None."""
        rec = self.records.get(peer_id)
        return (rec.state or {}).get("node") if rec is not None and rec.connected and rec.state else None

    def manifest_of(self, peer_id: str) -> dict | None:
        rec = self.records.get(peer_id)
        return rec.manifest if rec is not None else None

    def state_data(self, peer_id: str) -> dict | None:
        """state.data подключённого модуля (для профилей) или None."""
        rec = self.records.get(peer_id)
        return rec.state["data"] if rec is not None and rec.connected and rec.state else None

    # ── задачи ──────────────────────────────────────────────────────────────

    def update_job(self, peer_id: str, body: dict) -> dict | None:
        rec = self.records.get(peer_id)
        if rec is None:
            return None
        job = {**rec.jobs.get(body["job_id"], {}), **{k: v for k, v in body.items() if v is not None}}
        job["module"], job["updated"] = peer_id, time.time()
        if body.get("state") != "waiting_user":
            job.pop("question", None)
        rec.jobs[body["job_id"]] = job
        finished = sorted((j for j in rec.jobs.values() if j["state"] in JOB_FINISHED), key=lambda j: j["updated"])
        for old in finished[:-FINISHED_JOBS_KEPT]:
            rec.jobs.pop(old["job_id"], None)
        if job["state"] in JOB_FINISHED:
            fut = self._job_waiters.pop((peer_id, body["job_id"]), None)
            if fut is not None and not fut.done():
                fut.set_result(dict(job))
        return job

    async def wait_job(self, peer_id: str, job_id: str, timeout: float = 600.0) -> dict:
        """Дождаться завершения задачи модуля (ТЗ 14, DESK-11): вернуть её последний job (state done/failed/cancelled).
        Тайм-аут и обрыв модуля дают state failed с error."""
        rec = self.records.get(peer_id)
        if rec is not None and rec.jobs.get(job_id, {}).get("state") in JOB_FINISHED:
            return dict(rec.jobs[job_id])
        key = (peer_id, job_id)
        fut = asyncio.get_running_loop().create_future()
        self._job_waiters[key] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"job_id": job_id, "state": "failed", "text": "",
                    "error": {"code": "timeout", "message": f"задача не завершилась за {int(timeout)} с"}}
        finally:
            self._job_waiters.pop(key, None)

    @staticmethod
    def job_id_from(text: str) -> str | None:
        """Номер задачи из ответа инструмента с job: true («задача j-… запущена»), иначе None."""
        m = JOB_ID_IN_TEXT.search(text or "")
        return m.group(1) if m else None

    def jobs_all(self) -> list[dict]:
        """Задачи всех модулей: сначала незавершённые, затем завершённые; внутри - свежие выше."""
        rows = [dict(j) for rec in self.records.values() for j in rec.jobs.values()]
        rows.sort(key=lambda j: (j["state"] in ("done", "failed", "cancelled"), -j["updated"]))
        return rows

    async def cancel_job(self, peer_id: str, job_id: str) -> str:
        conn = self.hub.connection(peer_id) if self.hub is not None else None
        if conn is None:
            raise ToolError("модуль не подключён")
        try:
            await conn.request("job.cancel", {"job_id": job_id}, timeout=15.0)
        except PeerError as e:
            raise ToolError(e.message, e.code)
        return f"задача {job_id} отменена"

    # ── окружение для планировщика ────────────────────────────────────────────

    def _env_state(self, peer_id: str) -> tuple[bool, str]:
        rec = self.records.get(peer_id)
        if rec is None or not rec.connected:
            return False, "модуль не подключён"
        if not self.is_enabled(peer_id):
            return False, "модуль выключен"
        return True, "доступно"

    @staticmethod
    def render(value, job: bool) -> str:
        """Результат инструмента модуля одной строкой для планировщика (CORE-33)."""
        if job:
            job_id = value.get("job_id") if isinstance(value, dict) else None
            return f"задача {job_id} запущена" if job_id else "задача запущена"
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)

    async def _fill_core(self, spec, args: dict) -> dict:
        """core_fills (ТЗ 01 §6.1, DESK-12): параметры, которые заполняет ядро, а не планировщик. Сейчас только context."""
        if "context" not in spec.core_fills:
            return args
        query = next((str(v) for v in args.values() if isinstance(v, str)), "")
        try:
            context = await asyncio.to_thread(self.context_for, query)
        except Exception as e:
            logger.warning(f"Контекст для инструмента {spec.name}: {e}")
            context = ""
        return {**args, "context": str(context)[:4000]}

    def _make_tool(self, peer_id: str, spec) -> Tool:
        async def call(args: dict) -> str:
            conn = self.hub.connection(peer_id) if self.hub is not None else None
            if conn is None:
                raise ToolError("модуль не подключён")
            if not self.is_enabled(peer_id):
                raise ToolError("модуль выключен")
            attention = getattr(self.hub, "attention", None)
            if spec.announce and attention is not None:
                try:
                    await attention.submit_announce(peer_id, announce_text(spec, args), data={"tool": spec.name})
                except Exception as e:                  # объявление не должно мешать самому вызову
                    logger.warning(f"Объявление запуска {spec.full_id(peer_id)}: {e}")
            args = await self._fill_core(spec, args)
            try:
                value = await conn.request("tool.call", {"tool": spec.name, "args": args}, timeout=spec.timeout_s)
            except PeerError as e:
                raise ToolError(e.message, e.code)
            return self.render(value, spec.job)

        async def check(args: dict) -> str | None:
            conn = self.hub.connection(peer_id) if self.hub is not None else None
            try:
                if conn is None:
                    raise PeerError("unavailable", "модуль не подключён")
                value = await conn.request("tool.check", {"tool": spec.name, "args": args}, timeout=CHECK_TIMEOUT_S)
                danger = value.get("danger") if isinstance(value, dict) else None
                return str(danger) if danger else None
            except Exception:
                return "модуль не ответил на проверку"

        params = {k: v for k, v in spec.params.items() if k not in spec.core_fills}     # планировщик core_fills не видит
        return Tool(id=spec.full_id(peer_id), env=peer_id, name=spec.name, description=spec.description,
                    params=params, risk=spec.risk, keywords=tuple(spec.keywords), call=call,
                    self_confirming=spec.self_confirming, informative=spec.informative, always=spec.fallback,
                    availability=spec.availability, node=spec.node, timeout_s=spec.timeout_s, job=spec.job,
                    danger_check=spec.danger_check, check=check if spec.danger_check else None)

    def _build_environment(self, rec: PeerRecord, spec) -> None:
        """Окружение модуля (CORE-8): id окружения = id модуля, module = id модуля."""
        existing = self.envs.envs.get(rec.id)
        if existing is not None and rec.id not in self._module_envs:
            self.envs.load_errors[f"module:{rec.id}"] = (
                f"окружение «{rec.id}» уже занято (манифест или встроенное), инструменты модуля не добавлены")
            logger.warning(f"Окружение {rec.id} занято, инструменты модуля не добавлены")
            return
        env = Environment(id=rec.id, title=rec.title, description=rec.description, module=rec.id,
                          tools=[self._make_tool(rec.id, t) for t in spec.tools],
                          status=lambda mid=rec.id: self._env_state(mid))
        self.envs.add(env)                       # замена при другом манифесте (CORE-9)
        self._module_envs.add(rec.id)

    # ── для дашборда ────────────────────────────────────────────────────────

    def status_of(self, rec: PeerRecord) -> tuple[str, str]:
        """(код, пояснение): disabled, connected, not_configured, crashed, starting, backoff, running, offline."""
        if not self.is_enabled(rec.id):
            return "disabled", "выключен"
        if rec.connected:
            return "connected", "подключён"
        sup = self.supervisor.status(rec.id) if self.supervisor is not None else None
        if sup:
            return sup["status"], sup["text"]
        return "offline", "не отвечает" if rec.external else "не подключён"

    def describe(self, rec: PeerRecord) -> dict:
        m = rec.manifest or {}
        sup = (self.supervisor.status(rec.id) if self.supervisor is not None else None) or {}
        code, text = self.status_of(rec)
        tools = [{"id": f"{rec.id}.{t['name']}", "name": t["name"], "description": t["description"], "risk": t["risk"],
                  "availability": t["availability"]} for t in m.get("tools") or []]
        service = rec.external
        row = self._describe(rec, m, sup, code, text, tools, service)
        if rec.control_url and self.leases is not None and self.leases.manages(rec.id):
            info = self.leases.info(rec.id) or {}                 # описание и состояние аренды хранит сам сервис пространства
            row["lease"] = self.leases.view(rec.id)
            row["space"] = {"purpose": info.get("purpose") or "", "when_to_use": info.get("when_to_use") or "",
                            "keywords": list(info.get("keywords") or [])}
            if not rec.manifest and info.get("title"):
                row["title"] = rec.title_override or info["title"]
        return row

    async def refresh_leases(self) -> None:
        """Обновить сведения об арендуемых пространствах (по запросу: открытие вкладки, `workspace.status`)."""
        if self.leases is not None:
            await self.leases.refresh()

    def _describe(self, rec: PeerRecord, m: dict, sup: dict, code: str, text: str, tools: list, service: bool) -> dict:
        return {"id": rec.id, "title": rec.title_override or rec.title or rec.id,
                "description": rec.description_override or rec.description, "version": rec.version,
                "role": rec.role, "ownership": rec.ownership, "external": rec.external, "enabled": self.is_enabled(rec.id),
                "connected": bool(sup.get("healthy")) if service else rec.connected,
                "configured": sup.get("configured", True), "status": code, "status_text": text,
                "pid": sup.get("pid") or rec.pid, "restarts": sup.get("restarts", 0),
                "last_error": None if rec.connected else (sup.get("last_error") or rec.last_disconnect_reason),
                "state_summary": (rec.state or {}).get("summary"), "tools": tools,
                "capabilities": m.get("capabilities") or [], "settings_schema": m.get("settings_schema") or {},
                "module_settings": self.module_settings(rec.id), "dev": rec.dev, "panel": rec.panel}

    def snapshot(self) -> list[dict]:
        return [self.describe(rec) for rec in sorted(self.records.values(), key=lambda r: r.id)]

    def dashboard_rows(self, dev_mode: bool = False) -> list[dict]:
        """Строки вкладок «Окружения» и «Сервисы»: все участники (записи для разработчиков только при dev_mode), затем
        запланированные (`kind: planned`: id, title, tz, без состояния)."""
        rows = [row for row in self.snapshot() if dev_mode or not row["dev"]]
        rows += [{"id": p.id, "title": p.title, "tz": p.tz, "kind": "planned"} for p in self.planned.values()]
        return rows

    def rows_by_role(self, role: str, dev_mode: bool = False) -> list[dict]:
        """Строки одной роли (`workspace` или `service`) тем же снимком; запланированные (`planned`) идут к окружениям."""
        rows = [row for row in self.dashboard_rows(dev_mode) if row.get("kind") != "planned" and row["role"] == role]
        if role == P.ROLE_WORKSPACE:
            rows += [row for row in self.dashboard_rows(dev_mode) if row.get("kind") == "planned"]
        return rows
