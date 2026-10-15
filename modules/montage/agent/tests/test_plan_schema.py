"""plan_schema (MON-13, MON-28, CRAFT-2): верный план и по неверному примеру на каждое правило; сверка с plan.schema.json шаблона."""
import copy
import json

import pytest

from layout import DEFAULT
from plan_schema import PlanContext, insert_kinds_from_schema, validate_plan, validate_story, story_beat_ids
from tests.stubs import STORY, make_plan

BEATS = story_beat_ids(STORY)


def ctx(tmp_path=None, platform="shorts", **kw):
    return PlanContext.from_layout(DEFAULT, job_dir=tmp_path, platform=platform, beat_ids=BEATS, **kw)


def with_segment(tmp_path):
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "sources" / "a_h264.mp4").write_bytes(b"x")
    plan = make_plan()
    plan["segments"] = [{"id": "s1", "source": "sources/a_h264.mp4", "in_ms": 0, "out_ms": 5000, "at_ms": 0, "volume": 1,
                         "crop": {"mode": "center"}, "zoom": 0.02, "beat": "b2"}]
    return plan


def test_valid_plan_passes(tmp_path):
    assert validate_plan(make_plan(), ctx()) == []
    assert validate_plan(with_segment(tmp_path), ctx(tmp_path)) == []


def test_demo_plans_pass_shape_checks():
    for name in ("vertical", "horizontal", "square"):
        plan = json.loads((DEFAULT.template / "demo" / f"plan.{name}.json").read_text(encoding="utf-8"))
        problems = validate_plan(plan, PlanContext.from_layout(DEFAULT, platform="none"))
        noise = ("sources/", "captions.enabled", "нужен призыв", "cta", "нужен кадр", "ролик не длиннее")      # демо идёт без площадки и исходников
        assert [p for p in problems if not any(n in p for n in noise)] == [], name


def bad(mutate, expect, tmp_path=None, platform="shorts", plan=None):
    p = copy.deepcopy(plan or make_plan())
    mutate(p)
    problems = validate_plan(p, ctx(tmp_path, platform))
    assert any(expect in x for x in problems), (expect, problems)


def test_times_must_be_non_negative_and_inside_the_video():
    bad(lambda p: p["inserts"][0].update(at_ms=-5), "не меньше 0")
    bad(lambda p: p["inserts"][0].update(at_ms=11500, dur_ms=2000), "вставка заканчивается")
    bad(lambda p: p["sfx"][0].update(at_ms=99999), "не больше")
    bad(lambda p: p["voiceover"][0].update(at_ms=99999), "за пределами ролика")
    bad(lambda p: p["blackout"].append({"from_ms": 100, "to_ms": 99999}), "за пределами ролика")


def test_segments_must_not_overlap_and_files_must_exist(tmp_path):
    plan = with_segment(tmp_path)
    plan["segments"].append({**plan["segments"][0], "id": "s2", "at_ms": 3000})
    bad(lambda p: None, "пересекаются", tmp_path, plan=plan)
    plan2 = with_segment(tmp_path)
    plan2["segments"][0]["source"] = "sources/нет.mp4"
    bad(lambda p: None, "в папке задачи нет", tmp_path, plan=plan2)
    plan3 = with_segment(tmp_path)
    plan3["segments"][0]["source"] = "../secret.mp4"
    bad(lambda p: None, "внутри папки задачи", tmp_path, plan=plan3)
    plan4 = with_segment(tmp_path)
    plan4["segments"][0]["source"] = "C:/x/a.mp4"
    bad(lambda p: None, "sources/", tmp_path, plan=plan4)


def test_segment_must_be_inside_the_source_duration(tmp_path):
    plan = with_segment(tmp_path)
    c = ctx(tmp_path, source_ms={"sources/a_h264.mp4": 3000})
    assert any("исходник длится 3000" in x for x in validate_plan(plan, c))


def test_insert_kind_comes_from_the_template_schema():
    kinds = insert_kinds_from_schema(DEFAULT)
    assert {"chamber_card", "custom", "pixel_transition"} <= set(kinds)
    bad(lambda p: p["inserts"][0].update(kind="неизвестная"), "одно из")
    # новый вид в схеме шаблона принимается без правки Python
    c = ctx()
    c.insert_kinds = (*c.insert_kinds, "blueprint")
    plan = make_plan()
    plan["inserts"].append({"id": "i9", "kind": "blueprint", "at_ms": 5000, "dur_ms": 1500, "beat": "b2", "props": {"x": 1}})
    assert validate_plan(plan, c) == []


def test_custom_insert_needs_a_component_name():
    bad(lambda p: p["inserts"].append({"id": "c", "kind": "custom", "at_ms": 5000, "dur_ms": 1000, "beat": "b2", "props": {}}), "component")


def test_sfx_names_come_from_assets_and_are_spaced():
    bad(lambda p: p["sfx"][0].update(name="wilhelm-scream"), "нет такого звука")
    bad(lambda p: p["sfx"].append({"at_ms": 500, "name": "beep"}), "ближе секунды")


def test_on_screen_text_limits():
    bad(lambda p: p["inserts"][1]["props"].update(text="я" * 61), "предел 60")
    bad(lambda p: p["inserts"][0]["props"].update(title="я" * 41), "предел 40")
    bad(lambda p: p["cta"].update(text="я" * 81), "предел 80")
    bad(lambda p: p["voiceover"][0].update(text="я" * 401), "предел 400")


def test_cta_follows_the_platform_rules():
    bad(lambda p: p.update(cta=None), "нужен призыв")
    bad(lambda p: p["cta"].update(platform="youtube"), "площадка задачи")
    short = make_plan(duration=9000)
    short["cta"] = {"platform": "shorts", "at_ms": 7000, "text": "подпишись"}
    bad(lambda p: None, "до 10 секунд", plan=short)
    bad(lambda p: p.update(cta={"platform": "none", "at_ms": 11000, "text": "x"}), "призыва нет", platform="none")
    yt = make_plan(platform="youtube", duration=20000)
    yt["cta"]["at_ms"] = 3000
    bad(lambda p: None, "в последних 5 секундах", platform="youtube", plan=yt)


def test_format_and_duration_follow_the_platform_table():
    bad(lambda p: p["format"].update(width=1920, height=1080), "нужен кадр 1080×1920")
    bad(lambda p: p["format"].update(fps=24), "30 кадров")
    bad(lambda p: p.update(duration_ms=65000), "не длиннее 60 с")
    bad(lambda p: p["format"].update(width=1081), "чётными")


def test_pixel_transitions_limits():
    def add(p, n):
        for k in range(n):
            p["inserts"].append({"id": f"p{k}", "kind": "pixel_transition", "at_ms": 3000 + k * 2000, "dur_ms": 500, "beat": "b2",
                                 "props": {"direction": "in"}})
    bad(lambda p: add(p, 3), "переходов «пиксели» 3")
    bad(lambda p: p["inserts"].append({"id": "p", "kind": "pixel_transition", "at_ms": 3000, "dur_ms": 900, "beat": "b2", "props": {"direction": "in"}}),
        "400–600")


def test_captions_overrides_cannot_change_times_mon28():
    bad(lambda p: p["captions"]["overrides"].append({"word_index": 1, "text": "Aperture", "start_ms": 5}), "времена слов менять нельзя")
    plan = make_plan()
    plan["captions"]["overrides"] = [{"word_index": 3, "text": "Aperture"}]
    assert validate_plan(plan, ctx()) == []


def test_captions_only_in_vertical_short_formats():
    yt = make_plan(platform="youtube", duration=14000)
    yt["captions"]["enabled"] = True
    bad(lambda p: None, "только в вертикальные короткие", platform="youtube", plan=yt)
    bad(lambda p: p["captions"].update(source="transcript"), "без сегментов")


def test_unknown_keys_and_versions_are_rejected():
    bad(lambda p: p.update(version=2), "должно быть 1")
    bad(lambda p: p.update(лишнее=1), "лишнее поле")
    bad(lambda p: p["inserts"][0].pop("beat"), "beat")


def test_every_beat_needs_material_and_beats_must_exist():
    plan = make_plan()
    plan["inserts"][0]["beat"] = "b99"
    assert any("b99" in x for x in validate_plan(plan, ctx()))
    only = make_plan()
    only["inserts"] = only["inserts"][:1]
    only["voiceover"] = []
    only["cta"] = None
    only["duration_ms"] = 9000
    problems = validate_plan(only, ctx())
    assert any("нет материала для битов" in x and "b2" in x for x in problems)


def test_music_only_from_job_files(tmp_path):
    bad(lambda p: p.update(music={"src": "sources/m.mp3", "volume": 0.1}), "музыка только из исходников", tmp_path)


def test_story_validation():
    assert validate_story(STORY) == []
    for mutate, expect in [(lambda s: s.pop("logline"), "logline"), (lambda s: s["beats"][0].update(role="жуть"), "role"),
                           (lambda s: s["beats"][1].update(t_from_ms=100), "без нахлёста"), (lambda s: s["beats"][1].update(id="b1"), "повторяется"),
                           (lambda s: s["setups_payoffs"][0].update(payoff="b9"), "b9"), (lambda s: s.update(beats=[]), "непустой"),
                           (lambda s: s["hook"].pop("why"), "hook.why")]:
        s = copy.deepcopy(STORY)
        mutate(s)
        assert any(expect in p for p in validate_story(s)), expect


def test_python_validation_agrees_with_the_template_json_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((DEFAULT.template / "plan.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    good = [make_plan(), make_plan(voiceover=False), make_plan(platform="youtube", duration=14000, captions=False)]
    for plan in good:
        assert not list(validator.iter_errors(plan))
    cases = [lambda p: p.update(version=2), lambda p: p["inserts"][0].update(kind="x"), lambda p: p["inserts"][0].pop("beat"),
             lambda p: p["segments"].append({"id": "s"}), lambda p: p["sfx"][0].update(name="wilhelm"), lambda p: p.update(extra=1),
             lambda p: p["inserts"][1]["props"].update(result="draw"), lambda p: p["format"].update(fps=120)]
    for mutate in cases:
        p = make_plan()
        mutate(p)
        assert list(validator.iter_errors(p)), "схема шаблона должна отвергать"
        assert validate_plan(p, ctx()), "plan_schema.py должен отвергать то же"
