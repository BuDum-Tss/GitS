"""Команды окружения «Монтаж» (`run`, docs/WORKSPACE_CONTRACT.md раздел 3): `intake`, `transcribe`, `voiceover`, `render`, `still`,
`compositions`, `check`, `final`.

Каждая команда работает над одной рабочей единицей (роликом) и получает только аргументы из списка разрешённых: произвольной
оболочки и путей от агента нет, пути строятся здесь из папки единицы. Очередь, фон и события ведёт `Manager`; здесь сама работа.
Зависимости (ядро, Remotion, контроль качества) приходят в конструктор: тесты подставляют заглушки.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import intake as intake_mod
import media
import qa as qa_mod
import srt as srt_mod
import transcribe
from core_api import CoreAPI
from gits_sdk import ToolError
from jobs import Job, JobStore
from layout import Layout
from manifest import normalize_voiceover, resolve_output_dir
from plan_schema import PlanContext, story_beat_ids, validate_plan, validate_story
from plan_tools import fit_voiceover, load_voice_cache, synthesize_to, voice_digest
from render import RenderError, Renderer
from workspace import custom_components, sync_intro, sync_media

log = logging.getLogger("montage.commands")

Progress = Callable[[float, str], Awaitable[None]]
FORMAT_OF_PLATFORM = {"shorts": "vertical", "reels": "vertical", "youtube": "horizontal"}
EXTRA_ARGS = {"still": ("frame",)}               # необязательные аргументы после id единицы; у остальных команд их нет
FRAME_RE = re.compile(r"^\d{1,7}$")
PREVIEW_SCALE, PREVIEW_CRF, FINAL_CRF = 0.5, 28, 16


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def parse_args(command: str, args: list[str]) -> dict:
    """Аргументы команды после id единицы (список разрешённых): лишнего нет, `frame` целое."""
    allowed = EXTRA_ARGS.get(command, ())
    if len(args) > len(allowed):
        raise ToolError(f"команда «{command}»: после id единицы " + (f"допустимо: {', '.join(allowed)}" if allowed else "аргументов нет"),
                        code="invalid")
    out = dict(zip(allowed, args))
    if "frame" in out and not FRAME_RE.match(out["frame"]):
        raise ToolError("frame: номер кадра, целое неотрицательное", code="invalid")
    return out


class Commands:
    def __init__(self, store: JobStore, core: CoreAPI, layout: Layout, renderer: Renderer, qa: object | None = None,
                 setting: Callable[[str], object] = lambda name: None):
        self.store = store
        self.core = core
        self.layout = layout
        self.renderer = renderer
        self.qa = qa if qa is not None else qa_mod.QAService(layout, renderer)
        self.setting = setting

    async def run(self, command: str, job: Job, args: dict, progress: Progress) -> dict:
        """Выполнить команду над единицей. Результат: словарь с полем `text` (одна строка итога) и деталями."""
        handler = getattr(self, "_cmd_" + command, None)
        if handler is None:
            raise ToolError(f"команды «{command}» нет", code="unknown")
        try:
            return await handler(job, args, progress)
        except (RenderError, media.MediaError) as e:
            raise ToolError(str(e)[:600], code="unavailable") from e
        except OSError as e:
            raise ToolError(f"{type(e).__name__}: {str(e)[:300]}", code="unavailable") from e

    # ================================================================================================ вспомогательное

    async def _save(self, job: Job) -> None:
        await asyncio.to_thread(self.store.save, job)

    def _platform(self, job: Job) -> str:
        platform = job.request.get("platform")
        return "none" if platform in (None, "", "telegram") else platform

    def _info(self, job: Job) -> dict:
        return read_json(job.sources / "info.json") or {"files": []}

    def _plan_path(self, job: Job) -> Path:
        path = job.plan_dir / "plan.json"
        if not path.is_file():
            raise ToolError("плана ещё нет: запиши plan/plan.json (по регламенту монтажа)", code="unavailable")
        return path

    def _plan_context(self, job: Job, story: dict | None) -> PlanContext:
        info = self._info(job)
        src_ms = {f["path"]: f["duration_ms"] for f in info.get("files", []) if f.get("kind") == "video" and f.get("duration_ms")}
        return PlanContext.from_layout(self.layout, job_dir=job.folder, platform=self._platform(job),
                                       beat_ids=story_beat_ids(story) if story else None, source_ms=src_ms)

    @staticmethod
    def _format_from_sources(info: dict) -> str:
        for f in info.get("files", []):
            if f.get("kind") == "video":
                return "vertical" if f["height"] > f["width"] else "square" if f["height"] == f["width"] else "horizontal"
        return "horizontal"

    async def _render(self, job: Job, out: Path, *, scale: float | None, crf: int, progress: Progress, text: str,
                      span: tuple[float, float] = (0.0, 1.0)) -> float:
        t0 = time.monotonic()
        loop = asyncio.get_running_loop()

        def on_progress(frac: float) -> None:
            loop.create_task(progress(span[0] + max(0.0, min(1.0, frac)) * (span[1] - span[0]), f"{text}: {round(frac * 100)} %"))
        await asyncio.to_thread(sync_intro, job.work, self.layout)
        await self.renderer.render(job.work, job.plan_dir / "plan.json", out, scale=scale, crf=crf, on_progress=on_progress)
        return time.monotonic() - t0

    # ================================================================================================ intake

    async def _cmd_intake(self, job: Job, args: dict, progress: Progress) -> dict:
        """Копирование и проверка исходников единицы: перекодирование HEVC в H.264, ссылки в `sources/`, вид исходников."""
        r = job.request
        if r.get("sources"):
            for finder, label in ((media.find_ffmpeg, "ffmpeg"), (media.find_ffprobe, "ffprobe")):
                try:
                    finder()
                except FileNotFoundError:
                    raise ToolError(f"не найден {label}: приём исходников невозможен", code="unavailable") from None
        try:
            info = await intake_mod.run_intake(job.folder, r, progress)
        except ToolError as e:
            raise ToolError(e.message, code="invalid" if e.code == "bad_request" else e.code) from e
        kind = info["source_kind"]
        r["source_kind"] = kind
        if not r.get("voiceover"):
            r["voiceover"] = "character" if kind in ("narrated", "rimworld_run") else "keep"
        if not r.get("format"):
            r["format"] = FORMAT_OF_PLATFORM.get(r.get("platform") or "none") or self._format_from_sources(info)
        await asyncio.to_thread(sync_media, job.folder)
        await self._save(job)
        n = len(info["files"])
        return {"files": n, "source_kind": kind, "platform": r.get("platform") or "none", "format": r["format"],
                "voiceover": r["voiceover"], "info": "sources/info.json",
                "text": f"исходники проверены: файлов {n}, вид «{kind}», площадка {r.get('platform') or 'none'}"}

    # ================================================================================================ transcribe

    async def _cmd_transcribe(self, job: Job, args: dict, progress: Progress) -> dict:
        """Слова для субтитров: из журнала речи забега RimWorld; живая речь отложена (`unavailable`)."""
        kind = job.request.get("source_kind")
        words: list[dict] = []
        meta: dict = {"origin": "none", "words": 0}
        if kind == "rimworld_run":
            words, meta = await self._words_from_speech_log(job)
        elif kind == "talking_head":
            transcribe.transcribe_live()                          # MON-29 отложено: unavailable
        await asyncio.to_thread((job.transcript / "words.json").write_text, json.dumps(words, ensure_ascii=False, indent=1), "utf-8")
        await asyncio.to_thread((job.transcript / "meta.json").write_text, json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        await asyncio.to_thread(sync_media, job.folder)
        return {"words": len(words), "origin": meta.get("origin"), "note": meta.get("note"), "path": "transcript/words.json",
                "text": f"слов для субтитров: {len(words)}" if words else "расшифровка не нужна: речи людей в исходниках нет"}

    async def _words_from_speech_log(self, job: Job) -> tuple[list[dict], dict]:
        """MON-26a: субтитры из журнала речи, без распознавания."""
        log_path = job.sources / "speech_log.json"
        if not log_path.is_file():
            return [], {"origin": "speech_log", "words": 0, "note": "журнала речи нет (speech_log.json)"}
        lines = await asyncio.to_thread(transcribe.load_speech_log, log_path)
        rec = read_json(job.sources / "recording.json") or {}
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
        return await transcribe.words_from_speech_log(lines, float(started), duration, audible,
                                                      source=video["path"] if (multiple and video) else None)

    # ================================================================================================ voiceover

    async def _cmd_voiceover(self, job: Job, args: dict, progress: Progress) -> dict:
        """Озвучка реплик плана её голосом (core.tts): файлы `work/public/vo/<id>.wav`, длительности в плане, кэш в
        `plan/voiceover_cache.json`. Реплики с неизменным текстом и эмоцией не синтезируются заново."""
        path = self._plan_path(job)
        plan = read_json(path)
        if not isinstance(plan, dict):
            raise ToolError("plan/plan.json не является JSON", code="invalid")
        vos = plan.get("voiceover") or []
        if not vos:
            return {"voiceover": 0, "synthesized": 0, "text": "закадра в плане нет, синтез не нужен"}
        cache = load_voice_cache(job)
        made = 0
        for k, v in enumerate(vos):
            digest = voice_digest(v["text"], v.get("emotion"))
            cached = cache.get(v["id"])
            wav = job.work / "public" / "vo" / f"{v['id']}.wav"
            if cached and cached.get("hash") == digest and wav.is_file():
                v["duration_ms"] = int(cached["duration_ms"])
                continue
            await progress(k / len(vos), f"реплика {k + 1} из {len(vos)}")
            out = await synthesize_to(self.core, job, v["id"], v["text"], v.get("emotion") or "neutral")
            v["duration_ms"] = out["duration_ms"]
            made += 1
        fit = fit_voiceover(plan)
        await asyncio.to_thread(path.write_text, json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")
        await asyncio.to_thread((job.plan_dir / "voiceover_fit.json").write_text, json.dumps(fit, ensure_ascii=False, indent=2), "utf-8")
        note = ""
        if fit["shifted"]:
            note += "; сдвинуты реплики: " + ", ".join(f"{s['id']} {s['from']}→{s['to']} мс" for s in fit["shifted"])
        if fit["overflow_ms"] > 0:
            note += f"; закадр заканчивается на {fit['overflow_ms']} мс позже конца ролика, поправь duration_ms"
        return {"voiceover": len(vos), "synthesized": made, "fit": "plan/voiceover_fit.json",
                "text": f"закадр синтезирован: реплик {len(vos)}, заново {made}{note}"}

    # ================================================================================================ render / still / compositions

    async def _cmd_render(self, job: Job, args: dict, progress: Progress) -> dict:
        """Черновик (половинный размер) в `renders/preview.mp4`."""
        self._plan_path(job)
        await asyncio.to_thread(sync_media, job.folder)
        out = job.renders / "preview.mp4"
        spent = await self._render(job, out, scale=PREVIEW_SCALE, crf=PREVIEW_CRF, progress=progress, text="Рендер черновика")
        job.data["preview_render_s"] = round(spent, 1)
        job.add_artifact(out, "preview")
        await self._save(job)
        return {"path": "renders/preview.mp4", "seconds": round(spent, 1), "text": "черновик отрендерен: renders/preview.mp4"}

    async def _cmd_still(self, job: Job, args: dict, progress: Progress) -> dict:
        """Один кадр плана в `renders/still_<кадр>.png`."""
        plan_path = self._plan_path(job)
        plan = read_json(plan_path) or {}
        frame = int(args.get("frame") or 0)
        fps = (plan.get("format") or {}).get("fps") or 30
        last = max(0, int((plan.get("duration_ms") or 0) / 1000 * fps) - 1)
        if frame > last:
            raise ToolError(f"frame: в ролике кадры 0–{last}", code="invalid")
        out = job.renders / f"still_{frame}.png"
        await asyncio.to_thread(sync_intro, job.work, self.layout)
        await asyncio.to_thread(sync_media, job.folder)
        await self.renderer.still(job.work, plan_path, frame, out)
        return {"path": f"renders/still_{frame}.png", "frame": frame, "text": f"кадр {frame}: renders/still_{frame}.png"}

    async def _cmd_compositions(self, job: Job, args: dict, progress: Progress) -> dict:
        lines = await self.renderer.compositions(job.work)
        return {"compositions": lines, "text": "композиции проекта: " + "; ".join(lines[:10])}

    # ================================================================================================ check

    async def _cmd_check(self, job: Job, args: dict, progress: Progress) -> dict:
        """Проверка перед финалом: форма и правила плана (plan_schema, схема шаблона Remotion, tsc, закадр, ремесло) и, если есть
        черновик, его кадры и звук (qa.py). Отчёт `qa/report.md` и `qa/report.json`; хэш плана и итог запоминаются в единице:
        по ним `final` решает, пускать ли финальный рендер."""
        path = self._plan_path(job)
        await progress(0.1, "форма и правила монтажного листа")
        raw = path.read_bytes()
        plan_hash = hashlib.sha256(raw).hexdigest()
        try:
            plan = json.loads(raw.decode("utf-8"))
        except ValueError:
            plan = None
        story = read_json(job.plan_dir / "story.json")
        problems: list[str] = []
        if plan is None or not isinstance(plan, dict):
            problems.append("plan/plan.json не является JSON-объектом")
        else:
            if (job.plan_dir / "story.json").is_file() and story is None:
                problems.append("plan/story.json не является JSON")
            elif story is not None:
                problems += [f"story.json: {p}" for p in validate_story(story)]
            problems += validate_plan(plan, self._plan_context(job, story))
            r = job.request
            if plan.get("voiceover") and normalize_voiceover(r.get("voiceover")) in ("none", "keep"):
                problems.append(f"voiceover: закадр не запрошен (voiceover = {r.get('voiceover')}), массив должен быть пустым")
            if not problems:
                problems += await self.renderer.validate_plan(job.work, path)
            if not problems:
                have = custom_components(job.work)
                for ins in plan.get("inserts") or []:
                    if ins.get("kind") == "custom" and (ins.get("props") or {}).get("component") not in have:
                        problems.append(f"вставка {ins['id']}: компонента «{(ins.get('props') or {}).get('component')}» нет в "
                                        "work/src/custom/index.ts (создай и зарегистрируй)")
                for v in plan.get("voiceover") or []:
                    if not (job.work / "public" / "vo" / f"{v['id']}.wav").is_file():
                        problems.append(f"закадр {v['id']}: нет файла public/vo/{v['id']}.wav (run voiceover)")
                await progress(0.3, "типы и вставки проекта")
                problems += [f"tsc: {x}" for x in await self.renderer.tsc(job.work)]
        findings = [qa_mod.Finding("plan", "error", p) for p in problems]
        checked: dict = {"plan": True}
        words = read_json(job.transcript / "words.json") or []
        if not problems:
            preview = job.renders / "preview.mp4"
            if not preview.is_file():
                findings += qa_mod.craft_plan_checks(plan, story, words, self._platform(job))
                findings.append(qa_mod.Finding("preview", "warning", "черновика нет: кадры и звук не проверены (run render)"))
            elif preview.stat().st_mtime + 1 < path.stat().st_mtime:
                findings += qa_mod.craft_plan_checks(plan, story, words, self._platform(job))
                findings.append(qa_mod.Finding("preview", "warning", "черновик старше плана: кадры и звук не проверены, перерендери (run render)"))
            else:
                await progress(0.5, "кадры и звук черновика")
                tech, tech_checked = await self.qa.technical(job, plan, words, story, preview, draft=True, scale=PREVIEW_SCALE,
                                                             platform=self._platform(job))
                findings += tech
                checked.update(tech_checked)
        outcome = qa_mod.QAOutcome(findings=findings, checked=checked)
        _, md = await asyncio.to_thread(qa_mod.write_report, job.qa, job.job_id, outcome, draft=True, fixed=[])
        errors = [f for f in findings if f.severity == "error"]
        passed = not errors
        job.data["check"] = {"passed": passed, "plan_hash": plan_hash, "at": round(time.time(), 1), "errors": len(errors),
                             "warnings": len(findings) - len(errors), "report": "qa/report.md"}
        job.add_artifact(md, "qa_report")
        await self._save(job)
        text = ("проверка пройдена" if passed else f"проверка не пройдена: ошибок {len(errors)}") + f", предупреждений {len(findings) - len(errors)}"
        return {"passed": passed, "errors": len(errors), "warnings": len(findings) - len(errors), "report": "qa/report.md",
                "problems": [f.line() for f in errors][:25], "warnings_list": [f.line() for f in findings if f.severity != "error"][:15],
                "text": text}

    def check_state(self, job: Job) -> tuple[bool, str]:
        """Есть ли свежая успешная проверка текущего plan.json: (да/нет, причина отказа)."""
        path = job.plan_dir / "plan.json"
        if not path.is_file():
            return False, "плана ещё нет"
        chk = job.data.get("check")
        if not chk:
            return False, "проверка `check` ещё не запускалась"
        if hashlib.sha256(path.read_bytes()).hexdigest() != chk.get("plan_hash"):
            return False, "план изменён после последней проверки `check`"
        if not chk.get("passed"):
            return False, f"последняя проверка `check` не пройдена (ошибок {chk.get('errors')}): отчёт {chk.get('report')}"
        return True, ""

    # ================================================================================================ final

    async def _cmd_final(self, job: Job, args: dict, progress: Progress) -> dict:
        """Финальный рендер: сборка, выравнивание громкости, проверка файла, копия в выходную папку (необратимо). Без свежей
        успешной проверки `check` отказывает (`unavailable`)."""
        fresh, why = self.check_state(job)
        if not fresh:
            raise ToolError(f"финальный рендер невозможен: {why}. Выполни run check и поправь ошибки", code="unavailable")
        plan = read_json(job.plan_dir / "plan.json")
        platform = self._platform(job)
        raw = job.renders / "final_raw.mp4"
        final = job.renders / f"final_{platform}.mp4"
        dur = plan["duration_ms"] / 1000
        est = job.data.get("preview_render_s")
        est_min = max(1, round(((est or dur * 0.5) * 3 + 20) / 60))
        try:
            await self.core.event("montage.launch", f"Запускаю финальный рендер ролика «{job.data['title']}»: {dur:.0f} секунд, "
                                  f"{plan['format']['width']}×{plan['format']['height']}, займёт примерно {est_min} мин.",
                                  data={"job_id": job.job_id, "what": "final_render", "estimate_min": est_min}, announce=True,
                                  urgency_hint=3, dedupe_key=f"montage.launch:{job.job_id}")
        except Exception as e:
            log.info("событие montage.launch не отправлено: %s", e)
        await asyncio.to_thread(sync_media, job.folder)
        await self._render(job, raw, scale=None, crf=FINAL_CRF, progress=progress, text="Рендер финала", span=(0.0, 0.85))
        await progress(0.88, "Финал: выравниваю громкость")
        await media.loudnorm(raw, final)
        raw.unlink(missing_ok=True)
        await progress(0.93, "Финал: проверяю готовый файл")
        words = read_json(job.transcript / "words.json") or []
        story = read_json(job.plan_dir / "story.json")
        findings, checked = await self.qa.technical(job, plan, words, story, final, draft=False, scale=1.0, platform=platform)
        outcome = qa_mod.QAOutcome(findings=findings, checked=checked)
        _, mp = await asyncio.to_thread(qa_mod.write_report, job.qa, job.job_id, outcome, draft=False, fixed=[], stem="report_final")
        out_dir = Path(resolve_output_dir(self.setting("output_dir")))
        copy = await asyncio.to_thread(self._copy_out, final, out_dir, platform, job.job_id)
        fmt = plan.get("format") or {}
        burned = bool((plan.get("captions") or {}).get("enabled")) and fmt.get("height", 0) > fmt.get("width", 0)   # ТЗ 23 SC-4: только вертикаль
        if not burned:                                                      # субтитры не вжигались: файл .srt рядом с финалом
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
        errs = len(qa_mod.errors_of(findings))
        return {"output": str(copy), "qa_report": str(mp), "qa_errors": errs, "text": f"ролик готов: {copy}"
                + (f" (проверка финала: ошибок {errs}, отчёт qa/report_final.md)" if errs else "")}

    @staticmethod
    def _copy_out(final: Path, out_dir: Path, platform: str, job_id: str) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"final_{platform}.mp4"
        if target.exists():
            target = out_dir / f"final_{platform}_{job_id}.mp4"
        shutil.copyfile(final, target)
        return target
