"""Рабочие единицы, операции и связь с ядром окружения «Монтаж» (ТЗ 20; docs/WORKSPACE_CONTRACT.md).

Монтирует сама ГЛаДОС, поэтому здесь нет очереди стадий и своих агентов. Менеджер хранит рабочие единицы (ролики) на диске,
исполняет команды `run` (долгие идут фоном как `job` с `job.update` и итоговыми событиями `montage.done` / `montage.failed`),
отвечает на `workspace.enter` / `exit` / `afk` / `resume` и держит сайт и сводку для дашборда.

`Manager` ничего не знает о WebSocket: ядро приходит как `CoreAPI`, вызовы инструментов подключает `main.py`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import media
from area import MontageTools, discover_skills, make_sandbox
from commands import EXTRA_ARGS, Commands, parse_args
from core_api import CoreAPI
from env_state import WorkspaceState
from gits_sdk import ToolError
from gits_sdk.protocol import JOB_ID_RE, WorkspaceCommand
from jobs import Job, JobStore, default_title
from journal import Journal, hms, plan_state, active_task
from layout import Layout
from manifest import BRIEF, COMMANDS, FORMATS, MANIFEST, PLATFORMS, SOURCE_KINDS, VOICEOVERS, normalize_voiceover, resolve_output_dir
from montage_site import Site
from studio import StudioError, StudioPool
from workspace import prepare_work, sync_intro

log = logging.getLogger("montage.manager")

DISK_FACTOR = 5                 # свободного места не меньше 5 × размер исходников (MON-5)
PANEL_EVERY_S = 2               # как часто пересчитывать пространство (SDK шлёт не чаще раза в секунду)
UNITS_SHOWN = 20                # сколько единиц в сводке и в списке
DEFAULTS = {k: v["default"] for k, v in MANIFEST["settings_schema"].items()}
EXCLUSIVE = ("intake", "transcribe", "voiceover", "render", "check", "final")     # у единицы одна такая операция за раз
HEAVY = ("render", "final")                                                      # рендеры идут по одному на весь контейнер
RUN_TITLES = {"intake": "Приём исходников", "transcribe": "Расшифровка", "voiceover": "Озвучка", "render": "Черновик",
              "still": "Кадр", "compositions": "Композиции", "check": "Проверка", "final": "Финальный рендер"}
WORK_OPS = ("create", "list", "status", "update", "cancel")


def total_size(paths: list[Path]) -> int:
    size = 0
    for p in paths:
        if p.is_file():
            size += p.stat().st_size
        elif p.is_dir():
            size += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return size


def human_bytes(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} ГБ" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} МБ"


@dataclass
class Run:
    """Операция над единицей: запуск команды (фоновая или внутри вызова)."""
    run_id: str
    job_id: str
    command: str
    background: bool
    started: float = field(default_factory=time.time)
    task: asyncio.Task | None = None


class Manager:
    def __init__(self, store: JobStore, core: CoreAPI, commands: Commands, layout: Layout, *, site_port: int | None = None):
        self.store = store
        self.core = core
        self.cmds = commands
        commands.setting = self.setting                 # настройки (output_dir и др.) берутся у менеджера
        self.layout = layout
        self._jobs: dict[str, Job] = {}
        self._runs: dict[str, Run] = {}
        self._heavy = asyncio.Semaphore(1)
        self.site = Site(self, port=site_port)          # сайт монтажа: ролики, сценарий, монтажный лист, файлы, кнопка Studio
        self.studios = StudioPool()                     # Remotion Studio по единицам (TZ 15, TAB-4)
        self._reaper: asyncio.Task | None = None
        self._panel: asyncio.Task | None = None
        self.env = WorkspaceState()                     # окружение агента «Монтаж» (ТЗ 32, контракт 3.2)
        self.on_idle = None                             # () -> None: активных операций не осталось (аренда ждёт этого, lease_host)
        # стандартные инструменты workspace: песочница по области, навыки модуля, разрешённые команды, исполнитель
        self.journal = Journal(self)                    # журнал действий агента (раздел 7а контракта)
        self.tools = MontageTools(make_sandbox(layout), skills=discover_skills(layout.skills), commands=COMMANDS,
                                  executor=self.execute, on_action=self.journal.record, plan_validator=self.validate_plan_text)

    # ================================================================================================ настройки и состояние

    def setting(self, name: str):
        return {**DEFAULTS, **(self.core.settings or {})}.get(name, DEFAULTS.get(name))

    def running_job_ids(self) -> list[str]:
        """Идущие операции (id операций, а не единиц): их аренда ждёт при выходе и отменяет при обрыве связи."""
        return list(self._runs)

    def active_jobs(self) -> int:
        return len(self._runs)

    def _unit_runs(self, job_id: str) -> list[Run]:
        return [r for r in self._runs.values() if r.job_id == job_id]

    # ================================================================================================ окружение агента (контракт 3.2, раздел 7)

    async def _ensure_site(self) -> bool:
        try:
            await self.site.start()
            return True
        except OSError as e:
            log.warning("сайт монтажа не поднялся: %s", e)
            return False

    async def enter_workspace(self, body: dict) -> dict:
        """workspace.enter (и resume): занять окружение. Поднимает сайт. Вводная: brief, навыки, открытые единицы, сайт."""
        res = self.env.enter(str(body.get("goal") or "")[:200], str(body.get("task_id") or ""), restored=bool(body.get("restored")))
        site_up = await self._ensure_site()
        units = await asyncio.to_thread(lambda: [self.describe(j) for j in self.store.unfinished()][-UNITS_SHOWN:])
        res.update(brief=BRIEF, skills=[{"name": s.name, "description": s.description} for s in self.tools.skill_list], units=units)
        if site_up:
            res["site"] = {"url": self.site.url()}
        await self.publish_state()
        return res

    async def afk_workspace(self, body: dict) -> dict:
        """workspace.afk: отошла, не отпуская. Ничего не отпускается и не отменяется: операции идут, сайт на месте."""
        res = self.env.go_afk()
        res["checkpoint"]["running"] = [{"job_id": r.run_id, "command": r.command, "unit": r.job_id} for r in self._runs.values()]
        await self.publish_state()
        return res

    async def exit_workspace(self, body: dict) -> dict:
        """workspace.exit: выход с отпусканием. Идущие операции дорабатывают (аренду держит lease_host), новые начинать нельзя."""
        units = await asyncio.to_thread(lambda: [j.job_id for j in self.store.unfinished()])
        res = self.env.exit({"goal": self.env.goal, "was_active": self.env.active, "units": units,
                             "running": [r.run_id for r in self._runs.values()]})
        await self.publish_state()
        return res

    # ================================================================================================ рабочие единицы (инструмент work)

    def preflight(self, args: dict) -> tuple[list[Path], dict]:
        """Проверки MON-5 до создания единицы. Возвращает (пути исходников, request); иначе ToolError."""
        missing = self.layout.missing_fonts()
        if missing:
            raise ToolError(f"нет шрифта {missing[0]}", code="unavailable")             # MON-24
        sources = args.get("sources") or []
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            raise ToolError("sources: список строк с абсолютными путями файлов или папок", code="invalid")
        brief = str(args.get("brief") or "").strip()
        if not brief:
            raise ToolError("brief: нужно описать, что за ролик", code="invalid")
        paths = []
        for s in sources:
            p = Path(os.path.expandvars(s))
            if not p.is_absolute():
                found = self.layout.resolve_resource(s)                  # короткое имя своего ресурса: design/assets/Intro/...
                if found is None:
                    raise ToolError(f"путь должен быть абсолютным или вида resources/... (смотри инструмент resources): {s}",
                                    code="invalid")
                p = found
            if not p.exists():
                raise ToolError(f"нет такого файла или папки: {s}", code="invalid")
            if not os.access(p, os.R_OK):
                raise ToolError(f"нет доступа на чтение: {s}", code="invalid")
            paths.append(p)
        for key, allowed in (("source_kind", SOURCE_KINDS), ("platform", PLATFORMS), ("format", FORMATS),
                             ("voiceover", (*VOICEOVERS, "glados"))):
            if args.get(key) is not None and args[key] not in allowed:
                raise ToolError(f"{key}: одно из {', '.join(allowed)}", code="invalid")
        need = DISK_FACTOR * total_size(paths)
        free = shutil.disk_usage(self.store.dir).free
        if free < need:
            raise ToolError(f"мало места на диске: свободно {human_bytes(free)}, нужно не меньше {human_bytes(need)} "
                            f"(5 × размер исходников)", code="unavailable")
        voiceover = normalize_voiceover(args.get("voiceover")) or None
        duration = args.get("duration_s")
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0):
            raise ToolError("duration_s: целое число секунд", code="invalid")
        request = {"sources": [str(p) for p in paths], "brief": brief, "source_kind": args.get("source_kind"),
                   "platform": args.get("platform") or "none", "platform_explicit": args.get("platform") is not None,
                   "format": args.get("format"), "duration_s": duration, "voiceover": voiceover}
        if request["source_kind"] is None and not paths:
            request["source_kind"] = "narrated"
        if not request["format"] and request["platform"] in ("shorts", "reels"):
            request["format"] = "vertical"
        return paths, request

    def validate_plan_text(self, job_id: str, text: str) -> list[str]:
        """Проверка монтажного листа при записи агентом (форма и правила plan_schema, как в `check`): список ошибок."""
        import json
        from commands import read_json
        from plan_schema import validate_plan
        try:
            plan = json.loads(text)
        except ValueError as e:
            return [f"plan.json не является JSON: {e}"]
        if not isinstance(plan, dict):
            return ["plan.json должен быть JSON-объектом"]
        job = self._find(job_id)
        story = read_json(job.plan_dir / "story.json")
        return validate_plan(plan, self.cmds._plan_context(job, story if isinstance(story, dict) else None))

    # русские названия известных папок design/assets; новые папки попадают в каталог под своим именем
    _GROUP_NAMES = {"room": "Комната", "intro": "Интро", "pet": "Питомец", "hud": "Рамки/HUD", "frames": "Рамки/HUD",
                    "robots": "Чертежи роботов", "footage": "Кадры игры", "fonts": "Шрифты", "sfx": "Звуки",
                    "pictograms": "Пиктограммы", "components": "Компоненты дизайна"}
    _STYLE_GROUP = "Регламент стиля"

    @classmethod
    def _group_of(cls, path: str) -> str:
        """Группа файла по его папке: resources/design/assets/<Папка>/..., resources/assets/<папка>/...; файлы вне папок: регламент."""
        parts = path.split("/")[1:]
        if parts[:2] == ["design", "assets"] and len(parts) > 3:
            key = parts[2]
        elif parts[:1] in (["assets"], ["design"]) and len(parts) > 2:
            key = parts[1]
        else:
            return cls._STYLE_GROUP
        return cls._GROUP_NAMES.get(key.lower(), key)

    @staticmethod
    def _short_names(names: list[str]) -> str:
        """Имена через запятую; одинаковое начало и расширение сворачиваются: `Aperture_Boot_{dark,light}.mp4`."""
        if len(names) > 2:
            exts = {n.rpartition(".")[2] for n in names}
            stems = [n.rpartition(".")[0] for n in names]
            pre = os.path.commonprefix(stems)
            if len(exts) == 1 and len(pre) >= 5 and all(len(x) > len(pre) for x in stems):
                return pre + "{" + ",".join(x[len(pre):] for x in stems) + "}." + exts.pop()
        return ", ".join(names)

    def _fit(self, folders: dict[str, list[str]], total: int, limit: int = 200) -> str:
        """Строка группы: `папка/имя, имя`; не влезает в limit: первые имена и «… (всего N)»."""
        keep = {f: list(n) for f, n in folders.items()}
        while True:
            text = "; ".join(f"{f}{self._short_names(n)}" for f, n in keep.items())
            if len(text) <= limit or all(len(n) <= 1 for n in keep.values()):
                return text
            big = max(keep, key=lambda f: len(keep[f]))
            keep[big] = keep[big][:max(1, len(keep[big]) * 2 // 3 - 1)]
            text = "; ".join(f"{f}{self._short_names(n)}" for f, n in keep.items())
            if len(text) <= limit - 20:
                return text + f", … (всего {total})"

    def _catalog(self) -> dict[str, list[tuple[str, int]]]:
        groups: dict[str, list[tuple[str, int]]] = {}
        for path, size in self.layout.catalog_files():
            groups.setdefault(self._group_of(path), []).append((path, size))
        order = [self._STYLE_GROUP, *self._GROUP_NAMES.values()]
        return {k: groups[k] for k in sorted(groups, key=lambda g: (order.index(g) if g in order else len(order), g))}

    def resources_text(self, group: str | None = None) -> str:
        """Свои ресурсы монтажа. Без параметра: компактное оглавление по группам (пути в форме раздела files: `resources/...`);
        с group: полный список файлов группы с размерами."""
        groups = self._catalog()
        if not groups:
            return "Ресурсов монтажа у неё нет."
        if group is not None and str(group).strip():
            want = str(group).strip().casefold()
            name = next((k for k in groups if k.casefold() == want), None)
            if name is None:
                raise ToolError(f"нет группы {group}; группы: {', '.join(groups)}", code="invalid")
            lines = [f"{name}: {len(groups[name])} файл(ов); пути для files. Медиа можно использовать в work.sources."]
            if name == "Компоненты дизайна":
                components = sorted({p.split("/")[3] for p, _ in groups[name] if len(p.split("/")) > 4})
                return ("resources/design/components/<компонент>/README.md — описание; preview.html — просмотр. "
                        "Обзор назначения: resources/design/README.md. Компоненты: " + ", ".join(components))
            lines += [f"{p} ({size // 1024 or 1} КБ)" if size >= 1024 else f"{p} ({size} Б)" for p, size in groups[name]]
            return "\n".join(lines)
        lines = ["Ресурсы монтажа; пути как в files, в sources work годятся они и абсолютные. Полный список: resources с group=<имя>."]
        for name, items in groups.items():
            if name == "Компоненты дизайна":
                lines.append(f"{name}: {len(items)} файлов; открой resources с group={name}.")
                continue
            folders: dict[str, list[str]] = {}
            for path, _ in items:
                folder, _, fname = path.rpartition("/")
                if fname == "README.md" and "design/assets/" in folder:               # описание папки: в списке группы
                    continue
                folders.setdefault(folder + "/", []).append(fname)
            text = self._fit(folders, len(items))
            lines.append(f"{name}: {text}")
        return "\n".join(lines)

    def _find(self, job_id: str) -> Job:
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            raise ToolError("id единицы: j-xxxxxxxx (смотри work list)", code="invalid")
        job = self._jobs.get(job_id) or self.store.load(job_id)
        if job is None:
            raise ToolError(f"единицы {job_id} нет", code="unknown")
        self._jobs.setdefault(job_id, job)
        return job

    def _all_jobs(self) -> list[Job]:
        seen = {j.job_id: j for j in self.store.all()}
        seen.update(self._jobs)
        return sorted(seen.values(), key=lambda j: j.data.get("created", 0))

    @staticmethod
    def describe(job: Job) -> dict:
        return {"id": job.job_id, "title": job.data["title"], "state": job.state}

    async def work(self, args: dict) -> dict:
        op = args.get("op")
        if op not in WORK_OPS:
            raise ToolError(f"op: одно из {list(WORK_OPS)}", code="invalid")
        return await getattr(self, "_work_" + op)(args)

    async def _work_create(self, args: dict) -> dict:
        if self.env.releasing:
            raise ToolError("окружение отпускается: новые единицы создавать нельзя", code="busy")
        _, request = await asyncio.to_thread(self.preflight, args)
        title = str(args.get("title") or "").strip()[:80] or None
        job = await asyncio.to_thread(self.store.create, request, title or default_title(request))
        await asyncio.to_thread(prepare_work, job.folder, self.layout)             # копия шаблона, ссылки на node_modules
        self._jobs[job.job_id] = job
        await asyncio.to_thread(self.store.log, job, "created", request={k: v for k, v in request.items() if k != "sources"})
        await self.publish_state()
        jid = job.job_id
        return {"ok": True, **self.describe(job), "site": self.site.url(jid),
                "paths": {"plan": f"{jid}/plan", "src": f"{jid}/work/src", "public": f"{jid}/work/public", "sources": f"{jid}/sources",
                          "renders": f"{jid}/renders", "qa": f"{jid}/qa"}}

    async def _work_list(self, args: dict) -> dict:
        jobs = await asyncio.to_thread(self._all_jobs)
        return {"ok": True, "result": [self.describe(j) for j in jobs][-UNITS_SHOWN:], "site": self.site.url()}

    async def _work_status(self, args: dict) -> dict:
        job = self._find(str(args.get("id") or ""))
        mark = args.get("set")
        if mark is not None:
            if mark not in ("working", "waiting_user"):
                raise ToolError("set: working или waiting_user", code="invalid")
            if job.state in ("done", "cancelled"):
                raise ToolError(f"единица {job.job_id} закрыта ({job.state})", code="unavailable")
            job.data["state"] = mark
            await asyncio.to_thread(self.store.save, job)
            await self.publish_state()
        return {"ok": True, **await asyncio.to_thread(self.unit_status, job)}

    async def _work_update(self, args: dict) -> dict:
        job = self._find(str(args.get("id") or ""))
        voiceover = normalize_voiceover(args.get("voiceover"))
        if voiceover not in VOICEOVERS:
            raise ToolError("update: укажи voiceover: none, character или keep", code="invalid")
        if job.state in ("done", "cancelled") or self._unit_runs(job.job_id):
            raise ToolError("Изменение доступно для незакрытой единицы без идущих операций.", code="unavailable")
        if job.request.get("voiceover") != voiceover:
            job.request["voiceover"] = voiceover
            job.data.pop("check", None)
            await asyncio.to_thread(self.store.save, job)
            await self.publish_state()
        return {"ok": True, "id": job.job_id, "voiceover": voiceover}

    async def _work_cancel(self, args: dict) -> dict:
        job = await self.cancel_unit(str(args.get("id") or ""))
        return {"ok": True, **self.describe(job)}

    def unit_status(self, job: Job) -> dict:
        """Подробности единицы: состояние, запрос, итоги запусков, файлы, свежесть проверки, ссылка на сайт (читает диск)."""
        d = job.data
        r = d["request"]
        fresh, why = self.cmds.check_state(job)
        listing = lambda folder, pattern="*": sorted(p.name for p in folder.glob(pattern) if p.is_file())      # noqa: E731
        return {**self.describe(job),
                "request": {k: r.get(k) for k in ("brief", "source_kind", "platform", "format", "duration_s", "voiceover")},
                "running": [{"job_id": x.run_id, "command": x.command} for x in self._unit_runs(job.job_id)],
                "runs": d.get("runs") or {},
                "files": {"sources": listing(job.sources), "story": (job.plan_dir / "story.json").is_file(),
                          "plan": (job.plan_dir / "plan.json").is_file(), "words": (job.transcript / "words.json").is_file(),
                          "renders": listing(job.renders), "reports": listing(job.qa, "*.md")},
                "check": {"fresh": fresh, "passed": bool((d.get("check") or {}).get("passed")),
                          "errors": (d.get("check") or {}).get("errors"), "report": (d.get("check") or {}).get("report"),
                          "reason": why},
                "output": d.get("output"), "error": d.get("error"), "site": self.site.url(job.job_id)}

    async def cancel_unit(self, job_id: str) -> Job:
        """work.cancel: остановить операции единицы (дерево процессов ffmpeg и Remotion), закрыть единицу, Studio погасить.
        Папка остаётся до уборки."""
        job = self._find(job_id)
        if job.state in ("done", "cancelled"):
            return job
        job.data["state"] = "cancelled"                                  # сначала состояние: завершение операций его не тронет
        await asyncio.to_thread(self.store.save, job)
        for run in self._unit_runs(job.job_id):
            await self.cancel_run(run.run_id)
        await asyncio.to_thread(media.kill_processes_under, job.folder)
        await asyncio.to_thread(self.store.log, job, "cancelled")
        await self.journal.touch(job, "единица отменена", force=True)
        await self.studios.stop(job.job_id)
        await self.publish_state()
        return job

    async def cancel(self, job_id: str) -> None:
        """Отмена по id операции (job.cancel ядра, возврат аренды) или единицы."""
        if job_id in self._runs:
            await self.cancel_run(job_id)
        elif isinstance(job_id, str) and JOB_ID_RE.match(job_id):
            await self.cancel_unit(job_id)

    async def site_tool(self, args: dict) -> dict:
        """Инструмент site: адрес сайта и страница ролика."""
        if not await self._ensure_site():
            raise ToolError("сайт монтажа не поднят", code="unavailable")
        jid = args.get("id")
        if jid:
            job = self._find(str(jid))
            return {"ok": True, "url": self.site.url(job.job_id), "site": self.site.url(), "title": job.data["title"]}
        return {"ok": True, "url": self.site.url(), "site": self.site.url()}

    # ================================================================================================ запуск команд (инструмент run)

    async def execute(self, command: WorkspaceCommand, args: list, background: bool, cwd: Path):
        """Исполнитель `WorkspaceTools.run`: проверка аргументов по списку разрешённого, затем фон (возвращает `job_id` операции)
        или выполнение внутри вызова."""
        if self.env.releasing:
            raise ToolError("окружение отпускается: новые операции начинать нельзя", code="busy")
        if not args:
            raise ToolError(f"команда «{command.id}»: первым аргументом id единицы (инструмент work)", code="invalid")
        job = self._find(args[0])
        if job.state == "cancelled":
            raise ToolError(f"единица {job.job_id} отменена", code="unavailable")
        parsed = parse_args(command.id, [str(a) for a in args[1:]])
        if command.id in EXCLUSIVE:
            busy = [r for r in self._unit_runs(job.job_id) if r.command in EXCLUSIVE]
            if busy:
                raise ToolError(f"у единицы уже идёт «{busy[0].command}» ({busy[0].run_id}): дождись итога или отмени (job.cancel)",
                                code="busy")
        run = Run(run_id=self._new_run_id(), job_id=job.job_id, command=command.id, background=background)
        self._runs[run.run_id] = run
        if background:
            run.task = asyncio.create_task(self._background(run, job, command, parsed))
            await self.publish_state()
            return {"job_id": run.run_id}
        return await self._inline(run, job, command, parsed)

    def _new_run_id(self) -> str:
        from gits_sdk import protocol as P
        run_id = P.new_job_id()
        while run_id in self._runs or run_id in self._jobs:
            run_id = P.new_job_id()
        return run_id

    def _title(self, run: Run, job: Job) -> str:
        return f"{RUN_TITLES.get(run.command, run.command)}: {job.data['title']}"

    async def _begin(self, run: Run, job: Job) -> None:
        if job.state in ("new", "failed", "done", "waiting_user"):
            job.data["state"] = "working"
            job.data["error"] = None
        job.data.setdefault("runs", {})[run.command] = {"job_id": run.run_id, "status": "running", "at": round(run.started, 1),
                                                         "background": run.background}
        await asyncio.to_thread(self.store.save, job)
        await asyncio.to_thread(self.store.log, job, "run", command=run.command, job_id=run.run_id, status="start")
        await self.journal.send_unit(job, force=True)

    async def _end(self, run: Run, job: Job, status: str, *, text: str = "", result: dict | None = None,
                   error: dict | None = None) -> None:
        """Итог операции: запись в единицу, состояние единицы, job.update и событие (для фоновой), уборка."""
        self._runs.pop(run.run_id, None)
        rec = {"job_id": run.run_id, "status": status, "at": round(time.time(), 1), "background": run.background, "text": text[:500]}
        if error:
            rec["error"] = error
        job.data.setdefault("runs", {})[run.command] = rec
        if job.state != "cancelled":
            if status == "done" and run.command == "final":
                job.data["state"] = "done"
            elif status == "failed" and run.background:
                job.data["state"] = "failed"
                job.data["error"] = error
        await asyncio.to_thread(self.store.save, job)
        await asyncio.to_thread(self.store.log, job, "run", command=run.command, job_id=run.run_id, status=status,
                                **({"error": error} if error else {}))
        await self.journal.finished(run, job, status, text, error)
        if run.background:
            fields: dict = {"title": self._title(run, job), "state": status, "text": text[:300] or None}
            if status == "done":
                fields["progress"] = 1.0
                arts = [a for a in job.data.get("artifacts") or [] if a.get("kind") in ("preview", "final", "output", "qa_report", "qa_report_final")]
                fields["artifacts"] = arts or None
            if status == "failed":
                fields["error"] = error
            await self.core.job_update(run.run_id, **{k: v for k, v in fields.items() if v is not None or k == "text"})
            title = job.data["title"]
            if status == "done":
                if run.command == "final":
                    await self._emit("montage.done", f"Монтаж «{title}» закончен, файл: {job.data.get('output')}.", run, job,
                                     data={"job_id": job.job_id, "run": run.run_id, "path": job.data.get("output"),
                                           "qa_report": (result or {}).get("qa_report")}, urgency_hint=3)
                else:
                    await self._emit("montage.done", f"Монтаж «{title}»: {text}.", run, job,
                                     data={"job_id": job.job_id, "run": run.run_id, "command": run.command}, urgency_hint=2)
            elif status == "failed":
                await self._emit("montage.failed", f"Монтаж «{title}» не удался: {(error or {}).get('message')}", run, job,
                                 data={"job_id": job.job_id, "run": run.run_id, "command": run.command,
                                       "code": (error or {}).get("code")}, urgency_hint=3)
        if status == "done" and run.command == "final":
            removed = await asyncio.to_thread(self.store.cleanup, int(self.setting("keep_jobs")))
            if removed:
                log.info("убраны старые единицы: %s", ", ".join(removed))
        await self.publish_state()

    async def _emit(self, kind: str, text: str, run: Run, job: Job, **fields) -> None:
        try:
            await self.core.event(kind, text, dedupe_key=f"{kind}:{run.run_id}", **fields)
        except Exception as e:
            log.info("событие %s не отправлено: %s", kind, e)

    async def _background(self, run: Run, job: Job, command: WorkspaceCommand, parsed: dict) -> None:
        """Фоновая операция: job.update (queued, running, ход, итог) и события. Отмена задачи даёт состояние cancelled."""
        heavy = run.command in HEAVY
        try:
            await self._begin(run, job)
            await self.core.job_update(run.run_id, title=self._title(run, job), state="queued" if heavy else "running", progress=0.0,
                                       text="жду очереди рендера" if heavy else None)

            async def progress(frac: float, text: str) -> None:
                full = await self.journal.step(job, run.run_id, run.command, frac, text)
                await self.core.job_update(run.run_id, progress=round(max(0.0, min(1.0, frac)), 3), text=full[:300])
            async with self._heavy if heavy else contextlib.nullcontext():
                if heavy:
                    await self.core.job_update(run.run_id, state="running", text=None)
                result = await asyncio.wait_for(self.cmds.run(run.command, job, parsed, progress), command.timeout_s)
        except asyncio.CancelledError:
            await self._end(run, job, "cancelled", text="операция отменена")
            return
        except asyncio.TimeoutError:
            await self._end(run, job, "failed", text="превышено время операции",
                            error={"code": "unavailable", "message": f"{run.command}: превышено время ({command.timeout_s} с)"})
            return
        except ToolError as e:
            await self._end(run, job, "failed", text=e.message, error={"code": e.code, "message": f"{run.command}: {e.message}"})
            return
        except Exception as e:
            log.exception("операция %s единицы %s", run.command, job.job_id)
            msg = f"{run.command}: сбой {type(e).__name__}: {str(e)[:200]}"
            await self._end(run, job, "failed", text=msg, error={"code": "internal", "message": msg})
            return
        await self._end(run, job, "done", text=str(result.get("text") or ""), result=result)

    async def _inline(self, run: Run, job: Job, command: WorkspaceCommand, parsed: dict) -> dict:
        async def progress(frac: float, text: str) -> None:
            await self.journal.step(job, run.run_id, run.command, frac, text)
        try:
            await self._begin(run, job)
            result = await asyncio.wait_for(self.cmds.run(run.command, job, parsed, progress), command.timeout_s)
        except asyncio.TimeoutError:
            msg = f"{run.command}: превышено время ({command.timeout_s} с); запусти с background=true"
            await self._end(run, job, "failed", text=msg, error={"code": "unavailable", "message": msg})
            raise ToolError(msg, code="unavailable") from None
        except ToolError as e:
            await self._end(run, job, "failed", text=e.message, error={"code": e.code, "message": f"{run.command}: {e.message}"})
            raise
        except asyncio.CancelledError:
            self._runs.pop(run.run_id, None)
            raise
        except Exception as e:
            log.exception("команда %s единицы %s", run.command, job.job_id)
            msg = f"{run.command}: сбой {type(e).__name__}: {str(e)[:200]}"
            await self._end(run, job, "failed", text=msg, error={"code": "internal", "message": msg})
            raise ToolError(msg, code="internal") from e
        await self._end(run, job, "done", text=str(result.get("text") or ""), result=result)
        return result

    async def cancel_run(self, run_id: str) -> bool:
        """Отмена операции (job.cancel): задача останавливается, дерево процессов ffmpeg и Remotion убивается."""
        run = self._runs.get(run_id)
        if run is None or run.task is None:
            return False
        job = self._jobs.get(run.job_id)
        run.task.cancel()
        await asyncio.gather(run.task, return_exceptions=True)
        if job is not None:
            await asyncio.to_thread(media.kill_processes_under, job.folder)
        return True

    # ================================================================================================ восстановление и остановка

    async def recover(self) -> int:
        """Возврат к работе после перезапуска: открытые единицы на месте (файлы сохранились), операции, шедшие при обрыве,
        помечаются прерванными (`interrupted`): агент запускает их заново."""
        jobs = await asyncio.to_thread(self.store.unfinished)
        for job in jobs:
            self._jobs[job.job_id] = job
            changed = False
            for rec in (job.data.get("runs") or {}).values():
                if rec.get("status") == "running":
                    rec["status"] = "interrupted"
                    changed = True
            if changed:
                await asyncio.to_thread(self.store.save, job)
                await asyncio.to_thread(self.store.log, job, "recovered")
        return len(jobs)

    def start_worker(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self.studios.run_reaper())     # TZ 15: Studio гаснет без обращений
        if self._panel is None or self._panel.done():
            self._panel = asyncio.create_task(self._panel_loop())             # TZ 15: пространство «Монтаж»

    async def stop(self, *, kill: bool = True) -> None:
        """Остановить работу при выключении модуля: идущие операции отменяются, сайт и Studio гаснут. Единицы и файлы остаются."""
        for run in list(self._runs.values()):
            if run.task is not None and not run.task.done():
                run.task.cancel()
                await asyncio.gather(run.task, return_exceptions=True)
            job = self._jobs.get(run.job_id)
            if kill and job is not None:
                await asyncio.to_thread(media.kill_processes_under, job.folder)
        for task in (self._reaper, self._panel):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.studios.stop_all()
        await self.site.stop()

    # ================================================================================================ Studio и сайт (TZ 15)

    async def open_site(self, job_id: str) -> dict:
        """Сайт единицы (сценарий, монтажный лист, файлы, кнопка Remotion Studio): поднимает сервер при первом обращении."""
        job = self._find(job_id)
        await self.site.start()
        return {"url": self.site.url(job.job_id), "job_id": job.job_id}

    async def open_studio(self, job_id: str) -> dict:
        """Remotion Studio единицы (кнопка на сайте): поднимает (если его нет) и отдаёт адрес. Новый запуск объявляется событием
        montage.launch (факт, объявление звучит всегда: правило 14)."""
        job = self._find(job_id)
        if not (job.plan_dir / "plan.json").is_file():
            raise ToolError("у единицы ещё нет плана, Studio открыть нельзя", code="unavailable")
        try:
            await asyncio.to_thread(sync_intro, job.work, self.layout)
            url, started = await self.studios.open(job.job_id, job.work)
        except StudioError as e:
            raise ToolError(str(e), code="unavailable") from e
        if started:
            await self.core.event(
                "montage.launch",
                f"Открываю Remotion Studio для ролика «{job.data['title']}»: только просмотр, рендера не будет. "
                f"Погаснет через 30 минут без обращения.",
                announce=True, data={"job_id": job.job_id, "what": "studio"})
        return {"url": url, "job_id": job.job_id}

    # ================================================================================================ сводка для ядра и дашборда

    def panel_blocks(self, plan: dict | None = None) -> list[dict]:
        """Пространство «Монтаж» в дашборде: только сводка (кнопок одобрения нет: согласия идут вопросами ядра). Ролики, файлы,
        сценарий, монтажный лист и Studio на сайте окружения (`montage_site`, ссылка в `panel.links`). Читает файлы: звать через to_thread."""
        jobs = self._all_jobs()
        states = [j.state for j in jobs]
        return [{"type": "stats", "title": "Сейчас", "items": [
            {"label": "В работе", "value": sum(1 for j in jobs if j.open and j.state != "waiting_user")},
            {"label": "Ждут ответа", "value": states.count("waiting_user")},
            {"label": "Готово", "value": states.count("done")},
            {"label": "Операции", "value": len(self._runs)}]},
            {"type": "text", "title": "Последнее действие", "text": self._last_action_text(jobs, plan)}]

    def _last_action_text(self, jobs: list[Job], plan: dict | None = None) -> str:
        """Строка «Последнее действие»: самое свежее по единицам (время и текст), ниже «Шаг n из N» из плана агента (core.tasks)."""
        best = max((j.data["now"] for j in jobs if isinstance(j.data.get("now"), dict) and j.data["now"].get("at")),
                   key=lambda n: n["at"], default=None)
        if best is None and not jobs:
            env = self.store.read_env_actions(1)
            best = {"at": env[-1].get("ts"), "text": env[-1].get("text")} if env else None
        lines = [f"{hms(best['at'])} · {best['text']}" if best else "Действий пока нет."]
        summary = plan_state(active_task(plan if plan is not None else self.journal.cached_plan))
        if summary and summary["n"]:
            lines.append(f"Шаг {summary['n']} из {summary['total']}: {summary['text']}")
        return "\n".join(lines)

    async def publish_panel(self) -> None:
        client = getattr(self.core, "client", None)
        if client is None:
            return
        try:
            plan = await self.journal.plan()
            blocks = await asyncio.to_thread(self.panel_blocks, plan)
            await client.panel.set(blocks)
        except Exception as e:
            log.debug("пространство не обновлено: %s", e)

    async def publish_state(self) -> None:
        snapshot = list(self._runs.values())                      # пока читаются файлы, операция может закончиться и уйти из _runs
        runs = [{"job_id": r.run_id, "command": r.command, "unit": r.job_id} for r in snapshot]
        jobs = await asyncio.to_thread(self._all_jobs)
        open_units = [j for j in jobs if j.open]
        summary = f"единиц в работе {len(open_units)}, операций идёт {len(runs)}"
        if runs:
            first = snapshot[0]
            unit = self._jobs.get(first.job_id)
            summary += f": {RUN_TITLES.get(first.command, first.command).lower()}" + (f" «{unit.data['title']}»" if unit else "")
        data = {"units": [self.describe(j) for j in open_units][-UNITS_SHOWN:], "runs": runs,
                "status": self.env.status(),                               # ТЗ 18, 32: статус окружения для ядра
                "jobs_dir": os.path.abspath(str(self.layout.jobs)),        # папки для ссылок пространства (path_from, TZ 15)
                "output_dir": resolve_output_dir(self.setting("output_dir"))}
        client = getattr(self.core, "client", None)
        if client is not None:
            try:
                await client.state(summary, data)
            except Exception as e:
                log.debug("state не отправлен: %s", e)
        if not self._runs:
            if self.env.releasing:
                await self.site.stop()                                     # вышла и операций не осталось: сайт гаснет (аренда отпускается)
            if self.on_idle is not None:
                self.on_idle()                                             # аренда, ждавшая конца операций, освобождается

    async def _panel_loop(self) -> None:
        """Пространство «Монтаж» обновляется отдельным циклом, а не из пути состояния: публикация не задерживает операции."""
        while True:
            await self.publish_panel()
            await asyncio.sleep(PANEL_EVERY_S)


__all__ = ["Manager", "Run", "EXTRA_ARGS"]
