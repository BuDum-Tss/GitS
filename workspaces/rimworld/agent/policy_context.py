"""Подсказка хода без полного снимка мира (ТЗ 11e, POL-1): событие, вызвавшее ход, важное изменение с прошлого решения,
цель и шаг, список инструментов-запросов. Размер не зависит от размера колонии: виды событий и изменений ограничены,
мир агент читает сам инструментами (POL-2)."""
from __future__ import annotations

MAX_EVENTS = 6
MAX_CHANGES = 10
LINE_MAX = 200

# инструменты-запросы: что читать, когда нужно (полный снимок колонии — последним, он самый большой)
QUERY_TOOLS = (
    ("get_colonists", "колонисты: занятие, настроение, здоровье"),
    ("get_resources", "ресурсы и запас еды"),
    ("get_threats", "враги на карте"),
    ("get_research", "исследования"),
    ("get_buildings", "постройки и чертежи (можно с фильтром по виду)"),
    ("get_tasks", "тактические задачи"),
    ("get_goals", "стратегические цели"),
    ("get_items", "предметы на карте"),
    ("find_nearest", "ближайшие объекты к точке"),
    ("get_pawn_details", "подробности одного колониста"),
    ("get_colony_state", "полная сводка колонии (большая, только когда нужна вся картина)"),
)


def _line(text: str) -> str:
    return " ".join(str(text).split())[:LINE_MAX]


def turn_brief(events: list, changes: list[str], *, policy_title: str = "", ambition: str = "", step: str = "",
               research_next: str = "") -> str:
    """Текст подсказки хода. events: объекты с полями kind, importance, text (policy_events.Event) или строки.
    changes: только важные изменения с прошлого решения (строки)."""
    out = [f"=== ПОЛИТИКА: {policy_title or 'наблюдение'} ==="]
    if events:
        out.append("=== СОБЫТИЯ, ВЫЗВАВШИЕ ХОД ===")
        for ev in list(events)[-MAX_EVENTS:]:
            if isinstance(ev, str):
                out.append("- " + _line(ev))
            else:
                out.append(f"- [{ev.kind}, важность {ev.importance}] {_line(ev.text)}")
    if changes:
        out.append("=== ВАЖНОЕ С ПРОШЛОГО РЕШЕНИЯ ===")
        out.extend("- " + _line(c) for c in changes[:MAX_CHANGES])
        if len(changes) > MAX_CHANGES:
            out.append(f"- и ещё {len(changes) - MAX_CHANGES} изменений (по запросу: get_colonists, get_resources...)")
    if ambition:
        out.append("=== ГЛАВНАЯ ЦЕЛЬ ===\n" + _line(ambition))
    if step:
        out.append("=== ТЕКУЩИЙ ШАГ ===\n" + _line(step))
    if research_next:
        out.append("=== ОЧЕРЕДЬ ИССЛЕДОВАНИЙ ===\nследующее: " + _line(research_next) + " (начать: set_research, окно «Технологии» открыто)")
    out.append("=== ЗАПРОСЫ МИРА (полного снимка колонии здесь нет: читай нужное инструментами) ===")
    out.extend(f"- {name}: {desc}" for name, desc in QUERY_TOOLS)
    return "\n".join(out) + "\n"
