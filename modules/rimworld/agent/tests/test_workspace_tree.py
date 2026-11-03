"""Дерево рабочего места RimWorld (workspace_tree.py): ни один нынешний инструмент не потерян, лимиты узлов и уникальность
действий (правила WS-3, WS-6 из GitS/docs/WORKSPACE_MODULES.md)."""
import asyncio
from collections import Counter

import module_manifest as mm
import workspace_tree as wt


def _manifest_names() -> set[str]:
    return {s["name"] for s in asyncio.run(mm.mcp_tool_specs())}


def test_every_manifest_tool_has_a_fate():
    names = _manifest_names()
    mapped = set(wt.TOOLS)
    assert not names - mapped, f"инструменты без судьбы: {sorted(names - mapped)}"


def test_no_stale_names_in_the_tree():
    stale = set(wt.TOOLS) - _manifest_names() - {"get_pending_events"}    # get_pending_events исключён из манифеста, но жив в игровом агенте
    assert not stale, f"в дереве есть то, чего нет в модуле: {sorted(stale)}"


def test_kinds_are_known():
    assert set(wt.kinds()) <= {"action", "query", "skill", "nav", "core", "remove"}


def test_node_paths_exist_and_depth_is_limited():
    paths = {n.path for n in wt.NODES}
    for kind, path, _new in wt.TOOLS.values():
        if kind in ("action", "nav", "skill"):
            assert path in paths, f"узла «{path}» нет в дереве"
    for node in wt.NODES:
        assert wt.depth(node.path) <= wt.MAX_DEPTH
        parent = node.path.rpartition("/")[0]
        assert not parent or parent in paths, f"у узла «{node.path}» нет родителя"


def test_node_has_at_most_max_actions():
    too_many = {path: len(names) for path, names in wt.actions_by_node().items() if len(names) > wt.MAX_ACTIONS}
    assert not too_many, f"в узле больше {wt.MAX_ACTIONS} действий: {too_many}"


def test_one_road_per_action():
    seen = Counter((path, new) for kind, path, new in wt.TOOLS.values() if kind in ("action", "skill") and new)
    # set_hours допустим дважды только у «удалённых» дублей, у живых действий пара (узел, имя) уникальна
    live = Counter((path, new) for kind, path, new in wt.TOOLS.values() if kind == "action" and new)
    assert all(n == 1 for n in live.values()), [k for k, n in live.items() if n > 1]
    assert seen


def test_panels_and_watch_use_known_queries_and_topics():
    queries = {path for kind, path, _ in wt.TOOLS.values() if kind == "query"} | set(wt.NEW_QUERIES)
    topics = {"resources", "threat", "social", "health"}
    for node in wt.NODES:
        for q in node.context:
            assert q in queries, f"панель (context) узла «{node.path}»: нет запроса «{q}»"
        for topic, shift in node.watch:
            assert topic in topics and -2 <= shift <= 2
