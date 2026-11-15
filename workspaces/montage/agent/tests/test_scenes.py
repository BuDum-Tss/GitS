"""Конструктор монтажного листа по сценам: раскладка по времени, правила заставки, проход через work scenes и run compose."""
import json

import scenes as sc
from plan_schema import PlanContext, validate_plan
from tests.rig import Rig
from layout import DEFAULT


def ctx(platform="none"):
    return PlanContext.from_layout(DEFAULT, platform=platform)


GOOD = [
    {"kind": "intro", "description": "Заставка Aperture", "assets": ["Aperture_Boot"]},
    {"kind": "glados_room", "description": "ГЛаДОС в комнате приветствует", "say": ["Привет.", "Располагайся."], "mood": "happy"
     if "happy" in ctx().moods else ctx().moods[0]},
    {"kind": "result_sign", "description": "Табличка итога", "props": {"big": "ПРИВЕТ", "small": "канал открыт", "done": 1, "title": "Итог"}},
]


def test_parse_accepts_good_and_rejects_voice_in_intro_and_late_intro():
    scenes, errors = sc.parse_scenes(GOOD, ctx())
    assert errors == [] and [s["kind"] for s in scenes] == ["intro", "glados_room", "result_sign"]
    _, errors = sc.parse_scenes([{"kind": "intro", "description": "x", "say": ["Привет"]}], ctx())
    assert any("заставка" in e for e in errors)
    _, errors = sc.parse_scenes([GOOD[1], {"kind": "intro", "description": "x"}], ctx())
    assert any("только первой" in e for e in errors)
    _, errors = sc.parse_scenes([{"kind": "wow", "description": "x"}], ctx())
    assert any("kind" in e for e in errors)


def test_build_puts_voice_inside_its_scene_and_after_the_intro():
    scenes, _ = sc.parse_scenes(GOOD, ctx())
    plan, layout = sc.build_plan(scenes, {"s2p1": 2000, "s2p2": 1500}, title="Привет", fmt="horizontal", fps=30, platform="none",
                                 cta_text=None, ctx=ctx())
    intro, room, sign = plan["inserts"]
    assert intro["at_ms"] == 0 and intro["dur_ms"] == 7100 and room["at_ms"] == 7100
    v1, v2 = plan["voiceover"]
    assert v1["at_ms"] == 7100 + sc.LEAD_MS and v2["at_ms"] == v1["at_ms"] + 2000 + sc.GAP_MS
    assert room["at_ms"] <= v1["at_ms"] and v2["at_ms"] + 1500 <= room["at_ms"] + room["dur_ms"]
    assert sign["at_ms"] == room["at_ms"] + room["dur_ms"] and plan["duration_ms"] == sign["at_ms"] + sign["dur_ms"]
    assert room["props"]["speech"] == [{"from_ms": v1["at_ms"], "to_ms": v1["at_ms"] + 2000},
                                       {"from_ms": v2["at_ms"], "to_ms": v2["at_ms"] + 1500}]
    assert validate_plan(plan, ctx()) == []


async def test_work_scenes_then_compose_writes_a_valid_plan(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create(voiceover="none", format="horizontal", platform="none")
    res = await rig.tool("work", {"op": "scenes", "id": jid, "scenes": GOOD})
    assert res["ok"] and res["phrases"] == 2 and rig.unit(jid).request["voiceover"] == "character"
    done = await rig.wait_run((await rig.run("compose", jid))["job_id"])
    assert done["state"] == "done"
    plan = json.loads((rig.unit(jid).plan_dir / "plan.json").read_text(encoding="utf-8"))
    assert [i["kind"] for i in plan["inserts"]] == ["intro", "glados_room", "result_sign"]
    assert all(v["at_ms"] >= plan["inserts"][0]["dur_ms"] for v in plan["voiceover"])
    assert all(v.get("duration_ms") for v in plan["voiceover"])


async def test_work_scenes_rejects_bad_input_with_a_readable_message(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create(platform="none")
    try:
        await rig.tool("work", {"op": "scenes", "id": jid, "scenes": [{"kind": "intro", "description": "x", "say": ["нельзя"]}]})
    except Exception as e:                                       # ToolError с причиной
        assert "заставка" in str(e)
    else:
        raise AssertionError("ожидалась ошибка")
