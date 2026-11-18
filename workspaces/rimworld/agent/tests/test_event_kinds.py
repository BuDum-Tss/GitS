"""Виды событий RimWorld для внимания v2 (`event_kinds`): приоритет по срочности, потолок, срок, critical, passive, ключ слияния серий."""
from gits_sdk import protocol as P
from rigs import Rig

import core_link
import module_manifest

PRIORITY_BY_URGENCY = {5: 90, 4: 70, 3: 50, 2: 30, 1: 10}


def test_every_emitted_kind_is_declared_with_priority_from_its_urgency_and_ttl_from_expires():
    kinds = {k["kind"]: k for k in module_manifest.event_kinds()}
    assert set(kinds) == {e["kind"] for e in module_manifest.EMITS}
    for kind, urgency in core_link.EVENTS.values():
        spec = kinds[kind]
        assert spec["ttl_s"] == core_link.EXPIRES_S.get(kind, 120)
        assert spec["max_priority"] <= 90                                       # тревога не выглядит обращением человека (USER = 100)
        if not spec.get("passive"):
            assert spec["default_priority"] == PRIORITY_BY_URGENCY[urgency] and spec["max_priority"] == 90, kind


def test_alerts_are_critical_and_information_kinds_are_passive_and_low():
    kinds = {k["kind"]: k for k in module_manifest.event_kinds()}
    for kind in ("rimworld.raid", "rimworld.death", "rimworld.fire", "rimworld.pawn_downed", "rimworld.pawn_lost", "rimworld.run_ended"):
        assert kinds[kind]["critical"] is True, kind
    assert kinds["rimworld.raid"]["default_priority"] == 90 and kinds["rimworld.raid"]["ttl_s"] == 180
    for kind in ("rimworld.colonists", "rimworld.game_started", "rimworld.run_video", "rimworld.enemy_down", "rimworld.research_done"):
        assert kinds[kind]["passive"] is True and kinds[kind]["max_priority"] < 30 and "critical" not in kinds[kind], kind
    assert "critical" not in kinds["rimworld.letter"] and kinds["rimworld.letter"]["default_priority"] == 30


async def test_manifest_with_event_kinds_is_valid_and_keeps_emits():
    manifest = await module_manifest.build()
    assert P.validate_manifest(manifest) == []
    assert manifest["event_kinds"] == module_manifest.event_kinds()
    assert {e["kind"] for e in manifest["emits"]} == {k["kind"] for k in manifest["event_kinds"]}
    assert all("default_urgency" in e for e in manifest["emits"])


async def test_serial_events_carry_merge_key_and_alarms_do_not(tmp_path):
    async with Rig(tmp_path) as rig:
        await rig.link.event("pawn_hurt", {"name": "Борис"}, text="Борис ранен.")
        await rig.link.event("raid", {"label": "налёт"}, text="Налёт.")
        await rig.link.event("pawn_hurt", {"name": "Вика", "group_key": "свой"}, text="Вика ранена.")
        await rig.core.wait_for(lambda: len(rig.core.events) == 3)
        hurt, raid, own = rig.core.events
        assert hurt["data"]["group_key"] == "rimworld:pawn_hurt" and hurt["data"]["name"] == "Борис"
        assert "group_key" not in raid["data"]
        assert own["data"]["group_key"] == "свой"
