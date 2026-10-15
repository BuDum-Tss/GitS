"""Режимы игрового агента: в каждом режиме у него свой небольшой набор инструментов и своя подсказка.

Раньше агент видел все ~90 инструментов сразу: ход тонул в выборе, а слабой локальной модели (Gemma) такой набор не под силу.
Теперь ход идёт в одном режиме, где 20-35 инструментов по теме: наблюдение (за колонией и задачами), строительство, хозяйство
(производство и ресурсы), бой, здоровье, разведка, экран (запасной интерфейс). Базовые инструменты есть в любом режиме (состояние,
пауза, цели и задачи, знания, камера, `switch_mode`). Смена режима: агент зовёт `switch_mode`, контроллер начинает новый ход в нужном режиме.
Выбор режима по умолчанию: правила по состоянию игры (`pick_mode`): враги, недостроенное, нужды, приказ игрока.

Режимы описаны данными: добавить новый значит дописать запись в MODES (и тест проверит, что все инструменты существуют)."""
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# БАЗОВЫЕ инструменты (есть в любом режиме): только чтение состояния, пауза, окна, знания и смена режима. Всё остальное принадлежит
# РОВНО ОДНОМУ режиму: инструмент не повторяется в двух режимах, поэтому список каждого режима короткий и читается целиком.
CORE = (
    "get_colony_state", "get_pawn_details", "get_items", "find_nearest", "pause_game", "resume_game", "open_game_tab", "note_decision",
    "search_knowledge", "remember_knowledge", "switch_mode",
    "get_colonists", "get_resources", "switch_policy",       # ТЗ 11e (POL-2, POL-5): запросы мира и просьба о политике
)


@dataclass(frozen=True)
class GameMode:
    id: str
    title: str
    purpose: str                       # когда этот режим нужен (для агента и для выбора режима)
    tools: tuple[str, ...]             # СВОИ инструменты режима (сверх базовых)
    prompt: str = ""                   # что делать в этом режиме
    extra: dict = field(default_factory=dict)


MODES: dict[str, GameMode] = {m.id: m for m in (
    GameMode(
        "observe", "Наблюдение и управление",
        "Основной режим: следить за колонистами (здоровье, настроение, нужды), ресурсами и задачами; менять статусы задач, ставить новые, "
        "выбирать исследования.",
        ("get_goals", "get_tasks", "get_pending_events", "set_time_speed", "set_goal", "update_goal", "set_task", "request_planning",
         "set_research", "add_lesson", "get_research", "set_watch_rule", "clear_watch_rule"),
        "Ты наблюдаешь за колонией. Проверь колонистов (здоровье, настроение, нужды, чем заняты), ресурсы (что есть, чего не хватает, "
        "хватит ли еды), задачи (что выполнено, что застряло). Обнови статусы задач (set_task, update_goal), поставь недостающие. Нужна "
        "стройка, работы и режим дня, лечение, хозяйство или бой: switch_mode в нужный режим."
    ),
    GameMode(
        "people", "Колонисты: работы и режим дня",
        "Кто чем занимается: приоритеты работ цифрами по навыкам, режим дня (сон, работа, отдых), отношение.",
        ("get_work_table", "set_work_priorities", "set_work_priority", "set_manual_priorities", "get_schedule_table", "set_schedule_plan",
         "set_schedule", "set_schedule_range", "set_opinion"),
        "Ты распределяешь работы и режим дня. get_work_table: кто в чём силён (навык, страсть). Лучшему в деле приоритет 1, остальным "
        "2-4 (set_work_priorities). Режим дня: сон около 8 часов подряд ночью, вечером отдых (get_schedule_table, set_schedule_plan). "
        "Окно: open_game_tab(work или schedule), правки, open_game_tab(close)."
    ),
    GameMode(
        "build", "Строительство",
        "Планировать и строить: комнаты, стены, мебель; узнать, что нужно для объекта, какие у него характеристики и из чего строить.",
        ("look_at", "describe_thing_def", "search_thing_defs", "estimate_build", "find_buildable_spot", "fit_room", "plan_room", "build_room", "clear_plans",
         "room_info", "check_enclosure", "place_building", "place_wall_line", "cancel_construction", "deconstruct", "designate_smooth",
         "set_home_area", "get_buildings", "unlock_for"),
        "Ты строишь. Закрыто исследованием (ЗАКРЫТО ИССЛЕДОВАНИЕМ): unlock_for с queue=true и waiting_task. Порядок: 1) что нужно (describe_thing_def: стоимость, работа, материалы; estimate_build: хватит ли ресурсов и часов); "
        "2) место (fit_room; руины это готовые стены, их смотрит режим разведки); 3) проверка плана (plan_room: контур замкнут, всё "
        "строится, цена); 4) постройка (build_room, place_building). Стены вокруг существующего, не сквозь него. Одна комната за раз. "
        "Закончила: switch_mode(observe)."
    ),
    GameMode(
        "economy", "Хозяйство и ресурсы",
        "Производство и запасы: заказы на верстаках, склады, огороды, охота, приручение, запреты, добыча.",
        ("add_bill", "list_recipes", "cancel_bill", "create_stockpile", "create_grow_zone", "delete_zone", "forbid_item", "unforbid_item",
         "order_tame", "order_slaughter", "designate_chop", "designate_mine", "force_work", "clear_work_queue"),
        "Ты ведёшь хозяйство: что производить (add_bill, list_recipes), где склад и огород, что запрещено лишнего (unforbid_item), "
        "что добывать (designate_chop, designate_mine). Еды меньше 10 дней: огород, охота, готовка."
    ),
    GameMode(
        "combat", "Бой и тревога",
        "Враги на карте, пожар, угроза: мобилизация, управление пешками, огонь, пауза.",
        ("draft_pawns", "undraft_pawns", "move_pawn", "attack_target", "cancel_job", "equip_item", "wear_apparel", "rescue_pawn", "capture_pawn", "get_threats"),
        "Бой. Быстро, не больше 8-10 вызовов: 1) get_colony_state (кто где, кто ранен, где враги); 2) вооружи безоружных (equip_item, "
        "оружие смотри через get_items, при нужде unforbid_item в режиме хозяйства); 3) draft_pawns способных бойцов, attack_target "
        "на ближайшего врага; 4) раненых вытащи (rescue_pawn); pause_game нужна, чтобы успеть отдать приказы. Опасность миновала: "
        "undraft_pawns, лечение в режиме health, потом switch_mode(observe)."
    ),
    GameMode(
        "health", "Здоровье и настроение",
        "Больные, раненые, срывы, плохое настроение: лечение и уход.",
        ("tend_pawn", "set_medical_care"),
        "Забота о колонистах. Найди, у кого плохо со здоровьем или настроением (get_pawn_details: травмы, потребности, мысли), лечи "
        "(tend_pawn), поставь уход (set_medical_care). Причины настроения (мало отдыха, тяжёлая работа): режим наблюдения."
    ),
    GameMode(
        "explore", "Разведка и знания",
        "Изучить карту, руины, ресурсы вокруг; найти в интернете, как что-то работает; заметки о местности.",
        ("survey_map", "analyze_area", "web_search", "get_territory", "set_territory_marker", "delete_territory_marker"),
        "Разведка. Изучи карту (survey_map), интересные места (analyze_area: руины, находки, опасности). Не знаешь механику: "
        "search_knowledge, потом web_search и remember_knowledge. Итог запиши заметками (set_territory_marker)."
    ),
    GameMode(
        "show", "Показ и камера",
        "Показать происходящее зрителям: камера, приближение, слежение, выделение.",
        ("camera_look", "camera_view", "camera_follow", "camera_zoom", "camera_move", "select_target", "set_auto_camera"),
        "Ты ведёшь камеру для зрителей: покажи место действия (camera_look), следи за пешкой (camera_follow), приблизь важное."
    ),
    GameMode(
        "screen", "Экран (запасной интерфейс)",
        "Нет прямого инструмента: управлять игрой или программой по экрану (мышь, клавиатура, OCR).",
        ("pc_read_screen", "pc_click_text", "pc_press_key", "pc_operate_screen", "game_screenshot", "game_click", "game_double_click",
         "game_drag", "game_type", "game_key", "game_scroll"),
        "Запасной уровень: только если у действия нет прямого инструмента. Сначала прочитай экран (pc_read_screen или game_screenshot), "
        "потом одно действие, потом проверь результат."
    ),
)}

DEFAULT = "observe"
_REQUEST = Path(__file__).parent / "memory" / "mode_request.json"


def home_mode(tool: str) -> str | None:
    """Режим, которому принадлежит инструмент (None для базовых)."""
    return next((m.id for m in MODES.values() if tool in m.tools), None)


def tools_for(mode: str) -> tuple[str, ...]:
    """Все инструменты режима: базовые и свои (без повторов, порядок сохранён)."""
    m = MODES.get(mode) or MODES[DEFAULT]
    seen, out = set(), []
    for name in CORE + m.tools:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def describe() -> str:
    """Перечень режимов для подсказки и для инструмента switch_mode."""
    return "; ".join(f"{m.id} ({m.title}): {m.purpose}" for m in MODES.values())


def prompt_for(mode: str) -> str:
    m = MODES.get(mode) or MODES[DEFAULT]
    return f"=== РЕЖИМ: {m.title} ===\n{m.prompt}\nИнструменты этого режима ограничены; нужен другой режим: switch_mode(режим, причина). Режимы: {describe()}\n"


# ── выбор режима ──────────────────────────────────────────────────────────────

_BUILD_WORDS = re.compile(r"постро|строй|комнат|стен|двер|кроват|крыш|склад|мебел|чертёж|чертеж|build|wall|room", re.I)
_PEOPLE_WORDS = re.compile(r"приоритет|режим дня|расписан|назнач|работ|навык|сон ", re.I)
_ECON_WORDS = re.compile(r"еда|еды|огород|готов|верстак|производ|охот|заказ|ресурс|дерев|сталь|запас", re.I)
_HEALTH_WORDS = re.compile(r"болен|ранен|настроен|срыв|депресс|истощ|лечи|здоров", re.I)


def pick_mode(state: dict | None, order: str = "", urgent: list | None = None, tasks_text: str = "") -> str:
    """Режим хода по состоянию: враги или тревога -> бой; приказ игрока по теме; плохо колонистам -> здоровье; недостроенное или задачи
    про стройку -> строительство; нехватка еды -> хозяйство; иначе наблюдение."""
    state = state or {}
    colony = state.get("colony") or {}
    if urgent or state.get("threats"):
        return "combat"
    text = order or ""
    if text:
        if _BUILD_WORDS.search(text):
            return "build"
        if _HEALTH_WORDS.search(text):
            return "health"
        if _PEOPLE_WORDS.search(text):
            return "people"
        if _ECON_WORDS.search(text):
            return "economy"
    pawns = colony.get("pawns") or []
    if any(p.get("is_downed") or p.get("bleeding") or (p.get("mood_pct") is not None and p.get("mood_pct") < 0.3) for p in pawns):
        return "health"
    if colony.get("pending_construction"):
        return "build"
    food = colony.get("food_days")
    if food is not None and food < 8:
        return "economy"
    if tasks_text and _BUILD_WORDS.search(tasks_text):
        return "build"
    return DEFAULT


# ── смена режима по просьбе агента ──────────────────────────────────────────────

def request_switch(mode: str, reason: str = "") -> str | None:
    """Записать просьбу сменить режим. Возвращает текст ошибки или None."""
    if mode not in MODES:
        return f"нет режима «{mode}». Есть: {', '.join(MODES)}"
    try:
        _REQUEST.parent.mkdir(parents=True, exist_ok=True)
        _REQUEST.write_text(json.dumps({"mode": mode, "reason": reason[:200], "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return str(e)
    return None


def take_request(max_age: float = 900.0) -> dict | None:
    """Забрать (и удалить) просьбу о смене режима, если она свежая."""
    try:
        data = json.loads(_REQUEST.read_text(encoding="utf-8"))
        _REQUEST.unlink()
    except (OSError, ValueError):
        return None
    return data if data.get("mode") in MODES and time.time() - data.get("ts", 0) < max_age else None
