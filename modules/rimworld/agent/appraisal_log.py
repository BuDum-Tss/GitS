"""Журнал оценок событий: что пришло, что ответила модель решений (категория, опасность, уверенность), что из этого вывел код и
что сделал агент. Нужен, чтобы по живой игре проверять и улучшать оценку: игрок отмечает верно/неверно (и правильную категорию),
`eval_decider.py journal` (теперь в GitS, `services/decider/`) считает точность по этим отметкам.

Файлы: memory/appraisal_log.jsonl (записи, только дописываются) и memory/appraisal_labels.json (отметки по id)."""
import json
import os
import threading
import time
from pathlib import Path

MAX_TEXT = 300
_DIR = Path(os.getenv("MEMORY_DIR") or Path(__file__).parent / "memory")
_last_ms = 0
_ids_lock = threading.Lock()


def _next_id(ts: float) -> str:
    """Идентификатор по миллисекундам, но всегда новый: две записи в одну миллисекунду не делят id (иначе итог и отметка
    попадали бы не в ту запись, а порядок «новые первыми» терялся)."""
    global _last_ms
    with _ids_lock:
        _last_ms = max(int(ts * 1000), _last_ms + 1)
        return f"a{_last_ms}"


def _log_file() -> Path:
    return _DIR / "appraisal_log.jsonl"


def _labels_file() -> Path:
    return _DIR / "appraisal_labels.json"


def append(kind: str, text: str, answers: dict, derived: dict) -> str:
    """Записать оценку; вернуть её id."""
    ts = time.time()
    entry_id = _next_id(ts)
    row = {"id": entry_id, "ts": round(ts, 2), "kind": kind, "text": text[:MAX_TEXT],
           "model": {k: {"answer": v.get("answer"), "confidence": v.get("confidence")} for k, v in (answers or {}).items() if isinstance(v, dict)},
           "derived": derived, "outcome": ""}
    try:
        _log_file().parent.mkdir(parents=True, exist_ok=True)
        with _log_file().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry_id


def _read() -> list[dict]:
    try:
        lines = _log_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-2000:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def labels() -> dict:
    try:
        return json.loads(_labels_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def label(entry_id: str, ok: bool, correct_kind: str = "", note: str = "") -> bool:
    """Отметка игрока: оценка верна или нет (и какая категория была правильной). False, если такой записи нет."""
    if not any(r["id"] == entry_id for r in _read()):
        return False
    data = labels()
    data[entry_id] = {"ok": bool(ok), "correct_kind": correct_kind[:30], "note": note[:200], "ts": round(time.time(), 2)}
    try:
        _labels_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return False
    return True


def set_outcome(entry_id: str, outcome: str) -> None:
    """Дописать итог (например, результат рефлекса) отдельной строкой: журнал только дописывается."""
    if not entry_id or not outcome:
        return
    try:
        with _log_file().open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": entry_id, "outcome": outcome[:200], "ts": round(time.time(), 2), "patch": True}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent(limit: int = 15) -> list[dict]:
    """Последние оценки с итогом и отметкой игрока (новые первыми)."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for r in _read():
        if r.get("patch"):
            if r["id"] in merged:
                merged[r["id"]]["outcome"] = r["outcome"]
            continue
        merged[r["id"]] = r
        order.append(r["id"])
    marks = labels()
    out = []
    for entry_id in reversed(order[-limit:]):
        row = dict(merged[entry_id])
        row["label"] = marks.get(entry_id)
        out.append(row)
    return out


def accuracy() -> dict:
    """Точность по отметкам игрока: доля верных оценок и по категориям, где игрок указал правильную."""
    marks = labels()
    rows = {r["id"]: r for r in _read() if not r.get("patch")}
    done = [(rows[i], m) for i, m in marks.items() if i in rows]
    if not done:
        return {"labeled": 0}
    ok = sum(1 for _r, m in done if m["ok"])
    wrong_kind = [(r["model"].get("kind", {}).get("answer"), m["correct_kind"]) for r, m in done if not m["ok"] and m["correct_kind"]]
    return {"labeled": len(done), "ok": ok, "accuracy": round(ok / len(done), 3), "kind_mistakes": wrong_kind[:20]}
