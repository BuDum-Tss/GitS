"""Вводная для агента при входе в среду RimWorld (ответ модуля на `workspace.enter`, ключ `entry.brief`, не длиннее 800 символов).
Названия инструментов здесь должны существовать в дереве узлов: проверяет тест `tests/test_game_brief.py`."""

BRIEF_MAX = 800

GAME_BRIEF = (
    "ПОРЯДОК ДЕЙСТВИЙ: действуй, а не планируй. "
    "1) Ресурсы считаются только на складе (без зоны colony_resources пусто, хоть вещи на земле): сначала "
    "architect_zone_stockpile у брёвен, камня, еды и inspect_unforbid_area по области. "
    "2) Укрытие: architect_structure_build_room; architect_orders_plan_room только проверка. "
    "would_be_room=false дважды: смени место (architect_fit_room) или ставь стены architect_structure_place_building. "
    "3) work_set_priorities: Construction, Hauling, Growing, Cooking. "
    "4) Зона выращивания (architect_zone_grow_zone) и посев. "
    "5) Простаивающим pawn_orders_force_work. "
    "Ход заканчивай действием, меняющим игру; больше двух чтений подряд нельзя. "
    "Узел достаточно открыть один раз (увидеть его действия); потом действия этого узла можно вызывать напрямую, модуль сам его откроет."
)
