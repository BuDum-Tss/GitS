"""Режим фокуса модуля RimWorld (правка fix/focus-bootstrap, ТЗ 11e, POL-11/POL-14): кто выдаёт модулю фокус (политика
`afk` -> `observe`), пока менеджера фокуса в ядре нет (ТЗ 18 не сделано).

Режимы настройки `focus_mode`:
  - `core` (по умолчанию, ТЗ 32): фокус только от запросов ядра (workspace.enter, прежнее policy.activate); игра фокус не берёт.
  - `auto` (прежний режим, включается вручную): пока ядро не прислало ни одного policy.activate / focus.release за время жизни модуля, модуль
    сам берёт фокус, когда игра запущена и мод отвечает; уходит в `afk`, когда игра закрыта. Игру сам не запускает (POL-12).
  - `manual`: фокус только кнопками на панели RimWorld; запросы ядра policy.activate отклоняются, focus.release принимается.

Здесь чистые функции (без сети и игры): решение и объяснение «почему afk» для журнала, state и панели."""
from __future__ import annotations

MODES = ("auto", "core", "manual")
DEFAULT = "core"          # ТЗ 32, WS-5: игра фокус сама не берёт, среду включает импульс (сообщение, решение агента)
MODE_LABEL = {
    "auto": "авто: без менеджера в ядре берёт фокус сам, когда игра запущена",
    "core": "от ядра: фокус только по запросу policy.activate",
    "manual": "вручную: фокус только кнопкой на панели",
}
GAME_GONE_TICKS = 3          # игра не отвечает столько тиков подряд (около 6 с): считаем её закрытой, а не сбоем опроса


def normalize(value) -> str:
    """Значение настройки -> один из режимов; что угодно другое даёт режим по умолчанию."""
    return value if value in MODES else DEFAULT


def auto_grab_allowed(*, mode: str, core_seen: bool, game_up: bool, in_focus: bool, held_off: bool) -> bool:
    """Модуль берёт фокус сам: режим auto, ядро ещё ничего не присылало, игра отвечает, фокуса нет и владелец его не отпускал."""
    return mode == "auto" and not core_seen and game_up and not in_focus and not held_off


def afk_explanation(*, mode: str, core_seen: bool, game_up: bool, held_off: bool) -> str:
    """Почему сейчас вне фокуса: одна фраза для журнала, state и панели."""
    if held_off:
        return "фокус отпущен кнопкой: сам не берётся, пока игра не закроется или не нажата «Взять фокус»"
    if mode == "manual":
        return "режим «вручную»: фокус только кнопкой «Взять фокус»"
    if mode == "core" or core_seen:
        return "фокус выдаёт ядро (запрос policy.activate): ждём его или кнопку «Взять фокус»"
    if not game_up:
        return "игра не запущена: модуль её сам не запускает; как только она откроется, фокус возьмётся сам"
    return "игра запущена: фокус берётся на ближайшем цикле"
