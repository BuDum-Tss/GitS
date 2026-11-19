"""Рефлекс обороны: чистая plan_threat_response на подделанных состояниях (без игры, сети и модели)."""
import pytest

import threat_responder as tr
from threat_responder import Memory, plan_threat_response


def pawn(pid, x=50, z=50, **kw):
    base = {"id": pid, "name": f"К{pid}", "health_pct": 1.0, "is_drafted": False, "is_prisoner": False, "is_downed": False,
            "equipped": "Gun_Rifle", "skills": {"Shooting": 5, "Melee": 2, "Medicine": 1}, "bleeding": 0, "age": 30,
            "position": {"x": x, "z": z}, "incapable_of": []}
    base.update(kw)
    return base


def enemy(tid, x=80, z=50, **kw):
    base = {"id": tid, "name": f"В{tid}", "def": "Human", "kind": "human", "health_pct": 1.0, "is_downed": False,
            "is_fleeing": False, "job": "AttackMelee", "position": {"x": x, "z": z}}
    base.update(kw)
    return base


def st(pawns, threats=(), buildings=()):
    return {"tick": 1, "colony": {"pawns": list(pawns), "buildings": list(buildings)}, "threats": list(threats)}


def ops(plan, op=None):
    return [a for a in plan.actions if op is None or a.op == op]


def gun(wid, x=52, z=50, ranged=True, quality="Normal", label="винтовка"):
    return {"id": wid, "def": "Gun_Rifle" if ranged else "MeleeWeapon_Knife", "label": label, "x": x, "z": z, "ranged": ranged,
            "quality": quality, "hp_pct": 1.0, "forbidden": False}


# ── что считается угрозой ─────────────────────────────────────────────────────

def test_no_threats_no_actions():
    plan = plan_threat_response(st([pawn("a")]), 100, Memory())
    assert plan.actions == [] and not plan.threat


def test_far_threat_is_ignored_near_threat_triggers():
    far = plan_threat_response(st([pawn("a")], [enemy("e", x=200)]), 100, Memory())
    assert not far.threat and far.actions == []
    near = plan_threat_response(st([pawn("a")], [enemy("e", x=100)]), 100, Memory())
    assert near.threat and ops(near, "draft")


def test_distance_parameter_is_respected():
    state = st([pawn("a")], [enemy("e", x=100)])           # 50 клеток
    assert plan_threat_response(state, 100, Memory(), max_dist=30).actions == []
    assert plan_threat_response(state, 100, Memory(), max_dist=60).threat


@pytest.mark.parametrize("bad", [{"def": "Sparrow", "kind": "animal"}, {"is_downed": True}, {"is_fleeing": True}])
def test_harmless_downed_and_fleeing_are_not_threats(bad):
    plan = plan_threat_response(st([pawn("a")], [enemy("e", x=60, **bad)]), 100, Memory())
    assert not plan.threat and plan.actions == []


def test_hostile_animal_is_a_threat():
    plan = plan_threat_response(st([pawn("a")], [enemy("e", x=60, **{"def": "Wolf", "kind": "animal"})]), 100, Memory())
    assert plan.threat and ops(plan, "attack")


# ── вооружение ────────────────────────────────────────────────────────────────

def test_unarmed_gets_best_free_weapon_ranged_for_shooter():
    state = st([pawn("a", equipped=None)], [enemy("e", x=70)])
    mem = Memory()
    plan = plan_threat_response(state, 100, mem, weapons=[gun("k", ranged=False), gun("r", ranged=True)])
    (eq,) = ops(plan, "equip")
    assert eq.item == "r" and eq.pawn == "a" and eq.command()["reflex"] is True
    assert plan.facts["armed"] == ["Кa (винтовка)"]
    again = plan_threat_response(state, 101, mem, weapons=[gun("r")])          # кулдаун: не мучаем повторами
    assert not ops(again, "equip")


def test_melee_skilled_pawn_prefers_melee_weapon():
    p = pawn("a", equipped=None, skills={"Shooting": 1, "Melee": 9})
    plan = plan_threat_response(st([p], [enemy("e", x=70)]), 100, Memory(), weapons=[gun("r"), gun("k", ranged=False)])
    assert ops(plan, "equip")[0].item == "k"


def test_one_weapon_is_not_given_to_two_pawns_and_far_or_grenade_skipped():
    state = st([pawn("a", equipped=None), pawn("b", x=51, equipped=None)], [enemy("e", x=70)])
    far = gun("far", x=200)
    grenade = {**gun("g"), "def": "Weapon_GrenadeFrag"}
    plan = plan_threat_response(state, 100, Memory(), weapons=[gun("only"), far, grenade])
    assert [a.item for a in ops(plan, "equip")] == ["only"]


def test_downed_and_prisoner_are_not_armed_or_drafted():
    pawns = [pawn("down", equipped=None, is_downed=True), pawn("pris", equipped=None, is_prisoner=True), pawn("ok")]
    plan = plan_threat_response(st(pawns, [enemy("e", x=70)]), 100, Memory(), weapons=[gun("r"), gun("r2")])
    assert not ops(plan, "equip")
    assert ops(plan, "draft")[0].pawns == ["ok"]


def test_wants_weapons_only_with_threat_and_unarmed_fit_pawn():
    mem = Memory()
    assert not tr.wants_weapons(st([pawn("a", equipped=None)]), mem, 100)
    assert tr.wants_weapons(st([pawn("a", equipped=None)], [enemy("e", x=70)]), mem, 100)
    assert not tr.wants_weapons(st([pawn("a")], [enemy("e", x=70)]), mem, 100)
    assert not tr.wants_weapons(st([pawn("a", equipped=None, is_downed=True)], [enemy("e", x=70)]), mem, 100)


# ── призыв ────────────────────────────────────────────────────────────────────

def test_draft_only_fit_colonists():
    pawns = [pawn("a"), pawn("weak", health_pct=0.3), pawn("child", age=9), pawn("mad", mental_state="Berserk"),
             pawn("pacifist", incapable_of=["Hunting"]), pawn("b", is_drafted=True)]
    plan = plan_threat_response(st(pawns, [enemy("e", x=70)]), 100, Memory())
    assert ops(plan, "draft")[0].pawns == ["a"]
    assert plan.facts["drafted"] == ["Кa"]


def test_draft_once_per_pawn_per_episode_even_if_undrafted_by_someone():
    mem = Memory()
    state = st([pawn("a")], [enemy("e", x=70)])
    plan_threat_response(state, 100, mem)
    again = plan_threat_response(state, 120, mem)                              # агент мог отпустить: не призываем снова
    assert not ops(again, "draft")


# ── боевые приказы ────────────────────────────────────────────────────────────

def test_attack_nearest_enemy_and_retarget_by_cooldown():
    pawns = [pawn("a", is_drafted=True)]
    mem = Memory()
    state = st(pawns, [enemy("far", x=90), enemy("near", x=70)])
    plan = plan_threat_response(state, 100, mem)
    (atk,) = ops(plan, "attack")
    assert atk.target == "near" and atk.command() == {"type": "attack", "pawn_id": "a", "target_id": "near", "reflex": True}
    assert not ops(plan_threat_response(state, 102, mem), "attack")            # та же цель, меньше 4 с
    assert ops(plan_threat_response(state, 104.5, mem), "attack")              # пора переназначить
    state2 = st(pawns, [enemy("n2", x=65)])
    assert ops(plan_threat_response(state2, 105, mem), "attack")[0].target == "n2"   # новая цель сразу


def test_weak_and_unarmed_without_weapon_retreat_to_center_not_attack():
    pawns = [pawn("w", x=70, is_drafted=True, health_pct=0.5), pawn("u", x=72, is_drafted=True, equipped=None),
             pawn("s", x=60, is_drafted=True)]
    state = st(pawns, [enemy("e", x=95)], buildings=[{"id": "b", "position": {"x": 20, "z": 20}}])
    plan = plan_threat_response(state, 100, Memory())
    moves = {a.pawn: a for a in ops(plan, "move")}
    assert set(moves) == {"w", "u"} and moves["w"].x == 20 and moves["w"].z == 20
    assert [a.pawn for a in ops(plan, "attack")] == ["s"]


def test_draft_and_attack_in_one_pass_for_fresh_pawn():
    plan = plan_threat_response(st([pawn("a")], [enemy("e", x=70)]), 100, Memory())
    assert [a.op for a in plan.actions] == ["draft", "attack"]


# ── упавшие и лечение ─────────────────────────────────────────────────────────

def downed_state(enemy_x):
    pawns = [pawn("d", x=50, is_downed=True, health_pct=0.3, bleeding=0.2), pawn("a", x=52, is_drafted=True),
             pawn("m", x=53, is_drafted=True, skills={"Medicine": 12, "Shooting": 1, "Melee": 1})]
    return st(pawns, [enemy("e", x=enemy_x)] if enemy_x else [])


def test_downed_is_left_alone_while_enemy_is_close():
    plan = plan_threat_response(downed_state(60), 100, Memory())
    assert not ops(plan, "rescue") and not ops(plan, "tend")


def test_rescue_and_tend_when_enemy_is_far_from_downed_or_gone():
    plan = plan_threat_response(downed_state(None), 100, Memory())
    rescue, tend = ops(plan, "rescue")[0], ops(plan, "tend")[0]
    assert rescue.target == "d" and tend.target == "d" and tend.pawn == "m" and rescue.pawn != tend.pawn
    far = plan_threat_response(downed_state(100), 100, Memory())              # враг дальше 25 клеток от упавшего
    assert ops(far, "rescue") and ops(far, "tend")


def test_rescue_cooldown_and_no_bed_backoff():
    mem = Memory()
    state = downed_state(None)
    plan = plan_threat_response(state, 100, mem)
    tr.note_result(mem, ops(plan, "rescue")[0], False, "нет свободной кровати для раненого", 100)
    assert not ops(plan_threat_response(state, 130, mem), "rescue")           # без кровати ждём 90 с
    assert ops(plan_threat_response(state, 191, mem), "rescue")


def test_weak_colonists_do_not_rescue():
    pawns = [pawn("d", is_downed=True, health_pct=0.3), pawn("w", health_pct=0.5)]
    assert not ops(plan_threat_response(st(pawns), 100, Memory()), "rescue")


# ── отпуск после боя ──────────────────────────────────────────────────────────

def test_undraft_after_20s_of_calm_and_tend_bleeders():
    mem = Memory()
    pawns = [pawn("a", is_drafted=True, bleeding=0.1, skills={"Medicine": 2}), pawn("m", is_drafted=True, skills={"Medicine": 9})]
    plan_threat_response(st(pawns, [enemy("e", x=70)]), 100, mem)             # бой начался
    calm = st(pawns)
    first = plan_threat_response(calm, 110, mem)                               # тихо 0 с
    assert not ops(first, "undraft") and ops(first, "tend")[0].target == "a" and ops(first, "tend")[0].pawn == "m"
    assert not ops(plan_threat_response(calm, 125, mem), "undraft")           # 15 с
    release = plan_threat_response(calm, 131, mem)                             # 21 с
    assert ops(release, "undraft")[0].pawns == ["a", "m"] and release.facts["released"] == ["Кa", "Кm"]
    assert not ops(plan_threat_response(calm, 150, mem), "undraft")           # эпизод закончен


def test_no_undraft_without_episode_and_new_threat_resets_calm():
    pawns = [pawn("a", is_drafted=True)]
    assert not ops(plan_threat_response(st(pawns), 500, Memory()), "undraft")  # агент призвал сам, боя не было
    mem = Memory()
    plan_threat_response(st(pawns, [enemy("e", x=70)]), 100, mem)
    plan_threat_response(st(pawns), 105, mem)
    plan_threat_response(st(pawns, [enemy("e", x=70)]), 118, mem)             # враг вернулся: отсчёт заново
    assert not ops(plan_threat_response(st(pawns), 130, mem), "undraft")


# ── делёж управления с агентом ────────────────────────────────────────────────

def test_agent_recent_combat_orders_block_orders_but_not_arming_or_rescue():
    pawns = [pawn("a", equipped=None), pawn("b"), pawn("c"), pawn("d", is_downed=True, health_pct=0.3, bleeding=0.2)]
    state = st(pawns, [enemy("e", x=110)])
    plan = plan_threat_response(state, 100, Memory(), weapons=[gun("r")], agent_combat_at=95)
    assert plan.agent_recent
    assert not (ops(plan, "draft") or ops(plan, "attack") or ops(plan, "move") or ops(plan, "undraft"))
    assert ops(plan, "equip") and ops(plan, "rescue")
    quiet = plan_threat_response(state, 100, Memory(), weapons=[gun("r")], agent_combat_at=90)
    assert not quiet.agent_recent and ops(quiet, "draft")


def test_release_waits_for_agent_silence():
    mem = Memory()
    pawns = [pawn("a", is_drafted=True)]
    plan_threat_response(st(pawns, [enemy("e", x=70)]), 100, mem)
    plan_threat_response(st(pawns), 105, mem)
    assert not ops(plan_threat_response(st(pawns), 140, mem, agent_combat_at=130), "undraft")
    assert ops(plan_threat_response(st(pawns), 151, mem, agent_combat_at=130), "undraft")


# ── выключатель ───────────────────────────────────────────────────────────────

def test_disabled_does_nothing_and_forgets_episode():
    mem = Memory()
    plan_threat_response(st([pawn("a")], [enemy("e", x=70)]), 100, mem)
    assert mem.episode
    off = plan_threat_response(st([pawn("a")], [enemy("e", x=70)]), 101, mem, enabled=False)
    assert off.actions == [] and not mem.episode and not mem.drafted_once


def test_describe_is_a_fact_text():
    plan = plan_threat_response(st([pawn("a", equipped=None)], [enemy("e", x=70)]), 100, Memory(), weapons=[gun("r")])
    text = tr.describe(plan)
    assert text.startswith("Колонисты мобилизованы: Кa") and "оружие выдано: Кa (винтовка)" in text and plan.notable
