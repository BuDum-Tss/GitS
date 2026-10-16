"""Шина трассы потока данных (ТЗ 17, FLOW-4): кольцевой буфер шагов хода для схемы «Поток данных» во вкладке «Состояние».

`step(turn, node, edge_from, edge_to, kind, ms, size, label)` кладёт запись и не блокирует (Lock на несколько операций).
Содержимое сообщений НЕ записывается: только тип, размер, id хода и короткая метка (категория, имя инструмента, модуль);
метка обрезается и очищается от переводов строк. Режим «показать текст» не реализован (владелец его не включал).

Имя файла `flow_trace`, а не `trace` из ТЗ: `trace` затеняет стандартный модуль. Ход (`turn`) обычно равен id трассы
задержки (`latency_trace`); без неё ход называется `bg`.

Путь последнего события по этапам с задержками берётся и из записей `trace` журнала (ТЗ 24): `turn_from_latency_row`.
"""
import collections
import math
import threading
import time

CAPACITY = 5000
LABEL_MAX = 48
WINDOWS = (60.0, 600.0)

_lock = threading.Lock()
_buf: collections.deque = collections.deque(maxlen=CAPACITY)
_seq = 0


def edge_id(a: str | None, b: str | None) -> str:
    return f"{a or ''}->{b or ''}"


def _clean(label) -> str:
    return " ".join(str(label or "").split())[:LABEL_MAX]


def current_turn() -> str:
    """Ход текущей задачи: id трассы задержки или `bg`."""
    try:
        import latency_trace
        t = latency_trace.current()
        if t is not None:
            return t.id
    except Exception:
        pass
    return "bg"


def step(turn, node, edge_from=None, edge_to=None, kind="", ms=None, size=None, label="") -> None:
    """Запись шага. Никогда не бросает исключение."""
    global _seq
    try:
        rec = {"t": time.time(), "turn": str(turn or current_turn())[:40], "node": str(node)[:60],
               "from": edge_from, "to": edge_to, "kind": str(kind)[:24],
               "ms": None if ms is None else round(float(ms), 2),
               "size": None if size is None else int(size), "label": _clean(label)}
        with _lock:
            _seq += 1
            rec["seq"] = _seq
            _buf.append(rec)
    except Exception:
        pass


def is_error(rec: dict) -> bool:
    return rec.get("kind") == "error" or rec.get("label", "").endswith(":error") or rec.get("label") == "error"


def records(since_seq: int = 0, limit: int | None = None) -> list[dict]:
    with _lock:
        out = [r for r in _buf if r["seq"] > since_seq]
    return out[-limit:] if limit else out


def clear() -> None:
    with _lock:
        _buf.clear()


def _p95(values: list[float]):
    if not values:
        return None
    s = sorted(values)
    return round(s[max(0, min(len(s) - 1, math.ceil(0.95 * len(s)) - 1))], 2)


def edge_stats(now: float | None = None) -> dict:
    """Счётчики по ребру: событий за 1 и 10 минут, последняя и p95 задержка, доля ошибок."""
    now = time.time() if now is None else now
    stats: dict[str, dict] = {}
    for r in records():
        if not r["from"] or not r["to"]:
            continue
        s = stats.setdefault(edge_id(r["from"], r["to"]),
                             {"n1": 0, "n10": 0, "last_ms": None, "ms": [], "err": 0, "n": 0})
        age = now - r["t"]
        s["n"] += 1
        s["err"] += 1 if is_error(r) else 0
        if age <= WINDOWS[0]:
            s["n1"] += 1
        if age <= WINDOWS[1]:
            s["n10"] += 1
        if r["ms"] is not None:
            s["last_ms"] = r["ms"]
            s["ms"].append(r["ms"])
    return {k: {"per_min": v["n1"], "per_10min": v["n10"], "last_ms": v["last_ms"], "p95_ms": _p95(v["ms"]),
                "error_rate": round(v["err"] / v["n"], 3) if v["n"] else 0.0} for k, v in stats.items()}


def turns(limit: int = 30) -> list[dict]:
    """Последние ходы (новые первыми): id, начало, число шагов, длительность и итог."""
    by: dict[str, list[dict]] = {}
    for r in records():
        by.setdefault(r["turn"], []).append(r)
    rows = []
    for tid, recs in by.items():
        rows.append({"turn": tid, "t": recs[0]["t"], "steps": len(recs), "span_ms": round((recs[-1]["t"] - recs[0]["t"]) * 1000, 1),
                     "errors": sum(1 for r in recs if is_error(r)), "outcome": outcome(recs)})
    rows.sort(key=lambda r: r["t"], reverse=True)
    return rows[:limit]


def outcome(recs: list[dict]) -> str:
    if any(r["node"].startswith("out_") for r in recs):
        return "answer"
    if any(r["node"] == "attention" and r["label"] in ("D0", "D1", "D2") for r in recs):
        return "silence"
    return "open"


def turn_steps(turn: str) -> list[dict]:
    return [r for r in records() if r["turn"] == turn]


def node_info(node: str, limit: int = 20) -> dict:
    """Панель узла (FLOW-8): что принял и выдал, частота, задержки, последние записи (без текста)."""
    recs = [r for r in records() if r["node"] == node or r["to"] == node]
    now = time.time()
    ms = [r["ms"] for r in recs if r["ms"] is not None]
    kinds: dict[str, int] = {}
    labels: dict[str, int] = {}
    for r in recs:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        if r["label"]:
            labels[r["label"]] = labels.get(r["label"], 0) + 1
    sizes = [r["size"] for r in recs if r["size"] is not None]
    return {"node": node, "count": len(recs), "per_min": sum(1 for r in recs if now - r["t"] <= 60),
            "kinds": kinds, "labels": dict(sorted(labels.items(), key=lambda kv: -kv[1])[:12]),
            "last_ms": ms[-1] if ms else None, "p95_ms": _p95(ms), "size_avg": round(sum(sizes) / len(sizes), 1) if sizes else None,
            "errors": sum(1 for r in recs if is_error(r)), "recent": recs[-limit:][::-1]}


# ── путь по записям журнала задержки (ТЗ 24) ───────────────────────────────────────────────────

STAGE_NODE = {"mic_end": "module:voice", "stt_done": "module:voice", "core_in": "bus", "classify_done": "gliner",
              "route": "attention", "retrieve_done": "graph_memory", "llm_first_token": "gemma", "llm_done": "gemma",
              "first_sentence_ready": "speech_queue", "tts_first_audio": "out_voice", "play_start": "out_voice"}


def turn_from_latency_row(row: dict) -> dict | None:
    """Строка журнала `trace` -> ход с этапами и задержками шагов (мс от предыдущего этапа)."""
    d = row.get("data") if isinstance(row, dict) else None
    if not isinstance(d, dict) or not isinstance(d.get("marks"), dict):
        return None
    marks = {k: float(v) for k, v in d["marks"].items() if k in STAGE_NODE and isinstance(v, (int, float))}
    if not marks:
        return None
    order = sorted(marks, key=lambda k: (marks[k], list(STAGE_NODE).index(k)))
    steps, prev = [], None
    for st in order:
        steps.append({"stage": st, "node": STAGE_NODE[st], "at_ms": round(marks[st], 1),
                      "ms": None if prev is None else round(marks[st] - marks[prev], 1)})
        prev = st
    return {"turn": str(d.get("trace_id") or ""), "t": row.get("ts"), "source": d.get("source"), "steps": steps,
            "end_to_sound_ms": d.get("end_to_sound_ms"), "exceeded": d.get("exceeded") or []}
