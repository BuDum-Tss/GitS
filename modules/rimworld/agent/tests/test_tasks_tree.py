"""Дерево задач для дашборда: вложенность по parent, прогресс родителя, сироты."""
import tasks_tree


def t(desc, status="pending", parent=""):
    return {"description": desc, "status": status, "parent": parent}


def test_children_nest_under_parent_with_progress():
    tree = tasks_tree.build([t("Построить склад", "in_progress"), t("Нарубить дерева", "done", "Построить склад"),
                             t("Разметить склад", "pending", "Построить склад"), t("Сварить еду")])
    root = tree[0]
    assert root["text"] == "Построить склад" and root["progress"] == 50
    assert [c["text"] for c in root["children"]] == ["Нарубить дерева", "Разметить склад"] or \
        [c["text"] for c in root["children"]] == ["Разметить склад", "Нарубить дерева"]
    assert [n["text"] for n in tree] == ["Построить склад", "Сварить еду"]


def test_orphan_and_self_parent_are_top_level():
    tree = tasks_tree.build([t("Шаг", parent="Нет такой"), t("Сама себе", parent="Сама себе")])
    assert {n["text"] for n in tree} == {"Шаг", "Сама себе"} and all(not n["children"] for n in tree)
