"""Очередь задач и связь с ядром (ТЗ 20: MON-5, MON-8, MON-10, MON-12, MON-31, MON-33).

Одна задача выполняется, остальные ждут в очереди. `Manager` ничего не знает о WebSocket: ядро приходит как `CoreAPI`,
вызовы инструментов и ответы пользователя — вызовами методов (их подключает `main.py`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

import approval_panel as ap
import intake as intake_mod
import media
import review_mode as rm
from core_api import CoreAPI
from env_state import WorkspaceState
from gits_sdk import ToolError
from jobs import FINISHED, Job, JobStore
from layout import Layout
from manifest import FORMATS, PLATFORMS, SOURCE_KINDS, VOICEOVERS
from pipeline import EDITS, Pipeline
from studio import StudioError, StudioPool

log = logging.getLogger("montage.manager")

DISK_FACTOR = 5                 # свободного места не меньше 5 × размер исходников (MON-5)
KB_RETRY_S = 600
PANEL_ROWS = 20                 # сколько задач в таблице пространства «Монтаж» (TZ 15, TAB-9)
PANEL_EVERY_S = 2               # как часто пересчитывать пространство (SDK шлёт не чаще раза в секунду)
REPORT_EXCERPT_MAX = 2500       # отчёт проверки в пространстве: начало текста, не весь файл


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


class Manager:
    def __init__(self, store: JobStore, core: CoreAPI, pipeline: Pipeline, layout: Layout):
        self.store = store
        self.core = core
        self.pipeline = pipeline
        self.layout = layout
        self.queue: list[str] = []
        self.current: str | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._jobs: dict[str, Job] = {}
        self._cancelled: set[str] = set()
        self._worker: asyncio.Task | None = None
        self.studios = StudioPool()                     # Remotion Studio по задачам (TZ 15, TAB-4)
        self._reaper: asyncio.Task | None = None
        self._panel: asyncio.Task | None = None
        pipeline.state_hook = self.publish_state
        self.env = WorkspaceState()                     # среда агента «Монтаж» (ТЗ 32)

    # ================================================================================================ среда агента (ТЗ 32)

    def _busy(self) -> bool:
        return bool(self.queue) or self.current is not None

    async def enter_workspace(self, body: dict) -> dict:
        """workspace.enter (прежнее policy.activate): агент входит в монтаж. Задачи, уже стоящие в очереди, становятся делом среды."""
        res = self.env.enter(str(body.get("goal") or "")[:200], str(body.get("task_id") or ""), busy=self._busy(),
                             restored=bool(body.get("restored")))
        await self.publish_state()
        return res

    async def exit_workspace(self, body: dict) -> dict:
        """workspace.exit (прежнее focus.release): монтаж в фон; задачи идут дальше, checkpoint хранит цель и очередь."""
        res = self.env.exit({"goal": self.env.goal, "was_active": self.env.active, "queue": list(self.queue),
                             "current": self.current})
        await self.publish_state()
        return res

    # ================================================================================================ поставить задачу

    def preflight(self, args: dict, *, battery_sleeping: bool) -> tuple[list[Path], dict]:
        """Проверки MON-5 до постановки. Возвращает (пути исходников, request); иначе ToolError."""
        missing = self.layout.missing_fonts()
        if missing:
            raise ToolError(f"нет шрифта {missing[0]}", code="unavailable")             # MON-24
        for finder, label in ((media.find_ffmpeg, "ffmpeg"), (media.find_ffprobe, "ffprobe"), (media.find_npx, "Node.js (npx)")):
            try:
                finder()
            except FileNotFoundError:
                raise ToolError(f"не найден {label}: монтаж невозможен", code="unavailable")
        sources = args.get("sources")
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            raise ToolError("sources: список строк с абсолютными путями файлов или папок", code="bad_request")
        brief = str(args.get("brief") or "").strip()
        if not brief:
            raise ToolError("brief: нужно описать, что за ролик", code="bad_request")
        paths = []
        for s in sources:
            p = Path(os.path.expandvars(s))
            if not p.is_absolute():
                raise ToolError(f"путь должен быть абсолютным: {s}", code="bad_request")
            if not p.exists():
                raise ToolError(f"нет такого файла или папки: {s}", code="bad_request")
            if not os.access(p, os.R_OK):
                raise ToolError(f"нет доступа на чтение: {s}", code="bad_request")
            paths.append(p)
        for key, allowed in (("source_kind", SOURCE_KINDS), ("platform", PLATFORMS), ("format", FORMATS), ("voiceover", VOICEOVERS)):
            if args.get(key) is not None and args[key] not in allowed:
                raise ToolError(f"{key}: одно из {', '.join(allowed)}", code="bad_request")
        if battery_sleeping:
            raise ToolError("нет заряда Claude", code="unavailable")                       # MON-5
        need = DISK_FACTOR * total_size(paths)
        free = shutil.disk_usage(self.store.dir).free
        if free < need:
            raise ToolError(f"мало места на диске: свободно {human_bytes(free)}, нужно не меньше {human_bytes(need)} "
                            f"(5 × размер исходников)", code="unavailable")
        request = {"sources": [str(p) for p in paths], "brief": brief, "source_kind": args.get("source_kind"),
                   "platform": args.get("platform") or "none", "platform_explicit": args.get("platform") is not None,
                   "format": args.get("format"), "duration_s": args.get("duration_s"), "voiceover": args.get("voiceover")}
        if request["source_kind"] is None and not paths:
            request["source_kind"] = "narrated"
        return paths, request

    async def start(self, args: dict) -> dict:
        _, request = await asyncio.to_thread(self.preflight, args, battery_sleeping=self.core.sleeping)
        if not request.get("format") and request["platform"] in ("shorts", "reels", "telegram"):
            request["format"] = "vertical"
        job = await asyncio.to_thread(self.store.create, request)
        try:
            persona = await self.core.persona()
            job.data["persona"] = {"name": persona.get("name"), "card": persona.get("card", "")}            # MON-30
        except (ToolError, asyncio.TimeoutError):
            job.data["persona"] = {}
        await asyncio.to_thread(self.store.save, job)
        await asyncio.to_thread(self.store.log, job, "created", request={k: v for k, v in request.items() if k != "sources"})
        self._jobs[job.job_id] = job
        self.env.note_job()
        await self._enqueue(job, "Задача поставлена в очередь")
        return {"job_id": job.job_id}

    async def _enqueue(self, job: Job, text: str) -> None:
        job.data["state"] = "queued"
        await asyncio.to_thread(self.store.save, job)
        await self.core.job_update(job.job_id, title=job.data["title"], state="queued", stage=job.stage,
                                   progress=job.data["progress"], text=text)
        if job.job_id not in self.queue and job.job_id != self.current:
            self.queue.append(job.job_id)
        self._wake.set()
        await self.publish_state()

    # ================================================================================================ восстановление и очередь

    async def recover(self) -> int:
        """MON-8: незавершённые задачи из jobs/*/job.json продолжаются с начала текущей стадии."""
        jobs = await asyncio.to_thread(self.store.unfinished)
        for job in jobs:
            self._jobs[job.job_id] = job
            await asyncio.to_thread(self.store.log, job, "recovered", state=job.state, stage=job.stage)
            self.queue.append(job.job_id)
        if jobs:
            self._wake.set()
        return len(jobs)

    def start_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self.studios.run_reaper())     # TZ 15: Studio гаснет без обращений
        if self._panel is None or self._panel.done():
            self._panel = asyncio.create_task(self._panel_loop())             # TZ 15: пространство «Монтаж»

    async def _work(self) -> None:
        while True:
            while not self.queue:
                self._wake.clear()
                await self._wake.wait()
            job_id = self.queue.pop(0)
            job = self._jobs.get(job_id) or await asyncio.to_thread(self.store.load, job_id)
            if job is None or job.finished:
                continue
            self._jobs[job_id] = job
            self.current = job_id
            self._task = asyncio.create_task(self.pipeline.run_job(job))
            await self.publish_state()
            try:
                await self._task
            except asyncio.CancelledError:
                if job_id in self._cancelled:
                    self._cancelled.discard(job_id)
                    await self.pipeline.mark_cancelled(job)
                elif asyncio.current_task().cancelled():
                    raise
            except Exception:
                log.exception("задача %s", job_id)
            finally:
                self.current = None
                self._task = None
                self.pipeline._last_saved.pop(job_id, None)
                if job.finished:
                    await self.studios.stop(job_id)              # Studio гаснет вместе с задачей (TZ 15)
                await self.publish_state()

    async def stop(self, *, kill: bool = True) -> None:
        """Остановить работу при выключении модуля. Задача остаётся в своём состоянии и продолжится при следующем запуске."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        for task in (self._reaper, self._panel):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.studios.stop_all()
        if kill and self.current:
            job = self._jobs.get(self.current)
            if job:
                await asyncio.to_thread(media.kill_processes_under, job.folder)

    # ================================================================================================ инструменты status / revise / cancel

    def _find(self, job_id: str) -> Job:
        job = self._jobs.get(job_id) or self.store.load(job_id)
        if job is None:
            raise ToolError(f"задачи {job_id} нет", code="unknown")
        self._jobs.setdefault(job_id, job)
        return job

    def describe(self, job: Job) -> dict:
        d = job.data
        return {"job_id": job.job_id, "title": d["title"], "state": d["state"], "stage": d["stage"],
                "progress": d["progress"], "text": self._line(job),
                "artifacts": d.get("artifacts") or [], "error": d.get("error")}

    @staticmethod
    def _line(job: Job) -> str:
        d = job.data
        base = f"{job.job_id} «{d['title']}»: {d['state']}, стадия {d['stage']}, {round(d['progress'] * 100)} %"
        if d.get("error"):
            base += f", ошибка: {d['error']['message']}"
        if d["state"] == "done" and d.get("output"):
            base += f", файл {d['output']}"
        return base

    async def status(self, args: dict) -> dict:
        jid = args.get("job_id")
        if jid:
            jobs = [self._find(jid)]
        else:
            jobs = [j for j in self._all_jobs()][-10:]
        stats = rm.agreement(await asyncio.to_thread(self.store.verdicts))
        return {"text": "\n".join(self._line(j) for j in jobs) or "задач монтажа нет", "jobs": [self.describe(j) for j in jobs],
                "review": {**stats, "text": rm.agreement_text(stats)}}

    def _all_jobs(self) -> list[Job]:
        seen = {j.job_id: j for j in self.store.all()}
        seen.update(self._jobs)
        return sorted(seen.values(), key=lambda j: j.data.get("created", 0))

    async def revise(self, args: dict) -> dict:
        job = self._find(str(args.get("job_id") or ""))
        notes = str(args.get("notes") or "").strip()
        if not notes:
            raise ToolError("notes: нужно сказать, что изменить", code="bad_request")
        if job.state in ("failed", "cancelled"):
            raise ToolError(f"задача {job.job_id} не активна ({job.state}): правки к ней не применить, поставь новую", code="unavailable")
        if self.pipeline.waiting_question(job.job_id) and job.stage == "review":
            job.data["preset_notes"] = notes                          # ответ «правки» вместо пользователя
            await asyncio.to_thread(self.store.save, job)
            self.pipeline.preset_answer(job.job_id, EDITS)
            return {"accepted": True, "text": "правки приняты, возвращаю на сборку"}
        job.data.setdefault("revisions", []).append({"notes": notes, "ts": time.time(), "applied": False})
        await asyncio.to_thread(self.store.log, job, "revision", notes=notes[:300])
        if job.state == "done":
            job.data.update(stage="build", progress=0.45, error=None)
            await self._enqueue(job, "Правки приняты, возвращаю на сборку")
            return {"accepted": True, "text": "правки приняты, задача снова в очереди на сборку"}
        await asyncio.to_thread(self.store.save, job)
        return {"accepted": True, "text": "правки приняты, учту на ближайшей сборке"}

    async def cancel(self, job_id: str) -> None:
        """MON-12: прервать агента и процессы ffmpeg/remotion (дерево), состояние `cancelled`, папка остаётся до уборки."""
        job = self._find(job_id)
        if job.finished:
            return
        if job_id in self.queue:
            self.queue.remove(job_id)
            await self.pipeline.mark_cancelled(job)
        elif job_id == self.current and self._task is not None:
            self._cancelled.add(job_id)
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            await asyncio.to_thread(media.kill_processes_under, job.folder)
        else:
            await self.pipeline.mark_cancelled(job)
        await self.studios.stop(job_id)
        await self.publish_state()

    def answer(self, job_id: str, question_id: str, answer) -> None:
        self.pipeline.deliver_answer(job_id, question_id, answer)

    # ================================================================================================ Studio и одобрения (TZ 15)

    async def open_studio(self, job_id: str) -> dict:
        """Remotion Studio задачи: поднимает (если его нет) и отдаёт адрес. Новый запуск объявляется событием montage.launch
        (факт, объявление звучит всегда: правило 14)."""
        job = self._find(job_id)
        if not (job.plan_dir / "plan.json").is_file():
            raise ToolError("у задачи ещё нет плана, Studio открыть нельзя", code="unavailable")
        try:
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

    async def panel_decide(self, action: str, args: dict) -> dict:
        """Кнопка этапа в пространстве «Монтаж» (TZ 15b): ответ на вопрос задачи тем же путём, что и из обзора
        (`deliver_answer`). Кнопка срабатывает, только если вопрос ещё открыт (тот же question_id) и подходит её этапу:
        кнопка «Одобрить черновик» не ответит на вопрос про историю и план, текстовый вопрос кнопкой не закрыть."""
        job_id = str(args.get("job_id") or "")
        qid = str(args.get("question_id") or "")
        job = self._find(job_id)
        q = job.data.get("question") or {}
        closed = {"ok": False, "message": "этот вопрос уже закрыт или задача ушла дальше"}
        if job.data.get("state") != "waiting_user" or not qid or q.get("question_id") != qid \
                or self.pipeline.waiting_question(job_id) != qid:
            return closed
        options = [str(o) for o in (q.get("options") or [])]
        if action in ap.STAGE_ACTIONS:
            answer, expected = ap.STAGE_ACTIONS[action]
            if set(options) != set(expected):
                return {"ok": False, "message": "кнопка этого этапа не подходит к текущему вопросу"}
        elif action == ap.ANSWER_ACTION:
            answer = args.get("answer")
            if q.get("kind") == "approval":
                if not isinstance(answer, bool):
                    return {"ok": False, "message": "ответ на это согласие: да или нет"}
            elif q.get("kind") == "choice":
                if answer not in options:
                    return {"ok": False, "message": "такого варианта в вопросе нет"}
            else:
                return {"ok": False, "message": "на этот вопрос нужен текстовый ответ в обзоре или в чате"}
        else:
            return {"ok": False, "message": f"неизвестное действие «{action}»"}
        if not self.pipeline.deliver_answer(job_id, qid, answer):
            return closed
        return {"ok": True, "message": ap.action_done_message(action, answer)}

    def panel_blocks(self) -> list[dict]:
        """Пространство «Монтаж» в дашборде (TZ 15, TAB-9): сводка, таблица задач с кнопками, согласие с вами, последний
        готовый ролик (видео, отчёт проверки). Читает файлы: звать через to_thread."""
        jobs = sorted(self._all_jobs(), key=lambda j: -float(j.data.get("created") or 0))
        states = [j.data.get("state") for j in jobs]
        stats = rm.agreement(self.store.verdicts())
        mode, why = rm.effective_mode(str(self.pipeline.setting("review_mode")), stats)
        blocks: list[dict] = [
            {"type": "stats", "title": "Сейчас", "items": [
                {"label": "В работе", "value": sum(1 for j in jobs if not j.finished)},
                {"label": "Ждут ответа", "value": states.count("waiting_user")},
                {"label": "Готово", "value": states.count("done")},
                {"label": "Режим приёмки", "value": mode}]},
        ]
        # задачи, которые ждут ответа: подпись этапа и кнопки именно этого этапа (TZ 15b)
        for j in jobs:
            q = j.data.get("question") or {}
            if j.data.get("state") == "waiting_user" and q and self.pipeline.waiting_question(j.job_id) == q.get("question_id"):
                blocks.extend(ap.waiting_blocks(j, q))
        rows = []
        for j in jobs[:PANEL_ROWS]:
            d = j.data
            actions = []
            if (j.plan_dir / "plan.json").is_file() and not j.finished:
                actions.append({"id": "open_studio", "label": "Studio", "args": {"job_id": j.job_id}})
            rows.append({"cells": [str(d.get("title") or j.job_id)[:120], ap.stage_name(d.get("stage")),
                                   f"{round(float(d.get('progress') or 0) * 100)} %", str(d.get("state") or "")],
                         "actions": actions[:3]})
        blocks.append({"type": "table", "title": "Ролики", "columns": ["Ролик", "Стадия", "Прогресс", "Состояние"],
                       "rows": rows} if rows else
                      {"type": "text", "title": "Ролики", "text": "Задач монтажа пока нет."})
        agree = rm.agreement_text(stats) + (f". {why}" if why else "")
        blocks.append({"type": "text", "title": "Согласие с вами", "text": agree[:900]})
        done = next((j for j in jobs if j.data.get("state") == "done" and (j.artifact("final") or j.artifact("preview"))),
                    None)
        if done is not None:
            name = os.path.basename(done.artifact("final") or done.artifact("preview") or "")
            blocks.append({"type": "video", "title": f"Ролик: {str(done.data.get('title'))[:80]}",
                           "artifact": {"job": done.job_id, "name": name}})
            report = done.artifact("qa_report_final")
            if report and os.path.isfile(report):
                text = Path(report).read_text(encoding="utf-8", errors="replace")[:REPORT_EXCERPT_MAX]
                if text.strip():
                    blocks.append({"type": "text", "title": "Отчёт проверки", "text": text})
        return blocks

    async def publish_panel(self) -> None:
        client = getattr(self.core, "client", None)
        if client is None:
            return
        try:
            blocks = await asyncio.to_thread(self.panel_blocks)
            await client.panel.set(blocks)
        except Exception as e:
            log.debug("пространство не обновлено: %s", e)

    # ================================================================================================ состояние для ядра (MON-33)

    async def publish_state(self) -> None:
        queued = [j for j in (self._jobs.get(i) for i in self.queue) if j is not None]
        cur = self._jobs.get(self.current) if self.current else None
        stats = rm.agreement(await asyncio.to_thread(self.store.verdicts))
        self.env.finish_if_done(busy=bool(queued) or cur is not None)
        summary = f"задач в очереди {len(queued)}"
        if cur is not None:
            summary += f", выполняется: {cur.data['title']} ({cur.stage}, {round(cur.data['progress'] * 100)} %)"
        else:
            summary += ", ничего не выполняется"
        if stats["decisions"] and len(summary) + len(rm.agreement_text(stats)) + 3 < 300:
            summary += "; " + rm.agreement_text(stats)
        mode, why = rm.effective_mode(str(self.pipeline.setting("review_mode")), stats)
        data = {"queue": [self.describe(j) for j in queued], "current": self.describe(cur) if cur else None,
                "review": {**stats, "text": rm.agreement_text(stats), "mode": mode, "mode_note": why},
                # папки для ссылок пространства «Монтаж» (path_from в манифесте, TZ 15)
                "status": self.env.status(cur.stage if cur is not None else None),       # ТЗ 18, 32: статус среды для ядра
                "jobs_dir": os.path.abspath(str(self.layout.jobs)),
                "output_dir": os.path.expandvars(str(self.pipeline.setting("output_dir")))}
        client = getattr(self.core, "client", None)
        if client is not None:
            try:
                await client.state(summary, data)
            except Exception as e:
                log.debug("state не отправлен: %s", e)

    async def _panel_loop(self) -> None:
        """Пространство «Монтаж» обновляется отдельным циклом, а не из пути состояния: публикация не задерживает стадии
        конвейера (раньше одна публикация из publish_state срывала ожидания в тестах)."""
        while True:
            await self.publish_panel()
            await asyncio.sleep(PANEL_EVERY_S)

    # ================================================================================================ справочник (MON-31)

    def kb_documents(self) -> tuple[str, list[dict]]:
        style = (self.layout.style).read_text(encoding="utf-8")
        craft = (self.layout.docs / "EDITING_CRAFT.md").read_text(encoding="utf-8")
        digest = hashlib.sha256((style + "\0" + craft).encode()).hexdigest()
        docs = [{"uri": "montage:style", "title": "Регламент монтажа Aperture Science", "text": style, "meta": {"kind": "style"}},
                {"uri": "montage:craft", "title": "Ремесло монтажа: история, ритм, склейка, звук", "text": craft, "meta": {"kind": "craft"}}]
        return digest, docs

    async def sync_kb(self) -> bool:
        """При первом подключении и при изменении регламента (по хешу) отправить его в `montage.style`. Хеш запоминается
        только после успеха; справочник ещё недоступен (ТЗ 04): False, повторит `kb_loop`."""
        digest, docs = await asyncio.to_thread(self.kb_documents)
        state_path = self.store.dir / "kb_state.json"
        try:
            known = json.loads(state_path.read_text(encoding="utf-8")).get("hash")
        except (OSError, ValueError):
            known = None
        if known == digest:
            return True
        try:
            await self.core.kb_ingest("montage.style", docs, replace=True, final=True)
        except (ToolError, asyncio.TimeoutError) as e:
            log.info("регламент не загружен в справочник: %s", getattr(e, "message", None) or type(e).__name__)
            return False
        await asyncio.to_thread(state_path.write_text, json.dumps({"hash": digest, "ts": time.time()}), "utf-8")
        return True

    async def kb_loop(self) -> None:
        while not await self.sync_kb():
            await asyncio.sleep(KB_RETRY_S)


__all__ = ["Manager", "FINISHED", "intake_mod"]
