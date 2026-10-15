"""Запись забега на видео: сценарий, монтаж, вертикальные клипы. Всё сохраняется в папку, загрузки на YouTube пока нет.

Запись: ffmpeg (gfxcapture, окно RimWorld целиком) снимает игру с начала забега до его конца. Файл фрагментированный, поэтому при падении
бота или игры записанное не теряется. После конца забега:
  1. `scenario.py` собирает сценарий из протокола событий, целей и меток (`scenario.json`);
  2. по сценарию монтируется основное видео (`montage_*.mp4`, горизонтальное, до 10 минут);
  3. по самым важным сценам режутся вертикальные клипы 9:16 (`clips/clip_NN.mp4` и `clips.json` с подписями).
Папка: `memory/videos/<run_id>/` (или GITS_VIDEO_DIR/<run_id>).

Звук пишется кусками по 30 минут (`audio_capture`), при сбое запись звука перезапускается (`ensure_audio`); видео больше
`max_record_hours` часов переходит в следующий кусок (`ensure_alive`). Сцены режутся перемоткой: каждая сцена это отдельный
вход `-ss -to -i`, поэтому длина записи на скорость нарезки не влияет. Уже записанный забег собирается заново скриптом
`tools/produce_run.py`.

Настройка: `record_runs` (settings.py), `max_record_hours` (настройка модуля). Окружение: GITS_FFMPEG (путь к ffmpeg), GITS_RECORD_WINDOW (заголовок окна),
GITS_RECORD_SOURCE=gdigrab|desktop (запасной захват), GITS_RECORD_FPS, GITS_VIDEO_DIR.
"""
import asyncio
import json
import logging

from envcompat import env
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import events_log
import game_launcher
import scenario
from audio_capture import RATE, AudioPart, LoopbackRecorder

logger = logging.getLogger("run_recorder")

WINDOW_TITLE = "RimWorld by Ludeon Studios"
MIN_RAW_BYTES = 200_000        # меньше этого запись пустая (окно свёрнуто, ffmpeg не смог захватить)
MIN_RAW_SECONDS = 30           # слишком короткий забег (случайный запуск) не монтируем
TIMELAPSE_TARGET_S = 480       # забег без событий: во сколько секунд ужимать
OUT_SIZE = "1920:1080"
DEFAULT_MAX_RECORD_HOURS = 6   # видео длиннее этого переходит в следующий кусок (REC-6)
AUDIO_RETRY_MIN_S = 30         # как часто пробовать перезапустить запись звука; при неудачах интервал растёт до MAX
AUDIO_RETRY_MAX_S = 600
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Recording:
    run_id: str
    colony: str
    raw: Path
    started: float
    proc: subprocess.Popen | None = None
    markers: list = field(default_factory=list)    # (смещение от начала в секундах, подпись, важность 1..3)
    duration: float = 0.0
    outcome: str = ""
    goal: dict | None = None
    audio: LoopbackRecorder | None = None          # текущий сеанс записи звука
    audio_old: list = field(default_factory=list)  # оборвавшиеся сеансы (их куски тоже идут в монтаж)
    audio_extra: list = field(default_factory=list)    # готовые куски звука (повторная сборка записи, `tools/produce_run.py`)
    audio_sound: bool | None = None                # заранее известно, что звук есть (для готовых кусков)
    audio_retry_at: float = 0.0
    audio_retry_s: float = AUDIO_RETRY_MIN_S
    out_dir: Path | None = None                    # куда писать результат (по умолчанию папка записи)
    events: list | None = None                     # готовые события за окно записи (по умолчанию из журнала)
    plan: dict | None = None                       # готовый сценарий (по умолчанию строится из событий и меток)

    def _recorders(self) -> list[LoopbackRecorder]:
        return [*self.audio_old, *([self.audio] if self.audio is not None else [])]

    def audio_parts(self) -> list[AudioPart]:
        """Все куски звука забега по времени начала."""
        parts = list(self.audio_extra)
        for r in self._recorders():
            parts += [p for p in r.parts if p.frames > 0]
        return sorted(parts, key=lambda p: p.started_at)

    def has_sound(self) -> bool:
        if self.audio_sound is not None:
            return self.audio_sound
        return any(r.has_sound() for r in self._recorders())


def find_ffmpeg() -> str | None:
    return env("GITS_FFMPEG") or shutil.which("ffmpeg")


def find_ffprobe(ffmpeg: str | None = None) -> str | None:
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


def probe_duration(ffprobe: str | None, path: Path) -> float | None:
    """Длительность файла по ffprobe; None, если не вышло."""
    if not ffprobe:
        return None
    try:
        done = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
        return float(done.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def capture_args(ffmpeg: str, out: Path) -> list[str]:
    """Команда записи. По умолчанию Windows Graphics Capture (filter gfxcapture): снимает именно окно RimWorld, даже если
    его закрыло другое окно (чужие окна, например мессенджер, в кадр не попадают). GITS_RECORD_SOURCE=gdigrab
    включает старый захват области экрана, =desktop весь экран."""
    fps = env("GITS_RECORD_FPS", "30")
    source = env("GITS_RECORD_SOURCE", "gfxcapture")
    if source == "gfxcapture":
        inp = ["-f", "lavfi", "-i", f"gfxcapture=window_exe=RimWorldWin64\\.exe:capture_cursor=0:max_framerate={fps},"
               "hwdownload,format=bgra"]
    else:
        src = "desktop" if source == "desktop" else "title=" + env("GITS_RECORD_WINDOW", WINDOW_TITLE)
        inp = ["-f", "gdigrab", "-framerate", fps, "-draw_mouse", "0", "-i", src]
    return [ffmpeg, "-y", "-loglevel", "error", *inp,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p", "-movflags", "+frag_keyframe+empty_moov", str(out)]


def is_timelapse(scenes: list[dict], duration: float) -> bool:
    """Сцена одна и занимает почти весь забег (событий не было): забег ускоряется целиком."""
    return len(scenes) == 1 and scenes[0]["end"] - scenes[0]["start"] >= duration - 1


def _seek(raw: Path, start: float, end: float) -> list[str]:
    """Отдельный вход для одного куска: быстрая перемотка до ближайшего ключевого кадра и точная доводка (REC-3).
    Декодируется только кусок, а не вся запись с начала."""
    return ["-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(raw)]


def _exact(length: float) -> str:
    """Кусок видео ровно `length` секунд. Запись хранит кадр только при изменении картинки (переменная частота), поэтому
    последний кадр перед концом куска мог бы закончиться раньше: его удерживают и обрезают по длине, иначе звук и
    картинка расходятся на каждом стыке сцен."""
    return f"tpad=stop_mode=clone:stop_duration={length:.2f},trim=duration={length:.3f},setpts=PTS-STARTPTS"


def montage_args(ffmpeg: str, raw: Path, out: Path, scenes: list[dict], duration: float,
                 audio: "AudioTrack | None" = None) -> list[str]:
    """Команда монтажа по сценам сценария: куски встык с плавным появлением и затуханием (каждая сцена: свой вход с
    перемоткой, потом concat). Звук (`AudioTrack`) берётся для каждой сцены из файлов кусков. Если сцена одна на всё
    видео (событий не было), забег ускоряется до TIMELAPSE_TARGET_S."""
    scale = f"scale={OUT_SIZE}:force_original_aspect_ratio=decrease,pad={OUT_SIZE}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30"
    if is_timelapse(scenes, duration):
        speed = max(1.0, duration / TIMELAPSE_TARGET_S)
        inputs = ["-i", str(raw)]
        graph = f"[0:v]setpts=PTS/{speed:.3f},{scale}[v]"
        audio = None                    # ускоренная речь бесполезна: таймлапс без звука
        total = duration / speed
    else:
        inputs = [a for s in scenes for a in _seek(raw, s["start"], s["end"])]
        chains = [f"[{i}:v]setpts=PTS-STARTPTS,{scale},{_exact(s['end'] - s['start'])}[c{i}]" for i, s in enumerate(scenes)]
        index = len(scenes)
        if audio:
            for i, s in enumerate(scenes):
                extra, parts = audio_window(audio, s["start"], s["end"], index, f"a{i}")
                inputs += extra
                chains += parts
                index += len(extra) // 6
        joined = "".join(f"[c{i}]" + (f"[a{i}]" if audio else "") for i in range(len(scenes)))
        graph = ";".join(chains) + f";{joined}concat=n={len(scenes)}:v=1:a={int(bool(audio))}" + ("[v][a0]" if audio else "[v]")
        total = sum(s["end"] - s["start"] for s in scenes)
    graph += f";[v]fade=t=in:d=1,fade=t=out:st={max(0.0, total - 1.5):.2f}:d=1.5[out]"
    if audio:
        graph += f";[a0]afade=t=in:d=1,afade=t=out:st={max(0.0, total - 1.5):.2f}:d=1.5[aout]"
    return [ffmpeg, "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
            *(["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"] if audio else []),
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out)]


def clip_args(ffmpeg: str, raw: Path, out: Path, start: float, end: float,
              audio: "AudioTrack | None" = None) -> list[str]:
    """Вертикальный клип 1080x1920: кадр целиком по центру, фон из него же, увеличенный и размытый. Вход с перемоткой."""
    graph = ("[0:v]setpts=PTS-STARTPTS,split=2[a][b];"
             "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5[bg];"
             "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,"
             f"{_exact(end - start)},fade=t=in:d=0.5,fade=t=out:st={max(0.0, end - start - 0.7):.2f}:d=0.7[out]")
    inputs = _seek(raw, start, end)
    if audio:
        extra, chains = audio_window(audio, start, end, 1, "araw")
        inputs += extra
        graph += ";" + ";".join(chains) + (f";[araw]afade=t=in:d=0.3,"
                                           f"afade=t=out:st={max(0.0, end - start - 0.5):.2f}:d=0.5[aout]")
    return [ffmpeg, "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
            *(["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"] if audio else []),
            "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out)]


MAX_STRETCH = 0.015             # расхождение часов звука и видео, которое лечится растяжением куска (а не обрезкой)


def audio_timeline(parts: list[tuple[float, float]], video_s: float) -> list[tuple]:
    """Куски звука -> дорожка на всё видео. `parts`: (смещение начала куска от начала видео, длина куска), секунды, по
    времени начала. Результат: ("part", номер, с какой секунды файла, сколько секунд взять, темп) и ("silence", секунды).
    Тишина заполняет промежутки между кусками. Часы звуковой карты уходят от часов компьютера (около 0,1 %), поэтому кусок,
    чья длина отличается от отведённого ему места не больше чем на 1,5 %, слегка ускоряется или замедляется (темп), а не
    обрезается: речь на стыке не теряется. Остальной хвост, заходящий на следующий кусок, отрезается; звук за концом
    видео не берётся."""
    out: list[tuple] = []
    cursor = 0.0
    for i, (offset, length) in enumerate(parts):
        begin, skip = max(offset, 0.0), max(-offset, 0.0)
        slot = min(parts[i + 1][0] if i + 1 < len(parts) else video_s, video_s) - begin
        have = length - skip
        if have <= 0.01 or slot <= 0.01:
            continue
        tempo, take = 1.0, min(have, slot)
        if skip == 0 and abs(have / slot - 1) <= MAX_STRETCH and abs(have / slot - 1) > 0.0005:
            tempo, take = have / slot, have
        if begin - cursor > 0.01:
            out.append(("silence", begin - cursor))
            cursor = begin
        out.append(("part", i, skip, take, tempo))
        cursor += take / tempo
    return out


class AudioTrack:
    """Звук забега на шкале видео: куски по времени начала и тишина между ними (`audio_timeline`). Дорожка целиком в файл не
    собирается (кодировать 5 часов звука дольше всего монтажа): для каждой сцены берётся только её окно прямо из файлов
    кусков (`-ss -t -i`), недостающее до конца окна дополняется тишиной, окно по длине равно видео (REC-1, REC-4)."""

    def __init__(self, parts: list[tuple[Path, float, float]], video_s: float):
        """`parts`: (файл, смещение начала от начала видео, длина в секундах) по времени начала."""
        self.files = [p for p, _, _ in parts]
        self.timeline = audio_timeline([(o, n) for _, o, n in parts], video_s)

    def has_sound(self) -> bool:
        return any(t[0] == "part" for t in self.timeline)

    def pieces(self, start: float, end: float) -> list[tuple]:
        """Окно [start, end] видео -> ("part", файл, с какой секунды файла, сколько секунд файла, темп, длина в видео) и
        ("silence", длина). Остаток окна после последнего куска не включается (его заполняет apad)."""
        out, cursor = [], 0.0
        for seg in self.timeline:
            if seg[0] == "silence":
                length, tempo = seg[1], 1.0
            else:
                _, i, skip, take, tempo = seg
                length = take / tempo
            lo, hi = max(cursor, start), min(cursor + length, end)
            if hi - lo > 0.005:
                out.append(("silence", hi - lo) if seg[0] == "silence" else
                           ("part", self.files[seg[1]], seg[2] + (lo - cursor) * tempo, (hi - lo) * tempo, tempo, hi - lo))
            cursor += length
        return out


def audio_window(track: AudioTrack, start: float, end: float, first_input: int, label: str) -> tuple[list[str], list[str]]:
    """Входы ffmpeg и цепочки фильтров, дающие звук окна [start, end] видео под именем `[label]`. Входы нумеруются с
    `first_input`. Окно точно по длине видео: хвост добивается тишиной (`apad`), лишнее обрезается."""
    fmt = f"aformat=sample_rates={RATE}:channel_layouts=stereo:sample_fmts=fltp"
    length = end - start
    inputs: list[str] = []
    chains, labels = [], []
    for k, piece in enumerate(track.pieces(start, end)):
        if piece[0] == "silence":
            chains.append(f"anullsrc=r={RATE}:cl=stereo:d={piece[1]:.3f},{fmt}[{label}_{k}]")
        else:
            _, path, off, dur, tempo, _ = piece
            stretch = f"atempo={tempo:.6f}," if tempo != 1.0 else ""
            chains.append(f"[{first_input + len(inputs) // 6}:a]asetpts=PTS-STARTPTS,{stretch}{fmt}[{label}_{k}]")
            inputs += ["-ss", f"{off:.3f}", "-t", f"{dur:.3f}", "-i", str(path)]
        labels.append(f"[{label}_{k}]")
    if not labels:
        return [], [f"anullsrc=r={RATE}:cl=stereo:d={length:.3f},{fmt}[{label}]"]
    chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1,apad=whole_dur={length:.3f},"
                  f"atrim=end={length:.3f},asetpts=PTS-STARTPTS[{label}]")
    return inputs, chains


class RunRecorder:
    """Один активный забег за раз. Все методы безопасны: нет ffmpeg или не вышло: забег идёт без записи."""

    def __init__(self, memory_dir: str, settings_get, max_hours: Callable[[], float] | None = None):
        self._memory = Path(memory_dir)
        self._dir = Path(env("GITS_VIDEO_DIR") or self._memory / "videos")
        self._settings = settings_get
        self._max_hours = max_hours or (lambda: DEFAULT_MAX_RECORD_HOURS)     # настройка модуля `max_record_hours`
        self.current: Recording | None = None

    def start(self, run_id: str, colony: str, why: str = "забег начался"):
        """Начать запись (кусок) забега. `why` - факт, зачем начинаем (объявляется, правило 14)."""
        if not self._settings().get("record_runs") or self.current is not None:
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            logger.warning("Запись забега включена, но ffmpeg не найден: укажи путь в GITS_FFMPEG")
            return
        folder = self._dir / run_id
        folder.mkdir(parents=True, exist_ok=True)
        raw = folder / f"raw_{time.strftime('%H%M%S')}.mp4"
        try:
            proc = subprocess.Popen(capture_args(ffmpeg, raw), stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=_NO_WINDOW)
        except OSError as e:
            logger.error(f"Не удалось запустить запись: {e}")
            return
        self.current = Recording(run_id, colony, raw, time.time(), proc)
        audio = LoopbackRecorder(raw.with_suffix(".wav"))
        if audio.start():
            self.current.audio = audio
        else:
            logger.warning(f"Звук не пишется, видео будет без голоса: {audio.error}")
        events_log.log("video", f"запись забега началась: {colony}")
        sound = "звук тоже пишется" if self.current.audio is not None else "звука нет: запись звука не запустилась"
        game_launcher.announce_self(f"запускаю запись видео забега (колония «{colony}»: окно игры снимает ffmpeg, "
                                    f"{sound}); {why}", kind="video")

    def mark(self, label: str, importance: int = 2):
        """Метка важного момента (1 мелочь, 2 заметно, 3 главное событие забега)."""
        rec = self.current
        if rec is not None:
            rec.markers.append((time.time() - rec.started, label, importance))

    def close(self, outcome: str) -> Recording | None:
        """Закончить забег сейчас (быстро): зафиксировать длительность, исход и цели, пока их не убрали в архив.
        Остановку ffmpeg, монтаж и клипы делает `produce`."""
        rec, self.current = self.current, None
        if rec is None:
            return None
        rec.duration = time.time() - rec.started
        rec.outcome = outcome
        rec.goal = scenario.read_goal(self._memory)
        return rec

    def overdue(self) -> bool:
        """Запись длится дольше `max_record_hours` (REC-6): пора перейти к следующему куску видео."""
        rec = self.current
        try:
            limit = float(self._max_hours()) * 3600
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_RECORD_HOURS * 3600
        return rec is not None and limit > 0 and time.time() - rec.started > limit

    def ensure_alive(self) -> Recording | None:
        """Если ffmpeg записи умер (окно закрыли, игра перезапустилась) или запись длится дольше `max_record_hours`
        (REC-6), закрыть эту часть и начать новую. Возвращает закрытую часть: её нужно отдать в `produce`."""
        rec = self.current
        if rec is None or rec.proc is None:
            return None
        dead = rec.proc.poll() is not None
        if not dead and not self.overdue():
            return None
        if not dead:
            events_log.log("video", f"запись забега идёт дольше {float(self._max_hours()):g} ч: перехожу к следующему куску")
        old = self.close("part")
        self.start(old.run_id, old.colony, why="прежний кусок закрыт (ffmpeg остановился или кусок дошёл до предела длины), "
                                               "пишу следующий кусок")
        return old

    def ensure_audio(self) -> bool:
        """Если запись звука оборвалась посреди забега (REC-2), записать это в протокол и начать новый кусок звука.
        Возвращает True, если новая запись звука пошла. Пробует не чаще раза в `rec.audio_retry_s` секунд (при неудачах
        интервал растёт), чтобы не засорять протокол. Блокирующая (до 5 секунд): вызывать через to_thread."""
        rec = self.current
        if rec is None or rec.audio is None or rec.audio.ok or rec.audio.stopping:
            return False
        now = time.time()
        if now < rec.audio_retry_at:
            return False
        old = rec.audio
        events_log.log("video", f"запись звука оборвалась ({old.error or 'поток остановился'}): запускаю новый кусок",
                       error=old.error, parts=len(old.parts))
        new = LoopbackRecorder(old.base, first_index=old.next_index)
        started = new.start()
        rec.audio_old.append(old)
        rec.audio = new
        rec.audio_retry_s = AUDIO_RETRY_MIN_S if started else min(rec.audio_retry_s * 2, AUDIO_RETRY_MAX_S)
        rec.audio_retry_at = now + rec.audio_retry_s
        events_log.log("video", "запись звука возобновлена" if started else
                       f"запись звука не возобновилась: {new.error or 'поток не запустился'}; повторю через {rec.audio_retry_s:.0f} с")
        return started

    async def produce(self, rec: Recording) -> dict | None:
        await asyncio.to_thread(self._stop_blocking, rec)
        return await asyncio.to_thread(self.process, rec)

    @staticmethod
    def _stop_blocking(rec: Recording):
        for audio in (*rec.audio_old, rec.audio):
            if audio is not None:
                audio.stop()
        proc = rec.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.stdin.write(b"q")
                proc.stdin.flush()
        except OSError:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.returncode not in (0, None) and proc.stderr:
            logger.warning("ffmpeg записи завершился с ошибкой: " + proc.stderr.read().decode("utf-8", "replace")[-300:])

    def _scenario(self, rec: Recording):
        """Общая часть `process` и `handoff`: проверка записи, события за окно, сценарий в `scenario*.json`. None: запись
        пустая или слишком короткая."""
        raw_ok = rec.raw.is_file() and rec.raw.stat().st_size >= MIN_RAW_BYTES
        if not raw_ok or rec.duration < MIN_RAW_SECONDS:
            events_log.log("video", "запись забега пустая или слишком короткая, монтаж пропущен")
            return None
        folder = rec.out_dir or rec.raw.parent
        folder.mkdir(parents=True, exist_ok=True)
        events = rec.events if rec.events is not None else events_log.tail(limit=20_000, kinds={"game", "say"},
                                                                          since=rec.started)
        events = [e for e in events if e["ts"] <= rec.started + rec.duration]     # события следующего куска сюда не относятся
        moments = scenario.collect_moments(events, rec.started, rec.markers)
        plan = rec.plan or scenario.build(moments, rec.duration, rec.run_id, rec.colony, rec.outcome, rec.goal)
        stem = rec.raw.stem.replace("raw", "")
        scenario_path = folder / f"scenario{stem}.json"
        scenario_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return folder, stem, events, plan, scenario_path

    def handoff(self, rec: Recording) -> dict | None:
        """Подготовить запись для модуля монтажа (ТЗ 20, MON-35, MON-36): сценарий, журнал речи (MON-26a: её реплики `say` за окно
        записи) и описание записи (`recording.json`: время начала и куски звука с их `started_at`, ТЗ 11b). Своего монтажа и
        клипов нет: это делает модуль montage. Блокирующая: вызывать через to_thread."""
        prepared = self._scenario(rec)
        if prepared is None:
            return None
        folder, stem, events, plan, scenario_path = prepared
        speech_path = folder / f"speech_log{stem}.json"
        speech = [{"ts": e["ts"], "kind": "say", "text": e["text"]} for e in events if e["kind"] == "say" and e["text"]]
        speech_path.write_text(json.dumps(speech, ensure_ascii=False, indent=2), encoding="utf-8")
        parts = [p for p in rec.audio_parts() if p.path.is_file()]
        recording_path = folder / f"recording{stem}.json"
        recording_path.write_text(json.dumps({
            "run_id": rec.run_id, "raw": str(rec.raw), "started_at": rec.started, "duration_s": rec.duration,
            "audio_parts": [{"path": str(p.path), "started_at": p.started_at, "seconds": p.seconds} for p in parts]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        events_log.log("video", f"забег отдан монтажу: сцен {len(plan['scenes'])}, реплик {len(speech)}, кусков звука {len(parts)}")
        return {"run_id": rec.run_id, "raw": str(rec.raw), "scenario": plan, "scenario_path": str(scenario_path),
                "speech_log_path": str(speech_path), "recording_path": str(recording_path),
                "audio": [str(p.path) for p in parts], "colony": rec.colony, "outcome": rec.outcome, "duration": rec.duration}

    async def produce_for_montage(self, rec: Recording) -> dict | None:
        await asyncio.to_thread(self._stop_blocking, rec)
        return await asyncio.to_thread(self.handoff, rec)

    def process(self, rec: Recording) -> dict | None:
        """Сценарий, монтаж и клипы готовой записи (блокирующая: вызывать через to_thread)."""
        prepared = self._scenario(rec)
        if prepared is None:
            return None
        game_launcher.announce_self(f"собираю видео забега (колония «{rec.colony}»): сценарий, основной ролик и клипы; "
                                    "это займёт время", kind="video")
        folder, stem, events, plan, scenario_path = prepared
        ffmpeg = find_ffmpeg()
        result = {"run_id": rec.run_id, "raw": str(rec.raw), "scenario": plan, "scenario_path": str(scenario_path),
                  "video": None, "clips": []}

        track = self._with_audio(ffmpeg, rec)         # дорожка стоит дёшево: куски читаются только по окнам сцен
        result["audio"] = track is not None
        events_log.log("video", "звук (голос бота и игра) записан" if track else
                       "звука в записи нет: колонки молчали или запись звука не работала")
        out = folder / f"montage{stem}.mp4"
        if self._run(montage_args(ffmpeg, rec.raw, out, plan["scenes"], rec.duration, track), out):
            result["video"] = str(out)
        clips = []
        for i, c in enumerate(scenario.clip_windows(plan), 1):
            path = folder / "clips" / f"clip{stem}_{i:02d}.mp4"
            path.parent.mkdir(exist_ok=True)
            if self._run(clip_args(ffmpeg, rec.raw, path, c["start"], c["end"], track), path):
                clips.append({**c, "file": path.name})
        if clips:
            (folder / "clips" / f"clips{stem}.json").write_text(json.dumps(clips, ensure_ascii=False, indent=2),
                                                                encoding="utf-8")
        result["clips"] = [str(folder / "clips" / c["file"]) for c in clips]
        events_log.log("video", f"забег готов: сцен {len(plan['scenes'])}, клипов {len(clips)}, папка {folder}")
        return result

    def _with_audio(self, ffmpeg: str, rec: Recording) -> AudioTrack | None:
        """Звук забега на шкале видео: куски по времени начала, тишина в промежутках, видео не обрезается (REC-1, REC-4:
        без `-shortest`; недостающий звук это тишина до конца окна). Нет звука или тишина: None."""
        parts = rec.audio_parts()
        if not parts or not rec.has_sound():
            return None
        video_s = probe_duration(find_ffprobe(ffmpeg), rec.raw) or rec.duration
        track = AudioTrack([(p.path, p.started_at - rec.started, p.seconds) for p in parts], video_s)
        return track if track.has_sound() else None

    @staticmethod
    def _run(cmd: list[str], out: Path) -> bool:
        done = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
        if done.returncode != 0 or not out.is_file():
            logger.error("ffmpeg не справился: " + done.stderr.decode("utf-8", "replace")[-300:])
            return False
        return True
