"""Оценка колонии: чистая функция по состоянию игры (без вызовов мода, сети и часов), тестируется на подделанных состояниях.

`assess(state, rooms=None, history=None)` берёт снимок `/state` мода (поля: colony.pawns, buildings, zones, resources, food_days,
pending_construction, research; threats; manual_priorities) и возвращает СТРУКТУРУ ФАКТОВ: сколько колонистов и чем заняты, еда,
склад, постройки и помещения, настроение и его причины, оборона, исследования, приоритеты работ и список пробелов (`gaps`).
Пробел это факт-доказательство с серьёзностью 1-5 (5 самый тяжёлый), а не приказ и не готовая цель: цель выбирает агент.

Что собирает инструмент `colony_assess` (rimworld_mcp), а чистая функция только принимает:
  rooms    {"x,z": ответ мода room_info в клетке кровати}: настоящее помещение (is_proper_room и roofed) известно только по ним;
           без `rooms` помещения «не проверены», пробела про них нет (кроме случая, когда стен и дверей нет совсем);
  history  {"raids": N, "repelled": M, "deaths": K}: сколько было атак, отбито и потеряно людей (из протокола событий; None: неизвестно).
"""
import re

LOW_MOOD = 0.4                  # настроение ниже этого считается плохим (срыв начинается около 0,35)
LOW_MOOD_BREAK = 0.35
LOW_MOOD_MAJOR = 0.2
GAPS_TOP = 4                    # сколько главных пробелов идёт в краткую сводку для события

_IDLE_JOBS = {"Wait_Wander", "GotoWander", "Wait", "Wait_MaintainPosture", "Wait_SafeTemperature"}       # как statediff.IDLE_JOBS
_FIGHT_JOBS = {"AttackMelee", "AttackStatic", "SocialFight", "Flee"}
_REST_PREFIXES = ("LayDown", "Sleep", "Wait_Asleep", "Ingest", "SocialRelax", "Skygaze", "Meditate", "Play", "WatchTelevision",
                  "ViewArt", "Pray", "Vomit", "Wait_Downed", "GetRest")

_BED = re.compile(r"bed(roll)?$|sleepingspot", re.I)
_HOSPITAL = re.compile(r"hospital", re.I)
_TABLE = re.compile(r"^table(short|long|\d)", re.I)
_CHAIR = re.compile(r"chair|stool|couch", re.I)
_LAMP = re.compile(r"lamp|torch", re.I)
_DOOR = re.compile(r"door", re.I)
_WALL = re.compile(r"^wall", re.I)
_TURRET = re.compile(r"turret", re.I)
_TRAP = re.compile(r"^trap|spike", re.I)
_STOVE = re.compile(r"stove", re.I)
_RESEARCH_BENCH = re.compile(r"researchbench", re.I)
_FOOD = re.compile(r"^(meal|rawrice|rawpotato|rawcorn|rawberr|rawagave|meat_|pemmican|milk|egg)", re.I)
_BUILD_MATERIAL = re.compile(r"^(woodlog|steel|blocks)", re.I)
_BUILD_WORKS = ("Construction", "Hauling", "Growing")        # приоритеты работ, которые нужны для развития
_WORK_RU = {"Construction": "Строительство", "Hauling": "Перенос", "Growing": "Выращивание"}


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(value: float, digits: int = 2) -> str:
    """Число по-русски: десятичная запятая, без хвоста нулей."""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def _is_rest(job: str | None) -> bool:
    return bool(job) and str(job).startswith(_REST_PREFIXES)


def _slots(def_name: str) -> int:
    """Сколько человек спит на кровати: двойная на двоих."""
    return 2 if "double" in def_name.lower() else 1


def _colonists(colony: dict) -> list[dict]:
    return [p for p in colony.get("pawns") or [] if isinstance(p, dict) and not p.get("is_prisoner")]


def _people(pawns: list[dict]) -> dict:
    out = {"total": len(pawns), "working": 0, "idle": 0, "downed": 0, "resting": 0, "drafted": 0, "fighting": 0,
           "idle_names": []}
    for p in pawns:
        job = p.get("current_job")
        if p.get("is_downed"):
            out["downed"] += 1
        elif p.get("is_drafted") or job in _FIGHT_JOBS:
            out["drafted" if p.get("is_drafted") else "fighting"] += 1
        elif job in _IDLE_JOBS or not job:
            out["idle"] += 1
            out["idle_names"].append(str(p.get("name") or p.get("id") or "?"))
        elif _is_rest(job):
            out["resting"] += 1
        else:
            out["working"] += 1
    able = out["total"] - out["downed"]
    out["working_share"] = round(out["working"] / able, 2) if able > 0 else 0.0
    return out


def count_buildings(buildings: list[dict]) -> dict:
    c = {"beds": 0, "bed_slots": 0, "hospital_beds": 0, "tables": 0, "chairs": 0, "lamps": 0, "doors": 0, "walls": 0, "turrets": 0,
         "sandbags": 0, "traps": 0, "stoves": 0, "research_benches": 0, "total": 0}
    beds: list[dict] = []
    for b in buildings:
        name = str((b or {}).get("def") or "")
        if not name:
            continue
        c["total"] += 1
        low = name.lower()
        if _HOSPITAL.search(name):
            c["hospital_beds"] += 1
        elif _BED.search(name):
            c["beds"] += 1
            c["bed_slots"] += _slots(name)
            beds.append(b)
        elif _TABLE.match(name):
            c["tables"] += 1
        elif _CHAIR.search(name):
            c["chairs"] += 1
        elif _LAMP.search(name):
            c["lamps"] += 1
        elif _DOOR.search(name):
            c["doors"] += 1
        elif _WALL.match(name):
            c["walls"] += 1
        elif _TURRET.search(name):
            c["turrets"] += 1
        elif low == "sandbags":
            c["sandbags"] += 1
        elif _TRAP.search(name):
            c["traps"] += 1
        elif _STOVE.search(name):
            c["stoves"] += 1
        elif _RESEARCH_BENCH.search(name):
            c["research_benches"] += 1
    c["_beds"] = beds
    return c


def _pending(colony: dict) -> dict:
    items = [p for p in colony.get("pending_construction") or [] if isinstance(p, dict)]
    by_def: dict[str, int] = {}
    for p in items:
        by_def[str(p.get("def"))] = by_def.get(str(p.get("def")), 0) + 1
    return {"count": len(items), "by_def": dict(sorted(by_def.items(), key=lambda kv: -kv[1])[:6])}


def _shelter(beds: list[dict], counts: dict, rooms: dict | None, n_people: int) -> dict:
    """Помещения: настоящее = мод говорит is_proper_room и roofed (в клетке кровати), дверь есть среди построек."""
    out = {"known": rooms is not None, "proper_rooms": 0, "beds_in_room": 0, "beds": counts["beds"], "colonists": n_people,
           "doors": counts["doors"], "walls": counts["walls"], "has_table": counts["tables"] > 0, "has_chairs": counts["chairs"] > 0,
           "has_light": counts["lamps"] > 0, "unroofed": 0}
    if rooms is None:
        return out
    seen = set()
    for b in beds:
        pos = b.get("position") or {}
        info = rooms.get(f"{pos.get('x')},{pos.get('z')}")
        if not isinstance(info, dict) or not info.get("room") or not info.get("is_proper_room"):
            continue
        if info.get("roofed") is False:
            out["unroofed"] += 1
            continue
        out["beds_in_room"] += _slots(str(b.get("def") or ""))
        seen.add((info.get("role"), info.get("cells")))
    out["proper_rooms"] = len(seen) if out["beds_in_room"] else 0
    if counts["doors"] == 0:
        out["proper_rooms"], out["beds_in_room"] = 0, 0           # без двери помещение не настоящее
    return out


def _mood(pawns: list[dict]) -> dict:
    moods = [(_num(p.get("mood_pct"), 0.5), str(p.get("name") or "?")) for p in pawns if p.get("mood_pct") is not None]
    if not moods:
        return {"known": False, "low_threshold": LOW_MOOD, "low_count": 0, "causes": []}
    values = [m for m, _ in moods]
    worst = min(moods)
    causes: dict[str, list] = {}
    for p in pawns:
        for t in p.get("mood_thoughts") or []:
            eff = _num((t or {}).get("effect"))
            if eff < 0:
                row = causes.setdefault(str(t.get("label")), [0.0, 0])
                row[0] += eff
                row[1] += 1
    top = sorted(causes.items(), key=lambda kv: kv[1][0])[:3]
    return {"known": True, "mean": round(sum(values) / len(values), 2), "min": round(worst[0], 2), "min_name": worst[1],
            "low_threshold": LOW_MOOD, "low_count": sum(1 for v in values if v < LOW_MOOD), "total": len(values),
            "causes": [{"label": k, "effect": round(v[0], 1), "colonists": v[1]} for k, v in top]}


def _defense(pawns: list[dict], counts: dict, history: dict | None) -> dict:
    armed = sum(1 for p in pawns if p.get("equipped"))
    out = {"armed": armed, "colonists": len(pawns), "turrets": counts["turrets"], "sandbags": counts["sandbags"],
           "traps": counts["traps"], "history_known": history is not None}
    if history is not None:
        out.update({k: int(history.get(k) or 0) for k in ("raids", "repelled", "deaths")})
    return out


def _work_setup(pawns: list[dict], manual: bool | None) -> dict:
    known = bool(pawns) and all(isinstance(p.get("work_priorities"), dict) for p in pawns)
    out = {"known": known, "manual_priorities": manual, "need": max(1, -(-len(pawns) // 3)) if pawns else 0}
    if known:
        for w in _BUILD_WORKS:
            out[w] = sum(1 for p in pawns if (p["work_priorities"].get(w) or 0) > 0)
    return out


def _gap(gaps: list, gid: str, severity: int, fact: str) -> None:
    gaps.append({"id": gid, "severity": max(1, min(5, int(severity))), "fact": fact})


def _food_gaps(gaps: list, food: dict, n: int) -> None:
    days = food["days"]
    stock = f"запасов еды {food['stock_items']}"
    if days is not None and n > 0:
        if days < 3:
            _gap(gaps, "food_critical", 5, f"еды на {_fmt(days, 1)} дня, {stock}, поля {'есть' if food['has_field'] else 'нет'}")
        elif days < 7:
            _gap(gaps, "food_low", 4, f"еды на {_fmt(days, 1)} дня, {stock}, поля {'есть' if food['has_field'] else 'нет'}")
    if not food["has_field"] and n > 0:
        left = f"еды на {_fmt(days, 1)} дня" if days is not None else "запас еды неизвестен"
        _gap(gaps, "no_field", 4 if (days is not None and days < 10) else 2, f"поля нет, {left}")
    if not food["has_stove"] and n > 0 and (food["has_field"] or food["stock_items"] > 0):
        _gap(gaps, "no_stove", 2, "печи для готовки нет, есть сырая еда" if food["stock_items"] else "печи для готовки нет, поле есть")


def _mood_gaps(gaps: list, mood: dict, n: int) -> None:
    if not mood["known"] or mood["low_count"] == 0:
        return
    low_min = mood["min"]
    sev = 5 if low_min < LOW_MOOD_MAJOR or mood["low_count"] * 2 >= max(n, 1) and mood["mean"] < LOW_MOOD else \
        4 if low_min < LOW_MOOD_BREAK else 3
    why = "; ".join(f"{c['label']} ({_fmt(c['effect'], 1)})" for c in mood["causes"])
    _gap(gaps, "low_mood", sev, f"колонистов с настроением ниже {_fmt(LOW_MOOD, 1)}: {mood['low_count']} из {mood['total']}, "
         f"минимум {_fmt(low_min)} ({mood['min_name']}), среднее {_fmt(mood['mean'])}" + (f"; причины: {why}" if why else ""))


def _shelter_gaps(gaps: list, sh: dict, counts: dict, n: int) -> None:
    if n == 0:
        return
    if counts["bed_slots"] < n:
        sev = 4 if counts["bed_slots"] * 2 < n else 3
        _gap(gaps, "beds", sev, f"кроватей {counts['beds']} при {n} колонистах" + (f" (мест для сна {counts['bed_slots']})" if counts["bed_slots"] != counts["beds"] else ""))
    if sh["known"]:
        if sh["proper_rooms"] == 0:
            extra = f"; без крыши {sh['unroofed']}" if sh["unroofed"] else ""
            _gap(gaps, "no_room", 4, f"настоящего помещения (замкнутого, с дверью и крышей) нет: дверей {sh['doors']}, стен {sh['walls']}{extra}")
        elif sh["beds_in_room"] < min(n, counts["bed_slots"]):
            _gap(gaps, "beds_outside", 3, f"в помещении кроватей {sh['beds_in_room']} из {counts['bed_slots']}, колонистов {n}")
    elif counts["walls"] == 0 and counts["doors"] == 0:
        _gap(gaps, "no_room", 4, "стен 0, дверей 0: помещения нет")
    if not sh["has_table"]:
        _gap(gaps, "no_table", 3, "обеденного стола нет")
    elif not sh["has_chairs"]:
        _gap(gaps, "no_chairs", 2, f"стол есть, стульев нет (колонистов {n})")
    if not sh["has_light"]:
        _gap(gaps, "no_light", 2, "освещения нет (ламп 0)")


def _economy_gaps(gaps: list, store: dict, pend: dict, people: dict, work: dict) -> None:
    n = people["total"]
    if not store["has_zone"]:
        _gap(gaps, "no_stockpile", 4, "зоны склада нет: ресурсы без неё в учёте не видны")
    elif store["materials"] < 30:
        _gap(gaps, "few_materials", 3, f"стройматериалов на складе {store['materials']} (дерево, сталь, блоки)")
    if pend["count"]:
        top = ", ".join(f"{k} x{v}" for k, v in list(pend["by_def"].items())[:3])
        _gap(gaps, "unfinished", 3 if pend["count"] >= 6 else 2, f"недостроено {pend['count']}: {top}")
    if people["downed"]:
        _gap(gaps, "downed", 4, f"лежат без сознания или ранены: {people['downed']} из {n}")
    if people["idle"]:
        who = ", ".join(people["idle_names"][:3])
        _gap(gaps, "idle", 4 if people["idle"] * 2 >= n else 2, f"простаивают {people['idle']} из {n} ({who}), работают {people['working']}")
    elif n and people["working_share"] < 0.5 and people["resting"] * 2 < n:
        _gap(gaps, "few_working", 3, f"работают {people['working']} из {n}")
    if work["known"]:
        lacking = [w for w in _BUILD_WORKS if work[w] < work["need"]]
        if lacking:
            zero = any(work[w] == 0 for w in lacking)
            _gap(gaps, "work_priorities", 4 if zero else 3,
                 "приоритеты работ: " + ", ".join(f"{_WORK_RU[w]} у {work[w]} из {n}" for w in _BUILD_WORKS) + f" (нужно хотя бы {work['need']})")


def _defense_gaps(gaps: list, d: dict, threats: int) -> None:
    if d["colonists"] == 0:
        return
    hist = f"; атак было {d['raids']}, отбито {d['repelled']}, потеряно людей {d['deaths']}" if d["history_known"] and d["raids"] else ""
    raided = d["history_known"] and d["raids"] > 0
    if threats:
        _gap(gaps, "threat_now", 5, f"на карте враги: {threats}")
    if d["armed"] * 2 < d["colonists"]:
        _gap(gaps, "unarmed", 4 if raided else 3, f"вооружены {d['armed']} из {d['colonists']}{hist}")
    if d["turrets"] + d["sandbags"] + d["traps"] == 0:
        _gap(gaps, "no_defense", 3 if raided else 2,
             f"укреплений нет: турелей {d['turrets']}, мешков с песком {d['sandbags']}, ловушек {d['traps']}{hist}")
    elif d["history_known"] and d["deaths"] > 1:
        _gap(gaps, "losses", 4, f"потеряно людей {d['deaths']}; атак было {d['raids']}, отбито {d['repelled']}")


def _research_gaps(gaps: list, r: dict, n: int) -> None:
    if n == 0:
        return
    if not r["has_bench"]:
        _gap(gaps, "no_research_bench", 2, f"исследовательского стола нет, доступно проектов {r['available']}")
    elif not r["current"] and r["available"]:
        _gap(gaps, "no_research", 3, f"исследование не выбрано, доступно проектов {r['available']}")


def assess(state: dict | None, rooms: dict | None = None, history: dict | None = None) -> dict:
    """Структура фактов о колонии и `gaps` (по убыванию серьёзности). Пустое состояние: `ok=False`, пробелов нет."""
    state = state if isinstance(state, dict) else {}
    colony = state.get("colony") if isinstance(state.get("colony"), dict) else {}
    pawns = _colonists(colony)
    if not colony or not pawns:
        return {"ok": False, "note": "состояния колонии нет или в ней нет колонистов", "gaps": []}
    people = _people(pawns)
    counts = count_buildings(colony.get("buildings") or [])
    beds = counts.pop("_beds")
    zones = [str((z or {}).get("type") or "").lower() for z in colony.get("zones") or []]
    resources = {str(k): int(_num(v)) for k, v in (colony.get("resources") or {}).items()}
    pend = _pending(colony)
    threats = len(state.get("threats") or [])
    mood = _mood(pawns)
    sh = _shelter(beds, counts, rooms, len(pawns))
    food = {"days": round(_num(colony.get("food_days")), 1) if colony.get("food_days") is not None else None,
            "has_field": any("grow" in z for z in zones), "has_stove": counts["stoves"] > 0,
            "stock_items": sum(v for k, v in resources.items() if _FOOD.match(k))}
    store = {"has_zone": any("stockpile" in z for z in zones), "materials": sum(v for k, v in resources.items() if _BUILD_MATERIAL.match(k)),
             "resources": dict(sorted(resources.items(), key=lambda kv: -kv[1])[:8])}
    res = colony.get("research") if isinstance(colony.get("research"), dict) else {}
    research = {"current": res.get("current"), "available": len(res.get("available") or []), "completed": len(res.get("completed") or []),
                "has_bench": counts["research_benches"] > 0}
    defense = _defense(pawns, counts, history)
    work = _work_setup(pawns, state.get("manual_priorities"))
    gaps: list[dict] = []
    _food_gaps(gaps, food, people["total"])
    _economy_gaps(gaps, store, pend, people, work)
    _shelter_gaps(gaps, sh, counts, people["total"])
    _mood_gaps(gaps, mood, people["total"])
    _defense_gaps(gaps, defense, threats)
    _research_gaps(gaps, research, people["total"])
    gaps.sort(key=lambda g: -g["severity"])               # сортировка устойчива: при равной серьёзности порядок выявления
    return {"ok": True, "colonists": people, "food": food, "storage": store, "buildings": {k: v for k, v in counts.items() if k != "total"} | {"built_total": counts["total"], "pending": pend},
            "shelter": sh, "mood": mood, "defense": defense, "research": research, "work": work, "threats": threats, "gaps": gaps}


def top_gaps_text(result: dict, top: int = GAPS_TOP) -> str:
    """Главные пробелы одной строкой (факты через «; ») для текста события; пробелов нет: сказано прямо."""
    gaps = (result or {}).get("gaps") or []
    if not gaps:
        return "заметных пробелов в оценке нет" if (result or {}).get("ok") else "оценка состояния недоступна"
    return "; ".join(g["fact"] for g in gaps[:top])
