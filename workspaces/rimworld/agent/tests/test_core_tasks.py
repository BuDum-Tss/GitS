"""Цель и задачи игры приходят из ядра (core.tasks): оверлей, панель и статус фокуса не читают файлы прежнего агента."""
import core_tasks

SNAP = {"workspace": "rimworld",
        "active": {"id": "t1", "goal": "Развивать колонию", "steps": [{"text": "еда", "status": "done"}, {"text": "жильё", "status": "pending"}]},
        "tasks": [{"id": "t1", "goal": "Развивать колонию", "status": "active", "progress": [1, 2],
                   "steps": [{"text": "еда", "status": "done"}, {"text": "жильё", "status": "pending"}]}]}


def teardown_function():
    core_tasks.set_snapshot(None)


def test_empty_without_core():
    core_tasks.set_snapshot(None)
    assert core_tasks.ambition() is None and core_tasks.tree() == [] and core_tasks.steps_for_focus() == []


def test_goal_tree_and_steps_from_core():
    core_tasks.set_snapshot(SNAP)
    assert core_tasks.ambition() == {"title": "Развивать колонию", "progress": 50}
    node = core_tasks.tree()[0]
    assert node["status"] == "in_progress" and node["progress"] == 50
    assert [c["status"] for c in node["children"]] == ["done", "in_progress"]
    assert core_tasks.steps_for_focus()[1] == {"description": "жильё", "status": "in_progress"}
    assert core_tasks.goal_title() == "Развивать колонию"
