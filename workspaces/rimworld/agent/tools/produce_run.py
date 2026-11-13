"""Смонтировать уже записанный забег тем же кодом, что и в конце забега (`RunRecorder.process`): сценарий, `montage_*.mp4`,
вертикальные клипы. Нужен для забегов, остановленных вручную или упавших, и для повторной сборки после правки сценария.

    python agent/tools/produce_run.py <папка_забега> [--raw raw_HHMMSS.mp4] [--out <папка>] [--memory <папка memory>]
                                      [--scenario scenario_X.json] [--colony ИМЯ] [--outcome ТЕКСТ] [--no-audio]

Время начала записи берётся из имени файла `raw_HHMMSS.mp4`, даты файла и протокола событий (запись «запись забега
началась» рядом с этим временем), длительность из ffprobe. События игры и реплики берутся из `events.db` за окно записи
(только чтение). Куски звука: `raw_HHMMSS_aNN.wav` с `.json` рядом (точное время начала) или старый `raw_HHMMSS.wav`
(начало считается совпадающим с видео). Результат пишется в `--out` (по умолчанию папка забега). `--scenario` берёт готовый
сценарий (его можно править руками) вместо построенного из событий.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("GITS_NO_LAUNCH", "1")        # сборка записи никогда не запускает игру
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import events_log  # noqa: E402
import run_recorder as rr  # noqa: E402
from audio_capture import RATE, AudioPart  # noqa: E402

WAV_HEADER = 44                       # заголовок WAV, который пишет модуль wave
START_SEARCH_S = 15                   # запись «запись забега началась» ищется не дальше этого от времени в имени файла
LEGACY_AUDIO_LAG_S = 0.3              # у старого одного WAV нет времени начала: считаем, что звук пошёл сразу за видео
STARTED_MARK = "запись забега началась"
_NAME = re.compile(r"raw_(\d{2})(\d{2})(\d{2})")


def pick_raw(folder: Path, name: str | None) -> Path:
    """Файл записи: указанный или самый большой `raw_*.mp4` (кроме склеенных со звуком `_av`)."""
    if name:
        raw = Path(name) if Path(name).is_absolute() else folder / name
        if not raw.is_file():
            raise SystemExit(f"Нет файла записи: {raw}")
        return raw
    found = sorted((p for p in folder.glob("raw_*.mp4") if not p.stem.endswith("_av")), key=lambda p: p.stat().st_size)
    if not found:
        raise SystemExit(f"В папке {folder} нет raw_*.mp4")
    if len(found) > 1:
        print(f"Несколько записей, беру самую большую: {found[-1].name} (остальные: "
              f"{', '.join(p.name for p in found[:-1])}; другую укажи через --raw)")
    return found[-1]


def guess_start(raw: Path, duration: float) -> float:
    """Время начала по имени `raw_HHMMSS` и дате последней записи в файл (имя дату не содержит; забег мог перейти через
    полночь, поэтому берётся ближайшая из трёх дат)."""
    m = _NAME.match(raw.stem)
    if not m:
        return raw.stat().st_mtime - duration
    hh, mm, ss = map(int, m.groups())
    est = raw.stat().st_mtime - duration
    day = datetime.fromtimestamp(est).replace(hour=hh, minute=mm, second=ss, microsecond=0)
    cands = [(day + timedelta(days=d)).timestamp() for d in (-1, 0, 1)]
    return min(cands, key=lambda t: abs(t - est)) + 0.5            # секунды в имени отброшены вниз


def refine_start(db: Path, guess: float) -> tuple[float, str | None]:
    """Уточнить время начала и название колонии по записи протокола «запись забега началась: <колония>» рядом с догадкой."""
    near = [e for e in events_log.read_window(db, guess - START_SEARCH_S, guess + START_SEARCH_S, {"video"})
            if e["text"].startswith(STARTED_MARK)]
    if not near:
        return guess, None
    e = min(near, key=lambda e: abs(e["ts"] - guess))
    return e["ts"] - 0.5, e["text"].partition(": ")[2] or None      # запись в протокол идёт позже старта ffmpeg


def audio_parts_for(raw: Path, started: float) -> list[AudioPart]:
    """Куски звука записи: `raw_X_aNN.wav` с `.json` (точное время начала) или старый одиночный `raw_X.wav`."""
    parts = []
    for wav in sorted(raw.parent.glob(f"{raw.stem}_a[0-9][0-9].wav")):
        frames = max(0, (wav.stat().st_size - WAV_HEADER) // (2 * 2))      # размер файла надёжнее заголовка (не закрыт при сбое)
        meta = wav.with_suffix(".json")
        try:
            at = float(json.loads(meta.read_text(encoding="utf-8"))["started_at"])
        except (OSError, ValueError, KeyError):
            at = wav.stat().st_mtime - frames / RATE                     # нет времени начала: по концу записи в файл
        parts.append(AudioPart(wav, at, frames))
    legacy = raw.with_suffix(".wav")
    if not parts and legacy.is_file():
        parts.append(AudioPart(legacy, started + LEGACY_AUDIO_LAG_S, max(0, (legacy.stat().st_size - WAV_HEADER) // 4)))
    return parts


def build_recording(folder: Path, raw: Path, args: argparse.Namespace, db: Path) -> rr.Recording:
    ffmpeg = rr.find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("Не найден ffmpeg: поставь его в PATH или укажи GITS_FFMPEG")
    duration = rr.probe_duration(rr.find_ffprobe(ffmpeg), raw)
    if not duration:
        raise SystemExit(f"ffprobe не прочитал длительность {raw}")
    started, colony = refine_start(db, guess_start(raw, duration))
    out = Path(args.out) if args.out else folder
    rec = rr.Recording(folder.name, args.colony or colony or folder.name, raw, started, duration=duration,
                       outcome=args.outcome, out_dir=out)
    rec.events = events_log.read_window(db, started, started + duration, {"game", "say"})
    if args.scenario:
        rec.plan = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    if not args.no_audio:
        rec.audio_extra = audio_parts_for(raw, started)
        rec.audio_sound = bool(rec.audio_extra)
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Смонтировать записанный забег (сценарий, montage_*.mp4, клипы)")
    ap.add_argument("folder", help="папка забега (memory/videos/<run_id>)")
    ap.add_argument("--raw", help="файл записи (по умолчанию самый большой raw_*.mp4 в папке)")
    ap.add_argument("--out", help="папка для результата (по умолчанию папка забега)")
    ap.add_argument("--memory", help="папка memory с events.db (по умолчанию рядом с videos/ или MEMORY_DIR)")
    ap.add_argument("--scenario", help="готовый scenario_*.json вместо построенного из событий")
    ap.add_argument("--colony", help="название колонии (по умолчанию из протокола событий)")
    ap.add_argument("--outcome", default="", help="исход забега для сценария")
    ap.add_argument("--no-audio", action="store_true", help="без звука")
    args = ap.parse_args(argv)

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        raise SystemExit(f"Нет папки забега: {folder}")
    memory = Path(args.memory) if args.memory else (
        folder.parent.parent if folder.parent.name == "videos" else Path(os.getenv("MEMORY_DIR", "agent/memory")))
    out = Path(args.out) if args.out else folder
    out.mkdir(parents=True, exist_ok=True)
    events_log.init(str(out / "_events"))          # записи о ходе сборки уходят сюда, основной протокол не трогаем
    raw = pick_raw(folder, args.raw)
    rec = build_recording(folder, raw, args, memory / "events.db")
    print(f"запись {raw.name}: {rec.duration:.0f} с, начало {datetime.fromtimestamp(rec.started):%Y-%m-%d %H:%M:%S}, "
          f"событий {len(rec.events)}, кусков звука {len(rec.audio_parts())}")
    t0 = time.time()
    result = rr.RunRecorder(str(memory), lambda: {}).process(rec)
    if result is None:
        print("Монтаж пропущен: запись пустая или слишком короткая")
        return 1
    scenes = result["scenario"]["scenes"]
    print(f"готово за {time.time() - t0:.0f} с: сцен {len(scenes)}, звук {'есть' if result['audio'] else 'нет'}, "
          f"видео {result['video']}, клипов {len(result['clips'])}")
    for s in scenes:
        print(f"  сцена {s['id']}: {s['start']:.0f}-{s['end']:.0f} с, {s['kind']}, {s['label'][:60]}")
    for c in result["clips"]:
        print(f"  клип {c}")
    return 0 if result["video"] else 2


if __name__ == "__main__":
    sys.exit(main())
