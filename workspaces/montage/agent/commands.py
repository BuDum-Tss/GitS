"""Команды окружения «Монтаж» (`run`, docs/WORKSPACE_CONTRACT.md раздел 3): `intake`, `transcribe`, `voiceover`, `render`, `still`,
`compositions`, `check`, `final`.

Для длинных исходников есть ещё `scan`, `moments`, `peek`, `clip` (поиск и нарезка моментов по сигналам и тексту на кадрах).

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
import moments as moments_mod
import qa as qa_mod
import scenes as scenes_mod
import speech as speech_mod
import srt as srt_mod
import stt_client
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
# необязательные аргументы после id единицы; у остальных команд их нет. Имена аргументов задают и порядок, и проверку в parse_args
EXTRA_ARGS = {"still": ("frame",), "scan": ("source",), "moments": ("source", "limit", "from_ms", "to_ms"),
              "peek": ("source", "t_ms"), "clip": ("source", "in_ms", "out_ms"), "speech": ("source", "from_ms", "to_ms"),
              "find": ("query", "source")}
REQUIRED_ARGS = {"peek": 2, "clip": 3}           # сколько первых аргументов обязательны
INT_ARGS = ("frame", "limit", "from_ms", "to_ms", "t_ms", "in_ms", "out_ms")
SKIP_DEFAULT = ("moments", "speech")             # у них «-» вместо числа значит «по умолчанию»
QUERY_MIN, QUERY_MAX = 2, 60
ANY_SOURCE = ("-", "*", "all")                    # «любой источник» / «по умолчанию»: чтобы дойти до следующего аргумента по порядку
FRAME_RE = re.compile(r"^\d{1,7}$")
INT_RE = re.compile(r"^\d{1,10}$")
PREVIEW_SCALE, PREVIEW_CRF, FINAL_CRF = 0.5, 28, 16
CLIP_MIN_MS, CLIP_MAX_MS = 1000, 120_000
ANALYSIS_DIR, CLIPS_DIR = "analysis", "clips"
MOMENTS_FILE = "moments.json"
AUDIO_SHARE = 0.6                                # доля прогресса scan на звук и кадры, остальное речь


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def video_names(info: dict) -> list[str]:
    return [str(f["name"]) for f in info.get("files", []) if f.get("kind") == "video" and f.get("name")]


def parse_args(command: str, args: list[str], videos: list[str] | None = None) -> dict:
    """Аргументы команды после id единицы (список разрешённых): лишнего нет, числа целые неотрицательные, источник это имя видео
    из `sources/info.json` (`videos`; допустим и вид `sources/<имя>`), произвольных путей нет."""
    allowed = EXTRA_ARGS.get(command, ())
    if command == "find":                                   # фраза может прийти несколькими словами, источник (если есть) последним
        return _parse_find(args, videos)
    if len(args) > len(allowed):
        raise ToolError(f"команда «{command}»: после id единицы " + (f"допустимо: {', '.join(allowed)}" if allowed else "аргументов нет"),
                        code="invalid")
    if len(args) < REQUIRED_ARGS.get(command, 0):
        raise ToolError(f"команда «{command}»: после id единицы нужно: {', '.join(allowed[:REQUIRED_ARGS[command]])}", code="invalid")
    out: dict = {}
    for key, value in zip(allowed, args):
        if key == "frame":
            if not FRAME_RE.match(value):
                raise ToolError("frame: номер кадра, целое неотрицательное", code="invalid")
            out[key] = value
        elif key == "source":
            if command in ("scan", "moments", "speech") and value in ANY_SOURCE:
                continue
            name = value[len("sources/"):] if value.startswith("sources/") else value
            if videos is None or name not in videos:
                have = ", ".join(videos or []) or "видео нет, сначала run intake"
                raise ToolError(f"source: имя видео из sources/info.json ({have})", code="invalid")
            out[key] = name
        elif key in INT_ARGS:
            if command in SKIP_DEFAULT and value in ANY_SOURCE:
                continue
            if not INT_RE.match(value):
                raise ToolError(f"{key}: целое неотрицательное число", code="invalid")
            out[key] = int(value)
    if command == "clip" and out["out_ms"] <= out["in_ms"]:
        raise ToolError("out_ms должен быть больше in_ms", code="invalid")
    if command == "moments" and "limit" in out and out["limit"] < 1:
        raise ToolError("limit: от 1 до %d" % moments_mod.MAX_LIMIT, code="invalid")
    if command in SKIP_DEFAULT and out.get("from_ms") is not None and out.get("to_ms") is not None and out["to_ms"] < out["from_ms"]:
        raise ToolError("to_ms не может быть меньше from_ms", code="invalid")
    return out


def _parse_find(args: list[str], videos: list[str] | None) -> dict:
    """`find фраза [источник]`: последний аргумент, если он имя видео из info.json, это источник, остальное фраза."""
    words = list(args)
    out: dict = {}
    if len(words) > 1:
        last = words[-1][len("sources/"):] if words[-1].startswith("sources/") else words[-1]
        if videos is not None and last in videos:
            out["source"] = last
            words.pop()
    query = " ".join(w for w in words if w).strip()
    if not QUERY_MIN <= len(query) <= QUERY_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in query):
        raise ToolError(f"query: слово или фраза, {QUERY_MIN}–{QUERY_MAX} символов без управляющих знаков", code="invalid")
    out["query"] = query
    return out


class Commands:
    def __init__(self, store: JobStore, core: CoreAPI, layout: Layout, renderer: Renderer, qa: object | None = None,
                 setting: Callable[[str], object] = lambda name: None, stt_factory: Callable[[], object] | None = None):
        self.store = store
        self.core = core
        self.layout = layout
        self.renderer = renderer
        self.qa = qa if qa is not None else qa_mod.QAService(layout, renderer)
        self.setting = setting
        self.stt_factory = stt_factory or stt_client.HttpStt     # клиент stt на время `scan`; тесты подставляют заглушку

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

    # ================================================================================================ моменты: scan / moments / peek / clip / speech / find

    def _video_entry(self, job: Job, name: str) -> dict:
        entry = next((f for f in self._info(job).get("files", []) if f.get("kind") == "video" and f.get("name") == name), None)
        if entry is None or not (job.sources / name).is_file():
            raise ToolError(f"видео {name} нет в sources: сначала run intake", code="unavailable")
        return entry

    @staticmethod
    def _stem(name: str) -> str:
        return Path(name).stem

    @staticmethod
    def _signature(path: Path) -> dict:
        st = path.stat()                                   # для ссылки это размер и время настоящего файла
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}

    @staticmethod
    def _write_json(path: Path, data) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _read_analysis(self, job: Job, name: str) -> dict | None:
        """Кэш анализа видео, если он есть и сделан для этого же файла (размер и время изменения)."""
        data = read_json(job.folder / ANALYSIS_DIR / f"{name}.json")
        path = job.sources / name
        if not isinstance(data, dict) or data.get("version") != moments_mod.CACHE_VERSION or not path.is_file():
            return None
        sig = self._signature(path)
        return data if data.get("size") == sig["size"] and data.get("mtime_ns") == sig["mtime_ns"] else None

    async def _cmd_scan(self, job: Job, args: dict, progress: Progress) -> dict:
        """Анализ длинных видео: за один проход ffmpeg громкость по секундам, смены кадра и тишина; затем речь (stt) по участкам
        между тишинами. Всё в кэше `analysis/<имя>.json` (не пересчитывается, пока файл не изменился) и ранжированные моменты
        всех видео единицы в `analysis/moments.json`. Недоступный stt не роняет команду: речь просто не распознана."""
        for finder, label in ((media.find_ffmpeg, "ffmpeg"), (media.find_ffprobe, "ffprobe")):
            try:
                finder()
            except FileNotFoundError:
                raise ToolError(f"не найден {label}: анализ видео невозможен", code="unavailable") from None
        names = video_names(self._info(job))
        if not names:
            raise ToolError("в sources нет видео: сначала run intake", code="unavailable")
        chosen = [args["source"]] if args.get("source") else names
        folder = job.folder / ANALYSIS_DIR
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        cached: list[str] = []
        stt_down = False
        stt = None
        n = len(chosen)

        def reporter(k: int, name: str, base: float, share: float, label: str):
            last = [-1.0]

            def on_pct(frac: float) -> None:                      # не чаще раза на 2%: статус видит и агент, и пользователь
                if frac - last[0] >= 0.02:
                    last[0] = frac
                    loop.create_task(progress((k + base + share * frac) / n, f"{label} {name}: {int((base + share * frac) * 100)} %"))
            return on_pct

        try:
            for k, name in enumerate(chosen):
                entry = self._video_entry(job, name)
                path = job.sources / name
                data = await asyncio.to_thread(self._read_analysis, job, name)
                if data is None:
                    await progress(k / n, f"Анализ {name}: 0 %")
                    sig = await asyncio.to_thread(self._signature, path)
                    dur = (entry.get("duration_ms") or 0) / 1000
                    data = await media.analyze_video(path, dur, bool(entry.get("has_audio")),
                                                     reporter(k, name, 0.0, AUDIO_SHARE, "Анализ"))
                    data.update(version=moments_mod.CACHE_VERSION, source=name, duration_s=round(dur, 2), speech=[],
                                speech_done=False, **sig)
                    await asyncio.to_thread(self._write_json, folder / f"{name}.json", data)
                elif data.get("speech_done") or not data.get("has_audio"):
                    cached.append(name)
                if data.get("speech_done") or not data.get("has_audio") or stt_down:
                    continue
                if stt is None:
                    stt = self.stt_factory()
                data["speech"], down = await self._recognize(path, data, stt, reporter(k, name, AUDIO_SHARE, 1 - AUDIO_SHARE, "Речь"),
                                                              folder)
                if down:
                    stt_down = True
                    if not data["speech"]:
                        continue                                  # ничего не распознано: повторим при следующем scan
                else:
                    data["speech_done"] = True
                await asyncio.to_thread(self._write_json, folder / f"{name}.json", data)
        finally:
            close = getattr(stt, "aclose", None)
            if close is not None:
                await close()
        counts = await asyncio.to_thread(self._rebuild_moments, job)
        shown = ", ".join(f"{name}: {counts.get(name, 0)}" for name in chosen)
        words = 0
        for name in chosen:
            data = await asyncio.to_thread(self._read_analysis, job, name)
            words += sum(len(s["text"].split()) for s in (data or {}).get("speech") or [])
        text = f"моментов найдено: {shown}" + (f" (звук из кэша: {', '.join(cached)})" if cached else "")
        text += f"; речь: слов {words}" if words else ""
        if stt_down:
            text += "; речь не распознана: stt недоступен (повтори run scan позже)"
        return {"moments": counts, "from_cache": cached, "speech_words": words, "stt_available": not stt_down,
                "path": f"{ANALYSIS_DIR}/{MOMENTS_FILE}", "text": text}

    async def _recognize(self, path: Path, data: dict, stt, on_pct, folder: Path) -> tuple[list[dict], bool]:
        """Речь видео: участки между тишинами, куски до 25 с, звук одним проходом ffmpeg в PCM-файл и куски в stt (2 разом).
        Возвращает (речь, stt недоступен)."""
        dur = float(data.get("duration_s") or 0)
        spans = speech_mod.speech_spans(data.get("silences") or [], dur)
        chunks = speech_mod.split_chunks(spans, dur, data.get("rms_db"))
        if not chunks:
            return [], False
        pcm_path = folder / f"{path.name}.pcm.tmp"
        try:
            await media.extract_pcm_file(path, pcm_path)

            def read_pcm(start_ms: int, end_ms: int) -> bytes:
                offset, length = speech_mod.pcm_slice_bounds(start_ms, end_ms)
                with open(pcm_path, "rb") as f:
                    f.seek(offset)
                    return f.read(length)
            return await speech_mod.transcribe_chunks(chunks, read_pcm, stt.transcribe, on_progress=on_pct)
        finally:
            await asyncio.to_thread(pcm_path.unlink, missing_ok=True)

    def _rebuild_moments(self, job: Job) -> dict[str, int]:
        """`analysis/moments.json` из кэшей всех видео единицы (дёшево: ffmpeg не нужен); возвращает число моментов по видео."""
        per_source, counts = [], {}
        for f in self._info(job).get("files", []):
            if f.get("kind") != "video" or not f.get("name"):
                continue
            data = self._read_analysis(job, f["name"])
            if data is None:
                continue
            items = moments_mod.rank_moments(data, float(data.get("duration_s") or 0))
            per_source.append((f["name"], items))
            counts[f["name"]] = len(items)
        self._write_json(job.folder / ANALYSIS_DIR / MOMENTS_FILE, {"version": moments_mod.CACHE_VERSION,
                                                                   "moments": moments_mod.assign_ids(per_source)})
        return counts

    async def _cmd_moments(self, job: Job, args: dict, progress: Progress) -> dict:
        """Лучшие моменты из `analysis/moments.json` текстом (ffmpeg не нужен)."""
        data = read_json(job.folder / ANALYSIS_DIR / MOMENTS_FILE)
        if not isinstance(data, dict) or not isinstance(data.get("moments"), list):
            raise ToolError("моментов ещё нет: сначала run scan", code="unavailable")
        found = moments_mod.select_moments(data["moments"], source=args.get("source"),
                                           limit=args.get("limit") or moments_mod.DEFAULT_LIMIT,
                                           from_ms=args.get("from_ms"), to_ms=args.get("to_ms"))
        if not found:
            return {"moments": [], "text": "подходящих моментов нет (проверь источник и диапазон времени)"}
        head = f"моментов: показано {len(found)} из {len(data['moments'])}, по убыванию оценки; in/out и at в мс исходника"
        return {"moments": found, "text": head + "\n" + "\n".join(moments_mod.moment_line(m) for m in found)}

    def _speech_of(self, job: Job, source: str | None) -> list[tuple[str, float, list[dict]]]:
        """Распознанная речь видео единицы (по одному или по всем): [(имя, длительность с, речь)]."""
        out = []
        for name in video_names(self._info(job)):
            if source and name != source:
                continue
            data = self._read_analysis(job, name)
            if data is not None and data.get("speech"):
                out.append((name, float(data.get("duration_s") or 0), data["speech"]))
        return out

    def _no_speech(self, job: Job, source: str | None) -> ToolError:
        scanned = [n for n in video_names(self._info(job)) if (not source or n == source) and self._read_analysis(job, n)]
        if not scanned:
            return ToolError("сначала run scan: расшифровки ещё нет", code="unavailable")
        return ToolError("речи в расшифровке нет: в видео нет слов или stt был недоступен при run scan (повтори run scan позже)",
                         code="unavailable")

    async def _cmd_speech(self, job: Job, args: dict, progress: Progress) -> dict:
        """Расшифровка речи по времени текстом: `[12:05–12:11] текст` (время куска, слов-времён нет)."""
        found = await asyncio.to_thread(self._speech_of, job, args.get("source"))
        if not found:
            raise self._no_speech(job, args.get("source"))
        lines: list[str] = []
        more = 0
        for name, _, speech in found:
            rows, rest = speech_mod.speech_lines(speech, args.get("from_ms"), args.get("to_ms"),
                                                 max_lines=speech_mod.SPEECH_LINES - len(lines))
            more += rest
            lines += [f"{name} {r}" if len(found) > 1 else r for r in rows]
        if not lines:
            return {"lines": [], "text": "в этом диапазоне речи нет"}
        head = f"расшифровка ({found[0][0] if len(found) == 1 else 'все видео'}): куски речи с временем куска, слов-времён нет"
        tail = f"\nещё строк: {more}; сузь диапазон (от_мс до_мс)" if more else ""
        return {"lines": lines, "more": more, "text": head + "\n" + "\n".join(lines) + tail}

    async def _cmd_find(self, job: Job, args: dict, progress: Progress) -> dict:
        """Поиск слова или фразы по расшифровке (без регистра и окончаний): время, источник, строка и готовые in/out."""
        found = await asyncio.to_thread(self._speech_of, job, args.get("source"))
        if not found:
            raise self._no_speech(job, args.get("source"))
        hits = []
        for name, dur, speech in found:
            hits += [{"source": name, **h} for h in speech_mod.find_in_speech(speech, args["query"], dur)]
        hits = hits[:speech_mod.FIND_LIMIT]
        if not hits:
            return {"matches": [], "text": f"«{args['query']}» в расшифровке не найдено"}
        head = (f"найдено {len(hits)}: время внутри куска речи оценено по положению слова в тексте (слов-времён нет), "
                "окно in/out ±4 с вокруг оценки; проверь через peek")
        return {"matches": hits, "text": head + "\n" + "\n".join(speech_mod.find_line(h) for h in hits)}

    def _ocr_fn(self):
        ocr = getattr(self.qa, "ocr", None)
        if ocr is None and qa_mod.ocr_available():
            ocr = qa_mod.QAService._default_ocr()
        return ocr

    async def _cmd_peek(self, job: Job, args: dict, progress: Progress) -> dict:
        """Три кадра вокруг времени (t-1 с, t, t+1 с) в `renders/peek_*.png`; текст на кадрах (OCR), громкость вокруг и речь рядом."""
        name, t_ms = args["source"], args["t_ms"]
        entry = self._video_entry(job, name)
        dur_ms = int(entry.get("duration_ms") or 0)
        if t_ms > dur_ms:
            raise ToolError(f"t_ms: в видео {name} время 0–{dur_ms} мс", code="invalid")
        job.renders.mkdir(parents=True, exist_ok=True)
        last_s = max(0.0, dur_ms / 1000 - 0.1)                    # кадр ровно на конце файла ffmpeg не отдаёт
        frames: list[tuple[str, Path]] = []
        for k, (label, shift) in enumerate((("t-1с", -1000), ("t", 0), ("t+1с", 1000))):
            out = job.renders / f"peek_{self._stem(name)}_{t_ms}_{k}.png"
            await media.extract_frame(job.sources / name, min(last_s, max(0.0, (t_ms + shift) / 1000)), out)
            frames.append((label, out))
        rels = [f"renders/{p.name}" for _, p in frames]
        ocr = self._ocr_fn()
        parts = [f"кадры {name} около {moments_mod.clock(t_ms)} ({t_ms} мс): " + ", ".join(rels)]
        texts: list[str] = []
        if ocr is not None:
            seen = []
            for _, path in frames:
                try:
                    seen.append(await ocr(path))
                except Exception as e:                           # сбой распознавания одного кадра не роняет команду
                    log.info("OCR кадра %s: %s", path.name, e)
            texts = moments_mod.merge_texts(seen)
            parts.append("на экране написано: " + "; ".join(f"«{t}»" for t in texts) if texts else "на экране текста не распознано")
        else:
            parts.append("OCR не установлен: что на экране, сказать не могу (картинок я не вижу)")
        analysis = await asyncio.to_thread(self._read_analysis, job, name)
        levels = moments_mod.loudness_around(analysis, t_ms) if analysis else []
        if levels:
            parts.append("громкость по секундам, дБ: " + ", ".join(
                f"{moments_mod.clock(s * 1000)} {'тишина' if v is None else v}" for s, v in levels))
            near = [s for s in (analysis.get("speech") or []) if s["end_ms"] >= t_ms - 3000 and s["start_ms"] <= t_ms + 3000]
            if near:
                parts.append("рядом говорят: " + "; ".join(f"[{moments_mod.clock(s['start_ms'])}–{moments_mod.clock(s['end_ms'])}] "
                                                           f"«{moments_mod.short_text(s['text'], 120)}»" for s in near[:3]))
        elif ocr is None:
            parts.append("уровней громкости тоже нет: сделай run scan")
        return {"frames": rels, "ocr": ocr is not None, "texts": texts, "levels": levels, "text": ". ".join(parts)}

    async def _cmd_clip(self, job: Job, args: dict, progress: Progress) -> dict:
        """Короткий клип исходника для просмотра пользователем на сайте: `clips/<имя>_<in>_<out>.mp4`."""
        name, in_ms, out_ms = args["source"], args["in_ms"], args["out_ms"]
        entry = self._video_entry(job, name)
        dur_ms = int(entry.get("duration_ms") or 0)
        length = out_ms - in_ms
        if not CLIP_MIN_MS <= length <= CLIP_MAX_MS:
            raise ToolError(f"клип длиной {length / 1000:.1f} с: нужно от {CLIP_MIN_MS // 1000} до {CLIP_MAX_MS // 1000} с", code="invalid")
        if out_ms > dur_ms:
            raise ToolError(f"out_ms: в видео {name} время 0–{dur_ms} мс", code="invalid")
        folder = job.folder / CLIPS_DIR
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        out = folder / f"{self._stem(name)}_{in_ms}_{out_ms}.mp4"
        tmp = out.with_name(out.stem + ".part.mp4")                 # ffmpeg выбирает формат по расширению, поэтому оно последнее
        try:
            await media.cut_clip(job.sources / name, in_ms / 1000, length / 1000, tmp)
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)
        rel = f"{CLIPS_DIR}/{out.name}"
        return {"path": rel, "duration_ms": length, "text": f"клип {length / 1000:.1f} с: {rel}"}

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

    # ================================================================================================ compose

    async def _cmd_compose(self, job: Job, args: dict, progress: Progress) -> dict:
        """Монтажный лист из сцен (`plan/scenes.json`, его пишет `work op=scenes`): озвучивает фразы, раскладывает сцены встык,
        ставит реплики внутрь своей сцены и пишет `plan/plan.json` (только если он прошёл проверку правил)."""
        spec = read_json(job.plan_dir / scenes_mod.SCENES_FILE)
        if not isinstance(spec, dict) or not spec.get("scenes"):
            raise ToolError("сцен ещё нет: сначала work op=scenes с описанием сцен", code="unavailable")
        story = read_json(job.plan_dir / "story.json")
        story = story if isinstance(story, dict) else None
        ctx = self._plan_context(job, story)
        scenes, problems = scenes_mod.parse_scenes(spec["scenes"], ctx, story_beat_ids(story) if story else None)
        if problems:
            raise ToolError("сцены не приняты: " + "; ".join(problems[:8]), code="invalid")
        mode = normalize_voiceover(job.request.get("voiceover"))
        if any(s["say"] for s in scenes) and mode != "character":
            job.request["voiceover"] = "character"          # фразы в сценах означают закадр её голосом
            job.data.pop("check", None)
        cache = load_voice_cache(job)
        durations: dict[str, int] = {}
        todo = [(n, k, text, s["emotion"]) for n, s in enumerate(scenes, 1) for k, text in enumerate(s["say"], 1)]
        made = 0
        for index, (n, k, text, emotion) in enumerate(todo):
            pid = scenes_mod.phrase_id(n, k)
            cached, wav = cache.get(pid), job.work / "public" / "vo" / f"{pid}.wav"
            if cached and cached.get("hash") == voice_digest(text, emotion) and wav.is_file():
                durations[pid] = int(cached["duration_ms"])
                continue
            await progress(index / max(1, len(todo)), f"фраза {index + 1} из {len(todo)}")
            durations[pid] = (await synthesize_to(self.core, job, pid, text, emotion))["duration_ms"]
            made += 1
        plan, layout = scenes_mod.build_plan(scenes, durations, title=str(spec.get("title") or job.data.get("title") or "Ролик"),
                                             fmt=job.request.get("format") or "horizontal", fps=30, platform=self._platform(job),
                                             cta_text=spec.get("cta") or None, ctx=ctx)
        for row in layout:
            high = scenes_mod.HOLD[row["kind"]][2]
            if row["dur_ms"] > high:
                problems.append(f"сцена {row['id']} ({row['kind']}): фразы занимают {row['dur_ms']} мс, а такая сцена держится не дольше "
                                f"{high} мс; для речи используй комнату (glados_room) или сократи фразы")
        problems += validate_plan(plan, ctx)
        if problems:
            raise ToolError("монтажный лист по сценам не прошёл проверку: " + "; ".join(problems[:8]), code="invalid")
        await asyncio.to_thread((job.plan_dir / "plan.json").write_text, json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")
        job.data.pop("check", None)
        await self._save(job)
        timeline = ", ".join(f"{r['id']} {r['kind']} {r['at_ms']}–{r['at_ms'] + r['dur_ms']} мс" for r in layout)
        return {"scenes": len(scenes), "phrases": len(todo), "synthesized": made, "duration_ms": plan["duration_ms"], "layout": layout,
                "text": f"монтажный лист собран: {len(scenes)} сцен, {plan['duration_ms'] / 1000:.1f} с ({timeline})"}

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
                problems.append(f"voiceover: режим {r.get('voiceover')} несовместим с репликами плана. "
                                f"Если нужен закадр, вызови work op=update, id={job.job_id}, voiceover=character, затем run voiceover. "
                                "Если закадр не нужен, убери реплики из плана.")
            if not problems:
                await asyncio.to_thread(sync_intro, job.work, self.layout)
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
        _, md = await asyncio.to_thread(qa_mod.write_report, job.qa, job.job_id, outcome, draft=True)
        errors = [f for f in findings if f.severity == "error"]
        passed = not errors
        job.data["check"] = {"passed": passed, "plan_hash": plan_hash, "at": round(time.time(), 1), "errors": len(errors),
                             "warnings": len(findings) - len(errors), "report": "qa/report.md"}
        job.add_artifact(md, "qa_report")
        await self._save(job)
        text = ("проверка пройдена" if passed else f"проверка не пройдена: ошибок {len(errors)}") + f", предупреждений {len(findings) - len(errors)}"
        return {"passed": passed, "errors": len(errors), "warnings": len(findings) - len(errors), "report": f"{job.job_id}/qa/report.md",
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
        _, mp = await asyncio.to_thread(qa_mod.write_report, job.qa, job.job_id, outcome, draft=False, stem="report_final")
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
        pending = target.with_name(f".{target.name}.{job_id}.tmp")
        try:
            shutil.copyfile(final, pending)
            pending.replace(target)
        finally:
            pending.unlink(missing_ok=True)
        return target
