"""Схема инструментов RimWorld из манифеста модуля: дерево узлов, действия узлов, запросы, порядок вызовов.

Запуск из корня репозитория:  python modules/rimworld/tools/build_tool_scheme.py [--check]
Пишет modules/rimworld/docs/TOOLS_SCHEME.html (данные вшиты в страницу, шаблон tools/tool_scheme_template.html).
Данные берутся из `module_manifest.build()` и `workspace_tree.py`, поэтому не расходятся с кодом; руками описаны только
области рисунка интерфейса (`UI_REGIONS`: какая часть экрана игры какому узлу соответствует). Тест
`agent/tests/test_tool_scheme.py` следит, что файл совпадает с кодом."""
import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT / "agent"), str(ROOT.parent.parent / "sdk")]
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

TEMPLATE = HERE / "tool_scheme_template.html"
OUT = ROOT / "docs" / "TOOLS_SCHEME.html"
MARK = "/*__DATA__*/"
# Снимки интерфейса игры (docs/gui, 1920x1080) и области на них: (узел, x, y, ширина, высота, подпись, переход на снимок).
# Узел должен быть в манифесте (проверяет тест), координаты в пикселях снимка.
COMMON = [
    ("map", 0, 0, 1920, 1036, "Карта: камера, обзор", None),
    ("pawn", 820, 5, 250, 90, "Полоса колонистов", "pawn"),
    ("notices", 1760, 595, 155, 280, "Уведомления справа", None),
    ("root", 1750, 895, 165, 32, "Время: пауза и скорость", None),
    ("architect", 0, 1038, 165, 42, "Вкладка «Проекты»", "architect-orders"),
    ("work", 165, 1038, 166, 42, "Вкладка «Работа»", "work"),
    ("schedule", 331, 1038, 166, 42, "Вкладка «Режим дня»", "schedule"),
    ("assign", 497, 1038, 166, 42, "Вкладка «Назначения»", "assign"),
    ("animals", 663, 1038, 167, 42, "Вкладка «Питомцы»", "animals"),
    ("research", 996, 1038, 166, 42, "Вкладка «Технологии»", "research"),
]
ARCH_PANEL = ("architect", 0, 345, 1660, 690, "Меню «Проекты»", None)
UI_SCREENS: dict[str, dict] = {
    "main": {"title": "Главный экран", "image": "gui/00-main.jpg",
             "regions": [("inspect", 0, 830, 540, 205, "Выделенное: постройка, предмет", None)]},
    "architect-orders": {"title": "Проекты: Приказы", "image": "gui/architect-orders.jpg", "regions": [
        ARCH_PANEL, ("architect/orders", 262, 815, 1400, 220, "Приказы: добыча, рубка, снос", None),
        ("architect/orders", 0, 683, 125, 38, "Категория «Приказы»", "architect-orders"),
        ("architect/zone", 125, 683, 125, 38, "Категория «Области»", "architect-zone"),
        ("architect/structure", 0, 723, 125, 38, "Категория «Постройка»", "architect-structure")]},
    "architect-zone": {"title": "Проекты: Области", "image": "gui/architect-zone.jpg", "regions": [
        ARCH_PANEL, ("architect/zone", 262, 815, 1400, 220, "Области: посадки, склад, домашняя зона", None),
        ("architect/orders", 0, 683, 125, 38, "Категория «Приказы»", "architect-orders"),
        ("architect/zone", 125, 683, 125, 38, "Категория «Области»", "architect-zone"),
        ("architect/structure", 0, 723, 125, 38, "Категория «Постройка»", "architect-structure")]},
    "architect-structure": {"title": "Проекты: Постройка", "image": "gui/architect-structure.jpg", "regions": [
        ARCH_PANEL, ("architect/structure", 262, 815, 1400, 220, "Постройка: стены, двери, комнаты", None),
        ("architect/orders", 0, 683, 125, 38, "Категория «Приказы»", "architect-orders"),
        ("architect/zone", 125, 683, 125, 38, "Категория «Области»", "architect-zone"),
        ("architect/structure", 0, 723, 125, 38, "Категория «Постройка»", "architect-structure")]},
    "work": {"title": "Работа", "image": "gui/tab-work.jpg", "regions": [("work", 0, 735, 1536, 300, "Таблица работ", None)]},
    "schedule": {"title": "Режим дня", "image": "gui/tab-schedule.jpg", "regions": [("schedule", 0, 765, 1326, 270, "Расписание на 24 часа", None)]},
    "assign": {"title": "Назначения", "image": "gui/tab-assign.jpg", "regions": [("assign", 0, 765, 1920, 270, "Назначения: медицина, одежда, питание", None)]},
    "research": {"title": "Технологии", "image": "gui/tab-research.jpg", "regions": [("research", 0, 90, 1905, 945, "Дерево исследований", None)]},
    "animals": {"title": "Питомцы", "image": "gui/tab-animals.jpg", "regions": [("animals", 0, 840, 1575, 195, "Животные: приручение, забой", None)]},
    "pawn": {"title": "Карточка колониста", "image": "gui/pawn-card.jpg", "regions": [
        ("pawn", 9, 795, 531, 225, "Карточка колониста (нужен id)", None),
        ("pawn/gear", 141, 795, 114, 30, "Вкладка «Вещи»: снаряжение", None),
        ("pawn/medical", 441, 795, 93, 30, "Вкладка «Здоровье»: лечение", None),
        ("pawn/orders", 555, 930, 95, 100, "Кнопка «К бою!»: приказы", None)]},
}


def _short(text: str, n: int = 170) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def collect(manifest: dict) -> dict:
    import workspace_module as wm
    specs = {name: (kind, path) for _old, kind, path, name, _node in wm._specs_table()}
    paths = [n["path"] for n in manifest["nodes"]]
    tools = []
    for t in manifest["tools"]:
        name = t["name"]
        kind, qpath = specs.get(name, ("", ""))
        node = t.get("node") or ""
        if name in wm.NAV:
            group, owner = "nav", ""
        elif node:
            group, owner = "action", node
        elif kind == "action":
            group, owner = "root", "root"
        elif kind == "query":
            group = "query"
            owner = next((p for p in sorted(paths, key=len, reverse=True) if qpath == p or qpath.startswith(p + "/")), "")
        else:
            group, owner = "other", ""
        tools.append({"name": name, "group": group, "owner": owner, "query_path": qpath if group == "query" else "",
                      "desc": _short(t.get("description", "")), "params": list(t.get("params", {})),
                      "required": [k for k, v in t.get("params", {}).items() if isinstance(v, dict) and v.get("required")]})
    nodes = []
    for n in manifest["nodes"]:
        nodes.append({"path": n["path"], "title": n["title"], "tab": n.get("tab", ""),
                      "context": [c["tool"] for c in n.get("context", [])], "watch": n.get("watch", {})})
    return {"module": manifest["id"], "nodes": nodes, "tools": tools, "screens": {k: {"title": v["title"], "image": v["image"], "regions": [list(r) for r in v["regions"] + COMMON]} for k, v in UI_SCREENS.items()},
            "max_actions": __import__("workspace_tree").MAX_ACTIONS}


def render() -> str:
    import module_manifest
    data = collect(asyncio.run(module_manifest.build()))
    body = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
    return TEMPLATE.read_text(encoding="utf-8").replace(MARK, body)


def main(argv: list[str]) -> int:
    html = render()
    if "--check" in argv:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == html
        print("актуально" if same else "расходится с кодом: запусти без --check")
        return 0 if same else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8", newline="\n")
    print(f"записано {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
