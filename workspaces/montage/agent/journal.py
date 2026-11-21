"""Журнал действий агента в окружении «Монтаж» (docs/WORKSPACE_CONTRACT.md, раздел 7а): что она читает, пишет и запускает.

Записи приходят готовыми из `WorkspaceTools(on_action=...)` (инструменты `skills`, `files`, `run`) и от инструментов
окружения `work`, `site`, `resources` (через `audited`, формат тот же, `gits_sdk.action_record`). Запись идёт в журнал единицы
(`log.jsonl`, вид `action`), а если единицы нет (вызов до `work create`), в общий журнал окружения. Заодно обновляются поле
«сейчас» (`now` в job.json единицы) и `job.update` единицы: главная строка списка «Задачи» (stage и progress берутся из
плана действий агента: задача ядра `core.tasks`, окружение план не хранит).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from gits_sdk import action_record
from gits_sdk.workspace_tools import failure_text

log = logging.getLogger("montage.journal")

JOB_RE = re.compile(r"\bj-[0-9a-f]{8}\b")
NOW_MAX = 120
UPDATE_EVERY_S = 1.0                 # job.update единицы не чаще раза в секунду (кроме смены состояния)
PLAN_TTL_S = 3.0                     # core.tasks спрашивается не чаще раза в 3 секунды
PLAN_TIMEOUT_S = 5.0
DEFAULT_STAGE = "подготовка"
WAITING_TEXT = "ждёт ответа пользователя"
STAGE_OF_COMMAND = {"intake": "приём исходников", "transcribe": "расшифровка", "voiceover": "озвучка", "render": "сборка",
                    "still": "сборка", "compositions": "сборка", "check": "проверка", "final": "финал",
                    "scan": "поиск моментов", "moments": "подбор кусков", "speech": "подбор кусков", "find": "подбор кусков",
                    "peek": "подбор кусков", "clip": "подбор кусков"}
STEP_PREFIX = {"intake": "Приём", "scan": "Поиск моментов", "voiceover": "Озвучка", "check": "Проверка"}     # у остальных тексты шагов уже полные
DONE_WORDS = ("done", "completed", "complete", "finished")
ACTIVE_WORDS = ("active", "in_progress", "doing", "running", "current")


def hms(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def short(text: str) -> str:
    """Короткий текст «сейчас»: с маленькой буквы («читает work/src/Main.tsx»), не длиннее NOW_MAX."""
    text = " ".join(str(text or "").split())
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return text if len(text) <= NOW_MAX else text[:NOW_MAX - 1].rstrip() + "…"


# ---------------------------------------------------------------------------------------------------- план действий (core.tasks)

def step_status(raw) -> str:
    word = str(raw or "").strip().lower()
    if word in DONE_WORDS:
        return "done"
    if word == "skipped":
        return "skipped"
    return "active" if word in ACTIVE_WORDS else "pending"


def active_task(data) -> dict | None:
    """Активная задача среды из ответа `core.tasks` `{workspace, active, tasks}`; нет её: первая незавершённая."""
    if not isinstance(data, dict):
        return None
    tasks = [t for t in data.get("tasks") or [] if isinstance(t, dict)]
    active = data.get("active")
    aid = active.get("id") if isinstance(active, dict) else active
    for t in tasks:
        if aid and t.get("id") == aid:
            return t
    for t in tasks:
        if str(t.get("status") or "").lower() not in (*DONE_WORDS, "cancelled", "canceled"):
            return t
    return None


def plan_steps(task: dict | None) -> list[dict]:
    out = []
    for s in (task or {}).get("steps") or []:
        if isinstance(s, dict):
            out.append({"text": str(s.get("text") or ""), "status": step_status(s.get("status"))})
        elif isinstance(s, str):
            out.append({"text": s, "status": "pending"})
    return out


def plan_state(task: dict | None) -> dict | None:
    """Сводка плана: `done`, `total`, `n` (номер текущего шага с 1) и `text` (первый незавершённый шаг). Нет шагов: None."""
    steps = plan_steps(task)
    if not steps:
        return None
    done = sum(1 for s in steps if s["status"] == "done")
    cur = next((i for i, s in enumerate(steps) if s["status"] in ("active", "pending")), None)
    return {"done": done, "total": len(steps), "n": None if cur is None else cur + 1,
            "text": None if cur is None else steps[cur]["text"], "goal": str((task or {}).get("goal") or "")}


class Journal:
    def __init__(self, manager):
        self.m = manager
        self.active_unit: str | None = None
        self.cached_plan: dict | None = None             # последний ответ core.tasks (для панели и сайта)
        self.plan_error: str | None = None
        self._plan_at = 0.0
        self._sent: dict[str, float] = {}
        self._seen: dict[str, tuple] = {}

    # ------------------------------------------------------------------------------------------------ план агента

    async def plan(self, *, fresh: bool = False) -> dict | None:
        """Ответ `core.tasks` текущей среды с кэшем на PLAN_TTL_S. Недоступно: прежний ответ (или None), причина в `plan_error`."""
        now = time.monotonic()
        if not fresh and self._plan_at and now - self._plan_at < PLAN_TTL_S:
            return self.cached_plan
        self._plan_at = now
        fn = getattr(self.m.core, "tasks", None)
        if fn is None:
            self.plan_error = "ядро не даёт задач"
            return self.cached_plan
        try:
            data = await asyncio.wait_for(fn(), PLAN_TIMEOUT_S)
            self.cached_plan = data if isinstance(data, dict) else None
            self.plan_error = None
        except Exception as e:
            self.plan_error = f"{type(e).__name__}: {str(e)[:120]}"
        return self.cached_plan

    # ------------------------------------------------------------------------------------------------ единицы

    def unit(self, job_id: str | None):
        if not job_id:
            return None
        job = self.m._jobs.get(job_id) or self.m.store.load(job_id)
        if job is not None:
            self.m._jobs.setdefault(job_id, job)
        return job

    @staticmethod
    def stage_from(rec: dict) -> str | None:
        """Короткое имя шага по записи (по команде и по тому, какой файл писала)."""
        if not rec.get("ok"):
            return None
        tool, op, target = rec.get("tool"), rec.get("op"), str(rec.get("target") or "")
        if tool == "run":
            return STAGE_OF_COMMAND.get(target.split(" ")[0]) if op in ("run", "background") else STAGE_OF_COMMAND.get(op)
        if tool == "files" and op in ("write", "replace"):
            if "/plan/plan.json" in target:
                return "план"
            if "/plan/story" in target:
                return "история"
            if "/plan/idea" in target:
                return "идея"
        if tool == "skills" and op == "read" and target.split("/")[0] == "montage-regulation":
            return "чтение регламента"
        return None

    # ------------------------------------------------------------------------------------------------ запись действий

    async def record(self, rec: dict, job_id: str | None = None, *, now: str | None = None, force: bool = False) -> None:
        """Записать действие (обработчик `on_action`). Сбой записи инструмент не ломает."""
        try:
            await self._record(rec, job_id, now, force)
        except Exception as e:
            log.warning("запись в журнал действий не удалась: %s", e)

    async def _record(self, rec: dict, job_id: str | None, now: str | None, force: bool) -> None:
        job = self.unit(job_id or self._id_in(rec))
        if job is None and self.active_unit:
            cur = self.unit(self.active_unit)
            job = cur if cur is not None and cur.open else None
        if job is None:
            await asyncio.to_thread(self.m.store.log_env, "action", **rec)
            return
        self.active_unit = job.job_id
        await asyncio.to_thread(self.m.store.log, job, "action", **rec)
        stage = self.stage_from(rec)
        if stage:
            force = force or stage != job.data.get("step_name")             # смена шага уходит сразу, без ожидания секунды
            job.data["step_name"] = stage
        await self.touch(job, now or rec.get("text") or "", rec.get("ts") or time.time(), force=force)

    @staticmethod
    def _id_in(rec: dict) -> str | None:
        found = JOB_RE.search(str(rec.get("target") or ""))
        return found.group(0) if found else None

    async def touch(self, job, text: str, ts: float | None = None, *, force: bool = False) -> None:
        """Поле «сейчас» единицы и её job.update."""
        job.data["now"] = {"text": short(text), "at": round(ts or time.time(), 3)}
        await asyncio.to_thread(self.m.store.save, job)
        await self.send_unit(job, force=force)

    async def send_unit(self, job, *, force: bool = False) -> None:
        """job.update единицы: название ролика, состояние, шаг (stage), прогресс по плану, текст «сейчас»."""
        mono = time.monotonic()
        last = self._sent.get(job.job_id)
        if not force and last is not None and mono - last < UPDATE_EVERY_S:
            return
        self._sent[job.job_id] = mono
        state = job.state
        task = active_task(await self.plan())
        summary = plan_state(task)
        stage = job.data.get("step_name") or DEFAULT_STAGE
        if summary and summary["text"]:
            stage = summary["text"]
        fields: dict = {"title": job.data.get("title") or job.job_id, "text": (job.data.get("now") or {}).get("text")}
        if state == "waiting_user":
            fields.update(state="running", stage=WAITING_TEXT)               # waiting_user в протоколе требует вопроса ядра
        elif state == "done":
            fields.update(state="done", stage=stage, progress=1.0)
        elif state == "failed":
            err = job.data.get("error")
            if not isinstance(err, dict) or not err.get("message"):
                err = {"code": "internal", "message": "единица завершилась с ошибкой"}
            fields.update(state="failed", stage=stage, error=err)
        elif state == "cancelled":
            fields.update(state="cancelled", stage=stage)
        else:
            fields.update(state="running", stage=stage)
        if state not in ("done",) and summary:
            fields["progress"] = round(summary["done"] / summary["total"], 3)
        try:
            await self.m.core.job_update(job.job_id, **{k: v for k, v in fields.items() if v is not None})
        except Exception as e:
            log.debug("job.update единицы не отправлен: %s", e)

    # ------------------------------------------------------------------------------------------------ инструменты окружения

    async def audited(self, tool: str, args, call):
        """Вызвать обработчик `work`, `site` или `resources` и записать действие (так же, как SDK для `skills`, `files`, `run`)."""
        args = args if isinstance(args, dict) else {}
        try:
            out = await call(args)
        except Exception as e:
            op = str(args.get("op") or "")
            what = f"выполнить {tool}" + (f" {op}" if op else "")
            jid = args.get("id") if isinstance(args.get("id"), str) else None
            await self.record(action_record(tool, op, jid or "", False, f"{getattr(e, 'code', type(e).__name__)}: {getattr(e, 'message', e)}",
                                            failure_text(e, what)), jid if jid and JOB_RE.fullmatch(jid) else None)
            raise
        try:
            rec, jid, now = self._described(tool, args, out)
        except Exception as e:
            log.warning("описание действия %s: %s", tool, e)
            return out
        await self.record(rec, jid, now=now, force=(tool == "work"))
        return out

    @staticmethod
    def _described(tool: str, args: dict, out) -> tuple[dict, str | None, str | None]:
        out = out if isinstance(out, dict) else {}
        op = str(args.get("op") or "")
        jid = out.get("id") if isinstance(out.get("id"), str) else (args.get("id") if isinstance(args.get("id"), str) else None)
        jid = jid if jid and JOB_RE.fullmatch(jid) else None
        now = None
        if tool == "work":
            if op == "create":
                text = f"Создала единицу {jid} «{out.get('title') or ''}»"
            elif op == "status" and args.get("set"):
                word = WAITING_TEXT if args.get("set") == "waiting_user" else "работает"
                text, now = f"Помечает {jid}: {word}", word
            elif op == "status":
                text = f"Смотрит состояние {jid}"
            elif op == "cancel":
                text = f"Отменила единицу {jid}"
            else:
                text = "Смотрит список единиц"
        elif tool == "site":
            text = f"Берёт ссылку на страницу {jid}" if jid else "Берёт адрес сайта монтажа"
        else:
            text = "Смотрит ресурсы монтажа"
        return action_record(tool, op, jid or "", True, None, text), jid, now

    # ------------------------------------------------------------------------------------------------ шаги долгих команд

    @staticmethod
    def step_text(command: str, text: str) -> str:
        prefix = STEP_PREFIX.get(command)
        return f"{prefix}: {text}" if prefix and not text.startswith(prefix) else text

    async def step(self, job, run_id: str, command: str, frac: float, text: str) -> str:
        """Шаг долгой команды: запись в журнал и «сейчас» (не на каждый процент: по десятой доле и при смене шага).
        Возвращает полный текст шага (для job.update команды)."""
        full = self.step_text(command, text)
        key, bucket = re.sub(r"\d+ %", "", full), int(max(0.0, min(1.0, frac)) * 10)
        last = self._seen.get(run_id)
        if last is not None and (last[0] == full or (last[1] == key and last[2] == bucket)):
            return full
        self._seen[run_id] = (full, key, bucket)
        await self.record(action_record("run", command, f"{command} {job.job_id}", True, None, full), job.job_id)
        return full

    async def finished(self, run, job, status: str, text: str, error: dict | None) -> None:
        """Итог операции: запись «готово» или «не удалось», строка единицы в списке задач обновляется сразу."""
        self._seen.pop(run.run_id, None)
        title = {"intake": "Приём исходников", "transcribe": "Расшифровка", "voiceover": "Озвучка", "render": "Рендер черновика",
                 "still": "Кадр", "compositions": "Композиции", "check": "Проверка", "final": "Финал",
                 "scan": "Поиск моментов", "moments": "Список моментов", "speech": "Расшифровка речи", "find": "Поиск по речи",
                 "peek": "Кадры момента", "clip": "Клип"}.get(run.command, run.command)
        word = {"done": "готово", "failed": "не удалось", "cancelled": "отменено"}.get(status, status)
        detail = text or (error or {}).get("message") or ""
        rec = action_record("run", run.command, f"{run.command} {job.job_id}", status == "done",
                            None if status == "done" else (error or {}).get("message") or status,
                            f"{title}: {word}" + (f", {detail}" if detail else ""))
        await self.record(rec, job.job_id, force=True)
