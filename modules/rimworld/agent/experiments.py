"""Эксперименты и закадровый текст: рабочая тетрадь ГЛаДОС для роликов (файл `experiments.json` в папке памяти).

Агент ведёт эксперимент от гипотезы до вывода: записывает ход, важные моменты (они станут метками ролика) и закадровые
реплики. Файл общий для процессов (MCP-сервер Claude и контроллер), поэтому все операции читают и пишут его целиком, без
состояния в памяти. Чистые функции: время передаётся аргументом.
"""
import json
import os
import time
from pathlib import Path

MAX_ENTRIES = 400                 # записей хода у одного эксперимента (старые мелкие вытесняются)
KINDS = ("step", "moment", "narration")


def _file(memory_dir: str | None = None) -> Path:
    base = memory_dir or os.getenv("MEMORY_DIR") or str(Path(__file__).resolve().parent / "memory")
    return Path(base) / "experiments.json"


def load(memory_dir: str | None = None) -> dict:
    try:
        data = json.loads(_file(memory_dir).read_text(encoding="utf-8"))
        if isinstance(data.get("experiments"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"experiments": [], "next_id": 1}


def save(data: dict, memory_dir: str | None = None) -> None:
    path = _file(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def active(data: dict) -> dict | None:
    return next((e for e in reversed(data["experiments"]) if e.get("status") == "active"), None)


def start(title: str, hypothesis: str, now: float | None = None, memory_dir: str | None = None) -> dict:
    """Новый эксперимент. Идущий прежде закрывается как «прерван»: одновременно активен один."""
    title, hypothesis = (title or "").strip(), (hypothesis or "").strip()
    if not title or not hypothesis:
        raise ValueError("нужны название и гипотеза")
    now = time.time() if now is None else now
    data = load(memory_dir)
    cur = active(data)
    if cur is not None:
        cur.update(status="aborted", finished=now, conclusion=cur.get("conclusion") or "прерван новым экспериментом")
    exp = {"id": data.get("next_id", 1), "title": title[:120], "hypothesis": hypothesis[:400], "status": "active",
           "started": now, "finished": None, "result": "", "conclusion": "", "entries": []}
    data["next_id"] = exp["id"] + 1
    data["experiments"].append(exp)
    save(data, memory_dir)
    return exp


def log(text: str, kind: str = "step", importance: int = 2, game_time: str = "", now: float | None = None,
        memory_dir: str | None = None) -> dict:
    """Запись в активный эксперимент. kind: step (ход), moment (момент для ролика, станет меткой), narration (закадровая реплика)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("пустая запись")
    if kind not in KINDS:
        raise ValueError("kind: " + ", ".join(KINDS))
    data = load(memory_dir)
    exp = active(data)
    if exp is None:
        raise ValueError("нет активного эксперимента: сначала experiment_start")
    entry = {"ts": time.time() if now is None else now, "kind": kind, "text": text[:500],
             "importance": max(1, min(3, int(importance))), "game_time": game_time, "marked": False}
    exp["entries"].append(entry)
    if len(exp["entries"]) > MAX_ENTRIES:                          # вытесняем самую мелкую старую запись хода
        drop = next((i for i, e in enumerate(exp["entries"]) if e["kind"] == "step" and e["importance"] < 3), 0)
        exp["entries"].pop(drop)
    save(data, memory_dir)
    return entry


def finish(result: str, conclusion: str, now: float | None = None, memory_dir: str | None = None) -> dict:
    data = load(memory_dir)
    exp = active(data)
    if exp is None:
        raise ValueError("нет активного эксперимента")
    exp.update(status="done", finished=time.time() if now is None else now, result=(result or "").strip()[:400],
               conclusion=(conclusion or "").strip()[:600])
    save(data, memory_dir)
    return exp


def unmarked_moments(memory_dir: str | None = None) -> list[dict]:
    """Моменты и реплики, ещё не ставшие метками записи (контроллер переносит их в рекордер и отмечает)."""
    exp = active(load(memory_dir))
    return [e for e in (exp["entries"] if exp else []) if e["kind"] in ("moment", "narration") and not e["marked"]]


def mark_done(entries: list[dict], memory_dir: str | None = None) -> None:
    data = load(memory_dir)
    exp = active(data)
    if exp is None:
        return
    stamps = {(e["ts"], e["text"]) for e in entries}
    for e in exp["entries"]:
        if (e["ts"], e["text"]) in stamps:
            e["marked"] = True
    save(data, memory_dir)


def summary(memory_dir: str | None = None, last: int = 5) -> str:
    """Короткий обзор для агента: активный эксперимент и итоги последних."""
    data = load(memory_dir)
    cur = active(data)
    lines = []
    if cur:
        lines.append(f"АКТИВНЫЙ #{cur['id']} «{cur['title']}»: гипотеза: {cur['hypothesis']}; записей: {len(cur['entries'])}")
        for e in cur["entries"][-6:]:
            lines.append(f"  [{e['kind']}] {e['text'][:140]}")
    else:
        lines.append("активного эксперимента нет")
    done = [e for e in data["experiments"] if e["status"] != "active"][-last:]
    for e in done:
        lines.append(f"#{e['id']} «{e['title']}» ({e['status']}): {e['result'] or '-'} | вывод: {e['conclusion'] or '-'}")
    return "\n".join(lines)
