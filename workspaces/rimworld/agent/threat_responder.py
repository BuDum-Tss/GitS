"""Оборона без модели: детерминированный рефлекс на настоящую угрозу (настройка модуля `threat_responder`, по умолчанию включена).

Шаг модели агента занимает секунды, а бой решается за секунды: пока агент рассуждал, упавшие колонисты погибали. Поэтому модуль сам
страхует выживание, а агент занят развитием колонии. `plan_threat_response` чистая: по состоянию игры, времени и памяти решает,
какие приказы отдать сейчас; применяет их controller (команды мода с признаком `reflex: true`: без камеры и без паузы на показ).

Что считается угрозой: враждебное существо (рейдер, механоид, агрессивное или бешеное животное) на ногах, которое не убегает и не
безвредно (воробей и подобные: `HARMLESS_DEFS`), в пределах `max_dist` клеток от любого колониста. Упавший враг не угроза.

Порядок при угрозе: оружие безоружным (дальнобойное тем, у кого стрельба выше ближнего боя), призыв всех способных (один раз на
бойца за эпизод: если агент потом сам отпустил кого-то, не призываем снова), приказ атаки на ближайшего врага (цель
переназначается раз в `ATTACK_REPEAT_S`), слабые (здоровье ниже 0,6) и безоружные без оружия отходят к центру колонии. Упавших, пока
рядом враг, не трогаем; когда рядом безопасно, спасаем на кровать (`rescue`) и лечим лучшим врачом (`tend`). После `CALM_RELEASE_S`
спокойствия отпускаем (`undraft`) и подлечиваем всех, кто кровоточит.

Делёж с агентом: метка `agent_activity.last_combat_order_ts()` (агент сам отдавал боевой приказ меньше `AGENT_QUIET_S` секунд
назад). Тогда рефлекс только добавляет недостающее (оружие, спасение, лечение) и не призывает, не отпускает, не двигает и не
переназначает цели. Отпускать после боя рефлекс начинает, лишь когда агент молчит `AGENT_QUIET_RELEASE_S`.
"""
from dataclasses import dataclass, field

DEFAULT_MAX_DIST = 60            # угроза в пределах стольких клеток от любого колониста запускает реакцию (настройка threat_response_dist)
MIN_HEALTH = 0.4                 # колонист слабее не воюет и не спасает
WEAK_HEALTH = 0.6                # ниже этого: не атакует, отходит к центру
MIN_AGE = 14                     # детей не призываем
ATTACK_REPEAT_S = 4.0            # цель атаки переназначается не чаще
MOVE_REPEAT_S = 6.0              # приказ отхода повторяется не чаще
EQUIP_REPEAT_S = 15.0            # одному бойцу оружие выдаём не чаще (он идёт к предмету)
CLAIM_S = 30.0                   # предмет закреплён за бойцом столько секунд (двоим одно оружие не даём)
RESCUE_REPEAT_S = 25.0           # спасение одного упавшего повторяем не чаще
RESCUE_NO_BED_S = 90.0           # нет свободной кровати: повторяем реже
TEND_REPEAT_S = 15.0             # лечение одного пациента повторяем не чаще
CALM_TEND_REPEAT_S = 30.0
DRAFT_REPEAT_S = 6.0
UNDRAFT_REPEAT_S = 10.0
AGENT_QUIET_S = 8.0              # агент отдавал боевые приказы меньше стольких секунд назад: рефлекс только добавляет недостающее
AGENT_QUIET_RELEASE_S = 20.0     # отпускать после боя при молчании агента не меньше стольких секунд
CALM_RELEASE_S = 20.0            # спокойствие не меньше этого: отпустить и подлечить
ENEMY_NEAR_DOWNED = 25           # враг ближе этого к упавшему: не спасаем и не лечим
ENGAGE_DIST = 45                 # цель атаки не дальше этого от бойца
WEAPON_MAX_DIST = 40             # оружие дальше этого от бойца не берём (в бою он дойти не успеет)
BLEED_MIN = 0.01
HURT_TEND = 0.85                 # лечим упавшего, если здоровье ниже этого или кровоточит

HARMLESS_DEFS = frozenset({"Sparrow", "Pigeon", "Finch", "Hare", "Squirrel", "Rat", "Chinchilla", "Cat", "Duck", "Goose", "Turkey"})
_QUALITY = {"Awful": 0, "Poor": 1, "Normal": 2, "Good": 3, "Excellent": 4, "Masterwork": 5, "Legendary": 6}


# ── небольшие помощники ───────────────────────────────────────────────────────

def _xz(obj) -> tuple[int, int] | None:
    pos = (obj or {}).get("position") or {}
    return (pos["x"], pos["z"]) if "x" in pos and "z" in pos else None


def _dist(a, b) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _colonists(state: dict) -> list[dict]:
    return [p for p in (state.get("colony") or {}).get("pawns") or [] if p.get("id") and not p.get("is_prisoner")]


def _mental(p: dict) -> bool:
    return bool(p.get("mental_state") or p.get("in_mental_break"))


def _hp(p: dict) -> float:
    v = p.get("health_pct")
    return float(v) if isinstance(v, (int, float)) else 1.0


def fit_to_fight(p: dict) -> bool:
    """Способен воевать: не лежит, не пленный, здоровье не ниже 0,4, не в срыве, не ребёнок и не пацифист (охота закрыта)."""
    age = p.get("age")
    return (not p.get("is_prisoner") and not p.get("is_downed") and _hp(p) >= MIN_HEALTH and not _mental(p)
            and not (isinstance(age, int) and age < MIN_AGE) and "Hunting" not in (p.get("incapable_of") or []))


def is_weak(p: dict) -> bool:
    return _hp(p) < WEAK_HEALTH


def active_threats(state: dict, max_dist: float = DEFAULT_MAX_DIST, harmless=HARMLESS_DEFS) -> list[dict]:
    """Угрозы, на которые отвечаем: враг на ногах, не убегает, не безвреден, в пределах max_dist клеток от любого колониста."""
    spots = [c for c in (_xz(p) for p in _colonists(state)) if c]
    out = []
    for t in state.get("threats") or []:
        if not t.get("id") or t.get("is_downed") or t.get("is_fleeing") or t.get("def") in harmless:
            continue
        spot = _xz(t)
        if spot is not None and spots and min(_dist(spot, s) for s in spots) > max_dist:
            continue
        out.append(t)
    return out


def _is_grenade(item: dict) -> bool:
    return "grenade" in str(item.get("def") or "").lower()


# ── приказ и память ───────────────────────────────────────────────────────────

@dataclass
class Action:
    """Один приказ модулю игры. `op`: draft, undraft (в `pawns` список), attack, move, equip, rescue, tend."""
    op: str
    pawn: str = ""
    pawns: list = field(default_factory=list)
    target: str = ""
    item: str = ""
    x: int | None = None
    z: int | None = None
    why: str = ""
    names: dict = field(default_factory=dict)        # id -> имя (для протокола)

    def command(self) -> dict:
        """Команда мода. Признак `reflex` отключает камеру и показ, а у окон-зависимых команд проверку открытого окна."""
        if self.op in ("draft", "undraft"):
            return {"type": self.op, "pawn_ids": list(self.pawns), "reflex": True}
        if self.op == "attack":
            return {"type": "attack", "pawn_id": self.pawn, "target_id": self.target, "reflex": True}
        if self.op == "move":
            return {"type": "move", "pawn_id": self.pawn, "x": self.x, "z": self.z, "reflex": True}
        if self.op == "equip":
            return {"type": "equip", "pawn_id": self.pawn, "item_id": self.item, "reflex": True}
        return {"type": self.op, "pawn_id": self.pawn, "target_id": self.target, "reflex": True}      # rescue, tend


@dataclass
class Memory:
    """Что рефлекс уже приказал (для кулдаунов) и в каком он эпизоде."""
    episode: bool = False                                    # угроза была и бой не закончен: после спокойствия отпустить
    calm_since: float | None = None
    drafted_once: set = field(default_factory=set)           # кого призывали в этом эпизоде
    drafted_at: dict = field(default_factory=dict)
    attack: dict = field(default_factory=dict)               # id бойца -> (id цели, время)
    move: dict = field(default_factory=dict)
    equip: dict = field(default_factory=dict)
    claimed: dict = field(default_factory=dict)              # id предмета -> время закрепления
    rescue: dict = field(default_factory=dict)               # id упавшего -> время, когда можно снова
    tend: dict = field(default_factory=dict)
    undraft_at: float = 0.0
    weapons_at: float = 0.0                                  # когда controller в последний раз опрашивал предметы

    def reset(self) -> None:
        self.__init__()


@dataclass
class Plan:
    actions: list = field(default_factory=list)
    threat: bool = False                 # есть угроза в пределах дистанции (в этот момент)
    enemies: int = 0
    calm_s: float = 0.0
    agent_recent: bool = False           # агент недавно сам отдавал боевые приказы
    facts: dict = field(default_factory=dict)    # drafted, armed, retreat, rescued, tended, released: имена для протокола и события

    @property
    def notable(self) -> bool:
        """Есть что сообщить ядру: призыв, оружие, спасение, лечение, отпуск (а не каждое переназначение цели)."""
        return any(self.facts.get(k) for k in ("drafted", "armed", "rescued", "tended", "released"))


def _unarmed(p: dict) -> bool:
    return not p.get("equipped")


def wants_weapons(state: dict, mem: Memory, now: float, max_dist: float = DEFAULT_MAX_DIST) -> bool:
    """Нужен ли список оружия на карте: есть угроза и способный безоружный боец, которому оружие ещё не выдано."""
    if not active_threats(state, max_dist):
        return False
    return any(fit_to_fight(p) and _unarmed(p) and now - mem.equip.get(p["id"], -1e9) >= EQUIP_REPEAT_S for p in _colonists(state))


def _pick_weapon(pawn: dict, weapons: list[dict], mem: Memory, now: float, taken: set) -> dict | None:
    spot = _xz(pawn)
    skills = pawn.get("skills") or {}
    want_ranged = skills.get("Shooting", 0) >= skills.get("Melee", 0)
    best, best_score = None, None
    for w in weapons:
        wid = w.get("id")
        if not wid or wid in taken or now - mem.claimed.get(wid, -1e9) < CLAIM_S or _is_grenade(w):
            continue
        if (w.get("hp_pct") if isinstance(w.get("hp_pct"), (int, float)) else 1.0) < 0.3:
            continue
        d = _dist(spot, (w.get("x", 0), w.get("z", 0))) if spot else 0
        if d > WEAPON_MAX_DIST:
            continue
        score = (100 if bool(w.get("ranged")) == want_ranged else 0) + 5 * _QUALITY.get(str(w.get("quality")), 2) - 0.5 * d
        if best_score is None or score > best_score:
            best, best_score = w, score
    return best


def _rally_point(state: dict, fighters: list[dict]) -> tuple[int, int] | None:
    """Центр колонии: середина построек, иначе середина колонистов."""
    pts = [c for c in (_xz(b) for b in (state.get("colony") or {}).get("buildings") or []) if c]
    if not pts:
        pts = [c for c in (_xz(p) for p in fighters or _colonists(state)) if c]
    if not pts:
        return None
    return round(sum(p[0] for p in pts) / len(pts)), round(sum(p[1] for p in pts) / len(pts))


def _best_medic(candidates: list[dict], exclude: set) -> dict | None:
    pool = [p for p in candidates if p["id"] not in exclude and "Doctor" not in (p.get("incapable_of") or [])]
    if not pool:
        return None
    return max(pool, key=lambda p: ((p.get("skills") or {}).get("Medicine", 0), str(p["id"])))


def _nearest(spot, pool: list[dict]) -> dict | None:
    if not pool:
        return None
    if spot is None:
        return pool[0]
    return min(pool, key=lambda p: (_dist(spot, _xz(p) or spot), str(p["id"])))


def plan_threat_response(state: dict, now: float, mem: Memory, *, weapons: list[dict] | None = None,
                         agent_combat_at: float | None = None, max_dist: float = DEFAULT_MAX_DIST,
                         enabled: bool = True) -> Plan:
    """Приказы на этот тик. Память `mem` обновляется как записанный приказ (кулдауны); результаты команд сообщает `note_result`.
    `weapons`: свободное оружие на карте (ответ get_items) или None, если не запрашивали. Выключено: пустой план и забытый эпизод."""
    plan = Plan()
    if not enabled:
        mem.reset()
        return plan
    colonists = _colonists(state)
    if not colonists:
        return plan
    names = {p["id"]: p.get("name") or p["id"] for p in colonists}
    threats = active_threats(state, max_dist)
    plan.threat, plan.enemies = bool(threats), len(threats)
    agent_age = None if agent_combat_at is None else now - agent_combat_at
    plan.agent_recent = agent_age is not None and agent_age < AGENT_QUIET_S
    fighters = [p for p in colonists if fit_to_fight(p)]
    acts = plan.actions
    facts = plan.facts

    if threats:
        mem.episode = True
        mem.calm_since = None
        # (а) оружие: можно и при недавних приказах агента (добавляем недостающее)
        taken: set = set()
        for p in fighters:
            if not (_unarmed(p) and weapons and now - mem.equip.get(p["id"], -1e9) >= EQUIP_REPEAT_S):
                continue
            w = _pick_weapon(p, weapons, mem, now, taken)
            if w is None:
                continue
            taken.add(w["id"])
            mem.equip[p["id"]] = now
            mem.claimed[w["id"]] = now
            acts.append(Action("equip", pawn=p["id"], item=w["id"], why=f"{names[p['id']]}: безоружен, берёт {w.get('label') or w.get('def')}"))
            facts.setdefault("armed", []).append(f"{names[p['id']]} ({w.get('label') or w.get('def')})")
        # (б) призыв: каждого способного один раз за эпизод; пока агент сам командует, не вмешиваемся
        if not plan.agent_recent:
            fresh = [p for p in fighters if not p.get("is_drafted") and p["id"] not in mem.drafted_once
                     and now - mem.drafted_at.get(p["id"], -1e9) >= DRAFT_REPEAT_S]
            if fresh:
                ids = [p["id"] for p in fresh]
                for pid in ids:
                    mem.drafted_once.add(pid)
                    mem.drafted_at[pid] = now
                acts.append(Action("draft", pawns=ids, names={i: names[i] for i in ids}, why="угроза рядом"))
                facts["drafted"] = [names[i] for i in ids]
            drafted_now = {p["id"] for p in fighters if p.get("is_drafted")} | {a for act in acts if act.op == "draft" for a in act.pawns}
            rally = _rally_point(state, fighters)
            # (в) боевые приказы
            for p in fighters:
                pid = p["id"]
                if pid not in drafted_now:
                    continue
                spot = _xz(p)
                equipping = now - mem.equip.get(pid, -1e9) < 10.0 and _unarmed(p)
                if equipping:
                    continue                                    # идёт за оружием: не перебиваем
                if is_weak(p) or _unarmed(p):
                    if rally and spot and _dist(spot, rally) > 8 and now - mem.move.get(pid, -1e9) >= MOVE_REPEAT_S:
                        mem.move[pid] = now
                        acts.append(Action("move", pawn=pid, x=rally[0], z=rally[1], why=f"{names[pid]}: слаб или безоружен, отходит"))
                        facts.setdefault("retreat", []).append(names[pid])
                    continue
                near = [t for t in threats if (_xz(t) is None or spot is None or _dist(spot, _xz(t)) <= ENGAGE_DIST)]
                tgt = _nearest(spot, near)
                if tgt is None:
                    continue
                last = mem.attack.get(pid)
                if last and last[0] == tgt["id"] and now - last[1] < ATTACK_REPEAT_S:
                    continue
                mem.attack[pid] = (tgt["id"], now)
                acts.append(Action("attack", pawn=pid, target=tgt["id"], why=f"{names[pid]} -> {tgt.get('name') or tgt['id']}"))
    else:
        if mem.episode and mem.calm_since is None:
            mem.calm_since = now
        plan.calm_s = 0.0 if mem.calm_since is None else now - mem.calm_since

    # (г) упавшие и раненые: рядом враг, не трогаем; безопасно, спасаем и лечим (и при недавних приказах агента)
    _care(state, plan, mem, now, colonists, fighters, threats, names)

    # (д) после спокойствия отпустить
    if (not threats and mem.episode and plan.calm_s >= CALM_RELEASE_S and now - mem.undraft_at >= UNDRAFT_REPEAT_S
            and (agent_age is None or agent_age >= AGENT_QUIET_RELEASE_S)):
        ids = [p["id"] for p in colonists if p.get("is_drafted")]
        mem.undraft_at = now
        if ids:
            acts.append(Action("undraft", pawns=ids, names={i: names[i] for i in ids}, why=f"спокойно {round(plan.calm_s)} с"))
            facts["released"] = [names[i] for i in ids]
        mem.episode = False
        mem.drafted_once.clear()
        mem.attack.clear()
        mem.move.clear()
    return plan


def _care(state, plan: Plan, mem: Memory, now: float, colonists, fighters, threats, names) -> None:
    """Спасение упавших и лечение. Занятых в этом плане бойцов не берём в спасатели, если они уже получили приказ атаки."""
    acts, facts = plan.actions, plan.facts
    busy = {a.pawn for a in acts if a.op in ("attack", "equip")}
    helpers = [p for p in fighters if not is_weak(p) and p["id"] not in busy]
    used: set = set()
    enemy_spots = [s for s in (_xz(t) for t in threats) if s]
    patients = []
    for p in colonists:
        if p.get("is_downed"):
            patients.append(p)
        elif (p.get("bleeding") or 0) > BLEED_MIN and not threats:
            patients.append(p)                                  # раненых на ногах лечим, когда в радиусе угроз тихо
    for patient in sorted(patients, key=lambda p: (not p.get("is_downed"), -(p.get("bleeding") or 0), str(p["id"]))):
        pid = patient["id"]
        spot = _xz(patient)
        if patient.get("is_downed") and spot and any(_dist(spot, e) <= ENEMY_NEAR_DOWNED for e in enemy_spots):
            continue                                            # рядом враг: не трогаем, спасатель тоже погибнет
        hurt = (patient.get("bleeding") or 0) > BLEED_MIN or _hp(patient) < HURT_TEND
        if hurt and now >= mem.tend.get(pid, 0):
            pool = helpers if patient.get("is_downed") else fighters
            medic = _best_medic([h for h in pool if h["id"] != pid], used)
            if medic is None and not patient.get("is_downed"):
                medic = _best_medic([h for h in pool if h["id"] == pid], used)      # один колонист: перевязывает себя, если умеет
            if medic is not None:
                used.add(medic["id"])
                mem.tend[pid] = now + (TEND_REPEAT_S if patient.get("is_downed") else CALM_TEND_REPEAT_S)
                acts.append(Action("tend", pawn=medic["id"], target=pid, why=f"{names[medic['id']]} лечит {names[pid]}"))
                facts.setdefault("tended", []).append(names[pid])
        if patient.get("is_downed") and now >= mem.rescue.get(pid, 0):
            cand = _nearest(spot, [h for h in helpers if h["id"] not in used and h["id"] != pid])
            if cand is not None:
                used.add(cand["id"])
                mem.rescue[pid] = now + RESCUE_REPEAT_S
                acts.append(Action("rescue", pawn=cand["id"], target=pid, why=f"{names[cand['id']]} несёт {names[pid]} на кровать"))
                facts.setdefault("rescued", []).append(names[pid])


def note_result(mem: Memory, action: Action, ok: bool, message: str, now: float) -> None:
    """Итог команды: при отказе освобождаем оружие и откладываем повтор (нет кровати: надолго)."""
    if ok:
        return
    if action.op == "equip":
        mem.claimed.pop(action.item, None)
        mem.equip[action.pawn] = now + EQUIP_REPEAT_S            # этого бойца не мучаем повторами сразу
    elif action.op == "rescue":
        mem.rescue[action.target] = now + (RESCUE_NO_BED_S if "кроват" in message else RESCUE_REPEAT_S)
    elif action.op == "tend":
        mem.tend[action.target] = now + CALM_TEND_REPEAT_S
    elif action.op == "attack":
        mem.attack.pop(action.pawn, None)


def describe(plan: Plan) -> str:
    """Факт для протокола и события ядру: что рефлекс сделал (не реплика)."""
    f, parts = plan.facts, []
    if f.get("drafted"):
        parts.append("Колонисты мобилизованы: " + ", ".join(f["drafted"]))
    if f.get("armed"):
        parts.append("оружие выдано: " + ", ".join(f["armed"]))
    if f.get("retreat"):
        parts.append("отходят к центру (слабы или безоружны): " + ", ".join(f["retreat"]))
    if f.get("rescued"):
        parts.append("спасают упавших: " + ", ".join(f["rescued"]))
    if f.get("tended"):
        parts.append("лечат: " + ", ".join(f["tended"]))
    if f.get("released"):
        parts.append("бой окончен, отпущены к работе: " + ", ".join(f["released"]))
    return "; ".join(parts) + ("." if parts else "")
