"""Стадия intake (ТЗ 20, раздел 6): проверить файлы, перекодировать HEVC в H.264, положить ссылки в `sources/`, определить
вид исходников. Результат — `sources/info.json` (имена файлов в папке задачи, длительности, размеры) и дополненный
`job.json.request`."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import media
from glados_module import ToolError
from workspace import link_or_copy

VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts", ".mts"}
AUDIO = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT = {".txt", ".md", ".srt", ".vtt"}
MAX_FILES = 500
SPECIAL_JSON = ("scenario.json", "speech_log.json", "recording.json")
Progress = Callable[[float, str], Awaitable[None]]


def kind_of(path: Path) -> str:
    s = path.suffix.lower()
    if s in VIDEO:
        return "video"
    if s in AUDIO:
        return "audio"
    if s in IMAGE:
        return "image"
    if s == ".json":
        return "json"
    if s in TEXT:
        return "text"
    return "other"


def expand_sources(paths: list[str]) -> list[Path]:
    """Файлы и папки (в папке — файлы до глубины 2) -> список файлов; повторы убираются."""
    files: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        found = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file() and len(f.relative_to(p).parts) <= 2) if p.is_dir() else []
        for f in found:
            if f.resolve() not in seen:
                seen.add(f.resolve())
                files.append(f)
    return files[:MAX_FILES]


def safe_name(name: str, taken: set[str]) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix
    stem = re.sub(r"[^\w.-]+", "_", stem, flags=re.U).strip("._") or "file"
    out = f"{stem}{suffix.lower()}"
    k = 2
    while out.lower() in taken:
        out = f"{stem}_{k}{suffix.lower()}"
        k += 1
    taken.add(out.lower())
    return out


def detect_kind(files: list[Path], brief: str, infos: dict[str, media.MediaInfo]) -> str:
    names = {f.name.lower() for f in files}
    if "scenario.json" in names:
        return "rimworld_run"
    videos = [f for f in files if kind_of(f) == "video"]
    if not videos and not [f for f in files if kind_of(f) == "audio"]:
        return "narrated"
    if videos and all(not infos[str(f)].has_audio for f in videos):
        return "narrated"
    if videos and re.search(r"закадр|озвуч|голос|комментар", brief, re.I):
        return "narrated"
    return "talking_head" if videos else "generic"


async def run_intake(job_dir: Path, request: dict, progress: Progress | None = None) -> dict:
    """Выполнить стадию. Возвращает `info` (то же, что записано в `sources/info.json`). Идемпотентно: готовые перекодировки
    не повторяются, если файл уже есть."""
    sources_dir = job_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    files = await asyncio.to_thread(expand_sources, list(request.get("sources") or []))
    if not files:
        if request.get("source_kind") in ("narrated", None) and not request.get("sources"):
            files = []
        else:
            raise ToolError("в исходниках не нашлось ни одного файла", code="bad_request")
    infos: dict[str, media.MediaInfo] = {}
    for f in files:
        if kind_of(f) in ("video", "audio"):
            infos[str(f)] = await media.probe(f)
            if kind_of(f) == "video" and not infos[str(f)].has_video:
                raise ToolError(f"в файле {f.name} нет видеодорожки", code="bad_request")
    kind = request.get("source_kind") or detect_kind(files, str(request.get("brief") or ""), infos)
    taken: set[str] = set()
    out_files: list[dict] = []
    recording = await _read_json(next((f for f in files if f.name.lower() == "recording.json"), None))
    raw_video = _pick_raw_video(files, infos) if kind == "rimworld_run" else None
    audio_files = [f for f in files if kind_of(f) == "audio"]
    n = len(files)
    for k, f in enumerate(files):
        if progress:
            await progress(k / max(1, n), f"проверяю {f.name}")
        k_ = kind_of(f)
        entry: dict = {"original": str(f), "kind": k_}
        if k_ == "video":
            info = infos[str(f)]
            use_audio = f == raw_video and bool(audio_files)
            transcode = media.needs_transcode(info) or use_audio or (kind == "rimworld_run" and f == raw_video)
            if transcode:
                name = safe_name(f"{f.stem}_h264.mp4", taken)
                dst = sources_dir / name
                if not dst.is_file():
                    if progress:
                        await progress(k / max(1, n), f"перекодирую {f.name} в H.264")
                    track = None
                    if use_audio:
                        track = await _assemble_track(job_dir, f, info, audio_files, recording)
                    tmp = dst.with_suffix(".part.mp4")
                    await media.transcode_h264(f, tmp, fps=30 if kind == "rimworld_run" else None, audio=track)
                    tmp.replace(dst)
                    if track is not None:
                        track.unlink(missing_ok=True)
            else:
                name = safe_name(f.name, taken)
                dst = sources_dir / name
                await asyncio.to_thread(link_or_copy, f, dst)
            final = await media.probe(dst)
            entry.update(name=name, path=f"sources/{name}", duration_ms=int(final.duration_s * 1000), width=final.width,
                         height=final.height, fps=round(final.fps, 2), has_audio=final.has_audio, vcodec=final.vcodec,
                         transcoded=transcode)
        else:
            if f.name.lower() in SPECIAL_JSON:
                name = f.name.lower()
                taken.add(name)
            else:
                name = safe_name(f.name, taken)
            await asyncio.to_thread(link_or_copy if k_ in ("audio", "image") else shutil.copyfile, f, sources_dir / name)
            entry.update(name=name, path=f"sources/{name}")
            if k_ == "audio":
                entry["duration_ms"] = int(infos[str(f)].duration_s * 1000)
        out_files.append(entry)
    total = sum(f.stat().st_size for f in files)
    info = {"source_kind": kind, "files": out_files, "total_bytes": total}
    await asyncio.to_thread((sources_dir / "info.json").write_text, json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
    return info


def _pick_raw_video(files: list[Path], infos: dict[str, media.MediaInfo]) -> Path | None:
    videos = [f for f in files if kind_of(f) == "video"]
    if not videos:
        return None
    raws = [f for f in videos if f.stem.lower().startswith("raw")]
    return max(raws or videos, key=lambda f: infos[str(f)].duration_s)


async def _read_json(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        return json.loads(await asyncio.to_thread(path.read_text, "utf-8"))
    except (OSError, ValueError):
        return None


async def _assemble_track(job_dir: Path, video: Path, info: media.MediaInfo, audio_files: list[Path],
                          recording: dict | None) -> Path:
    """Звук записи на шкале видео: куски по времени начала (recording.json, ТЗ 11b) или один файл с нуля."""
    parts: list[tuple[Path, float]] = []
    by_name = {f.name: f for f in audio_files}
    listed = (recording or {}).get("audio_parts") or []
    started = float((recording or {}).get("started_at") or 0)
    for p in listed:
        name = Path(str(p.get("path") or "")).name
        f = by_name.get(name) or (Path(p["path"]) if p.get("path") and Path(p["path"]).is_file() else None)
        if f is not None and p.get("started_at") is not None:
            parts.append((f, float(p["started_at"]) - started))
    if not parts:
        parts = [(f, 0.0) for f in audio_files[:1]]
    dst = job_dir / "sources" / "_audio_track.wav"
    await media.assemble_audio(parts, info.duration_s, dst)
    return dst
