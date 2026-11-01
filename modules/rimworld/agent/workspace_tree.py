"""Дерево рабочего места RimWorld (по `GitS/docs/WORKSPACE_MODULES.md`): узлы повторяют интерфейс игры, а каждый нынешний
инструмент MCP-сервера получает судьбу: действие или запрос узла, навык (составное действие), переезд в ядро или удаление.

Это данные, не поведение: код модуля пока работает по старому плоскому списку. Дерево нужно для миграции: тест
`tests/test_workspace_tree.py` проверяет, что ни один нынешний инструмент не потерян, в узле не больше `MAX_ACTIONS` действий
(правило WS-6) и что у каждого действия одна дорога (WS-3).

Виды (`kind`):
  action  действие узла (меняет игру, делается только в этом узле)
  query   запрос, чтение без навигации (дерево запросов зеркалит вкладки, раздел 4.3 документа)
  skill   составное действие из шагов узла (ставится как навык, пока работает прежней одной командой мода)
  nav     навигация и текущее состояние (`open`, `back`, `where`)
  core    не игровое: переезжает в ядро (задачи, память, ожидание событий, заметки)
  remove  удаляется (мета-инструменты, обходы, дубли)
"""
from dataclasses import dataclass

MAX_ACTIONS = 9            # WS-6: действий в узле не больше
MAX_DEPTH = 4              # WS-6: глубина дерева


@dataclass(frozen=True)
class Node:
    path: str                      # "architect/zone"
    title: str
    tab: str = ""                  # defName вкладки игры (MainButtonDef), если узел это вкладка
    context: tuple[str, ...] = ()  # запросы панели узла (раздел 4.4; в манифесте поле `nodes[].context`)
    watch: tuple[tuple[str, int], ...] = ()     # сдвиг срочности событий по теме (раздел 5.1)


NODES: tuple[Node, ...] = (
    Node("root", "Карта и время (глобальные элементы управления: время, навигация)", context=("colony/resources", "colony/threats"), watch=(("threat", 1),)),
    Node("architect", "Архитектор", tab="Architect", context=("colony/resources", "colony/buildings"),
         watch=(("resources", 1), ("threat", 1), ("social", -1))),
    Node("architect/orders", "Архитектор: приказы", tab="Architect"),
    Node("architect/zone", "Архитектор: зоны", tab="Architect"),
    Node("architect/structure", "Архитектор: постройки", tab="Architect"),
    Node("work", "Работа", tab="Work", context=("colony/colonists",)),
    Node("schedule", "Режим дня", tab="Schedule", context=("colony/colonists",)),
    Node("assign", "Назначения", tab="Assign", context=("colony/colonists",)),
    Node("research", "Исследования", tab="Research", context=("research/current",)),
    Node("animals", "Животные", tab="Animals"),
    Node("inspect", "Выделенное (постройка, предмет)", tab="Inspect"),
    Node("pawn", "Колонист", tab="Inspect",
         watch=(("health", 1), ("threat", 1))),
    Node("pawn/orders", "Колонист: приказы", tab="Inspect", watch=(("threat", 2),)),
    Node("pawn/gear", "Колонист: снаряжение", tab="Inspect"),
    Node("pawn/medical", "Колонист: лечение и спасение", tab="Inspect", watch=(("health", 2),)),
    Node("map", "Карта: камера и взгляд", context=("colony/threats",)),
)

# нынешний инструмент -> (вид, путь узла или запроса, новое имя). Новое имя пустое, если инструмент удаляется или уходит в ядро.
TOOLS: dict[str, tuple[str, str, str]] = {
    # навигация и состояние
    "open_game_tab": ("nav", "root", "open"),
    "ui_state": ("nav", "root", "where"),
    "ui_steps": ("remove", "", ""),                        # заменён действиями узлов: открыть вкладку, категорию, постройку, поставить
    # время и навигация: действия узла root глобальны (как кнопки внизу экрана игры), доступны из любого узла
    "pause_game": ("action", "root", "pause"),
    "resume_game": ("action", "root", "resume"),
    "set_time_speed": ("action", "root", "set_speed"),
    # Архитектор
    "cancel_construction": ("action", "architect/orders", "cancel_blueprint"),
    "deconstruct": ("action", "architect/orders", "deconstruct"),
    "designate_mine": ("action", "architect/orders", "mine"),
    "designate_chop": ("action", "architect/orders", "chop"),
    "designate_smooth": ("action", "architect/orders", "smooth"),
    "clear_plans": ("action", "architect/orders", "clear_plans"),
    "plan_room": ("action", "architect/orders", "plan_room"),
    "create_grow_zone": ("action", "architect/zone", "grow_zone"),
    "create_stockpile": ("action", "architect/zone", "stockpile"),
    "delete_zone": ("action", "architect/zone", "delete_zone"),
    "set_home_area": ("action", "architect/zone", "home_area"),
    "place_building": ("skill", "architect/structure", "place_building"),
    "place_wall_line": ("skill", "architect/structure", "wall_line"),
    "build_room": ("skill", "architect/structure", "build_room"),
    # Работа, расписание, назначения, исследования, животные
    "set_work_priority": ("remove", "work", "set_priority"),            # дубль set_work_priorities
    "set_work_priorities": ("action", "work", "set_priorities"),
    "set_manual_priorities": ("action", "work", "manual_priorities"),
    "set_schedule": ("remove", "schedule", "set_hours"),                # дубль set_schedule_plan
    "set_schedule_range": ("remove", "schedule", "set_hours"),
    "set_schedule_plan": ("action", "schedule", "set_hours"),
    "set_medical_care": ("action", "assign", "medical_care"),
    "set_research": ("action", "research", "set_project"),
    "order_tame": ("action", "animals", "tame"),
    "order_slaughter": ("action", "animals", "slaughter"),
    # Выделенное: верстаки, предметы
    "add_bill": ("action", "inspect", "add_bill"),
    "cancel_bill": ("action", "inspect", "cancel_bill"),
    "forbid_item": ("action", "inspect", "forbid"),
    "unforbid_item": ("action", "inspect", "unforbid"),
    # Колонист
    "draft_pawns": ("action", "pawn/orders", "draft"),
    "undraft_pawns": ("action", "pawn/orders", "undraft"),
    "move_pawn": ("action", "pawn/orders", "move"),
    "attack_target": ("action", "pawn/orders", "attack"),
    "cancel_job": ("action", "pawn/orders", "cancel_job"),
    "force_work": ("action", "pawn/orders", "force_work"),
    "clear_work_queue": ("action", "pawn/orders", "clear_work_queue"),
    "equip_item": ("action", "pawn/gear", "equip"),
    "wear_apparel": ("action", "pawn/gear", "wear"),
    "tend_pawn": ("action", "pawn/medical", "tend"),
    "rescue_pawn": ("action", "pawn/medical", "rescue"),
    "capture_pawn": ("action", "pawn/medical", "capture"),
    # Карта и камера
    "select_target": ("action", "map", "select"),
    "camera_look": ("action", "map", "look"),
    "camera_follow": ("action", "map", "follow"),
    "camera_zoom": ("action", "map", "zoom"),
    "camera_move": ("action", "map", "move"),
    "set_auto_camera": ("remove", "", ""),                  # всегда включён: агент не должен им управлять
    "camera_view": ("query", "map/view", "view"),
    # Запросы
    "get_colony_state": ("query", "colony/overview", "overview"),
    "get_colonists": ("query", "colony/colonists", "colonists"),
    "get_resources": ("query", "colony/resources", "resources"),
    "get_buildings": ("query", "colony/buildings", "buildings"),
    "get_threats": ("query", "colony/threats", "threats"),
    "get_items": ("query", "colony/items", "items"),
    "get_research": ("query", "research/current", "current"),
    "get_pawn_details": ("query", "pawn/details", "details"),           # дальше дробится по вкладкам персонажа
    "get_work_table": ("query", "work/table", "table"),
    "get_schedule_table": ("query", "schedule/table", "table"),
    "list_recipes": ("query", "inspect/recipes", "recipes"),
    "find_nearest": ("query", "map/find", "find"),
    "look_at": ("query", "map/cell", "cell"),
    "survey_map": ("query", "map/survey", "survey"),
    "analyze_area": ("query", "map/analyze", "analyze"),
    "search_thing_defs": ("query", "architect/defs", "search"),
    "describe_thing_def": ("query", "architect/defs", "describe"),
    "estimate_build": ("query", "architect/estimate", "estimate"),
    "fit_room": ("query", "architect/fit", "room"),
    "check_enclosure": ("query", "architect/check", "enclosure"),
    "room_info": ("query", "architect/check", "room_info"),
    "find_buildable_spot": ("query", "architect/fit", "spot"),
    "unlock_for": ("query", "research/unlock", "unlock"),
    # не игровое: переезжает в ядро
    "get_goals": ("core", "", ""),
    "set_goal": ("core", "", ""),
    "update_goal": ("core", "", ""),
    "get_tasks": ("core", "", ""),
    "set_task": ("core", "", ""),
    "add_lesson": ("core", "", ""),
    "note_decision": ("core", "", ""),
    "search_knowledge": ("core", "", ""),
    "remember_knowledge": ("core", "", ""),
    "web_search": ("core", "", ""),
    "get_territory": ("core", "", ""),
    "set_territory_marker": ("core", "", ""),
    "delete_territory_marker": ("core", "", ""),
    "set_watch_rule": ("core", "", ""),                     # ожидание события: watch ядра (раздел про подписки агента)
    "clear_watch_rule": ("core", "", ""),
    "set_opinion": ("core", "", ""),                        # отношения это память ядра, не игра
    "experiment_start": ("core", "", ""),
    "experiment_log": ("core", "", ""),
    "experiment_finish": ("core", "", ""),
    "experiment_list": ("core", "", ""),
    # удаляется
    "switch_mode": ("remove", "", ""),
    "switch_policy": ("remove", "", ""),
    "request_planning": ("remove", "", ""),
    "pc_read_screen": ("remove", "", ""),                   # запасной интерфейс это модуль desktop (WS-5)
    "pc_click_text": ("remove", "", ""),
    "pc_press_key": ("remove", "", ""),
    "pc_operate_screen": ("remove", "", ""),
    "get_pending_events": ("remove", "", ""),               # события идут одним каналом, в ядро (WS-12)
}


# запросы, которых в модуле ещё нет и которые надо написать (панели узлов и запросы по вкладкам персонажа)
NEW_QUERIES: tuple[str, ...] = (
    "colony/construction",      # чертежи и каркасы: сколько, что не строится и почему (запрещённые материалы, нет исследования)
    "pawn/health", "pawn/skills", "pawn/gear", "pawn/social", "pawn/needs",      # get_pawn_details по вкладкам персонажа
)


def kinds() -> dict[str, list[str]]:
    """Нынешние инструменты, сгруппированные по судьбе."""
    out: dict[str, list[str]] = {}
    for name, (kind, _path, _new) in TOOLS.items():
        out.setdefault(kind, []).append(name)
    return out


def actions_by_node() -> dict[str, list[str]]:
    """Новые имена действий и навыков по узлам: нужны для проверки лимита узла и уникальности."""
    out: dict[str, list[str]] = {}
    for kind, path, new in TOOLS.values():
        if kind in ("action", "nav") and new:
            out.setdefault(path, [])
            if new not in out[path]:
                out[path].append(new)
    return out


def depth(path: str) -> int:
    return path.count("/") + 1
