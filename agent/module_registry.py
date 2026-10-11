"""Реестр модулей (ТЗ 02, раздел 6): кто подключён, что объявил, в каком состоянии и какие у него задачи.

Запись о модуле живёт и при отключении (модуль «офлайн»): дашборд показывает её, а супервизор по ней решает, что запускать.
Из манифеста подключившегося модуля реестр собирает окружение для планировщика (`environments.registry`): инструменты
модуля вызываются запросом `tool.call` через шину (`module_hub.py`). Видимость инструментов по профилю решает
`Registry.focus` (CORE-10), состояние окружения - включён ли модуль и подключён ли он (CORE-9).
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import settings
from environments.base import Environment, Tool, ToolError
from module_hub import ModuleError

logger = logging.getLogger("modules")

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
    """Модуль, которого ещё нет (modules.yaml, planned): только карточка во вкладке, процесса и записи в реестре нет."""
    id: str
    title: str
    tz: str = ""


@dataclass
class ModuleRecord:
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
    default_enabled: bool = True                    # из modules.yaml, пока в настройках нет явного выбора
    kind: str = "module"                            # service: процесс без WebSocket, жив по health_url (ТЗ 03, ATT-2)
    title_override: str = ""                        # title из modules.yaml: дашборд показывает его вместо названия из манифеста
    description_override: str = ""                  # description из modules.yaml (пояснение для вкладки «Модули»)
    dev: bool = False                               # для разработчиков: во вкладке только при настройке dev_mode
    panel: dict | None = None                       # пространство модуля из манифеста (TZ 15): PanelSpec.to_dict()
    panel_blocks: dict | None = None                # последнее panel.set: {"blocks": [...], "ts": float} (TZ 15)


class ModuleRegistry:
    def __init__(self, envs, *, on_state: Callable[[], None] | None = None,
                 settings_source: Callable[[], dict] = settings.load):
        """envs - реестр окружений планировщика (environments.registry.Registry); on_state() - состояние какого-то
        модуля изменилось (ProfileManager.poke)."""
        self.envs = envs
        self.on_state = on_state
        self.hub = None                    # ModuleHub, ставит ядро
        self.supervisor = None             # Supervisor, ставит ядро
        self.records: dict[str, ModuleRecord] = {}
        self.planned: dict[str, PlannedRecord] = {}
        self._settings_source = settings_source
        self._module_envs: set[str] = set()
        self._enabled_cache: tuple[float, dict] = (0.0, {})
        self.context_for: Callable[[str], str] = lambda query: ""   # core_fills: краткий контекст разговора (ставит ядро, DESK-12)
        self._job_waiters: dict[tuple[str, str], asyncio.Future] = {}

    # ── записи ──────────────────────────────────────────────────────────────

    def declare(self, module_id: str, *, title: str = "", default_enabled: bool = True, kind: str = "module",
                description: str = "", dev: bool = False) -> ModuleRecord:
        """Модуль или сервис, который ядро знает заранее (modules.yaml): запись есть и пока он не подключился."""
        rec = self.records.setdefault(module_id, ModuleRecord(module_id))
        rec.title = rec.title or title or module_id
        rec.default_enabled = default_enabled
        rec.kind = kind
        rec.title_override, rec.description_override, rec.dev = title, description, dev
        return rec

    def declare_planned(self, module_id: str, *, title: str, tz: str = "") -> PlannedRecord:
        """Запланированный модуль (modules.yaml, planned): виден в дашборде серой карточкой, ядро его не запускает."""
        rec = self.planned[module_id] = PlannedRecord(module_id, title, tz)
        return rec

    def get(self, module_id: str) -> ModuleRecord | None:
        return self.records.get(module_id)

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

    def is_enabled(self, module_id: str, cfg: dict | None = None) -> bool:
        """Включён ли модуль: явный выбор в настройках (modules.<id>.enabled), иначе значение по умолчанию (включён)."""
        entry = (self._settings(cfg).get("modules") or {}).get(module_id)
        if isinstance(entry, dict) and isinstance(entry.get("enabled"), bool):
            return entry["enabled"]
        rec = self.records.get(module_id)
        return rec.default_enabled if rec is not None else True

    def module_settings(self, module_id: str, cfg: dict | None = None) -> dict:
        """Собственные настройки модуля: значения по умолчанию из манифеста, поверх них сохранённые."""
        rec = self.records.get(module_id)
        schema = ((rec.manifest or {}).get("settings_schema") or {}) if rec else {}
        stored = (self._settings(cfg).get("modules") or {}).get(module_id)
        stored = stored if isinstance(stored, dict) else {}
        return {name: stored.get(name, spec["default"]) for name, spec in schema.items()}

    # ── подключение ─────────────────────────────────────────────────────────

    def on_connected(self, conn) -> ModuleRecord:
        rec = self.records.setdefault(conn.module_id, ModuleRecord(conn.module_id))
        changed = rec.manifest != conn.manifest
        m = conn.manifest
        rec.title, rec.description, rec.version = m["title"], m["description"], m["version"]
        rec.manifest = m
        rec.panel = conn.spec.panel.to_dict() if conn.spec.panel is not None else None
        rec.connected, rec.instance, rec.pid, rec.connected_at = True, conn.instance, conn.pid, conn.connected_at
        rec.last_disconnect_reason = None
        if changed or conn.module_id not in self._module_envs:
            self._build_environment(rec, conn.spec)
        return rec

    def on_disconnected(self, module_id: str, reason: str) -> None:
        rec = self.records.get(module_id)
        if rec is None:
            return
        rec.connected = False
        rec.last_disconnect_reason = reason
        for key in [k for k in self._job_waiters if k[0] == module_id]:       # ждавшие задачу модуля получают сбой
            fut = self._job_waiters.pop(key)
            if not fut.done():
                fut.set_result({"job_id": key[1], "state": "failed", "text": "",
                                "error": {"code": "unavailable", "message": "модуль отключился"}})

    def set_state(self, module_id: str, summary: str, data: dict) -> None:
        rec = self.records.get(module_id)
        if rec is None:
            return
        rec.state = {"summary": summary, "data": data, "ts": time.time()}
        if self.on_state is not None:
            try:
                self.on_state()
            except Exception as e:
                logger.warning(f"Реакция на state модуля: {e}")

    def set_panel(self, module_id: str, body: dict) -> bool:
        """Блоки пространства модуля (`panel.set`, TZ 15): последнее значение целиком. Чаще раза в секунду - лишнее
        отбрасывается (с допуском на задержку сети). False, если модуль не объявил panel или сообщение отброшено."""
        rec = self.records.get(module_id)
        if rec is None or rec.panel is None:
            return False
        now = time.time()
        if rec.panel_blocks is not None and now - rec.panel_blocks["ts"] < PANEL_SET_MIN_GAP_S:
            return False
        rec.panel_blocks = {"blocks": body["blocks"], "ts": now}
        return True

    def notes(self) -> list[str]:
        """Строки для контекста разговора: что умеет каждый подключённый включённый модуль с инструментами и в каком он
        состоянии (по сводке state). Это факты для неё, не готовые реплики."""
        lines = []
        for rec in sorted(self.records.values(), key=lambda r: r.id):
            if rec.kind != "module" or not rec.connected or not rec.manifest or not (rec.manifest.get("tools")):
                continue
            if not self.is_enabled(rec.id):
                continue
            summary = ((rec.state or {}).get("summary") or "").strip()
            lines.append(f"Модуль «{rec.title or rec.id}»: {rec.description}" + (f" Сейчас: {summary}." if summary else ""))
        return lines

    def state_data(self, module_id: str) -> dict | None:
        """state.data подключённого модуля (для профилей) или None."""
        rec = self.records.get(module_id)
        return rec.state["data"] if rec is not None and rec.connected and rec.state else None

    # ── задачи ──────────────────────────────────────────────────────────────

    def update_job(self, module_id: str, body: dict) -> dict | None:
        rec = self.records.get(module_id)
        if rec is None:
            return None
        job = {**rec.jobs.get(body["job_id"], {}), **{k: v for k, v in body.items() if v is not None}}
        job["module"], job["updated"] = module_id, time.time()
        if body.get("state") != "waiting_user":
            job.pop("question", None)
        rec.jobs[body["job_id"]] = job
        finished = sorted((j for j in rec.jobs.values() if j["state"] in JOB_FINISHED), key=lambda j: j["updated"])
        for old in finished[:-FINISHED_JOBS_KEPT]:
            rec.jobs.pop(old["job_id"], None)
        if job["state"] in JOB_FINISHED:
            fut = self._job_waiters.pop((module_id, body["job_id"]), None)
            if fut is not None and not fut.done():
                fut.set_result(dict(job))
        return job

    async def wait_job(self, module_id: str, job_id: str, timeout: float = 600.0) -> dict:
        """Дождаться завершения задачи модуля (ТЗ 14, DESK-11): вернуть её последний job (state done/failed/cancelled).
        Тайм-аут и обрыв модуля дают state failed с error."""
        rec = self.records.get(module_id)
        if rec is not None and rec.jobs.get(job_id, {}).get("state") in JOB_FINISHED:
            return dict(rec.jobs[job_id])
        key = (module_id, job_id)
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

    async def cancel_job(self, module_id: str, job_id: str) -> str:
        conn = self.hub.connection(module_id) if self.hub is not None else None
        if conn is None:
            raise ToolError("модуль не подключён")
        try:
            await conn.request("job.cancel", {"job_id": job_id}, timeout=15.0)
        except ModuleError as e:
            raise ToolError(e.message, e.code)
        return f"задача {job_id} отменена"

    # ── окружение для планировщика ────────────────────────────────────────────

    def _env_state(self, module_id: str) -> tuple[bool, str]:
        rec = self.records.get(module_id)
        if rec is None or not rec.connected:
            return False, "модуль не подключён"
        if not self.is_enabled(module_id):
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

    def _make_tool(self, module_id: str, spec) -> Tool:
        async def call(args: dict) -> str:
            conn = self.hub.connection(module_id) if self.hub is not None else None
            if conn is None:
                raise ToolError("модуль не подключён")
            if not self.is_enabled(module_id):
                raise ToolError("модуль выключен")
            attention = getattr(self.hub, "attention", None)
            if spec.announce and attention is not None:
                try:
                    await attention.submit_announce(module_id, announce_text(spec, args), data={"tool": spec.name})
                except Exception as e:                  # объявление не должно мешать самому вызову
                    logger.warning(f"Объявление запуска {spec.full_id(module_id)}: {e}")
            args = await self._fill_core(spec, args)
            try:
                value = await conn.request("tool.call", {"tool": spec.name, "args": args}, timeout=spec.timeout_s)
            except ModuleError as e:
                raise ToolError(e.message, e.code)
            return self.render(value, spec.job)

        async def check(args: dict) -> str | None:
            conn = self.hub.connection(module_id) if self.hub is not None else None
            try:
                if conn is None:
                    raise ModuleError("unavailable", "модуль не подключён")
                value = await conn.request("tool.check", {"tool": spec.name, "args": args}, timeout=CHECK_TIMEOUT_S)
                danger = value.get("danger") if isinstance(value, dict) else None
                return str(danger) if danger else None
            except Exception:
                return "модуль не ответил на проверку"

        params = {k: v for k, v in spec.params.items() if k not in spec.core_fills}     # планировщик core_fills не видит
        return Tool(id=spec.full_id(module_id), env=module_id, name=spec.name, description=spec.description,
                    params=params, risk=spec.risk, keywords=tuple(spec.keywords), call=call,
                    self_confirming=spec.self_confirming, informative=spec.informative, always=spec.fallback,
                    availability=spec.availability, timeout_s=spec.timeout_s, job=spec.job,
                    danger_check=spec.danger_check, check=check if spec.danger_check else None)

    def _build_environment(self, rec: ModuleRecord, spec) -> None:
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

    def status_of(self, rec: ModuleRecord) -> tuple[str, str]:
        """(код, пояснение): disabled, connected, not_configured, crashed, starting, backoff, running, offline."""
        if not self.is_enabled(rec.id):
            return "disabled", "выключен"
        if rec.connected:
            return "connected", "подключён"
        sup = self.supervisor.status(rec.id) if self.supervisor is not None else None
        if sup:
            return sup["status"], sup["text"]
        return "offline", "не отвечает" if rec.kind == "service" else "не подключён"

    def describe(self, rec: ModuleRecord) -> dict:
        m = rec.manifest or {}
        sup = (self.supervisor.status(rec.id) if self.supervisor is not None else None) or {}
        code, text = self.status_of(rec)
        tools = [{"id": f"{rec.id}.{t['name']}", "name": t["name"], "description": t["description"], "risk": t["risk"],
                  "availability": t["availability"]} for t in m.get("tools") or []]
        service = rec.kind == "service"
        return {"id": rec.id, "title": rec.title_override or rec.title or rec.id,
                "description": rec.description_override or rec.description, "version": rec.version,
                "kind": rec.kind, "enabled": self.is_enabled(rec.id),
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
        """Строки вкладки «Модули»: модули и сервисы (записи для разработчиков только при dev_mode), затем запланированные
        (`kind: planned`: id, title, tz, без состояния)."""
        rows = [row for row in self.snapshot() if dev_mode or not row["dev"]]
        rows += [{"id": p.id, "title": p.title, "tz": p.tz, "kind": "planned"} for p in self.planned.values()]
        return rows
