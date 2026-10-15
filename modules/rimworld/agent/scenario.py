"""Сценарий забега: из протокола событий, целей и меток собирается список сцен; по нему монтируется видео и режутся клипы.

Сценарий (`scenario.json` в папке забега) отделён от видео: его можно читать, править и пересобирать видео заново.
Моменты берутся из протокола (`events_log`: события игры и реплики), из целей игры (`goals.json`) и из меток контроллера.
Сцена это окно вокруг группы близких важных моментов. Все времена в секундах от начала записи.
"""
import json
from pathlib import Path

OPENING_S, ENDING_S = 25, 30    # сколько брать от начала и конца забега
PRE_S, POST_S = 12, 18          # вокруг момента: до и после
CLUSTER_GAP_S = 40              # моменты ближе этого попадают в одну сцену
MAX_TOTAL_S = 600               # длина основного видео не больше 10 минут
MAX_CLIP_S = 50                 # вертикальный клип короче минуты
MAX_CLIPS = 6
MIN_CLIP_IMPORTANCE = 2

# важность по виду события игры (тип события мода); письма и мелочи = 1, изменения состояния из протокола = 2
_IMPORTANCE = {"colony_died": 3, "pawn_died": 3, "incident": 2, "fire_started": 2}


def collect_moments(events: list[dict], started: float, markers: list) -> list[dict]:
    """Моменты забега: {t, kind, label, importance}. Реплики (kind=say) идут с importance 0: они не делают сцену,
    а подписывают её."""
    moments = []
    for e in events:
        t = e["ts"] - started
        if t < 0:
            continue
        data = e.get("data") or {}
        if e["kind"] == "say":
            moments.append({"t": t, "kind": "say", "label": e["text"], "importance": 0})
        elif e["kind"] == "game":
            imp = _IMPORTANCE.get(data.get("type"), 2 if e["text"].startswith("изменение:") else 1)
            moments.append({"t": t, "kind": data.get("type") or "change", "label": e["text"], "importance": imp})
    moments += [{"t": at, "kind": "marker", "label": label, "importance": imp} for at, label, imp in markers]
    return sorted(moments, key=lambda m: m["t"])


def build(moments: list[dict], duration: float, run_id: str, colony: str, outcome: str, goal: dict | None) -> dict:
    """Сценарий: открытие, сцены вокруг важных моментов (в пределах MAX_TOTAL_S), финал."""
    if duration <= 0:
        return {"version": 1, "run_id": run_id, "colony": colony, "outcome": outcome, "duration": 0, "scenes": []}
    key = [m for m in moments if m["importance"] >= 2]
    groups: list[list[dict]] = []
    for m in key:
        if groups and m["t"] - groups[-1][-1]["t"] <= CLUSTER_GAP_S:
            groups[-1].append(m)
        else:
            groups.append([m])
    scenes = []
    for g in groups:
        peak = max(g, key=lambda m: (m["importance"], -m["t"]))
        start, end = max(0.0, g[0]["t"] - PRE_S), min(duration, g[-1]["t"] + POST_S)
        said = [m["label"] for m in moments if m["kind"] == "say" and start <= m["t"] <= end][:2]
        scenes.append({"kind": "event", "start": start, "end": end, "peak": peak["t"], "label": peak["label"],
                       "importance": peak["importance"], "events": len(g), "said": said})
    budget = MAX_TOTAL_S - min(OPENING_S, duration) - min(ENDING_S, duration)
    kept = []
    for s in sorted(scenes, key=lambda s: (-s["importance"], s["start"])):
        if s["end"] - s["start"] <= budget:
            kept.append(s)
            budget -= s["end"] - s["start"]
    kept += [{"kind": "opening", "start": 0.0, "end": min(OPENING_S, duration), "peak": 0.0,
              "label": "начало забега", "importance": 1, "events": 0, "said": []},
             {"kind": "ending", "start": max(0.0, duration - ENDING_S), "end": duration, "peak": duration,
              "label": outcome or "конец забега", "importance": 1, "events": 0, "said": []}]
    merged: list[dict] = []
    for s in sorted(kept, key=lambda s: s["start"]):
        if merged and s["start"] <= merged[-1]["end"] + 1:
            prev = merged[-1]
            if s["importance"] > prev["importance"]:
                prev.update(kind=s["kind"], peak=s["peak"], label=s["label"], importance=s["importance"])
            prev["end"] = max(prev["end"], s["end"])
            prev["events"] += s["events"]
            prev["said"] = (prev["said"] + s["said"])[:2]
        else:
            merged.append(dict(s))
    for i, s in enumerate(merged, 1):
        s["id"] = i
        s["act"] = "начало" if s["start"] < duration / 3 else "середина" if s["start"] < duration * 2 / 3 else "финал"
        s["start"], s["end"], s["peak"] = round(s["start"], 1), round(s["end"], 1), round(s["peak"], 1)
    return {"version": 1, "run_id": run_id, "colony": colony, "outcome": outcome, "duration": round(duration, 1),
            "goal": goal, "scenes": merged}


def clip_windows(scenario: dict) -> list[dict]:
    """Окна вертикальных клипов: самые важные сцены, не длиннее MAX_CLIP_S, вокруг пика сцены."""
    picks = sorted((s for s in scenario["scenes"] if s["kind"] == "event" and s["importance"] >= MIN_CLIP_IMPORTANCE),
                   key=lambda s: (-s["importance"], s["start"]))[:MAX_CLIPS]
    out = []
    for s in picks:
        start = max(s["start"], s["peak"] - MAX_CLIP_S * 0.3)
        end = min(s["end"], start + MAX_CLIP_S)
        start = max(s["start"], end - MAX_CLIP_S)
        out.append({"scene": s["id"], "start": round(start, 1), "end": round(end, 1), "label": s["label"],
                    "said": s["said"]})
    return sorted(out, key=lambda c: c["start"])


def read_goal(memory_dir: Path) -> dict | None:
    """Главная цель игры (ambition) из goals.json, как её видит контроллер."""
    try:
        goals = json.loads((Path(memory_dir) / "goals.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found = [g for g in goals if g.get("category") == "ambition"]
    found.sort(key=lambda g: g.get("status") != "active")
    if not found:
        return None
    g = found[0]
    return {"title": g.get("title"), "status": g.get("status"), "progress_pct": g.get("progress_pct", 0),
            "criteria": g.get("success_criteria")}
