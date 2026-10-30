"""Шаги интерфейса -> команды мода (чистые функции).

Агент отдаёт за ход последовательность шагов так, как их делает игрок: открыть вкладку, выбрать категорию и постройку, поставить
в клетку. Каждый шаг становится одной командой мода (`ui_*`), мод исполняет их по очереди с курсором. Состояние интерфейса
(`ui_state`) приходит текстом, картинок нет. Шаги, у которых в моде уже есть команда с привязкой к вкладке (`select`, `draft`,
`set_work_priority`, `research`...), идут как есть: поле `do` равно типу команды."""

# шаг -> (тип команды мода, допустимые поля)
STEP_COMMANDS = {
    "open_tab": ("open_tab", ("tab",)),
    "category": ("ui_category", ("id",)),
    "designator": ("ui_designator", ("id", "stuff", "rot")),
    "place": ("ui_place", ("x", "z")),
    "drag": ("ui_drag", ("x1", "z1", "x2", "z2", "shape")),
    "cancel": ("ui_cancel", ()),
    "assign": ("ui_assign", ("pawn_id", "kind", "value")),
    "letter": ("ui_letter", ("index", "action", "choice")),
    "quest": ("ui_quest", ("id", "action", "pawn_id")),
}
# команды мода, привязанные к вкладкам или к пешке: идут как есть (`do` равно типу команды)
PASS_THROUGH = (
    "select", "draft", "undraft", "move", "attack", "set_work_priority", "set_manual_priorities", "research", "set_medical_care",
    "order_tame", "order_slaughter", "set_zone_home", "create_grow_zone", "create_stockpile", "delete_zone", "set_schedule",
    "pause", "resume", "set_time_speed",
)
MAX_STEPS = 12


def plan(steps) -> tuple[list[dict], str | None]:
    """(команды, ошибка). Ошибка - строка, если шаги нельзя разобрать; тогда ничего не исполняется."""
    if not isinstance(steps, list) or not steps:
        return [], "steps: непустой список шагов"
    if len(steps) > MAX_STEPS:
        return [], f"steps: не больше {MAX_STEPS} шагов за раз"
    out: list[dict] = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict) or not isinstance(step.get("do"), str):
            return [], f"шаг {i}: нужен объект с полем do"
        name = step["do"]
        if name in STEP_COMMANDS:
            ctype, fields = STEP_COMMANDS[name]
            cmd = {"type": ctype, **{k: step[k] for k in fields if k in step}}
        elif name in PASS_THROUGH:
            cmd = {"type": name, **{k: v for k, v in step.items() if k != "do"}}
        else:
            known = sorted([*STEP_COMMANDS, *PASS_THROUGH])
            return [], f"шаг {i}: неизвестное действие «{name}». Есть: {', '.join(known)}"
        out.append(cmd)
    return out, None
