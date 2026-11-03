"""ТЗ 33: дерево узлов рабочего места в манифесте, tools[].node, расширенные события, state.node."""
import pytest

from gits_sdk import protocol as P


def _manifest(**extra):
    base = {
        "id": "demo", "title": "Demo", "description": "d", "version": "1.0.0",
        "tools": [
            {"name": "open", "description": "открыть узел", "params": {}, "risk": "auto", "availability": "always"},
            {"name": "get_res", "description": "ресурсы", "params": {}, "risk": "auto", "availability": "always"},
            {"name": "place", "description": "поставить", "params": {}, "risk": "auto", "availability": "always",
             "node": "build/zone"},
        ],
        "emits": [{"kind": "demo.low", "description": "мало", "default_urgency": 2, "urgency_unfocused": 1,
                   "topics": ["resources"], "follow_up": ["get_res"]}],
        "nodes": [
            {"path": "build", "title": "Строительство", "context": [{"tool": "get_res", "mode": "on_change", "max_chars": 300}],
             "watch": {"resources": 1, "social": -1}},
            {"path": "build/zone", "title": "Зоны"},
        ],
    }
    base.update(extra)
    return base


def test_valid_manifest_with_nodes_round_trips():
    assert P.validate_manifest(_manifest()) == []
    m = P.Manifest.from_dict(_manifest())
    out = m.to_dict()
    assert [n["path"] for n in out["nodes"]] == ["build", "build/zone"]
    assert out["emits"][0]["topics"] == ["resources"] and out["emits"][0]["urgency_unfocused"] == 1
    assert out["emits"][0]["follow_up"] == ["get_res"]
    assert next(t for t in out["tools"] if t["name"] == "place")["node"] == "build/zone"
    assert "node" not in next(t for t in out["tools"] if t["name"] == "open")           # лишнего поля у обычных инструментов нет


def test_manifest_without_nodes_keeps_working():
    m = _manifest()
    m.pop("nodes")
    m["tools"] = [dict(t) for t in m["tools"]]
    for t in m["tools"]:
        t.pop("node", None)
    m["emits"] = [{"kind": "demo.low", "description": "мало", "default_urgency": 2}]
    assert P.validate_manifest(m) == []
    assert P.Manifest.from_dict(m).nodes == []


@pytest.mark.parametrize("nodes,part", [
    ([{"path": "Build", "title": "x"}], "nodes[0].path"),
    ([{"path": "a/b/c/d/e", "title": "x"}], "nodes[0].path"),                                  # глубже 4
    ([{"path": "a", "title": "x"}, {"path": "a", "title": "y"}], "повторяется"),
    ([{"path": "a/b", "title": "x"}], "нет родительского узла"),
    ([{"path": "a", "title": ""}], "title"),
    ([{"path": "a", "title": "x", "context": [{"tool": "нет такого"}]}], "context[0].tool"),
    ([{"path": "a", "title": "x", "context": [{"tool": "get_res", "mode": "always"}]}], "context[0].mode"),
    ([{"path": "a", "title": "x", "context": [{"tool": "get_res", "max_chars": 5}]}], "max_chars"),
    ([{"path": "a", "title": "x", "watch": {"resources": 9}}], "watch[resources]"),
    ([{"path": "a", "title": "x", "watch": {"Bad": 1}}], "watch"),
])
def test_bad_nodes_are_rejected(nodes, part):
    problems = P.validate_manifest(_manifest(nodes=nodes, tools=_manifest()["tools"][:2]))
    assert any(part in p for p in problems), problems


def test_tool_node_must_exist_and_follow_up_must_be_a_tool():
    problems = P.validate_manifest(_manifest(nodes=[{"path": "other", "title": "x"}]))
    assert any("tools[place].node" in p for p in problems)
    m = _manifest()
    m["emits"][0]["follow_up"] = ["нет_такого"]
    assert any("follow_up" in p for p in P.validate_manifest(m))
    m = _manifest()
    m["emits"][0]["urgency_unfocused"] = 9
    assert any("urgency_unfocused" in p for p in P.validate_manifest(m))
    m = _manifest()
    m["emits"][0]["topics"] = ["Плохая тема"]
    assert any("topics" in p for p in P.validate_manifest(m))


def test_state_may_carry_node_and_node_must_be_a_string():
    from gits_sdk import contract
    assert contract.validate("state", {"summary": "в Архитекторе", "node": "architect/zone"}) == []
    assert contract.validate("state", {"summary": "x", "node": 5})
    assert contract.validate("state", {"summary": "x"}) == []                       # старые модули не шлют node
