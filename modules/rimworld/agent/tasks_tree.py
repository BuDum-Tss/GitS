"""Дерево тактических задач: подзадачи связаны с родителем по description (поле parent)."""

_ORDER = {"in_progress": 0, "pending": 1, "done": 2}


def build(tasks: list[dict]) -> list[dict]:
    """Корневые задачи с вложенными подзадачами. Прогресс родителя: доля выполненных подзадач.
    Подзадача с неизвестным родителем показывается как обычная задача."""
    names = {t.get("description") for t in tasks}
    nodes = {t.get("description"): {"text": t.get("description", ""), "status": t.get("status", "pending"),
                                    "goal_id": t.get("goal_id", ""), "children": []} for t in tasks}
    roots = []
    for t in tasks:
        node, parent = nodes[t.get("description")], t.get("parent") or ""
        if parent and parent in names and parent != t.get("description"):
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)
    for n in nodes.values():
        kids = n["children"]
        n["progress"] = round(100 * sum(k["status"] == "done" for k in kids) / len(kids)) if kids else \
            (100 if n["status"] == "done" else 0)
        kids.sort(key=lambda k: _ORDER.get(k["status"], 1))
    roots.sort(key=lambda n: _ORDER.get(n["status"], 1))
    return roots
