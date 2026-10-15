"""ТЗ 11e (POL-2..POL-4, POL-11, POL-13): реестр политик, каталог событий, проверка схемы."""
import shutil
from pathlib import Path

import pytest

import policies

SRC = Path(policies.DIR)


def copy_policies(tmp_path) -> Path:
    dst = tmp_path / "policies"
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def test_real_registry_has_five_policies_and_afk_is_default():
    reg = policies.get()
    assert set(reg.policies) == {"afk", "observe", "plan", "combat", "manual"}
    assert reg.default == "afk"                                  # POL-11: вне фокуса по умолчанию
    assert reg.policy("afk").focus == "background"               # POL-13: минимум одна фоновая политика


def test_entering_picks_the_most_important_policy_and_respects_thresholds():
    reg = policies.get()
    assert reg.entering("raid", 5).id == "combat"
    assert reg.entering("raid", 3) is None                      # ниже порога enter.min_importance
    assert reg.entering("task_stalled", 3).id == "manual"
    assert reg.entering("plan_request", 2).id == "plan"
    assert reg.entering("research_done", 2) is None             # в правила и журнал, не на политику


def test_request_transitions_follow_switch_to_only():
    reg = policies.get()
    assert reg.can_request("observe", "plan") and not reg.can_request("observe", "combat")
    assert not reg.can_request("combat", "plan")                # из боя по просьбе не выходят
    assert set(reg.requestable()) == {"observe", "plan"}


def test_mode_sets_come_from_game_modes_and_fall_back_to_observe():
    reg = policies.get()
    assert "combat" in reg.modes_of("combat") and "build" not in reg.modes_of("combat")
    assert reg.modes_of("afk") == ("observe",)                  # без режимов: безопасный запасной


def test_public_description_lists_transitions_for_the_dashboard():
    items = {p["id"]: p for p in policies.get().public()}
    assert items["plan"]["transitions"] == ["observe"] and items["afk"]["default"] is True


def test_broken_policy_file_fails_loudly(tmp_path):
    d = copy_policies(tmp_path)
    (d / "plan.yaml").write_text((d / "plan.yaml").read_text(encoding="utf-8").replace("kinds: [plan_request", "kinds: [nope"),
                                 encoding="utf-8")
    with pytest.raises(policies.PolicyError, match="nope"):
        policies.load(d)


@pytest.mark.parametrize("old,new,msg", [
    ("modes: [combat, health, observe]", "modes: [combat, nonexistent_mode]", "нет режима"),
    ("  to: observe\nswitch_to: [observe]", "  to: nowhere\nswitch_to: [observe]", "exit.to"),
    ("loop: iterative", "loop: forever", "loop"),
    ("priority: 4", "priority: 2", "приоритеты"),
    ("  model: mixed", "  model: gpt", "think.model"),
])
def test_schema_errors_are_named(tmp_path, old, new, msg):
    d = copy_policies(tmp_path)
    text = (d / "combat.yaml").read_text(encoding="utf-8")
    assert old in text
    (d / "combat.yaml").write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(policies.PolicyError, match=msg):
        policies.load(d)


def test_unknown_field_is_rejected(tmp_path):
    d = copy_policies(tmp_path)
    (d / "manual.yaml").write_text((d / "manual.yaml").read_text(encoding="utf-8") + "\nbogus: 1\n", encoding="utf-8")
    with pytest.raises(policies.PolicyError, match="bogus"):
        policies.load(d)


def test_exactly_one_default_policy(tmp_path):
    d = copy_policies(tmp_path)
    (d / "observe.yaml").write_text((d / "observe.yaml").read_text(encoding="utf-8").replace(
        "id: observe", "id: observe\ndefault: true"), encoding="utf-8")
    with pytest.raises(policies.PolicyError, match="default"):
        policies.load(d)
