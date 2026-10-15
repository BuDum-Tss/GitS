"""Что изменилось в игре: разница между двумя снимками состояния.

Снимок состояния говорит, как есть. Для решения важнее, что изменилось: упал колонист, появились враги,
построилась кровать, игра встала на паузу. snapshot() сжимает состояние до нужного, compare() выдаёт
изменения по-русски: список (важное_ли, текст). Важное идёт в протокол событий, всё вместе в её ход.
"""

_HP_STEP = 0.15       # здоровье и настроение: сообщаем изменения не меньше этого
_RES_MIN = 10         # ресурсы: не меньше стольких единиц и не меньше 15%
_SPEEDS = {0: "пауза", 1: "обычная скорость", 2: "быстрая", 3: "очень быстрая", 4: "максимальная"}


_FIGHT_JOBS = {"AttackMelee": "дерётся в ближнем бою", "AttackStatic": "стреляет", "SocialFight": "дерётся с другим колонистом",
               "Flee": "убегает"}
_DISMANTLE_JOBS = {"Deconstruct", "Uninstall", "RemoveFloor", "SmoothWall", "ClearSnow"}
IDLE_JOBS = {"Wait_Wander", "GotoWander", "Wait", "Wait_MaintainPosture", "Wait_SafeTemperature"}


def idle_pawns(snap: dict) -> list[str]:
    """Колонисты, которые бродят или ждут без дела (не в бою и не лежат): значит, их работу пора назначить."""
    return [p["name"] for p in snap["pawns"].values()
            if p["job"] in IDLE_JOBS and not p["drafted"] and not p["downed"] and not p["prisoner"]]


def snapshot(state: dict) -> dict:
    colony = state.get("colony") or {}
    pawns = {}
    for p in colony.get("pawns", []):
        pawns[p["id"]] = {
            "name": p.get("name"), "hp": p.get("health_pct", 1.0), "mood": p.get("mood_pct", 0.5),
            "job": p.get("current_job"), "drafted": bool(p.get("is_drafted")), "downed": bool(p.get("is_downed")),
            "bleeding": p.get("bleeding", 0) or 0, "weapon": p.get("equipped"),
            "apparel": len(p.get("apparel") or []), "prisoner": bool(p.get("is_prisoner")),
        }
    threats = {t["id"]: {"name": t.get("name"), "hp": t.get("health_pct", 1.0), "downed": bool(t.get("is_downed")),
                         "kind": t.get("kind")} for t in state.get("threats") or []}
    buildings = {}
    for b in colony.get("buildings", []):
        buildings[b.get("def")] = buildings.get(b.get("def"), 0) + 1
    pending = {}
    for c in colony.get("pending_construction", []):
        pending[c.get("def")] = pending.get(c.get("def"), 0) + 1
    research = colony.get("research") or {}
    return {
        "tick": state.get("tick", 0), "save": state.get("save_id"), "paused": bool(state.get("paused")),
        "speed": state.get("time_speed"), "weather": state.get("weather"), "season": state.get("season"),
        "manual": state.get("manual_priorities"),
        "pawns": pawns, "threats": threats, "resources": dict(colony.get("resources") or {}),
        "buildings": buildings, "pending": pending, "food_days": colony.get("food_days"),
        "research": research.get("current"), "researched": set(research.get("completed") or []),
        "alerts": {a.get("message") for a in colony.get("alerts") or []},
        "visitors": {v.get("name") for v in colony.get("visitors") or []},
    }


def compare(old: dict, new: dict) -> list[tuple[bool, str]]:
    """Изменения от old к new: [(важное, текст)], важные первыми."""
    if not old or old.get("save") != new.get("save") or new["tick"] < old["tick"]:
        return []
    out: list[tuple[bool, str]] = []

    def add(important, text):
        out.append((important, text))

    if old["paused"] != new["paused"]:
        add(True, "игра поставлена на паузу" if new["paused"] else "игра снята с паузы")
    elif not new["paused"] and old["speed"] != new["speed"]:
        add(False, f"скорость игры: {_SPEEDS.get(new['speed'], new['speed'])}")
    if old.get("manual") is not None and old["manual"] != new.get("manual"):
        add(False, "ручные приоритеты " + ("включены" if new["manual"] else "выключены"))

    # враги
    for tid, t in new["threats"].items():
        if tid not in old["threats"]:
            add(True, f"появился враг: {t['name']} ({t['kind']}, здоровье {round(t['hp'] * 100)}%)")
        elif t["downed"] and not old["threats"][tid]["downed"]:
            add(True, f"враг {t['name']} упал и лежит")
    for tid, t in old["threats"].items():
        if tid not in new["threats"]:
            add(True, f"врага {t['name']} больше нет на карте (убит, пленён или ушёл)")

    # колонисты
    for pid, p in new["pawns"].items():
        o = old["pawns"].get(pid)
        n = p["name"]
        if o is None:
            add(True, f"в колонии новый человек: {n}" + (" (пленный)" if p["prisoner"] else ""))
            continue
        if p["downed"] and not o["downed"]:
            add(True, f"{n} упал(а) и лежит")
        elif o["downed"] and not p["downed"]:
            add(True, f"{n} снова на ногах")
        if p["bleeding"] > 0.2 and o["bleeding"] <= 0.2:
            add(True, f"{n} кровоточит ({p['bleeding']})")
        fight = _FIGHT_JOBS.get(p["job"]) or _FIGHT_JOBS.get(o["job"])
        if fight and p["job"] != o["job"] and p["job"] in _FIGHT_JOBS:
            add(True, f"{n} {fight}" + (" (враги на карте)" if new["threats"] else " (врагов нет: это драка между своими или с животным)"))
        if o["hp"] - p["hp"] >= _HP_STEP:
            cause = f", только что {fight}" if fight else (", враги рядом" if new["threats"] else "")
            add(True, f"{n}: здоровье {round(o['hp'] * 100)}% → {round(p['hp'] * 100)}%{cause}")
        elif p["hp"] - o["hp"] >= _HP_STEP:
            add(False, f"{n}: здоровье {round(o['hp'] * 100)}% → {round(p['hp'] * 100)}%")
        if abs(p["mood"] - o["mood"]) >= _HP_STEP or (p["mood"] < 0.35 <= o["mood"]):
            add(p["mood"] < 0.35 <= o["mood"], f"{n}: настроение {round(o['mood'] * 100)}% → {round(p['mood'] * 100)}%"
                + (" (расстроен(а), на грани срыва)" if p["mood"] < 0.35 else ""))
        if p["drafted"] != o["drafted"]:
            add(False, f"{n} " + ("в бою (драфт)" if p["drafted"] else "вернулась к обычной работе"))
        if p["weapon"] != o["weapon"]:
            add(False, f"{n}: в руках {p['weapon'] or 'ничего'} (было {o['weapon'] or 'ничего'})")
        if p["apparel"] != o["apparel"]:
            add(False, f"{n}: одежды на ней {p['apparel']} шт. (было {o['apparel']})")
        if p["job"] != o["job"] and not (p["downed"] or o["downed"]):
            add(False, f"{n}: {o['job'] or 'ничего'} → {p['job'] or 'ничего'}")
    for pid, o in old["pawns"].items():
        if pid not in new["pawns"]:
            add(True, f"{o['name']} больше нет в колонии (погибла, ушла или схвачена)")

    # постройки и чертежи
    for d, c in new["buildings"].items():
        diff = c - old["buildings"].get(d, 0)
        if diff > 0:
            planned = old["pending"].get(d, 0) > 0      # был чертёж: колонисты достроили то, что она сама разметила
            add(True, f"построено: {d} ×{diff}" + (" (достроен твой чертёж: колонисты выполнили твою разметку)" if planned else ""))
    dismantling = any(p["job"] in _DISMANTLE_JOBS for s in (old, new) for p in s["pawns"].values())
    for d, c in old["buildings"].items():
        diff = c - new["buildings"].get(d, 0)
        if diff > 0 and dismantling:
            add(False, f"колонисты разобрали {d} ×{diff} ради материалов (намеренно, не потеря и не налёт; "
                       "стены могли быть любые, в том числе старые или чужие)")
        elif diff > 0:
            add(True, f"потеряно или снесено: {d} ×{diff}")
    for d, c in new["pending"].items():
        if c > old["pending"].get(d, 0):
            add(False, f"поставлен чертёж: {d} ×{c - old['pending'].get(d, 0)}")

    # исследования
    for r in sorted(new["researched"] - old["researched"]):
        add(True, f"исследование завершено: {r}")
    if new["research"] != old["research"] and new["research"]:
        add(False, f"сейчас исследуется: {new['research']}")

    # ресурсы
    moves = []
    for k in set(old["resources"]) | set(new["resources"]):
        a, b = old["resources"].get(k, 0), new["resources"].get(k, 0)
        if abs(b - a) >= max(_RES_MIN, 0.15 * max(a, 1)):
            moves.append((abs(b - a), f"{k}: {a} → {b}"))
    for _, text in sorted(moves, reverse=True)[:6]:
        add(False, "ресурсы " + text)
    fo, fn = old.get("food_days"), new.get("food_days")
    if fo is not None and fn is not None and abs(fn - fo) >= 1:
        add(fn < 3, f"запас еды: {fo} → {fn} дн.")

    # прочее
    for a in sorted(new["alerts"] - old["alerts"]):
        add(True, f"тревога: {a}")
    for v in sorted(new["visitors"] - old["visitors"]):
        add(False, f"пришёл гость: {v}")
    for v in sorted(old["visitors"] - new["visitors"]):
        add(False, f"гость ушёл: {v}")
    if new["weather"] != old["weather"]:
        add(False, f"погода: {new['weather']}")
    if new["season"] != old["season"]:
        add(False, f"сезон: {new['season']}")

    out.sort(key=lambda x: not x[0])
    return out


def is_pause_change(line: str) -> bool:
    return line.startswith("игра поставлена на паузу") or line.startswith("игра снята с паузы")


def render(changes: list[tuple[bool, str]], limit: int = 25) -> list[str]:
    lines = [t for _, t in changes][:limit]
    if len(changes) > limit:
        lines.append(f"…и ещё {len(changes) - limit} мелких изменений")
    return lines
