"""Конвейер монтажа (ТЗ 20, раздел 6): стадии одной задачи от intake до done.

Стадии идут по порядку (`jobs.STAGES`); назад можно только на `story` и `plan` из `plan_approval` и на `build` из `review`.
Каждая стадия идемпотентна: её выход перезаписывается, поэтому после падения процесса задача продолжается с начала текущей
стадии (MON-8). Сбой стадии даёт одну повторную попытку, затем `failed` (MON-9). Зависимости (ядро, агент Claude, Remotion,
контроль качества) передаются в конструктор: тесты подставляют заглушки.
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

import intake as intake_mod
import media
import qa as qa_mod
import review_mode as rm
import srt as srt_mod
import transcribe
from agent_tools import JobEnv, load_voice_cache, synthesize_to
from claude_runner import AgentRunner, ClaudeLimitError, StageFailed, StageResult
from core_api import CoreAPI
from gits_sdk import ToolError
from jobs import PAUSED_STAGE, STAGE_PROGRESS, STAGES, Job, JobStore
from layout import Layout
from manifest import MANIFEST, normalize_voiceover
from plan_schema import PlanContext, story_beat_ids, validate_plan, validate_story
from render import RenderError, Renderer
from workspace import custom_components, prepare_work, sync_media

log = logging.getLogger("montage.pipeline")

DEFAULTS = {k: v["default"] for k, v in MANIFEST["settings_schema"].items()}
APPROVE = "утвердить"
REDO_PLAN = "переделать план"
REDO_STORY = "переделать историю"
READY = "готово"
EDITS = "правки"
QUESTION_TIMEOUT_S = 86400
VALIDATION_ATTEMPTS = 3
BUILD_ATTEMPTS = 3
PROGRESS_SAVE_S = 2.0
NON_RETRYABLE = ("bad_request", "unavailable", "denied", "unknown")
VOICE_GAP_MS = 300                      # пауза между репликами закадра (регламент §5: 250-400 мс)
FORMAT_OF_PLATFORM = {"shorts": "vertical", "reels": "vertical", "telegram": "vertical", "youtube": "horizontal"}
SIZES = {"vertical": (1080, 1920), "horizontal": (1920, 1080), "square": (1080, 1080)}


class StageError(Exception):
    """Стадия не удалась. `code` из ТЗ 01 (MOD-9), `message` по-русски: что и почему, без трассировки."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Safe(dict):
    def __missing__(self, key):
        return ""


def classify_choice(answer, options: dict[str, tuple[str, ...]]) -> str | None:
    """Ответ пользователя (вариант или свободный текст голосом) -> ключ варианта по подстрокам; None, если неясно."""
    if answer is None:
        return None
    text = str(answer).strip().lower()
    for key, needles in options.items():
        if text == key or any(n in text for n in needles):
            return key
    return None


def needs_clarification(request: dict, info: dict) -> list[str]:
    """Чего не хватает для начала работы (пусто: стадия clarify пропускается без вызова агента)."""
    reasons = []
    kind = info.get("source_kind") or request.get("source_kind")
    if not str(request.get("brief") or "").strip():
        reasons.append("бриф пуст: непонятно, что за ролик")
    if not request.get("platform_explicit"):
        reasons.append("площадка не названа (shorts, reels, telegram, youtube или none)")
    videos = [f for f in info.get("files", []) if f.get("kind") == "video"]
    total_s = sum(f.get("duration_ms", 0) for f in videos) / 1000
    if kind not in ("rimworld_run", "narrated") and total_s > 180 and not request.get("duration_s"):
        reasons.append(f"исходного видео {total_s / 60:.0f} мин, а целевая длительность не названа")
    if kind == "talking_head" and len(videos) > 1:
        reasons.append(f"видеофайлов несколько ({len(videos)}): непонятно, какой из дублей брать")
    return reasons


class Pipeline:
    def __init__(self, store: JobStore, core: CoreAPI, runner: AgentRunner, layout: Layout, renderer: Renderer,
                 qa: object | None = None):
        self.store = store
        self.core = core
        self.runner = runner
        self.layout = layout
        self.renderer = renderer
        self.qa = qa if qa is not None else qa_mod.QAService(layout, renderer)
        self._waiters: dict[str, tuple[str, asyncio.Future]] = {}
        self._awake = asyncio.Event()
        self._awake.set()
        self._last_saved: dict[str, float] = {}
        self.state_hook = None                         # async (): менеджер обновляет `state` для ядра при смене стадии или состояния
        self.handlers = {
            "intake": self._stage_intake, "clarify": self._stage_clarify, "transcribe": self._stage_transcribe,
            "story": self._stage_story, "plan": self._stage_plan, "plan_approval": self._stage_plan_approval,
            "voiceover": self._stage_voiceover, "build": self._stage_build, "preview": self._stage_preview,
            "review": self._stage_review, "final": self._stage_final,
        }

    # ================================================================================================ настройки и состояние

    def setting(self, name: str):
        return {**DEFAULTS, **(self.core.settings or {})}.get(name, DEFAULTS.get(name))

    def set_sleeping(self, sleeping: bool) -> None:
        """Тема battery (MON-10): `sleeping: true` ставит задачи на паузу перед следующим вызовом Claude."""
        if sleeping:
            self._awake.clear()
        else:
            self._awake.set()

    async def _save(self, job: Job) -> None:
        await asyncio.to_thread(self.store.save, job)

    async def _log(self, job: Job, kind: str, **fields) -> None:
        await asyncio.to_thread(self.store.log, job, kind, **fields)

    async def _set(self, job: Job, *, state: str | None = None, stage: str | None = None, progress: float | None = None,
                   text: str | None = None, question: dict | None = None, error: dict | None = None) -> None:
        d = job.data
        if state is not None:
            d["state"] = state
        if stage is not None:
            d["stage"] = stage
        if progress is not None:
            d["progress"] = round(max(0.0, min(1.0, progress)), 3)
        d["question"] = question if d["state"] == "waiting_user" else None
        d["error"] = error if d["state"] == "failed" else None
        await self._save(job)
        fields: dict = {"title": d["title"], "state": d["state"], "stage": d["stage"], "progress": d["progress"],
                        "artifacts": d.get("artifacts") or []}
        if text:
            fields["text"] = text
        if d["state"] == "waiting_user":
            fields["question"] = d["question"]
        if d["state"] == "failed":
            fields["error"] = d["error"]
        if not fields["artifacts"]:
            fields.pop("artifacts")
        await self.core.job_update(job.job_id, **fields)
        if (state is not None or stage is not None) and self.state_hook is not None:
            await self.state_hook()

    async def _progress(self, job: Job, stage: str, frac: float, text: str | None = None) -> None:
        """Прогресс внутри стадии: доля `frac` пути до следующей стадии; сохраняется не чаще раза в 2 секунды (MON-7)."""
        nxt = STAGES[min(STAGES.index(stage) + 1, len(STAGES) - 1)] if stage in STAGES else "done"
        base = STAGE_PROGRESS.get(stage, 0.0)
        value = base + max(0.0, min(1.0, frac)) * (STAGE_PROGRESS[nxt] - base)
        now = time.monotonic()
        if now - self._last_saved.get(job.job_id, 0.0) < PROGRESS_SAVE_S:
            return
        self._last_saved[job.job_id] = now
        await self._set(job, progress=value, text=text)

    # ================================================================================================ вопросы пользователю

    def deliver_answer(self, job_id: str, question_id: str, answer) -> bool:
        waiter = self._waiters.get(job_id)
        if waiter is None or waiter[0] != question_id or waiter[1].done():
            return False
        waiter[1].set_result(answer)
        return True

    def preset_answer(self, job_id: str, answer) -> bool:
        """Ответ в обход ядра (инструмент `revise` во время вопроса «готово / правки»)."""
        waiter = self._waiters.get(job_id)
        if waiter is None or waiter[1].done():
            return False
        waiter[1].set_result(answer)
        return True

    def waiting_question(self, job_id: str) -> str | None:
        waiter = self._waiters.get(job_id)
        return waiter[0] if waiter else None

    async def ask(self, job: Job, question: str, kind: str, options: list[str] | None, *, text: str,
                  artifacts: list[dict] | None = None, context: dict | None = None):
        """Вопрос через задачу (waiting_user, MOD-20). Ответа может не быть (тайм-аут ядра): тогда вопрос задаётся снова,
        с новым question_id. Возвращает ответ; ответ пишется в job.json."""
        while True:
            qid = f"q-{len(job.data['answers']) + 1}"
            q = {"question_id": qid, "kind": kind, "question": question, "timeout_s": QUESTION_TIMEOUT_S}
            if options:
                q["options"] = list(options)
            if context:
                q["context"] = context
            fut = asyncio.get_running_loop().create_future()
            self._waiters[job.job_id] = (qid, fut)
            if artifacts:
                for a in artifacts:
                    job.add_artifact(a["path"], a["kind"])
            await self._log(job, "question", question_id=qid, qkind=kind, stage=job.stage)
            await self._set(job, state="waiting_user", question=q, text=text)
            try:
                answer = await fut
            finally:
                self._waiters.pop(job.job_id, None)
            if answer is None:
                continue
            job.data["answers"].append({"question_id": qid, "answer": answer, "ts": time.time()})
            await self._log(job, "answer", question_id=qid, answer=str(answer)[:200])
            await self._set(job, state="running", text=None)
            return answer

    async def _agent_ask(self, job: Job, question: str, kind: str, options: list[str] | None):
        """Вопрос от агента (`ask_user`). После перезапуска ответ на вопрос, заданный до падения, берётся из `carry_answer`."""
        carry = job.data.pop("carry_answer", None)
        if carry is not None:
            await self._save(job)
            return carry
        return await self.ask(job, question, kind, options, text=f"Монтажёру нужен ответ: {question}")

    async def _resume_question(self, job: Job) -> None:
        """Восстановление (MON-8): процесс упал во время вопроса. На `plan_approval` и `review` вопрос зададут сами стадии;
        вопрос агента (clarify, build) задаётся заново с тем же текстом, ответ сохраняется для первого `ask_user`."""
        q = job.data.get("question")
        if job.state != "waiting_user" or not q or job.stage not in ("clarify", "build"):
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters[job.job_id] = (q["question_id"], fut)
        await self._set(job, state="waiting_user", question=q, text=f"Монтажёру нужен ответ: {q['question']}")
        try:
            answer = await fut
        finally:
            self._waiters.pop(job.job_id, None)
        if answer is not None:
            job.data["answers"].append({"question_id": q["question_id"], "answer": answer, "ts": time.time()})
            job.data["carry_answer"] = answer
        await self._set(job, state="running")

    # ================================================================================================ батарейка и агент

    async def _wait_awake(self, job: Job) -> None:
        """MON-10: пока Claude спит (тема battery), задача ждёт в `queued` со стадией `paused_battery`."""
        if self._awake.is_set() and not self.core.sleeping:
            return
        if self.core.sleeping:
            self._awake.clear()
        if self._awake.is_set():
            return
        prev = job.stage
        job.data["paused_from"] = prev
        await self._log(job, "pause", reason="battery", stage=prev)
        await self._set(job, state="queued", stage=PAUSED_STAGE, text="пауза: нет заряда Claude")
        await self._awake.wait()
        job.data.pop("paused_from", None)
        await self._log(job, "resume", reason="battery", stage=prev)
        await self._set(job, state="running", stage=prev, text="заряд Claude вернулся, продолжаю")

    def _env(self, job: Job, stage: str) -> JobEnv:
        async def ask(q, kind, options):
            return await self._agent_ask(job, q, kind, options)

        async def progress(frac, text):
            await self._progress(job, job.stage, frac, text)
        return JobEnv(job=job, store=self.store, core=self.core, layout=self.layout, stage=stage, ask=ask, progress=progress)

    def _model(self, which: str) -> str:
        return str(self.setting("plan_model" if which == "plan" else "build_model"))

    async def _agent(self, job: Job, stage: str, prompt: str, *, model: str, readonly: bool = False,
                     fresh: bool = False) -> StageResult:
        await self._wait_awake(job)
        env = self._env(job, stage)
        res = await self.runner.run(env, stage, prompt, model=model, max_turns=int(self.setting("max_turns_stage")),
                                    readonly=readonly, fresh=fresh)
        claude = job.data.setdefault("claude", {"session_id": None, "tokens": 0})
        claude["tokens"] = int(claude.get("tokens") or 0) + res.tokens
        if res.session_id and not readonly and not fresh:
            claude["session_id"] = res.session_id
        await self._save(job)
        return res

    async def _agent_checked(self, job: Job, stage: str, prompt: str, *, model: str, check, attempts: int = VALIDATION_ATTEMPTS,
                             what: str = "результат") -> StageResult:
        """Вызвать агента и проверить результат; ошибки проверки возвращаются ему на исправление (до `attempts` попыток)."""
        res = await self._agent(job, stage, prompt, model=model)
        for attempt in range(1, attempts + 1):
            problems = await check(attempt == attempts)
            if not problems:
                return res
            await self._log(job, "validation", stage=stage, attempt=attempt, problems=problems[:12])
            if attempt == attempts:
                raise StageError("bad_request", f"{what} не прошёл проверку после {attempts} попыток: " + "; ".join(problems[:4]))
            more = "\n".join(f"- {p}" for p in problems[:25])
            res = await self._agent(job, stage, f"Проверка модуля нашла ошибки в {what}. Исправь файлы и ответь одним предложением.\n\n{more}",
                                    model=model)
        return res

    def _prompt(self, name: str, **kw) -> str:
        text = (self.layout.prompts / f"stage_{name}.md").read_text(encoding="utf-8")
        return text.format_map(_Safe({k: v for k, v in kw.items()}))

    # ================================================================================================ факты о задаче

    def _request(self, job: Job) -> dict:
        return job.data["request"]

    def _info(self, job: Job) -> dict:
        try:
            return json.loads((job.sources / "info.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"files": []}

    def _platform(self, job: Job) -> str:
        return self._request(job).get("platform") or "none"

    def _read_json(self, path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _facts(self, job: Job) -> str:
        r = self._request(job)
        info = self._info(job)
        fmt = r.get("format") or "по исходнику"
        size = SIZES.get(fmt)
        lines = [f"Бриф пользователя: «{r.get('brief', '').strip()}»",
                 f"Вид исходников: {r.get('source_kind') or info.get('source_kind')}. Площадка: {r.get('platform') or 'none'}. "
                 f"Формат: {fmt}" + (f" ({size[0]}×{size[1]}, 30 кадров в секунду)" if size else "") + ". "
                 f"Целевая длительность: {str(r['duration_s']) + ' с' if r.get('duration_s') else 'по материалу и брифу'}. "
                 f"Закадр: {r.get('voiceover') or 'none'}."]
        files = info.get("files") or []
        if files:
            lines.append("Исходники (в плане путь `sources/<имя>`):")
            for f in files:
                if f.get("kind") == "video":
                    lines.append(f"- {f['name']}: видео {f['duration_ms'] / 1000:.1f} с, {f['width']}×{f['height']}, "
                                 f"{'со звуком' if f.get('has_audio') else 'без звука'}")
                elif f.get("kind") == "audio":
                    lines.append(f"- {f['name']}: звук {f.get('duration_ms', 0) / 1000:.1f} с")
                else:
                    lines.append(f"- {f['name']}: {f['kind']}")
        else:
            lines.append("Исходных файлов нет: ролик целиком на графике и закадре.")
        if job.data.get("answers"):
            lines.append("Ответы пользователя: " + "; ".join(str(a["answer"]) for a in job.data["answers"]))
        return "\n".join(lines)

    def _material(self, job: Job) -> str:
        info = self._info(job)
        parts = []
        scenario = self._read_json(job.sources / "scenario.json")
        if scenario:
            scenes = scenario.get("scenes") or []
            parts.append(f"Сценарий забега RimWorld (`../sources/scenario.json`): колония «{scenario.get('colony')}», исход "
                         f"«{scenario.get('outcome')}», длительность записи {scenario.get('duration')} с, сцен {len(scenes)}. Сцены:")
            for s in scenes[:30]:
                said = "; ".join(str(x) for x in (s.get("said") or [])[:2])
                parts.append(f"- сцена {s.get('id')} {s.get('kind')}: {s.get('start', 0):.0f}–{s.get('end', 0):.0f} с записи, пик "
                             f"{s.get('peak', 0):.0f} с, важность {s.get('importance')}, «{s.get('label')}»" + (f", она говорила: «{said}»" if said else ""))
            parts.append("Реплики `said` — материал для закадра: перепиши в её характере, не цитируй дословно, если длинные. "
                         "Время в сценарии идёт по записи (секунды); `segments[].in_ms/out_ms` в миллисекундах записи.")
        words = self._read_json(job.transcript / "words.json") or []
        meta = self._read_json(job.transcript / "meta.json") or {}
        if words:
            sample = " ".join(w["text"] for w in words[:60])
            parts.append(f"Слова для субтитров: {len(words)} (происхождение: {meta.get('origin', 'расшифровка')}); они уже на шкале "
                         f"исходного видео. Начало: «{sample[:300]}». Выборка по отрезку: инструмент `transcript_words`. В плане "
                         "в вертикальном коротком ролике субтитры по ним — `captions.source: transcript` (или `both`, если есть и закадр); в "
                         "горизонтальном `captions.enabled: false` (конвейер выдаст .srt).")
        else:
            parts.append("Слов расшифровки нет: субтитры только по закадру (`captions.source: voiceover`) или без них.")
        if not info.get("files"):
            parts.append("Ролик собирается из графики Aperture (карточки, баннеры, журнал) и закадра; фон `chamberDark` с сеткой.")
        return "\n".join(parts)

    def _references(self) -> str:
        notes = []
        for path in sorted(self.layout.references.glob("*")):
            if path.suffix.lower() in (".md", ".txt") and path.is_file():
                try:
                    notes.append(f"### {path.name}\n" + path.read_text(encoding="utf-8")[:3000])
                except OSError:
                    continue
        return "\n\n".join(notes)[:8000] or "(папка references/ пуста: эталонов нет)"

    # ================================================================================================ запуск задачи

    async def run_job(self, job: Job) -> None:
        """Выполнить задачу с её текущей стадии до `done`. Отмену задачи (CancelledError) пробрасывает наружу."""
        try:
            if job.stage == PAUSED_STAGE:
                job.data["stage"] = job.data.pop("paused_from", "intake")
            if job.stage not in STAGES:
                job.data["stage"] = "intake"
            await self._set(job, state="running", text="продолжаю" if job.data.get("answers") or job.stage != "intake" else None)
            await self._resume_question(job)
            while job.stage != "done":
                stage = job.stage
                await self._log(job, "stage", stage=stage, event="start")
                nxt = await self._attempt(job, stage)
                await self._log(job, "stage", stage=stage, event="end", next=nxt)
                job.data["stage"] = nxt
                await self._set(job, stage=nxt, progress=STAGE_PROGRESS.get(nxt, job.data["progress"]))
            await self._complete(job)
        except asyncio.CancelledError:
            raise
        except StageError as e:
            await self._fail(job, e.code, e.message)
        except Exception as e:
            log.exception("задача %s упала", job.job_id)
            await self._fail(job, "internal", f"сбой на стадии {job.stage}: {type(e).__name__}: {str(e)[:200]}")

    async def _attempt(self, job: Job, stage: str) -> str:
        handler = self.handlers[stage]
        for attempt in (1, 2):
            try:
                return await handler(job)
            except asyncio.CancelledError:
                raise
            except ClaudeLimitError as e:
                raise StageError("unavailable", f"стадия {stage}: лимит Claude исчерпан ({str(e)[:120]})")
            except StageError as e:
                if e.code in NON_RETRYABLE or attempt == 2:
                    raise StageError(e.code, f"стадия {stage}: {e.message}")
                err = e.message
            except ToolError as e:
                if e.code in NON_RETRYABLE or attempt == 2:
                    raise StageError(e.code, f"стадия {stage}: {e.message}")
                err = e.message
            except (StageFailed, media.MediaError, RenderError, OSError) as e:
                if attempt == 2:
                    raise StageError("internal", f"стадия {stage}: {str(e)[:300]}")
                err = str(e)
            await self._log(job, "retry", stage=stage, error=err[:300])
            await self._set(job, text=f"стадия {stage} не удалась, пробую ещё раз")
        raise StageError("internal", f"стадия {stage}: не удалась")

    async def _fail(self, job: Job, code: str, message: str) -> None:
        await self._log(job, "failed", code=code, message=message)
        await self._set(job, state="failed", error={"code": code, "message": message}, text=message)
        await self._emit(job, "montage.failed", f"Монтаж «{job.data['title']}» не удался: {message}",
                         data={"job_id": job.job_id, "code": code}, urgency_hint=3)

    async def _emit(self, job: Job, kind: str, text: str, **fields) -> None:
        try:
            await self.core.event(kind, text, dedupe_key=f"{kind}:{job.job_id}", **fields)
        except Exception as e:
            log.info("событие %s не отправлено: %s", kind, e)

    async def mark_cancelled(self, job: Job) -> None:
        await self._log(job, "cancelled")
        await self._set(job, state="cancelled", text="задача отменена")

    # ================================================================================================ стадии

    async def _stage_intake(self, job: Job) -> str:
        r = self._request(job)

        async def progress(frac, text):
            await self._progress(job, "intake", frac, text)
        info = await intake_mod.run_intake(job.folder, r, progress)
        r["source_kind"] = info["source_kind"]
        kind = info["source_kind"]
        if not r.get("voiceover"):
            r["voiceover"] = "character" if kind in ("narrated", "rimworld_run") else "keep"
        if not r.get("format"):
            r["format"] = FORMAT_OF_PLATFORM.get(r.get("platform") or "none") or self._format_from_sources(info)
        await asyncio.to_thread(self._ensure_persona_file, job)
        await asyncio.to_thread(prepare_work, job.folder, self.layout)
        await self._save(job)
        n = len(info["files"])
        await self._set(job, text=f"Исходники проверены: файлов {n}, вид «{kind}», площадка {r.get('platform') or 'none'}")
        return "clarify"

    @staticmethod
    def _format_from_sources(info: dict) -> str:
        for f in info.get("files", []):
            if f.get("kind") == "video":
                return "vertical" if f["height"] > f["width"] else "square" if f["height"] == f["width"] else "horizontal"
        return "horizontal"

    def _ensure_persona_file(self, job: Job) -> None:
        path = job.folder / "persona.json"
        if not path.is_file():
            path.write_text(json.dumps(job.data.get("persona") or {}, ensure_ascii=False), encoding="utf-8")

    async def _stage_clarify(self, job: Job) -> str:
        r = self._request(job)
        reasons = needs_clarification(r, self._info(job))
        if not reasons:
            await self._log(job, "clarify_skipped")
            return "transcribe"
        await self._set(job, text="Уточняю детали задачи")
        out = job.plan_dir / "clarify.json"
        out.unlink(missing_ok=True)
        prompt = self._prompt("clarify", facts=self._facts(job), reasons="\n".join(f"- {x}" for x in reasons))
        await self._agent(job, "clarify", prompt, model=self._model("build"))
        data = self._read_json(out) or {}
        if data.get("platform") in ("shorts", "reels", "telegram", "youtube", "none"):
            r["platform"] = data["platform"]
            r["platform_explicit"] = True
        if data.get("format") in SIZES:
            r["format"] = data["format"]
        elif r.get("platform") in FORMAT_OF_PLATFORM:
            r["format"] = FORMAT_OF_PLATFORM[r["platform"]]
        if isinstance(data.get("duration_s"), int) and 3 <= data["duration_s"] <= 3600:
            r["duration_s"] = data["duration_s"]
        if normalize_voiceover(data.get("voiceover")) in ("none", "character", "keep"):
            r["voiceover"] = normalize_voiceover(data["voiceover"])
        if data.get("notes"):
            job.data["notes_clarify"] = str(data["notes"])[:500]
        await self._save(job)
        return "transcribe"

    async def _stage_transcribe(self, job: Job) -> str:
        r = self._request(job)
        kind = r.get("source_kind")
        words: list[dict] = []
        meta: dict = {"origin": "none", "words": 0}
        if kind == "rimworld_run":
            words, meta = await self._words_from_speech_log(job)
        elif kind == "talking_head":
            transcribe.transcribe_live()                          # MON-29 отложено: unavailable
        await asyncio.to_thread((job.transcript / "words.json").write_text, json.dumps(words, ensure_ascii=False, indent=1), "utf-8")
        await asyncio.to_thread((job.transcript / "meta.json").write_text, json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        await asyncio.to_thread(sync_media, job.folder)
        await self._set(job, text=f"Слов для субтитров: {len(words)}" if words else "Расшифровка не нужна: речи людей в исходниках нет")
        return "story"

    async def _words_from_speech_log(self, job: Job) -> tuple[list[dict], dict]:
        """MON-26a: субтитры из журнала речи, без распознавания."""
        log_path = job.sources / "speech_log.json"
        if not log_path.is_file():
            return [], {"origin": "speech_log", "words": 0, "note": "журнала речи нет (speech_log.json)"}
        lines = await asyncio.to_thread(transcribe.load_speech_log, log_path)
        rec = self._read_json(job.sources / "recording.json") or {}
        info = self._info(job)
        video = next((f for f in info.get("files", []) if f.get("kind") == "video"), None)
        duration = float(rec.get("duration_s") or (video["duration_ms"] / 1000 if video else 0))
        started = rec.get("started_at")
        if started is None:
            return [], {"origin": "speech_log", "words": 0, "note": "в recording.json нет started_at: время реплик не привязать к видео"}
        parts = []
        for p in rec.get("audio_parts") or []:
            local = job.sources / Path(str(p.get("path") or "")).name
            f = local if local.is_file() else Path(str(p.get("path") or ""))
            parts.append((f, float(p["started_at"]) - float(started), float(p.get("seconds") or 0)))
        if not parts:
            audio = [f for f in info.get("files", []) if f.get("kind") == "audio"]
            parts = [(job.sources / a["name"], 0.0, a.get("duration_ms", 0) / 1000) for a in audio[:1]]
        audible = transcribe.make_audible(parts) if parts else None
        if audible is None:
            return [], {"origin": "speech_log", "words": 0, "note": "звука записи нет: реплик в звуке нет, субтитры не ставятся",
                        "utterances": len(lines)}
        multiple = len([f for f in info.get("files", []) if f.get("kind") == "video"]) > 1
        words, meta = await transcribe.words_from_speech_log(lines, float(started), duration, audible,
                                                             source=video["path"] if (multiple and video) else None)
        return words, meta

    # ---------------------------------------------------------------------------------------------- story / plan

    def _notes_block(self, job: Job, key: str) -> str:
        notes = job.data.get(key) or []
        extra = job.data.get("notes_clarify")
        lines = []
        if extra:
            lines.append(f"Заметка со стадии уточнения: {extra}")
        if notes:
            lines.append("Правки пользователя (учти все, последняя главнее):\n" + "\n".join(f"- {n}" for n in notes))
        return "\n".join(lines)

    async def _stage_story(self, job: Job) -> str:
        await asyncio.to_thread(prepare_work, job.folder, self.layout)
        for name in ("story.json", "story.md"):
            (job.plan_dir / name).unlink(missing_ok=True)
        prompt = self._prompt("story", facts=self._facts(job), material=self._material(job), references=self._references(),
                              notes=self._notes_block(job, "story_notes"))

        async def check(last: bool) -> list[str]:
            story = self._read_json(job.plan_dir / "story.json")
            if story is None:
                return ["plan/story.json отсутствует или не является JSON"]
            return validate_story(story)
        await self._set(job, text="Пишу историю ролика")
        await self._agent_checked(job, "story", prompt, model=self._model("plan"), check=check, what="story.json")
        story = self._read_json(job.plan_dir / "story.json")
        if not (job.plan_dir / "story.md").is_file():
            await asyncio.to_thread((job.plan_dir / "story.md").write_text, story_markdown(story), "utf-8")
        job.add_artifact(job.plan_dir / "story.md", "story")
        await self._set(job, text=f"История готова: {len(story['beats'])} битов, {story['target_duration_ms'] / 1000:.0f} с")
        return "plan"

    def _plan_context(self, job: Job) -> PlanContext:
        story = self._read_json(job.plan_dir / "story.json")
        info = self._info(job)
        src_ms = {f["path"]: f["duration_ms"] for f in info.get("files", []) if f.get("kind") == "video" and f.get("duration_ms")}
        return PlanContext.from_layout(self.layout, job_dir=job.folder, platform=self._platform(job),
                                       beat_ids=story_beat_ids(story) if story else None, source_ms=src_ms)

    async def _plan_problems(self, job: Job, *, include_craft: bool) -> list[str]:
        path = job.plan_dir / "plan.json"
        plan = self._read_json(path)
        if plan is None:
            return ["plan/plan.json отсутствует или не является JSON"]
        problems = validate_plan(plan, self._plan_context(job))
        r = self._request(job)
        if plan.get("voiceover") and normalize_voiceover(r.get("voiceover")) != "character":
            problems.append(f"voiceover: закадр не запрошен (voiceover = {r.get('voiceover')}), массив должен быть пустым")
        if not problems:
            problems += await self.renderer.validate_plan(job.work, path)
        if not problems and include_craft:
            story = self._read_json(job.plan_dir / "story.json")
            words = self._read_json(job.transcript / "words.json") or []
            craft = qa_mod.craft_plan_checks(plan, story, words, self._platform(job))
            problems += [f.line() for f in qa_mod.errors_of(craft)]
        return problems

    async def _stage_plan(self, job: Job) -> str:
        await asyncio.to_thread(prepare_work, job.folder, self.layout)
        for name in ("plan.json", "plan.md"):
            (job.plan_dir / name).unlink(missing_ok=True)
        r = self._request(job)
        size = SIZES.get(r.get("format") or "")
        fmt_hint = f"{size[0]}×{size[1]}, 30 кадров в секунду" if size else "по исходнику"
        cta_hint = {"shorts": "приглашение подписаться на «следующие испытания»", "reels": "то же", "telegram": "подписаться на канал лаборатории",
                    "youtube": "в последних 5 секундах «следующий отчёт об испытании»", "none": "нет"}[self._platform(job)]
        prompt = self._prompt("plan", facts=self._facts(job), material=self._material(job), notes=self._notes_block(job, "plan_notes"),
                              format_hint=fmt_hint, cta_hint=cta_hint, template_dir=str(self.layout.template).replace("\\", "/"))

        async def check(last: bool) -> list[str]:
            return await self._plan_problems(job, include_craft=not last)
        await self._set(job, text="Составляю план монтажа")
        await self._agent_checked(job, "plan", prompt, model=self._model("plan"), check=check, what="plan.json")
        plan = self._read_json(job.plan_dir / "plan.json")
        if not (job.plan_dir / "plan.md").is_file():
            await asyncio.to_thread((job.plan_dir / "plan.md").write_text, plan_markdown(plan), "utf-8")
        job.add_artifact(job.plan_dir / "plan.md", "plan")
        job.add_artifact(job.plan_dir / "plan.json", "plan_json")
        n_seg, n_ins, vo = len(plan["segments"]), len(plan["inserts"]), len(plan.get("voiceover") or [])
        await self._set(job, text=f"План готов: {n_seg} сцен, {n_ins} вставок, {plan['duration_ms'] / 1000:.0f} секунд, "
                                  + ("закадр нужен" if vo else "закадр не нужен"))
        return "plan_approval"

    async def _stage_plan_approval(self, job: Job) -> str:
        if not self.setting("approve_plan"):
            return "voiceover"
        story = self._read_json(job.plan_dir / "story.json") or {}
        plan = self._read_json(job.plan_dir / "plan.json") or {}
        question = (f"Утвердить монтаж? История: {story.get('logline')} Крючок: {(story.get('hook') or {}).get('what')}. "
                    f"Битов {len(story.get('beats') or [])}. План: {len(plan.get('segments') or [])} сцен, {len(plan.get('inserts') or [])} вставок, "
                    f"{plan.get('duration_ms', 0) / 1000:.0f} секунд, закадр {'есть' if plan.get('voiceover') else 'не нужен'}. "
                    f"История и план целиком: {job.plan_dir / 'story.md'} и {job.plan_dir / 'plan.md'}.")
        answer = await self.ask(job, question, "choice", [APPROVE, REDO_PLAN, REDO_STORY],
                                text="План монтажа готов и ждёт решения пользователя",
                                artifacts=[{"path": str(job.plan_dir / "plan.md"), "kind": "plan"}])
        choice = classify_choice(answer, {APPROVE: ("утверд", "да", "ок", "ok", "годится", "норм"),
                                          REDO_STORY: ("истори", "сюжет"), REDO_PLAN: ("перед", "план", "нет", "измен")})
        notes = None
        if choice in (REDO_PLAN, REDO_STORY):
            notes = await self.ask(job, "Что изменить в " + ("истории" if choice == REDO_STORY else "плане") + "?", "text", None,
                                   text="Нужны правки: спрашиваю, что именно")
        elif choice is None:
            choice = REDO_PLAN
            notes = str(answer)
        await asyncio.to_thread(self.store.append_verdict, rm.verdict_record(job.job_id, "plan_approval", choice, notes, {}))
        if choice == APPROVE:
            return "voiceover"
        key = "story_notes" if choice == REDO_STORY else "plan_notes"
        job.data.setdefault(key, []).append(str(notes))
        if choice == REDO_STORY:
            job.data.setdefault("plan_notes", [])
        await self._save(job)
        return "story" if choice == REDO_STORY else "plan"

    # ---------------------------------------------------------------------------------------------- voiceover / build

    async def _stage_voiceover(self, job: Job) -> str:
        path = job.plan_dir / "plan.json"
        plan = self._read_json(path)
        vos = plan.get("voiceover") or []
        if not vos:
            return "build"
        env = self._env(job, "voiceover")
        cache = load_voice_cache(job)
        for k, v in enumerate(vos):
            digest = hashlib.sha1(f"{v.get('emotion') or 'neutral'}|{v['text']}".encode()).hexdigest()
            cached = cache.get(v["id"])
            wav = job.work / "public" / "vo" / f"{v['id']}.wav"
            if cached and cached.get("hash") == digest and wav.is_file():
                v["duration_ms"] = int(cached["duration_ms"])
            else:
                await self._progress(job, "voiceover", k / len(vos), f"Синтезирую реплику {k + 1} из {len(vos)}")
                out = await synthesize_to(env, v["id"], v["text"], v.get("emotion") or "neutral")
                v["duration_ms"] = out["duration_ms"]
        fit = fit_voiceover(plan)
        await asyncio.to_thread(path.write_text, json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")
        await asyncio.to_thread((job.plan_dir / "voiceover_fit.json").write_text, json.dumps(fit, ensure_ascii=False, indent=2), "utf-8")
        await self._set(job, text=f"Закадр синтезирован: реплик {len(vos)}")
        return "build"

    def _voiceover_block(self, job: Job) -> str:
        plan = self._read_json(job.plan_dir / "plan.json") or {}
        vos = plan.get("voiceover") or []
        if not vos:
            return ""
        lines = ["Закадр (синтезирован, длительности точные):"]
        lines += [f"- {v['id']}: с {v['at_ms']} мс, длится {v.get('duration_ms')} мс — «{v['text'][:80]}»" for v in vos]
        fit = self._read_json(job.plan_dir / "voiceover_fit.json") or {}
        if fit.get("shifted"):
            lines.append("Модуль сдвинул реплики, чтобы они не наезжали друг на друга: " + ", ".join(f"{s['id']} {s['from']}→{s['to']} мс" for s in fit["shifted"])
                         + ". Проверь, что вставки и субтитры по-прежнему совпадают с репликами.")
        if fit.get("overflow_ms", 0) > 0:
            lines.append(f"Закадр заканчивается на {fit['overflow_ms']} мс позже конца ролика: поправь `duration_ms` и вставки в плане.")
        return "\n".join(lines)

    async def _build_problems(self, job: Job) -> list[str]:
        problems: list[str] = []
        path = job.plan_dir / "plan.json"
        plan = self._read_json(path)
        if plan is None:
            return ["plan/plan.json повреждён после правок"]
        problems += validate_plan(plan, self._plan_context(job))
        problems += await self.renderer.validate_plan(job.work, path) if not problems else []
        have = custom_components(job.work)
        for ins in plan.get("inserts") or []:
            if ins.get("kind") == "custom":
                name = (ins.get("props") or {}).get("component")
                if name not in have:
                    problems.append(f"вставка {ins['id']}: компонента «{name}» нет в src/custom/index.ts (создай и зарегистрируй)")
        for v in plan.get("voiceover") or []:
            if not (job.work / "public" / "vo" / f"{v['id']}.wav").is_file():
                problems.append(f"закадр {v['id']}: нет файла public/vo/{v['id']}.wav (инструмент synthesize_voice)")
        problems += [f"tsc: {x}" for x in await self.renderer.tsc(job.work)]
        return problems

    async def _build_pass(self, job: Job, fixes: list[str]) -> None:
        await asyncio.to_thread(prepare_work, job.folder, self.layout)
        pending = [r for r in job.data.get("revisions", []) if not r.get("applied")]
        blocks = []
        if pending:
            blocks.append("## Правки пользователя к черновику (внеси все)\n" + "\n".join(f"- {r['notes']}" for r in pending))
        if fixes:
            blocks.append("## Замечания автоматического контроля качества (исправь)\n" + "\n".join(f"- {x}" for x in fixes[:30]))
        prompt = self._prompt("build", facts=self._facts(job), voiceover=self._voiceover_block(job), fixes="\n\n".join(blocks))

        async def check(last: bool) -> list[str]:
            return await self._build_problems(job)
        await self._agent_checked(job, "build", prompt, model=self._model("build"), check=check, attempts=BUILD_ATTEMPTS,
                                  what="собранный проект")
        for r in pending:
            r["applied"] = True
        await self._save(job)

    async def _stage_build(self, job: Job) -> str:
        await self._set(job, text="Собираю проект Remotion")
        await self._build_pass(job, [])
        return "preview"

    # ---------------------------------------------------------------------------------------------- preview / QA

    async def _render(self, job: Job, out: Path, *, scale: float | None, crf: int, stage: str, text: str) -> float:
        t0 = time.monotonic()
        loop = asyncio.get_running_loop()

        def on_progress(frac: float) -> None:
            loop.create_task(self._progress(job, stage, frac, f"{text}: {round(frac * 100)} %"))
        await self.renderer.render(job.work, job.plan_dir / "plan.json", out, scale=scale, crf=crf, on_progress=on_progress)
        return time.monotonic() - t0

    async def _run_qa(self, job: Job, video: Path, *, draft: bool) -> qa_mod.QAOutcome:
        plan = self._read_json(job.plan_dir / "plan.json")
        story = self._read_json(job.plan_dir / "story.json")
        words = self._read_json(job.transcript / "words.json") or []
        findings, checked = await self.qa.technical(job, plan, words, story, video, draft=draft, scale=0.5 if draft else 1.0,
                                                    platform=self._platform(job))
        return qa_mod.QAOutcome(findings=findings, checked=checked)

    async def _semantic(self, job: Job, plan: dict, story: dict | None, video: Path, findings: list[qa_mod.Finding]) -> dict | None:
        frames_dir = job.qa / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        for old in frames_dir.glob("*.png"):
            old.unlink()
        lines = []
        for k, (t_ms, what) in enumerate(qa_mod.key_frame_times(plan, story)):
            png = frames_dir / f"f{k:02d}_{t_ms}ms.png"
            try:
                await media.extract_frame(video, t_ms / 1000, png, width=540)
            except media.MediaError:
                continue
            lines.append(f"- `{png}` — {t_ms} мс: {what}")
        if not lines:
            return None
        r = self._request(job)
        tech = "; ".join(f.line() for f in findings if f.severity == "error")[:600] or "замечаний нет"
        prompt = self._prompt("review", plan_path=str(job.plan_dir / "plan.json"), story_path=str(job.plan_dir / "story.json"),
                              brief=r.get("brief", ""), platform=r.get("platform") or "none", format=r.get("format"),
                              duration=f"{plan['duration_ms'] / 1000:.0f} с", tech_summary=tech, frames="\n".join(lines),
                              rubric=(self.layout.prompts / "qa_rubric.md").read_text(encoding="utf-8"))
        res = await self._agent(job, "review", prompt, model=self._model("build"), readonly=True, fresh=True)
        sem = qa_mod.parse_semantic(res.text)
        if sem is None:
            await self._log(job, "semantic_unparsed", text=res.text[:300])
        return sem

    async def _stage_preview(self, job: Job) -> str:
        preview = job.renders / "preview.mp4"
        fixed: list[str] = []
        iterations: list[dict] = []
        outcome = qa_mod.QAOutcome()
        for it in range(qa_mod.MAX_FIX_ITERATIONS + 1):
            await self._set(job, text="Рендерю черновик" if it == 0 else f"Рендерю черновик после автоисправлений ({it})")
            spent = await self._render(job, preview, scale=0.5, crf=28, stage="preview", text="Черновик")
            job.data["preview_render_s"] = round(spent, 1)
            await self._set(job, text="Проверяю качество черновика")
            last = it == qa_mod.MAX_FIX_ITERATIONS
            outcome = await self._run_qa(job, preview, draft=True)
            if not outcome.errors or last:                         # смысловая проверка дорогая: сначала чинятся технические ошибки
                outcome.semantic = await self._semantic(job, self._read_json(job.plan_dir / "plan.json"),
                                                        self._read_json(job.plan_dir / "story.json"), preview, outcome.findings)
            iterations.append({"n": it + 1, **outcome.summary()})
            if outcome.passed or last:
                break
            notes = outcome.fix_notes()
            fixed += [f"круг {it + 1}: {n}" for n in notes]
            await self._set(job, text=f"Автопроверка нашла замечаний: {len(notes)}, отдаю монтажёру")
            await self._build_pass(job, notes)
        outcome.iterations = iterations
        jp, mp = await asyncio.to_thread(qa_mod.write_report, job.qa, job.job_id, outcome, draft=True, fixed=fixed)
        job.add_artifact(preview, "preview")
        job.add_artifact(mp, "qa_report")
        job.data["qa"] = {**outcome.summary(), "report": str(mp), "fixed": len(fixed)}
        await self._save(job)
        s = outcome.summary()
        await self._set(job, text=f"Черновик готов, автопроверка: ошибок {s['technical_errors']}, оценка {s['semantic_overall']}")
        return "review"

    # ---------------------------------------------------------------------------------------------- review / final

    async def _stage_review(self, job: Job) -> str:
        qa_sum = job.data.get("qa") or {}
        stats = rm.agreement(await asyncio.to_thread(self.store.verdicts))
        mode, why = rm.effective_mode(str(self.setting("review_mode")), stats)
        if why:
            await self._log(job, "review_mode", requested=self.setting("review_mode"), effective=mode, reason=why)
        if self.setting("auto_final"):
            return "final"
        if rm.skip_human_review(mode, qa_sum):
            await self._log(job, "review_skipped", mode=mode, **qa_sum)
            return "final"
        preview = job.artifact("preview")
        flag = "" if qa_sum.get("passed", True) else " Автопроверка не прошла: замечания помечены в отчёте."
        question = (f"Черновик готов: {preview}. Автопроверка: ошибок {qa_sum.get('technical_errors', 0)}, смысловая оценка "
                    f"{qa_sum.get('semantic_overall')}.{flag} Отчёт: {qa_sum.get('report')}. Готово или нужны правки?")
        if any(not r.get("applied") for r in job.data.get("revisions", [])):
            return "build"                                           # правки пришли, пока шла проверка: сначала внести их
        preset = job.data.pop("preset_notes", None)
        if preset:
            answer = EDITS
        else:
            answer = await self.ask(job, question, "choice", [READY, EDITS], text="Черновик готов и ждёт решения пользователя",
                                    artifacts=[{"path": str(preview), "kind": "preview"}])
            preset = job.data.pop("preset_notes", None)              # инструмент revise ответил за пользователя
        choice = classify_choice(answer, {READY: ("готов", "да", "ок", "ok", "годится", "норм", "отлично"), EDITS: ("правк", "нет", "перед", "измен")})
        notes = preset
        if choice == EDITS and not notes:
            notes = await self.ask(job, "Что изменить в ролике?", "text", None, text="Нужны правки: спрашиваю, что именно")
        elif choice is None:
            choice, notes = EDITS, str(answer)
        await asyncio.to_thread(self.store.append_verdict, rm.verdict_record(job.job_id, "review", choice, notes, qa_sum))
        if choice == READY:
            return "final"
        job.data.setdefault("revisions", []).append({"notes": str(notes), "ts": time.time(), "applied": False})
        await self._log(job, "revision", notes=str(notes)[:300])
        await self._save(job)
        return "build"

    async def _stage_final(self, job: Job) -> str:
        plan = self._read_json(job.plan_dir / "plan.json")
        platform = self._platform(job)
        raw = job.renders / "final_raw.mp4"
        final = job.renders / f"final_{platform}.mp4"
        dur = plan["duration_ms"] / 1000
        est = job.data.get("preview_render_s")
        est_min = max(1, round(((est or dur * 0.5) * 3 + 20) / 60))
        await self._emit(job, "montage.launch", f"Запускаю финальный рендер ролика «{job.data['title']}»: {dur:.0f} секунд, "
                         f"{plan['format']['width']}×{plan['format']['height']}, займёт примерно {est_min} мин.",
                         data={"job_id": job.job_id, "what": "final_render", "estimate_min": est_min}, announce=True, urgency_hint=3)
        await self._set(job, text="Рендерю финальный ролик")
        await self._render(job, raw, scale=None, crf=16, stage="final", text="Финал")
        await self._set(job, text="Выравниваю громкость")
        await media.loudnorm(raw, final)
        raw.unlink(missing_ok=True)
        await self._set(job, text="Проверяю финал")
        words = self._read_json(job.transcript / "words.json") or []
        findings, checked = await self.qa.technical(job, plan, words, self._read_json(job.plan_dir / "story.json"), final, draft=False,
                                                    scale=1.0, platform=platform)
        outcome = qa_mod.QAOutcome(findings=findings, checked=checked)
        _, mp = await asyncio.to_thread(qa_mod.write_report, job.qa, job.job_id, outcome, draft=False, fixed=[], stem="report_final")
        out_dir = Path(os.path.expandvars(str(self.setting("output_dir"))))
        copy = await asyncio.to_thread(self._copy_out, final, out_dir, platform, job.job_id)
        if not (plan.get("captions") or {}).get("enabled"):                 # субтитры не вжигались: файл .srt рядом с финалом
            text = await asyncio.to_thread(srt_mod.build_srt, plan, words, qa_mod.load_zones(self.layout))
            if text:
                srt_path = final.with_suffix(".srt")
                await asyncio.to_thread(srt_path.write_text, text, "utf-8")
                srt_copy = copy.with_suffix(".srt")
                await asyncio.to_thread(shutil.copyfile, srt_path, srt_copy)
                job.add_artifact(srt_path, "subtitles")
                job.add_artifact(srt_copy, "subtitles_output")
        job.add_artifact(final, "final")
        job.add_artifact(copy, "output")
        job.add_artifact(mp, "qa_report_final")
        job.data["output"] = str(copy)
        await self._save(job)
        return "done"

    @staticmethod
    def _copy_out(final: Path, out_dir: Path, platform: str, job_id: str) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"final_{platform}.mp4"
        if target.exists():
            target = out_dir / f"final_{platform}_{job_id}.mp4"
        shutil.copyfile(final, target)
        return target

    # ================================================================================================ завершение

    async def _complete(self, job: Job) -> None:
        out = job.data.get("output")
        await self._log(job, "done", output=out)
        qa_final = job.artifact("qa_report_final")
        await self._set(job, state="done", stage="done", progress=1.0, error=None,
                        text=f"Ролик готов: {out}" if out else "Ролик готов")
        await self._emit(job, "montage.done", f"Монтаж «{job.data['title']}» закончен, файл: {out}.",
                         data={"job_id": job.job_id, "path": out, "qa_report": qa_final}, urgency_hint=3)
        try:
            await self._experience(job)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                    # опыт не должен ломать готовую задачу
            await self._log(job, "experience_failed", error=str(e)[:200])
        removed = await asyncio.to_thread(self.store.cleanup, int(self.setting("keep_jobs")))
        if removed:
            log.info("убраны старые задачи: %s", ", ".join(removed))

    async def _experience(self, job: Job) -> None:
        """MON-32: если были правки, агент формулирует 1-3 вывода, они идут в опыт (scope montage, source user, 0.8)."""
        revs = [r["notes"] for r in job.data.get("revisions", []) if r.get("notes")]
        if not revs:
            return
        r = self._request(job)
        prompt = self._prompt("experience", revisions="\n".join(f"{i}. {n}" for i, n in enumerate(revs, 1)),
                              context=f"площадка {r.get('platform')}, вид {r.get('source_kind')}, формат {r.get('format')}")
        res = await self._agent(job, "experience", prompt, model=self._model("build"), readonly=True, fresh=True)
        a, b = res.text.find("{"), res.text.rfind("}")
        try:
            doc = json.loads(res.text[a:b + 1])
        except ValueError:
            return
        for c in (doc.get("conclusions") or [])[:3]:
            statement = str((c or {}).get("statement") or "").strip()
            if not statement:
                continue
            try:
                await self.core.experience_add({"scope": "montage", "statement": statement, "context": str(c.get("context") or "")[:300],
                                                "outcome": "success", "source": "user", "confidence": 0.8,
                                                "evidence": [f"montage:{job.job_id}"]})
            except ToolError as e:
                await self._log(job, "experience_unavailable", code=e.code)
                return


# ==================================================================================================== вспомогательное

def fit_voiceover(plan: dict) -> dict:
    """Реплики закадра не наезжают друг на друга: следующая сдвигается, если начинается раньше конца предыдущей плюс пауза
    (регламент §5). Реплика с `duck: true` намеренно идёт поверх. Ролик без исходного видео удлиняется до конца закадра."""
    vos = sorted(plan.get("voiceover") or [], key=lambda v: v["at_ms"])
    shifted = []
    cursor = 0
    for v in vos:
        dur = int(v.get("duration_ms") or 0)
        if not v.get("duck") and v["at_ms"] < cursor + (VOICE_GAP_MS if cursor else 0):
            new = cursor + VOICE_GAP_MS
            shifted.append({"id": v["id"], "from": v["at_ms"], "to": new})
            v["at_ms"] = new
        cursor = max(cursor, v["at_ms"] + dur)
    overflow = max(0, cursor - plan["duration_ms"])
    extended = False
    if overflow and not plan.get("segments"):
        plan["duration_ms"] = cursor + 600
        extended = True
        overflow = 0
    plan["voiceover"] = vos
    return {"shifted": shifted, "overflow_ms": overflow, "duration_extended": extended}


def story_markdown(story: dict) -> str:
    out = ["# История ролика", "", f"**Логлайн.** {story['logline']}", "", f"**Крючок.** {story['hook']['what']} ({story['hook']['why']})", "",
           f"**Обещание.** {story['promise']}", "", "## Биты"]
    for b in story["beats"]:
        out.append(f"- {b['id']} [{b['role']}] {b['t_from_ms'] / 1000:.1f}–{b['t_to_ms'] / 1000:.1f} с: {b['purpose']}")
    for sp in story.get("setups_payoffs") or []:
        out.append(f"- подготовка {sp['setup']} → отдача {sp['payoff']}: {sp.get('what', '')}")
    return "\n".join(out) + "\n"


def plan_markdown(plan: dict) -> str:
    out = [f"# План монтажа: {plan['title']}", "", f"Длительность {plan['duration_ms'] / 1000:.1f} с, кадр {plan['format']['width']}×{plan['format']['height']}.",
           "", "## Сцены"]
    for s in plan["segments"]:
        out.append(f"- {s['id']} ({s['beat']}): {s['source']} {s['in_ms']}–{s['out_ms']} мс, на {s['at_ms']} мс")
    out.append("\n## Вставки")
    for i in plan["inserts"]:
        out.append(f"- {i['id']} ({i['beat']}) {i['kind']} на {i['at_ms']} мс, {i['dur_ms']} мс")
    if plan.get("voiceover"):
        out.append("\n## Закадр")
        out += [f"- {v['id']} на {v['at_ms']} мс: {v['text']}" for v in plan["voiceover"]]
    if plan.get("cta"):
        out.append(f"\n## Призыв\n{plan['cta']['text']} (на {plan['cta']['at_ms']} мс)")
    return "\n".join(out) + "\n"

