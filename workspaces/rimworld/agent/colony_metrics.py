"""Замер автономной игры: чистая логика метрик колонии (без сети и без игры, тестируется на подделанных состояниях).

Критерий владельца: за полчаса АКТИВНОЙ игры (без пауз) агент строит и обустраивает здание, отбивает хотя бы одну атаку,
не меньше половины поселенцев работает, потеряно не больше одного человека, срывов нет (половина колонистов всегда в норме).

`ColonyMetrics.update(state, now, rooms)` кормят снимками `/state` мода (поля: tick, paused, time_speed, save_id,
colony.pawns/buildings/resources/food_days, threats, pending_events). Время `now` в секундах реального времени
(монотонные часы): активное время копится между двумя опросами, если игра не на паузе и тики шли.
Запрос к игре делает `tools/colony_eval.py`; здесь только арифметика, поэтому всё проверяется тестами.

Что мод НЕ отдаёт и чем это заменено, см. `APPROXIMATIONS` (печатается в отчёте).
"""
import json
import math
import re
from dataclasses import asdict, dataclass

try:
    from statediff import IDLE_JOBS, _FIGHT_JOBS      # те же списки простоя и боя, что у наблюдателя агента
except Exception:                                     # запуск вне agent/: запасные копии
    IDLE_JOBS = {"Wait_Wander", "GotoWander", "Wait", "Wait_MaintainPosture", "Wait_SafeTemperature"}
    _FIGHT_JOBS = {"AttackMelee": "", "AttackStatic": "", "SocialFight": "", "Flee": ""}

TICKS_PER_HOUR = 2500
TICKS_PER_DAY = 60000

# Работа не по плану: сон, еда, отдых и развлечения. Колонист в них не считается ни работающим, ни бездельничающим.
REST_JOB_PREFIXES = ("LayDown", "Sleep", "Wait_Asleep", "Ingest", "SocialRelax", "Skygaze", "Meditate", "Play", "WatchTelevision",
                     "ViewArt", "Pray", "Vomit", "Wait_Downed", "GetRest")

APPROXIMATIONS = [
    "Мод не отдаёт ни часа суток, ни расписания в удобном виде: «часы суток» считаются от тика (тик/2500 по модулю 24), смещение начала дня неизвестно.",
    "Помещение и мебель в нём: в состоянии нет списка комнат, поэтому клетки кроватей и мебели опрашиваются командой мода room_info "
    "(is_proper_room, roofed); одна комната опознаётся по паре (роль, число клеток). Если команды нет, берётся ограничивающий "
    "прямоугольник стен и дверей (крыша не проверяется).",
    "Срыв: мод не отдаёт психическое состояние. Срывом считается явный признак (in_mental_break или mental_state, если мод их добавит) "
    "либо падение настроения ниже порога (--break-mood); число таких падений отдельно от явных срывов.",
    "Гибель и пропажа: событие pawn_died из pending_events (мод отдаёт его без очистки, но очередь может опустошить сам агент, "
    "поэтому события дополняются исчезновением колониста из списка).",
    "Атака: появление врагов в threats (не лежащих и не бегущих) после отфильтровывания далёких бродячих (real_threats) или событие incident; "
    "отбита, когда активных врагов нет несколько опросов подряд при живой колонии. Враги за туманом мод не показывает.",
    "Работа: job не из простоя и не из сна/еды/отдыха; пешки в бою, лежащие и пленные в расчёт не входят.",
]


@dataclass
class Config:
    """Пороги критериев. Всё можно менять параметрами инструмента."""
    active_seconds: float = 1800.0       # сколько секунд АКТИВНОЙ игры нужно набрать
    max_gap: float = 30.0                # интервал между опросами длиннее считается сбоем связи: лишнее в активное время не идёт
    min_beds: int | None = None          # кроватей в помещении; по умолчанию ceil(колонистов/2), не меньше 1
    min_furniture: int = 1               # другой мебели в помещении
    min_new_structures: int = 4          # сколько стен и дверей должно появиться за прогон («здание создано», а не найдено)
    min_walls_bbox: int = 8              # в запасном режиме: минимум стен и дверей для здания
    max_probe: int = 12                  # сколько клеток опрашивать командой room_info за раз
    min_attackers: int = 1               # сколько активных врагов считаются атакой
    clear_polls: int = 2                 # опросов подряд без активных врагов, чтобы считать атаку отбитой
    min_repelled: int = 1                # сколько атак нужно отбить
    work_min_share: float = 0.5          # доля работающих в одной выборке, при которой выборка «хорошая»
    work_mean_min: float = 0.5           # среднее по выборкам
    work_samples_min: float = 0.7        # доля «хороших» выборок
    strict_work: bool = False            # считать сон и отдых как неработающих (по умолчанию они выбывают из знаменателя)
    max_losses: int = 1                  # погибших и пропавших не больше
    health_hp_min: float = 0.5           # здоров: health_pct не ниже
    health_mood_min: float = 0.3         # в норме: mood_pct не ниже
    health_ok_share: float = 0.5         # доля колонистов в норме в каждой выборке не ниже
    max_health_violations: int = 0       # выборок-нарушений не больше
    break_mood: float = 0.2              # ниже этого настроения считаем срывом (приближение)
    stall_seconds: float = 60.0          # пауза без единого изменения колонии дольше этого (реальных секунд) проваливает прогон; 0: не проверять
    inject_window: float = 180.0         # атака в течение стольких активных секунд после инъекции считается инъекцией


# ── Классификация объектов и занятий ───────────────────────────────────────────────

_FURNITURE = re.compile(r"^(Table\d|TableShort|TableLong|Table$|DiningChair|Chair|Stool|Armchair|Couch|Sofa|EndTable|Dresser|Shelf|"
                        r"ShelfSmall|Bookcase|Bench|Lamp|StandingLamp|TorchLamp)", re.I)


def is_wall(d: str) -> bool:
    d = d or ""
    return d.startswith("Wall") and "Lamp" not in d and "Torch" not in d


def is_door(d: str) -> bool:
    d = d or ""
    return d.startswith("Door") or d in ("Autodoor", "Doorway")


def bed_slots(d: str) -> int:
    """Сколько спальных мест даёт постройка (0: не кровать)."""
    d = d or ""
    if "Bed" not in d and d != "SleepingSpot":
        return 0
    return 2 if "Double" in d else 1


def is_furniture(d: str) -> bool:
    d = d or ""
    return bool(_FURNITURE.match(d)) and not bed_slots(d)


def classify_job(job, drafted: bool = False) -> str:
    """work | idle | rest | combat."""
    if drafted or job in _FIGHT_JOBS:
        return "combat"
    if not job or job in IDLE_JOBS:
        return "idle"
    if str(job).startswith(REST_JOB_PREFIXES):
        return "rest"
    return "work"


def _xz(obj) -> tuple[int, int] | None:
    pos = (obj or {}).get("position") or {}
    try:
        return int(pos["x"]), int(pos["z"])
    except (KeyError, TypeError, ValueError):
        return None


def _loaded(state) -> bool:
    return (isinstance(state, dict) and state.get("tick") is not None and state.get("status") not in ("no_game", "no_map", "error")
            and isinstance(state.get("colony"), dict))


def _pawns(state: dict) -> list[dict]:
    raw = (state.get("colony") or {}).get("pawns")
    return [p for p in raw if isinstance(p, dict) and p.get("id")] if isinstance(raw, list) else []


def _real_threats(state: dict) -> list[dict]:
    """Враги рядом или в бою (далёкие бродячие отбрасываются, как у агента)."""
    threats = [t for t in (state.get("threats") or []) if isinstance(t, dict)]
    try:
        import real_threats
        return real_threats.filter_threats({"threats": threats, "colony": {"pawns": _pawns(state)}})["threats"]
    except Exception:
        return threats


def _active_hostiles(threats: list[dict]) -> list[dict]:
    return [t for t in threats if not t.get("is_downed") and not t.get("is_fleeing")]


# ── Здание и обустройство ──────────────────────────────────────────────────────────

def probe_cells(buildings: list[dict], limit: int) -> list[tuple[int, int]]:
    """Клетки, где стоит спросить мод о помещении: сначала кровати, потом остальная мебель."""
    beds, furn = [], []
    for b in buildings:
        xz = _xz(b)
        if xz is None:
            continue
        d = b.get("def") or ""
        (beds if bed_slots(d) else furn if is_furniture(d) else []).append(xz)
    out = []
    for xz in beds + furn:
        if xz not in out:
            out.append(xz)
    return out[:limit]


def _needed_beds(cfg: Config, colonists: int) -> int:
    return cfg.min_beds if cfg.min_beds is not None else max(1, math.ceil(colonists / 2))


def evaluate_building(buildings: list[dict], colonists: int, cfg: Config, rooms: dict | None) -> dict:
    """Есть ли настоящее обустроенное помещение. rooms: {(x, z): ответ room_info} или None (команды нет: запасной режим).

    Режим room_info: комната (is_proper_room и крыша, не улица) опознаётся по (роль, число клеток); в ней считаются кровати
    (двойная даёт 2 места) и другая мебель, стоящие в опрошенных клетках. Лучшая комната по числу мест и мебели.
    Запасной режим bbox: стен и дверей не меньше cfg.min_walls_bbox, есть дверь; кровати и мебель внутри их ограничивающего прямоугольника."""
    need = _needed_beds(cfg, colonists)
    walls = [b for b in buildings if is_wall(b.get("def"))]
    doors = [b for b in buildings if is_door(b.get("def"))]
    out = {"mode": "room_info" if rooms is not None else "bbox", "ok": False, "beds": 0, "furniture": 0, "beds_needed": need,
           "walls": len(walls), "doors": len(doors), "room": None, "approximate": rooms is None}
    if rooms is not None:
        groups: dict[tuple, dict] = {}
        for b in buildings:
            xz = _xz(b)
            info = rooms.get(xz) if xz else None
            if not isinstance(info, dict) or not info.get("room") or not info.get("is_proper_room") or info.get("outdoors"):
                continue
            if info.get("roofed") is False:
                continue
            g = groups.setdefault((info.get("role"), info.get("cells")), {"role": info.get("role"), "cells": info.get("cells"),
                                                                           "beds": 0, "furniture": 0})
            d = b.get("def") or ""
            if bed_slots(d):
                g["beds"] += bed_slots(d)
            elif is_furniture(d):
                g["furniture"] += 1
        best = max(groups.values(), key=lambda g: (g["beds"] >= need and g["furniture"] >= cfg.min_furniture, g["beds"], g["furniture"]),
                   default=None)
        if best:
            out.update(room=best, beds=best["beds"], furniture=best["furniture"])
    else:
        pts = [xz for xz in (_xz(b) for b in walls + doors) if xz]
        if len(pts) >= cfg.min_walls_bbox and doors:
            x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
            z0, z1 = min(p[1] for p in pts), max(p[1] for p in pts)
            for b in buildings:
                xz = _xz(b)
                if xz and x0 < xz[0] < x1 and z0 < xz[1] < z1:
                    d = b.get("def") or ""
                    out["beds"] += bed_slots(d)
                    out["furniture"] += 1 if is_furniture(d) else 0
            out["room"] = {"bbox": [x0, z0, x1, z1]}
    out["ok"] = out["room"] is not None and out["beds"] >= need and out["furniture"] >= cfg.min_furniture
    return out


SERVICE_EVENTS = ("game_loaded",)


def fingerprint(state: dict, events_seen: int = 0) -> dict:
    """Отпечаток колонии: то, что меняется от действий агента (даже на паузе). Тики, настроение и здоровье не входят.
    Группы: работы колонистов (работа, призыв в бой, лежит, пленный), приоритеты работ, постройки и чертежи по видам, зоны,
    ресурсы на складе, исследование, число врагов, число событий мода (без служебных)."""
    colony = state.get("colony") or {}
    pawns = {p["id"]: [p.get("current_job"), bool(p.get("is_drafted")), bool(p.get("is_downed")), bool(p.get("is_prisoner"))]
             for p in _pawns(state)}
    prios = {p["id"]: p.get("work_priorities") for p in _pawns(state) if p.get("work_priorities")}
    blds: dict = {}
    for b in colony.get("buildings") or []:
        if isinstance(b, dict):
            blds[b.get("def")] = blds.get(b.get("def"), 0) + 1
    pend: dict = {}
    for c in colony.get("pending_construction") or []:
        if isinstance(c, dict):
            k = f"{c.get('def')}:{c.get('status')}"
            pend[k] = pend.get(k, 0) + 1
    zones = sorted(str(z.get("id")) for z in colony.get("zones") or [] if isinstance(z, dict))
    research = colony.get("research") if isinstance(colony.get("research"), dict) else {}
    return {"jobs": pawns, "priorities": prios, "buildings": blds, "pending": pend, "zones": zones,
            "resources": dict(colony.get("resources") or {}), "research": research.get("current"),
            "threats": len(state.get("threats") or []), "events": events_seen}


# ── Метрики ───────────────────────────────────────────────────────────────────────

class ColonyMetrics:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        # время
        self.active_seconds = 0.0
        self.active_ticks = 0
        self.paused_seconds = 0.0
        self.stalled_seconds = 0.0           # игра не на паузе, но тики не идут (меню, окно, зависание)
        self.unloaded_seconds = 0.0
        self.gap_seconds = 0.0               # потеряно на сбоях связи (больше max_gap)
        self.speed_seconds: dict[int, float] = {}
        self.polls = 0
        self.misses = 0
        self.consecutive_misses = 0
        self.reloads = 0
        self.first_now: float | None = None
        self.last_now: float | None = None
        self._prev: dict | None = None
        self._last_loaded_now: float | None = None
        # колонисты
        self._known: dict[str, dict] = {}
        self.dead: dict[str, str] = {}
        self.missing: dict[str, str] = {}
        self.colony_died = False
        self.peak_colonists = 0
        self._low_mood: set[str] = set()
        self._flagged: set[str] = set()
        self.breaks_estimated = 0
        self.breaks_flagged = 0
        self.breaks_events = 0
        # выборки
        self.work_samples: list[float] = []
        self.work_strict_samples: list[float] = []
        self.health_samples = 0
        self.health_violations = 0
        self.health_worst = 1.0
        self._idle_run: dict[str, float] = {}
        self.idle_streaks: list[float] = []
        self.idle_pawn_seconds = 0.0
        self.hour_work: dict[int, int] = {}      # игровой час -> максимум работающих за его выборки
        # здание
        self.room_info_supported: bool | None = None
        self.baseline_structures: int | None = None
        self.building_done_at: float | None = None
        self.building: dict = {}
        self.new_structures = 0
        # атаки
        self.attacks: list[dict] = []
        self._attack: dict | None = None
        self._pending_incident: dict | None = None
        self.incidents: list[dict] = []
        self.enemies_killed = 0
        self.injections: list[dict] = []
        self._seen_events: set[str] = set()
        # прочее
        self.food_days_min: float | None = None
        self.food_days_last: float | None = None
        self.last_resources: dict = {}
        self.last_buildings: dict[str, int] = {}
        self.instant: dict = {}
        # зависание на паузе
        self._events_seen = 0
        self._still_fp: dict | None = None        # отпечаток текущей паузы
        self._still_start: float | None = None
        self._last_change: dict = {"at_real_s": None, "what": []}
        self.longest_still = 0.0
        self.stall: dict = {"failed": False, "at_real_s": None, "paused_s": 0.0, "last_change": None}

    # ── вход ──

    def note_miss(self, now: float):
        """Опрос не удался (игра не отвечает)."""
        self._touch(now)
        self.misses += 1
        self.consecutive_misses += 1

    def note_injection(self, ok: bool, supported: bool, message: str = ""):
        self.injections.append({"active_s": round(self.active_seconds, 1), "ok": ok, "supported": supported, "message": message[:200]})

    def want_room_probe(self) -> bool:
        return self.building_done_at is None and self.room_info_supported is not False

    def set_room_support(self, supported: bool):
        self.room_info_supported = supported

    def _touch(self, now: float):
        if self.first_now is None:
            self.first_now = now
        self.last_now = now

    def update(self, state, now: float, rooms: dict | None = None, events: list | None = None):
        """Один опрос. rooms: ответы room_info по клеткам (если опрашивали), events: события, полученные отдельно от состояния."""
        self._touch(now)
        self.polls += 1
        if not _loaded(state):
            self._unloaded(now)
            return
        self.consecutive_misses = 0
        tick = int(state["tick"])
        paused = bool(state.get("paused"))
        speed = state.get("time_speed")
        speed = int(speed) if isinstance(speed, (int, float)) else 0
        prev = self._prev
        if prev and (prev["save"] != state.get("save_id") or tick < prev["tick"]):
            self.reloads += 1                  # загрузка другого сохранения или откат: интервал не засчитываем
            prev = None
        active = False
        if prev:
            raw = max(0.0, now - prev["now"])
            dt = min(raw, self.cfg.max_gap)
            self.gap_seconds += raw - dt
            dtick = tick - prev["tick"]
            if dtick > 0:
                active = True
                self.active_seconds += dt
                self.active_ticks += dtick
                sp = speed if not paused else prev["speed"]
                self.speed_seconds[sp] = self.speed_seconds.get(sp, 0.0) + dt
            elif not paused and not prev["paused"]:
                self.stalled_seconds += dt
            else:
                self.paused_seconds += dt
        self._prev = {"now": now, "tick": tick, "paused": paused, "speed": speed, "save": state.get("save_id")}
        self._last_loaded_now = now
        dt_sample = min(max(0.0, now - prev["now"]), self.cfg.max_gap) if prev else 0.0

        pawns = _pawns(state)
        colonists = [p for p in pawns if not p.get("is_prisoner")]
        self.peak_colonists = max(self.peak_colonists, len(colonists))
        if isinstance((state.get("colony") or {}).get("pawns"), list):       # список колонистов неполон: исчезновения не считаем
            self._track_pawns(pawns)
        self._read_events(list(state.get("pending_events") or []) + list(events or []))
        self._track_attack(state, pawns, tick)
        self._track_breaks(colonists)
        self._track_misc(state)
        work_now = self._sample_instant(state, colonists, tick)
        if active:
            self._record_samples(colonists, tick, dt_sample, work_now)
        self._track_building(state, colonists, rooms)
        self._track_stall(state, now, paused)

    def _track_stall(self, state: dict, now: float, paused: bool):
        """Пауза подряд >= stall_seconds и отпечаток колонии не менялся: провал (фиксируется сразу, один раз)."""
        if not paused:
            self._still_fp = self._still_start = None
            return
        fp = fingerprint(state, self._events_seen)
        rel = now - (now if self.first_now is None else self.first_now)
        if self._still_fp is None or fp != self._still_fp:
            if self._still_fp is not None:
                self._last_change = {"at_real_s": round(rel, 1), "what": sorted(k for k in fp if fp[k] != self._still_fp.get(k))}
            self._still_fp, self._still_start = fp, now
        still = now - self._still_start
        self.longest_still = max(self.longest_still, still)
        limit = self.cfg.stall_seconds
        if limit > 0 and still >= limit and not self.stall["failed"]:
            self.stall = {"failed": True, "at_real_s": round(rel, 1), "paused_s": round(still, 1),
                          "last_change": self._last_change if self._last_change["at_real_s"] is not None else None}

    def _unloaded(self, now: float):
        if self._last_loaded_now is not None or self._prev is not None:
            ref = self._prev["now"] if self._prev else now
            self.unloaded_seconds += min(max(0.0, now - ref), self.cfg.max_gap)
            self._prev = {**self._prev, "now": now} if self._prev else None
        self.instant = {"loaded": False}

    # ── колонисты, потери, срывы ──

    def _track_pawns(self, pawns: list[dict]):
        now_ids = {p["id"] for p in pawns}
        for p in pawns:
            self._known[p["id"]] = {"name": p.get("name"), "prisoner": bool(p.get("is_prisoner"))}
            self.missing.pop(p["id"], None)
        if pawns or self._known:
            for pid, info in self._known.items():
                if pid not in now_ids and not info["prisoner"] and pid not in self.dead and pid not in self.missing:
                    self.missing[pid] = info.get("name") or pid

    def _track_breaks(self, colonists: list[dict]):
        cfg = self.cfg
        for p in colonists:
            pid = p["id"]
            mood = p.get("mood_pct")
            if isinstance(mood, (int, float)):
                if mood < cfg.break_mood and pid not in self._low_mood:
                    self._low_mood.add(pid)
                    self.breaks_estimated += 1
                elif mood >= cfg.break_mood + 0.05:
                    self._low_mood.discard(pid)
            state = p.get("mental_state")
            flagged = bool(p.get("in_mental_break")) or (state not in (None, "", "None", False))
            if flagged and pid not in self._flagged:
                self._flagged.add(pid)
                self.breaks_flagged += 1
            elif not flagged:
                self._flagged.discard(pid)

    def _read_events(self, events: list):
        for e in events:
            if not isinstance(e, dict):
                continue
            key = json.dumps([e.get("type"), e.get("tick"), e.get("data")], sort_keys=True, default=str)
            if key in self._seen_events:
                continue
            self._seen_events.add(key)
            kind = str(e.get("type") or "")
            if kind not in SERVICE_EVENTS:
                self._events_seen += 1
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            if kind == "pawn_died":
                pid = str(data.get("pawn_id") or data.get("name") or key)
                known = self._known.get(pid)
                if known is None or not known["prisoner"]:
                    self.dead[pid] = str(data.get("name") or pid)
                    self.missing.pop(pid, None)
            elif kind == "colony_died":
                self.colony_died = True
            elif kind == "enemy_killed":
                self.enemies_killed += 1
            elif kind == "incident":
                inc = {"incident": data.get("incident"), "points": data.get("points"), "active_s": round(self.active_seconds, 1)}
                self.incidents.append(inc)
                self._pending_incident = inc
            elif "mental" in kind:
                self.breaks_events += 1

    # ── атака ──

    def _track_attack(self, state: dict, pawns: list[dict], tick: int):
        cfg = self.cfg
        hostiles = _active_hostiles(_real_threats(state))
        alive = any(not p.get("is_prisoner") for p in pawns) and not self.colony_died
        fighting = any(p.get("is_drafted") or p.get("current_job") in _FIGHT_JOBS for p in pawns if not p.get("is_prisoner"))
        a = self._attack
        if a is None:
            if len(hostiles) < cfg.min_attackers:
                return
            inc = self._pending_incident
            injected = any(i["ok"] and 0 <= self.active_seconds - i["active_s"] <= cfg.inject_window for i in self.injections)
            a = self._attack = {"start_active_s": round(self.active_seconds, 1), "start_tick": tick, "peak": len(hostiles), "clear": 0,
                                "reaction_s": None, "injected": injected, "incident": (inc or {}).get("incident"),
                                "kinds": sorted({str(t.get("kind")) for t in hostiles}), "status": "ongoing"}
            self._pending_incident = None
        a["peak"] = max(a["peak"], len(hostiles))
        if fighting and a["reaction_s"] is None:
            a["reaction_s"] = round(self.active_seconds - a["start_active_s"], 1)
        if hostiles:
            a["clear"] = 0
            return
        a["clear"] += 1
        if not alive:
            self._finish_attack("lost")
        elif a["clear"] >= cfg.clear_polls:
            self._finish_attack("repelled")

    def _finish_attack(self, status: str):
        a = self._attack
        a["status"] = status
        a["end_active_s"] = round(self.active_seconds, 1)
        a["duration_s"] = round(a["end_active_s"] - a["start_active_s"], 1)
        a.pop("clear", None)
        self.attacks.append(a)
        self._attack = None

    # ── работа и здоровье ──

    def _sample_instant(self, state: dict, colonists: list[dict], tick: int) -> dict:
        cats = {"work": 0, "idle": 0, "rest": 0, "combat": 0, "downed": 0}
        for p in colonists:
            if p.get("is_downed"):
                cats["downed"] += 1
            else:
                cats[classify_job(p.get("current_job"), bool(p.get("is_drafted")))] += 1
        lenient = cats["work"] + cats["idle"]
        strict = lenient + cats["rest"]
        ok = [self._pawn_ok(p) for p in colonists]
        hostiles = _active_hostiles(_real_threats(state))
        self.instant = {
            "loaded": True, "tick": tick, "paused": bool(state.get("paused")), "speed": state.get("time_speed"),
            "colonists": len(colonists), "jobs": cats,
            "work_share": round(cats["work"] / lenient, 3) if lenient else None,
            "work_share_strict": round(cats["work"] / strict, 3) if strict else None,
            "ok_share": round(sum(ok) / len(ok), 3) if ok else None,
            "hostiles_active": len(hostiles), "hostiles_total": len(state.get("threats") or []),
            "food_days": self.food_days_last, "building": self.building or None,
        }
        return {"lenient": lenient, "strict": strict, "cats": cats, "ok": ok}

    def _pawn_ok(self, p: dict) -> bool:
        cfg = self.cfg
        hp = p.get("health_pct", 1.0)
        mood = p.get("mood_pct", 0.5)
        hp = 1.0 if not isinstance(hp, (int, float)) else hp
        mood = 0.5 if not isinstance(mood, (int, float)) else mood
        flagged = bool(p.get("in_mental_break")) or (p.get("mental_state") not in (None, "", "None", False))
        return not p.get("is_downed") and hp >= cfg.health_hp_min and mood >= cfg.health_mood_min and not flagged

    def _record_samples(self, colonists: list[dict], tick: int, dt: float, s: dict):
        cats = s["cats"]
        if s["lenient"]:
            self.work_samples.append(cats["work"] / s["lenient"])
        if s["strict"]:
            self.work_strict_samples.append(cats["work"] / s["strict"])
        hour = tick // TICKS_PER_HOUR
        self.hour_work[hour] = max(self.hour_work.get(hour, 0), cats["work"])
        for p in colonists:                       # простои: подряд идущие активные секунды без дела
            pid = p["id"]
            idle = (not p.get("is_downed")) and classify_job(p.get("current_job"), bool(p.get("is_drafted"))) == "idle"
            if idle:
                self._idle_run[pid] = self._idle_run.get(pid, 0.0) + dt
                self.idle_pawn_seconds += dt
            elif pid in self._idle_run:
                self.idle_streaks.append(self._idle_run.pop(pid))
        if s["ok"]:
            share = sum(s["ok"]) / len(s["ok"])
            self.health_samples += 1
            self.health_worst = min(self.health_worst, share)
            if share < self.cfg.health_ok_share:
                self.health_violations += 1

    # ── прочее ──

    def _track_misc(self, state: dict):
        colony = state.get("colony") or {}
        fd = colony.get("food_days")
        if isinstance(fd, (int, float)):
            self.food_days_last = fd
            self.food_days_min = fd if self.food_days_min is None else min(self.food_days_min, fd)
        if isinstance(colony.get("resources"), dict):
            self.last_resources = dict(colony["resources"])

    def _track_building(self, state: dict, colonists: list[dict], rooms: dict | None):
        buildings = [b for b in (state.get("colony") or {}).get("buildings") or [] if isinstance(b, dict)]
        counts: dict[str, int] = {}
        for b in buildings:
            counts[b.get("def")] = counts.get(b.get("def"), 0) + 1
        self.last_buildings = counts
        structs = sum(1 for b in buildings if is_wall(b.get("def")) or is_door(b.get("def")))
        if self.baseline_structures is None:
            self.baseline_structures = structs
        self.new_structures = max(0, structs - self.baseline_structures)
        if self.building_done_at is not None:
            return
        if rooms is None and self.room_info_supported is not False:
            return                                  # ответов room_info пока нет: ждём опрос
        res = evaluate_building(buildings, len(colonists), self.cfg, rooms)
        res["new_structures"] = self.new_structures
        self.building = res
        if res["ok"] and self.new_structures >= self.cfg.min_new_structures:
            self.building_done_at = round(self.active_seconds, 1)

    def pending_probe_cells(self, state: dict) -> list[tuple[int, int]]:
        buildings = [b for b in (state.get("colony") or {}).get("buildings") or [] if isinstance(b, dict)]
        return probe_cells(buildings, self.cfg.max_probe)

    # ── итог ──

    @property
    def done(self) -> bool:
        return self.active_seconds >= self.cfg.active_seconds

    def _lost(self) -> list[str]:
        names = list(self.dead.values()) + list(self.missing.values())
        if self.colony_died:                              # колония погибла целиком: потеряны все, кого видели
            names += ["?"] * max(1, self.peak_colonists - len(names))
        return names

    def _criteria(self, final: bool) -> list[dict]:
        cfg = self.cfg
        out = []

        def add(cid, title, ok, value, threshold, detail="", pending=False):
            out.append({"id": cid, "title": title, "status": "PENDING" if pending and not final and not ok else ("PASS" if ok else "FAIL"),
                        "value": value, "threshold": threshold, "detail": detail})

        b = self.building
        built = self.building_done_at is not None
        add("building", "Здание создано и обустроено", built,
            f"кроватей {b.get('beds', 0)}/{b.get('beds_needed', '?')}, мебели {b.get('furniture', 0)}, новых стен и дверей {self.new_structures}"
            if b else "нет данных о помещении",
            f"помещение с >= {b.get('beds_needed', '?')} кроватями и >= {cfg.min_furniture} мебелью, новых стен и дверей >= {cfg.min_new_structures}",
            ("готово на " + f"{self.building_done_at} с активной игры" if built else
             "режим: " + str(b.get("mode", "нет")) + ("; приближение" if b.get("approximate") else "")), pending=True)
        done_n = sum(1 for a in self.attacks if a["status"] == "repelled")
        started = len(self.attacks) + (1 if self._attack else 0)
        react = [a["reaction_s"] for a in self.attacks if a.get("reaction_s") is not None]
        add("attack", "Атака отбита", done_n >= cfg.min_repelled, f"начато {started}, отбито {done_n}", f"отбито >= {cfg.min_repelled}",
            ("реакция %.0f с; " % (sum(react) / len(react)) if react else "") + ("атака была инъекцией" if any(a["injected"] for a in self.attacks) else
                                                                                   "атаки естественные"), pending=True)
        ws = self.work_strict_samples if cfg.strict_work else self.work_samples
        mean = sum(ws) / len(ws) if ws else 0.0
        good = sum(1 for x in ws if x >= cfg.work_min_share) / len(ws) if ws else 0.0
        add("work", "Работает не меньше половины", bool(ws) and mean >= cfg.work_mean_min and good >= cfg.work_samples_min,
            f"среднее {mean:.2f}, выборок с долей >= {cfg.work_min_share}: {good:.0%} (выборок {len(ws)})",
            f"среднее >= {cfg.work_mean_min}, хороших выборок >= {cfg.work_samples_min:.0%}", "", pending=not ws)
        lost = self._lost()
        add("losses", "Потери не больше допустимых", len(lost) <= cfg.max_losses,
            f"погибло {len(self.dead)}, пропало {len(self.missing)}", f"<= {cfg.max_losses}", ", ".join(lost))
        add("stall", "Нет зависания на паузе", not self.stall["failed"],
            f"самая долгая неподвижная пауза {self.longest_still:.0f} с" + (f", провал на {self.stall['at_real_s']} с прогона" if self.stall["failed"] else ""),
            f"< {cfg.stall_seconds:g} с без изменений колонии" if cfg.stall_seconds > 0 else "проверка выключена",
            "изменение отпечатка (работы, постройки и чертежи, зоны, ресурсы, исследование, враги, события) сбрасывает отсчёт")
        add("health", "Здоровье и срывы", self.health_violations <= cfg.max_health_violations,
            f"выборок-нарушений {self.health_violations} из {self.health_samples}, худшая доля в норме {self.health_worst:.2f}",
            f"нарушений <= {cfg.max_health_violations}, доля в норме >= {cfg.health_ok_share}",
            f"срывов: явных {self.breaks_flagged}, по настроению < {cfg.break_mood} {self.breaks_estimated}, событий {self.breaks_events}",
            pending=self.health_samples == 0)
        return out

    def report(self, aborted: bool = False, abort_reason: str = "", wall_seconds: float | None = None) -> dict:
        final = self.done or aborted
        crit = self._criteria(final)
        passed = all(c["status"] == "PASS" for c in crit)
        speed_total = sum(self.speed_seconds.values()) or 1.0
        wall = wall_seconds if wall_seconds is not None else ((self.last_now or 0) - (self.first_now or 0))
        observed = {h: w for h, w in self.hour_work.items()}
        empty_hours = sorted(h for h, w in observed.items() if w == 0)
        hours_of_day = sorted({h % 24 for h in empty_hours})
        streaks = self.idle_streaks + list(self._idle_run.values())
        rep = {
            "version": 1,
            "config": asdict(self.cfg),
            "reached": self.done, "aborted": aborted, "abort_reason": abort_reason, "passed": passed,
            "exit_code": self.exit_code(aborted),
            "time": {
                "active_seconds": round(self.active_seconds, 1), "target_seconds": self.cfg.active_seconds,
                "active_ticks": self.active_ticks, "active_game_hours": round(self.active_ticks / TICKS_PER_HOUR, 1),
                "paused_seconds": round(self.paused_seconds, 1), "stalled_seconds": round(self.stalled_seconds, 1),
                "unloaded_seconds": round(self.unloaded_seconds, 1), "gap_seconds": round(self.gap_seconds, 1),
                "wall_seconds": round(wall, 1), "polls": self.polls, "misses": self.misses, "reloads": self.reloads,
                "speed_seconds": {str(k): round(v, 1) for k, v in sorted(self.speed_seconds.items())},
                "speed_share": {str(k): round(v / speed_total, 3) for k, v in sorted(self.speed_seconds.items())},
            },
            "criteria": crit, "stall": {**self.stall, "threshold_s": self.cfg.stall_seconds, "longest_still_s": round(self.longest_still, 1)},
            "building": self.building, "building_done_at_s": self.building_done_at, "new_structures": self.new_structures,
            "attacks": {"started": len(self.attacks) + (1 if self._attack else 0),
                        "repelled": sum(1 for a in self.attacks if a["status"] == "repelled"),
                        "list": self.attacks + ([{**self._attack, "clear": None}] if self._attack else []),
                        "incidents": self.incidents, "enemies_killed": self.enemies_killed,
                        "injections": self.injections},
            "work": {"samples": len(self.work_samples),
                     "mean": round(sum(self.work_samples) / len(self.work_samples), 3) if self.work_samples else None,
                     "mean_strict": round(sum(self.work_strict_samples) / len(self.work_strict_samples), 3) if self.work_strict_samples else None,
                     "idle_pawn_seconds": round(self.idle_pawn_seconds, 1),
                     "idle_streaks": len(streaks), "idle_streak_mean_s": round(sum(streaks) / len(streaks), 1) if streaks else 0.0,
                     "idle_streak_max_s": round(max(streaks), 1) if streaks else 0.0},
            "losses": {"dead": self.dead, "missing": self.missing, "colony_died": self.colony_died, "peak_colonists": self.peak_colonists},
            "health": {"samples": self.health_samples, "violations": self.health_violations, "worst_share": round(self.health_worst, 3),
                       "breaks_flagged": self.breaks_flagged, "breaks_by_mood": self.breaks_estimated, "break_events": self.breaks_events},
            "extra": {"food_days_min": self.food_days_min, "food_days_last": self.food_days_last,
                      "buildings": dict(sorted(self.last_buildings.items(), key=lambda kv: -kv[1])),
                      "resources": self.last_resources,
                      "game_hours_observed": len(observed), "empty_game_hours": len(empty_hours), "empty_hours_of_day": hours_of_day},
            "approximations": APPROXIMATIONS,
        }
        return rep

    def exit_code(self, aborted: bool = False) -> int:
        """0: все критерии PASS при набранном активном времени; 1: FAIL (или время не набрано); 2: прогон прерван."""
        if aborted and not self.done and not self.stall["failed"]:
            return 2
        crit = self._criteria(True)
        return 0 if self.done and all(c["status"] == "PASS" for c in crit) else 1


# ── Текст отчёта ───────────────────────────────────────────────────────────────────

def format_report(rep: dict) -> str:
    t = rep["time"]
    head = "ПРОГОН ПРЕРВАН: " + rep["abort_reason"] if rep["aborted"] and not rep["reached"] else (
        "ИТОГ: PASS" if rep["passed"] and rep["reached"] else "ИТОГ: FAIL" if rep["reached"] else "ПРОМЕЖУТОЧНО (активное время не набрано)")
    lines = [head, "", f"Активная игра: {t['active_seconds']:.0f} из {t['target_seconds']:.0f} с ({t['active_game_hours']} игровых часов, "
             f"{t['active_ticks']} тиков); пауза {t['paused_seconds']:.0f} с, без хода тиков {t['stalled_seconds']:.0f} с, "
             f"игра не загружена {t['unloaded_seconds']:.0f} с, потеряно на сбоях связи {t['gap_seconds']:.0f} с; "
             f"опросов {t['polls']}, пропусков {t['misses']}, перезагрузок {t['reloads']}."]
    if t["speed_share"]:
        lines.append("Скорость игры (доля активного времени): " + ", ".join(f"x{k}: {v:.0%}" for k, v in t["speed_share"].items()))
    lines += ["", f"{'Критерий':<34}{'Итог':<9}Значение", "-" * 100]
    for c in rep["criteria"]:
        lines.append(f"{c['title']:<34}{c['status']:<9}{c['value']}")
        lines.append(f"{'':<43}порог: {c['threshold']}" + (f"; {c['detail']}" if c["detail"] else ""))
    a, w, ex, lo = rep["attacks"], rep["work"], rep["extra"], rep["losses"]
    lines.append("")
    for att in a["list"]:
        lines.append(f"Атака: {att['status']}, с {att['start_active_s']} с активной игры, врагов до {att['peak']} ({', '.join(att['kinds'])}), "
                     f"реакция {att['reaction_s'] if att['reaction_s'] is not None else 'нет'} с" + (", ИНЪЕКЦИЯ сценария" if att["injected"] else ""))
    for inj in a["injections"]:
        lines.append(f"Инъекция рейда на {inj['active_s']} с: " + ("выполнена" if inj["ok"] else "не выполнена (" + inj["message"] + ")"))
    lines.append(f"Простои: колонист-секунд {w['idle_pawn_seconds']}, серий {w['idle_streaks']}, средняя {w['idle_streak_mean_s']} с, "
                 f"наибольшая {w['idle_streak_max_s']} с. Погибшие: {list(lo['dead'].values()) or 'нет'}, пропавшие: {list(lo['missing'].values()) or 'нет'}.")
    lines.append(f"Еда: минимум {ex['food_days_min']} дн., сейчас {ex['food_days_last']} дн. Игровых часов с выборками {ex['game_hours_observed']}, "
                 f"из них без работы ни у кого {ex['empty_game_hours']} (часы суток: {ex['empty_hours_of_day'] or 'нет'}).")
    lines.append("Постройки: " + (", ".join(f"{k} x{v}" for k, v in list(ex["buildings"].items())[:15]) or "нет"))
    lines += ["", "Приближения замера:"] + [" - " + x for x in rep["approximations"]]
    return "\n".join(lines)


def format_instant(inst: dict) -> str:
    """Короткая строка текущего состояния (режимы --once и --watch)."""
    if not inst.get("loaded"):
        return "игра не загружена (меню или нет колонии)"
    j = inst["jobs"]
    return (f"тик {inst['tick']}{' [пауза]' if inst['paused'] else ''} x{inst['speed']}; колонистов {inst['colonists']} "
            f"(работа {j['work']}, простой {j['idle']}, отдых {j['rest']}, бой {j['combat']}, лежат {j['downed']}); "
            f"доля работающих {inst['work_share']}, в норме {inst['ok_share']}; активных врагов {inst['hostiles_active']}; еда {inst['food_days']} дн.")
