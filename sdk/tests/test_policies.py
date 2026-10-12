"""Политики модуля в манифесте (ТЗ 18, FOC-1): поле `policies` и `default_policy`, режим совместимости без них."""
import copy

import pytest

from glados_module import protocol as P
from glados_module.protocol import Manifest, ManifestError

BASE = {
    "id": "montage",
    "title": "Монтаж",
    "description": "Монтаж роликов.",
    "version": "1.0.0",
}

POLICIES = [
    {"id": "idle", "title": "Простой", "focus": "background", "transitions": ["working"], "interruptible": True},
    {"id": "working", "title": "Монтаж", "focus": "active", "transitions": ["idle", "waiting_for_materials"],
     "interruptible": True, "safe_points": ["кадр записан"], "resources": {"cpu": 1, "tokens": 0}},
    {"id": "waiting_for_materials", "title": "Нет исходников", "focus": "background", "transitions": ["working"]},
]


def with_policies(**changes):
    manifest = {**copy.deepcopy(BASE), "policies": copy.deepcopy(POLICIES), "default_policy": "idle"}
    manifest.update(changes)
    return manifest


def test_valid_policies_pass():
    assert P.validate_manifest(with_policies()) == []


def test_manifest_without_policies_is_compatible():
    """Модуль без policies работает как раньше: проблем нет, фокус ему не назначается."""
    assert P.validate_manifest(copy.deepcopy(BASE)) == []
    m = Manifest.from_dict(copy.deepcopy(BASE))
    assert m.policies == [] and m.default_policy is None
    assert "policies" not in m.to_dict()


def test_default_policy_without_policies_is_rejected():
    problems = P.validate_manifest({**copy.deepcopy(BASE), "default_policy": "idle"})
    assert any("default_policy" in p and "без policies" in p for p in problems)


def test_at_least_one_background_policy_required():
    only_active = [{"id": "working", "title": "Монтаж", "focus": "active"}]
    problems = P.validate_manifest(with_policies(policies=only_active, default_policy="working"))
    assert any("focus background" in p for p in problems)


def test_default_policy_must_exist_and_be_background():
    assert any("default_policy" in p for p in P.validate_manifest(with_policies(default_policy="nope")))
    problems = P.validate_manifest(with_policies(default_policy="working"))
    assert any("default_policy" in p and "фоновой" in p for p in problems)


@pytest.mark.parametrize("bad, needle", [
    ({"id": "Bad Id", "title": "x", "focus": "background"}, "id"),
    ({"id": "idle", "title": "", "focus": "background"}, "title"),
    ({"id": "idle", "title": "x", "focus": "sometimes"}, "focus"),
    ({"id": "idle", "title": "x", "focus": "background", "transitions": "working"}, "transitions"),
    ({"id": "idle", "title": "x", "focus": "background", "interruptible": "yes"}, "interruptible"),
    ({"id": "idle", "title": "x", "focus": "background", "resources": {"gpu": -1}}, "resources"),
    ({"id": "idle", "title": "x", "focus": "background", "resources": {"ram": 2}}, "resources"),
])
def test_bad_policy_fields_are_reported(bad, needle):
    problems = P.validate_manifest(with_policies(policies=[bad, POLICIES[1]], default_policy="idle"))
    assert any(needle in p for p in problems), problems


def test_duplicate_policy_and_unknown_transition():
    dup = [POLICIES[0], dict(POLICIES[0])]
    assert any("повторяется" in p for p in P.validate_manifest(with_policies(policies=dup)))
    unknown = [{"id": "idle", "title": "x", "focus": "background", "transitions": ["ghost"]}]
    assert any("ghost" in p for p in P.validate_manifest(with_policies(policies=unknown)))


def test_extra_policy_fields_are_ignored():
    """RimWorld отдаёт и поля purpose, priority, default: лишние поля не мешают (MOD-11)."""
    extra = [{"id": "idle", "title": "x", "focus": "background", "priority": 0, "default": True, "purpose": "фон"}]
    assert P.validate_manifest(with_policies(policies=extra, default_policy="idle")) == []


def test_manifest_round_trip_keeps_policies():
    m = Manifest.from_dict(with_policies())
    assert m.default_policy == "idle"
    assert m.policy("working").focus == "active"
    assert m.policy("working").resources == {"cpu": 1, "tokens": 0}
    again = Manifest.from_dict(m.to_dict())
    assert [p.id for p in again.policies] == ["idle", "working", "waiting_for_materials"]
    assert again.default_policy == "idle"


def test_from_dict_raises_on_invalid_policies():
    with pytest.raises(ManifestError):
        Manifest.from_dict(with_policies(policies=[POLICIES[1]], default_policy="working"))


def test_policy_request_types_are_known_to_protocol():
    for typ in ("policy.activate", "focus.release", "focus.resume"):
        assert P.is_request(typ)
