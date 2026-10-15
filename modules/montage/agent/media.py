"""ffmpeg и ffprobe: поиск программ (MON-2), проверка файлов, перекодирование, кадры, анализ звука и картинки (ТЗ 20).

Любой внешний процесс запускается через `run_cmd`: асинхронно, без окна консоли, с убийством всего дерева процессов при
отмене задачи (MON-12). Быстрые вспомогательные вызовы (ffprobe, silencedetect, кадры) пользователю не объявляются
(README, правило 14); долгие (перекодирование, рендер) объявляет конвейер.
"""
import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# winget добавляет ffmpeg в PATH только для новых сессий: если процесс стартовал раньше, ищем по типовым путям
# (функция скопирована из GitS: modules/voice/opus_encoder.find_ffmpeg)
_FALLBACKS = (
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    "C:/Users/*/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe",
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for pattern in _FALLBACKS:
        for m in glob.glob(pattern):
            return m
    raise FileNotFoundError("ffmpeg не найден. Установи: winget install --id Gyan.FFmpeg")


def find_ffprobe() -> str:
    """ffprobe лежит рядом с ffmpeg (в одной сборке)."""
    found = shutil.which("ffprobe")
    if found:
        return found
    ffmpeg_path = find_ffmpeg()
    candidate = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe" if os.name == "nt" else "ffprobe")
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError("ffprobe не найден рядом с ffmpeg")


def find_npx() -> str:
    found = shutil.which("npx")
    if not found:
        raise FileNotFoundError("npx не найден: нужен Node.js 18+ (node --version)")
    return found


# ---------------------------------------------------------------------------------------------------- процессы

@dataclass
class CmdResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def kill_tree(pid: int) -> None:
    """Убить процесс и всех его потомков (Windows: taskkill /T /F; иначе группа процессов)."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def kill_processes_under(folder: Path) -> int:
    """Убить процессы, в командной строке которых есть путь папки задачи (остатки после отмены: рендер Remotion, браузер,
    ffmpeg, которые агент запустил через свою оболочку). Возвращает число убитых. Только Windows; в других системах 0."""
    if sys.platform != "win32":
        return 0
    needle = str(folder).replace("'", "''")
    script = ("$n = '" + needle + "'; $me = $PID; "
              "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $me -and $_.CommandLine -and "
              "$_.CommandLine.Contains($n) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
              "-ErrorAction SilentlyContinue; $_.ProcessId }")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                              text=True, timeout=60, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return 0
    return len([ln for ln in done.stdout.split() if ln.isdigit()])


async def run_cmd(args: list, *, cwd: Path | str | None = None, on_line=None, timeout: float | None = None,
                  env: dict | None = None) -> CmdResult:
    """Запустить программу и дождаться конца. `on_line(строка)` вызывается для каждой строки stdout и stderr (в Remotion
    прогресс идёт возвратами каретки, они считаются концами строк). Отмена задачи или тайм-аут убивают дерево процессов."""
    proc = await asyncio.create_subprocess_exec(
        *map(str, args), cwd=str(cwd) if cwd else None, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL, creationflags=_NO_WINDOW | _NEW_GROUP, env={**os.environ, **(env or {})})
    out: list[str] = []
    err: list[str] = []

    async def pump(stream, sink):
        buf = b""
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace")
                sink.append(line)
                if on_line is not None and line.strip():
                    on_line(line)
        if buf:
            line = buf.decode("utf-8", "replace")
            sink.append(line)
            if on_line is not None and line.strip():
                on_line(line)

    try:
        await asyncio.wait_for(asyncio.gather(pump(proc.stdout, out), pump(proc.stderr, err), proc.wait()), timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.to_thread(kill_tree, proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            pass
        raise
    return CmdResult(proc.returncode or 0, "\n".join(out), "\n".join(err))


async def ffmpeg(args: list, **kw) -> CmdResult:
    return await run_cmd([find_ffmpeg(), "-hide_banner", "-y", *args], **kw)


class MediaError(RuntimeError):
    """Сбой ffmpeg/ffprobe с понятным текстом (без трассировки)."""


def _tail(text: str, n: int = 400) -> str:
    return text.strip()[-n:]


# ---------------------------------------------------------------------------------------------------- проверка

@dataclass
class MediaInfo:
    path: str
    duration_s: float = 0.0
    size_bytes: int = 0
    has_video: bool = False
    has_audio: bool = False
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    pix_fmt: str = ""
    streams: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("path", "duration_s", "size_bytes", "has_video", "has_audio", "width",
                                              "height", "fps", "vcodec", "acodec", "pix_fmt")}


def _fps(rate: str) -> float:
    try:
        a, _, b = rate.partition("/")
        den = float(b or 1)
        return float(a) / den if den else 0.0
    except ValueError:
        return 0.0


async def probe(path: Path | str) -> MediaInfo:
    res = await run_cmd([find_ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                        timeout=120)
    if not res.ok:
        raise MediaError(f"ffprobe не открыл файл {Path(path).name}: {_tail(res.err)}")
    try:
        doc = json.loads(res.out)
    except ValueError:
        raise MediaError(f"ffprobe вернул неразборчивый ответ для {Path(path).name}")
    info = MediaInfo(path=str(path), streams=doc.get("streams") or [])
    fmt = doc.get("format") or {}
    info.duration_s = float(fmt.get("duration") or 0.0)
    info.size_bytes = int(fmt.get("size") or 0)
    for s in info.streams:
        if s.get("codec_type") == "video" and not info.has_video and not (s.get("disposition") or {}).get("attached_pic"):
            info.has_video = True
            info.width, info.height = int(s.get("width") or 0), int(s.get("height") or 0)
            info.fps = _fps(str(s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"))
            info.vcodec, info.pix_fmt = str(s.get("codec_name") or ""), str(s.get("pix_fmt") or "")
            if not info.duration_s:
                info.duration_s = float(s.get("duration") or 0.0)
        elif s.get("codec_type") == "audio" and not info.has_audio:
            info.has_audio = True
            info.acodec = str(s.get("codec_name") or "")
            if not info.duration_s:
                info.duration_s = float(s.get("duration") or 0.0)
    return info


def needs_transcode(info: MediaInfo) -> bool:
    """Chromium в Remotion не декодирует HEVC (регламент §10) и 10-битные форматы: такие видео перекодируются в H.264."""
    if not info.has_video:
        return False
    return info.vcodec != "h264" or info.pix_fmt not in ("yuv420p", "yuvj420p", "")


async def transcode_h264(src: Path, dst: Path, *, fps: int | None = None, audio: Path | None = None) -> None:
    """`ffmpeg -c:v libx264 -crf 18 -c:a aac` (стадия intake). `fps` приводит запись с переменной частотой кадров (захват
    окна пишет кадр только при изменении картинки) к постоянной; `audio` подмешивает готовую дорожку."""
    args = ["-i", str(src)]
    if audio is not None:
        args += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
    if fps:
        args += ["-vf", f"fps={fps}"]
    args += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", str(dst)]
    res = await ffmpeg(args)
    if not res.ok or not dst.is_file():
        raise MediaError(f"не удалось перекодировать {src.name} в H.264: {_tail(res.err)}")


async def extract_audio_16k(src: Path, dst: Path) -> None:
    res = await ffmpeg(["-i", str(src), "-vn", "-ar", "16000", "-ac", "1", str(dst)])
    if not res.ok:
        raise MediaError(f"не удалось извлечь звук из {src.name}: {_tail(res.err)}")


async def extract_frame(video: Path, t_s: float, out: Path, *, width: int | None = None) -> None:
    """Один кадр в момент t (быстро: ffmpeg, не Remotion)."""
    vf = ["-vf", f"scale={width}:-2"] if width else []
    res = await ffmpeg(["-ss", f"{max(0.0, t_s):.3f}", "-i", str(video), "-frames:v", "1", *vf, str(out)])
    if not res.ok or not out.is_file():
        raise MediaError(f"не удалось взять кадр {t_s:.1f} с из {video.name}: {_tail(res.err)}")


# ---------------------------------------------------------------------------------------------------- анализ

_SIL_START = re.compile(r"silence_start: (-?[\d.]+)")
_SIL_END = re.compile(r"silence_end: (-?[\d.]+)")


def parse_intervals(text: str, start_re: re.Pattern, end_re: re.Pattern, end_of: float | None = None) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    start = None
    for line in text.splitlines():
        s, e = start_re.search(line), end_re.search(line)
        if s:
            start = max(0.0, float(s.group(1)))
        if e and start is not None:
            out.append((start, float(e.group(1))))
            start = None
    if start is not None and end_of is not None and end_of > start:
        out.append((start, end_of))
    return out


async def silencedetect(path: Path, *, noise_db: float = -32, min_s: float = 0.05,
                        limit_s: float | None = None) -> list[tuple[float, float]]:
    """Паузы в звуке: [(начало, конец)] в секундах (MON-QA-1). Пауза до конца файла закрывается длительностью файла
    (или `limit_s`, если проверяется только начало)."""
    limit = ["-t", f"{limit_s:.3f}"] if limit_s else []
    res = await ffmpeg([*limit, "-i", str(path), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"])
    if not res.ok:
        raise MediaError(f"silencedetect не сработал: {_tail(res.err)}")
    end = min((await probe(path)).duration_s, limit_s) if limit_s else (await probe(path)).duration_s
    return parse_intervals(res.err, _SIL_START, _SIL_END, end_of=end)


_BLACK = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")
_FREEZE_S = re.compile(r"freeze_start: ([\d.]+)")
_FREEZE_E = re.compile(r"freeze_end: ([\d.]+)")


async def blackdetect(path: Path, *, min_s: float = 0.5, pix_th: float = 0.05) -> list[tuple[float, float]]:
    """Чёрные участки. Порог 0.05 ниже яркости фона `chamberDark` (около 0.09): фон вставок чёрным не считается."""
    res = await ffmpeg(["-i", str(path), "-an", "-vf", f"blackdetect=d={min_s}:pix_th={pix_th}:pic_th=0.98", "-f", "null", "-"])
    if not res.ok:
        raise MediaError(f"blackdetect не сработал: {_tail(res.err)}")
    return [(float(a), float(b)) for a, b in _BLACK.findall(res.err)]


async def freezedetect(path: Path, *, min_s: float = 2.0, noise_db: float = -60,
                       limit_s: float | None = None) -> list[tuple[float, float]]:
    limit = ["-t", f"{limit_s:.3f}"] if limit_s else []
    res = await ffmpeg([*limit, "-i", str(path), "-an", "-vf", f"freezedetect=n={noise_db}dB:d={min_s}", "-f", "null", "-"])
    if not res.ok:
        raise MediaError(f"freezedetect не сработал: {_tail(res.err)}")
    end = min((await probe(path)).duration_s, limit_s) if limit_s else (await probe(path)).duration_s
    return parse_intervals(res.err, _FREEZE_S, _FREEZE_E, end_of=end)


_EBU_I = re.compile(r"^\s*I:\s+(-?[\d.]+|-inf)\s+LUFS", re.M)
_EBU_PEAK = re.compile(r"True peak:\s*\n\s*Peak:\s+(-?[\d.]+|-inf)\s+dBFS", re.M)
_EBU_LRA = re.compile(r"^\s*LRA:\s+(-?[\d.]+)\s+LU", re.M)


def _num(vals: list[str]) -> float | None:
    try:
        return float(vals[-1]) if vals else None
    except ValueError:
        return None


async def loudness(path: Path) -> dict:
    """Интегральная громкость (LUFS), пик (dBTP) и LRA по ebur128 (MON-QA-1). Нет звука: integral None."""
    res = await ffmpeg(["-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    if not res.ok:
        raise MediaError(f"ebur128 не сработал: {_tail(res.err)}")
    tail = res.err[res.err.rfind("Summary:"):] if "Summary:" in res.err else res.err
    return {"integral": _num(_EBU_I.findall(tail)), "true_peak": _num(_EBU_PEAK.findall(tail)),
            "lra": _num(_EBU_LRA.findall(tail))}


async def max_level(path: Path, start_s: float, dur_s: float) -> float | None:
    """Максимальный уровень звука в окне, дБ (volumedetect): None, если окна нет в файле или в нём цифровой ноль."""
    res = await ffmpeg(["-ss", f"{start_s:.3f}", "-t", f"{dur_s:.3f}", "-i", str(path), "-vn", "-af", "volumedetect", "-f",
                        "null", "-"])
    m = re.search(r"max_volume: (-?[\d.]+) dB", res.err)
    return float(m.group(1)) if m else None


async def loudnorm(src: Path, dst: Path, *, target: float = -14.0, peak: float = -1.5, lra: float = 11.0) -> dict:
    """Двухпроходный loudnorm до -14 LUFS (регламент §5). Видео копируется без перекодирования, звук AAC. Возвращает
    результат измерения первого прохода."""
    first = await ffmpeg(["-i", str(src), "-vn", "-af", f"loudnorm=I={target}:TP={peak}:LRA={lra}:print_format=json", "-f",
                          "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", first.err, re.S)
    if not first.ok or not m:
        raise MediaError(f"loudnorm, измерение не удалось: {_tail(first.err)}")
    stats = json.loads(m.group(0))
    measured = (f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:measured_LRA={stats['input_lra']}:"
                f"measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:linear=true")
    second = await ffmpeg(["-i", str(src), "-map", "0:v", "-map", "0:a", "-c:v", "copy", "-af",
                           f"loudnorm=I={target}:TP={peak}:LRA={lra}:{measured}", "-ar", "48000", "-c:a", "aac", "-b:a",
                           "192k", "-movflags", "+faststart", str(dst)])
    if not second.ok or not dst.is_file():
        raise MediaError(f"loudnorm, второй проход не удался: {_tail(second.err)}")
    return stats


# ---------------------------------------------------------------------------------------------------- синтетика

async def synth_tone(path: Path, seconds: float, *, freq: float = 220.0, volume: float = 0.3) -> None:
    """Тон нужной длины в WAV 48 кГц (заглушка синтеза речи для тестов и живого прогона против FakeCore)."""
    res = await ffmpeg(["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=48000", "-af",
                        f"volume={volume}", "-ac", "1", str(path)])
    if not res.ok:
        raise MediaError(f"не удалось синтезировать тон: {_tail(res.err)}")


_FMT = "aformat=sample_rates=48000:channel_layouts=stereo:sample_fmts=fltp"


def audio_timeline_filter(parts: list[tuple[float, float]], total_s: float) -> tuple[list[str], str]:
    """Цепочки фильтров, собирающие звук забега на шкале видео из кусков `[(смещение начала, длина)]` (секунды). Входы с
    номерами по порядку сортировки по смещению. Промежутки заполняются тишиной, начало куска внахлёст обрезается, хвост
    добивается тишиной, длина точно `total_s`. Возвращает (цепочки, метка выхода)."""
    chains: list[str] = []
    labels: list[str] = []
    cursor = 0.0
    for k, (offset, length) in enumerate(sorted(parts)):
        gap = offset - cursor
        if gap > 0.005:
            chains.append(f"anullsrc=r=48000:cl=stereo:d={gap:.3f},{_FMT}[g{k}]")
            labels.append(f"[g{k}]")
            cursor = offset
        skip = max(0.0, cursor - offset)
        trim = f"atrim=start={skip:.3f}," if skip else ""
        chains.append(f"[{k}:a]{trim}asetpts=PTS-STARTPTS,{_FMT}[p{k}]")
        labels.append(f"[p{k}]")
        cursor += max(0.0, length - skip)
    chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1,apad=whole_dur={total_s:.3f},atrim=end={total_s:.3f},"
                  "asetpts=PTS-STARTPTS[a]")
    return chains, "[a]"


async def assemble_audio(parts: list[tuple[Path, float]], total_s: float, dst: Path) -> None:
    """Дорожка записи на шкале видео: `parts` — [(файл звука, смещение его начала от начала видео, секунд)]
    (запись забега RimWorld идёт кусками по 30 минут, ТЗ 11b). Нет кусков: тишина."""
    if not parts:
        res = await ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{total_s:.3f}", str(dst)])
        if not res.ok:
            raise MediaError(f"не удалось создать тишину: {_tail(res.err)}")
        return
    ordered = sorted(parts, key=lambda p: p[1])
    lengths = [(await probe(p)).duration_s for p, _ in ordered]
    chains, label = audio_timeline_filter([(off, ln) for (_, off), ln in zip(ordered, lengths)], total_s)
    inputs: list[str] = []
    for p, _ in ordered:
        inputs += ["-i", str(p)]
    res = await ffmpeg([*inputs, "-filter_complex", ";".join(chains), "-map", label, "-c:a", "pcm_s16le", str(dst)])
    if not res.ok or not dst.is_file():
        raise MediaError(f"не удалось собрать звук записи: {_tail(res.err)}")
