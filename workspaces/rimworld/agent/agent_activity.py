"""Что делает агент прямо сейчас: поставил ли он паузу и вызывает ли инструменты (память процесса модуля).

Автоснятие забытой паузы (speed_plan, controller) и сторож застоя (game_watchdog) не должны трогать паузу, которую агент
поставил сознательно (чтобы разобраться с упавшими колонистами), и не должны вмешиваться, пока он активно работает:
идут его вызовы инструментов. Метка паузы по образцу `planning_pause.mark`, но хранится только в памяти: в контейнерном
режиме инструменты исполняются в процессе модуля, диск не нужен. Если MCP-сервер игры запущен отдельным процессом
(старый режим под LLM), его вызовы сюда не доходят: там действует только страховка по возрасту паузы.
Время хранится меткой `time.time()`; возраст считает вызывающий (так проще подменять часы в тестах)."""
import time

AGENT_PAUSE_HOLD_S = 600       # пауза агента считается сознательной не дольше этого (настройка agent_pause_hold_s, 60-3600)
AGENT_ACTIVE_S = 90            # столько секунд после последнего вызова инструмента агент считается активным (agent_active_s)
CLEAR_GRACE_S = 5              # свежую метку не сбрасываем по «игра не на паузе»: состояние могло быть прочитано до исполнения паузы

COMBAT_COMMANDS = frozenset({"draft", "undraft", "attack", "move", "equip", "wear", "rescue", "tend", "cancel_job", "capture"})
SCHEDULE_COMMANDS = frozenset({"set_schedule", "set_schedule_range"})

_pause_at: float | None = None
_tool_at: float | None = None
_combat_at: float | None = None      # метка last_combat_order_ts: агент сам отдал боевой приказ (рефлекс обороны не мешает ему)
_schedule_at: float | None = None    # агент сам правил расписание (рефлекс настроения его расписание не переписывает)


def mark_pause(now: float | None = None) -> None:
    """Агент поставил паузу (инструмент pause, set_speed 0)."""
    global _pause_at
    _pause_at = time.time() if now is None else now


def clear_pause() -> None:
    """Агент сам снял паузу (resume, set_speed с ненулевой скоростью) или пауза кончилась."""
    global _pause_at
    _pause_at = None


def pause_at() -> float | None:
    return _pause_at


def touch_tool(now: float | None = None) -> None:
    """Агент вызвал инструмент (начало и конец вызова: долгий вызов тоже считается работой)."""
    global _tool_at
    _tool_at = time.time() if now is None else now


def tool_at() -> float | None:
    return _tool_at


def last_combat_order_ts() -> float | None:
    """Когда агент в последний раз сам отдал боевой приказ (draft, attack, equip, rescue, tend...); None: не отдавал."""
    return _combat_at


def last_schedule_order_ts() -> float | None:
    """Когда агент в последний раз сам менял расписание (set_schedule); None: не менял."""
    return _schedule_at


def note_command(command: dict, now: float | None = None) -> None:
    """Команда агента игре исполнена: pause, resume и set_time_speed ведут метку паузы, боевые приказы и расписание свои метки.
    Свои команды рефлексов (`reflex: true`) метку агента не двигают."""
    global _combat_at, _schedule_at
    kind = (command or {}).get("type")
    if not (command or {}).get("reflex"):
        if kind in COMBAT_COMMANDS:
            _combat_at = time.time() if now is None else now
        elif kind in SCHEDULE_COMMANDS:
            _schedule_at = time.time() if now is None else now
    if kind == "pause":
        mark_pause(now)
    elif kind == "resume":
        clear_pause()
    elif kind == "set_time_speed":
        try:
            speed = int(command.get("speed"))
        except (TypeError, ValueError):
            return
        if speed <= 0:
            mark_pause(now)
        else:
            clear_pause()


def pause_is_fresh(now: float, hold_s: float = AGENT_PAUSE_HOLD_S, at: float | None = None) -> bool:
    """Метка паузы агента есть и не старше `hold_s`."""
    at = _pause_at if at is None else at
    return at is not None and now - at < hold_s


def is_active(now: float, active_s: float = AGENT_ACTIVE_S, at: float | None = None) -> bool:
    """Последний вызов инструмента был меньше `active_s` секунд назад (0: не учитывать)."""
    at = _tool_at if at is None else at
    return active_s > 0 and at is not None and now - at < active_s


def reset() -> None:
    """Сброс (тесты)."""
    global _tool_at, _combat_at, _schedule_at
    clear_pause()
    _tool_at = None
    _combat_at = None
    _schedule_at = None
