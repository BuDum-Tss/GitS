"""Вводная для агента при входе в среду RimWorld (ответ модуля на `workspace.enter`, ключ `entry.brief`, не длиннее 800 символов).
Названия инструментов здесь должны существовать в дереве узлов: проверяет тест `tests/test_game_brief.py`."""

BRIEF_MAX = 800

GAME_BRIEF = (
    "Цели ставишь сама. Нет активной цели или прежняя закрыта: colony_assess, затем assistant.create_task (шаги конкретные: что, "
    "сколько, где) и выполняй. Выполненную цель закрывай и ставь новую, развитие не останавливается. Действуй, не планируй. "
    "1) Склад: без зоны colony_resources пусто: architect_zone_stockpile у брёвен, камня, еды, inspect_unforbid_area. "
    "2) Укрытие: architect_structure_build_room (architect_orders_plan_room только проверка); would_be_room=false дважды: "
    "architect_fit_room или стены architect_structure_place_building. "
    "3) work_set_priorities: Construction, Hauling, Growing, Cooking. "
    "4) architect_zone_grow_zone. 5) Простаивающим pawn_orders_force_work. "
    "Ход кончай действием, меняющим игру; больше двух чтений подряд нельзя. "
    "Узел открой раз, потом действия вызывай напрямую."
)
