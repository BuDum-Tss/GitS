"""Что меняется на компьютере: смена активного приложения и простой ввода (ТЗ 14, раздел 5).

Чистая логика без Windows API: на вход идут время, активное окно и секунды простоя, на выход - события. Так её проверяют
тестами с подменёнными часами (DESK-17). Сам опрос окна и ввода делает `desktop_module.py` в потоке.

  - desktop.app_changed: смена приложения (имя процесса), не чаще раза в 3 с; заголовок окна - только не чувствительного;
  - desktop.user_idle: простой ввода стал ≥ 600 с; desktop.user_back: ввод после такого простоя.
"""
from dataclasses import dataclass

APP_DEBOUNCE_S = 3.0          # антидребезг смены приложения (DESK-x, раздел 5)
IDLE_AWAY_S = 600.0           # простой, после которого пользователь «отошёл»
TITLE_HINT_MAX = 80           # заголовок окна в событии обрезается


@dataclass
class FocusWatch:
    app: str | None = None                 # приложение, о котором уже сообщено
    app_at: float | None = None            # когда сообщено последний раз (для антидребезга)
    away: bool = False                     # сейчас простой ≥ IDLE_AWAY_S
    last_idle: float = 0.0                 # простой на прошлом шаге (для user_back: сколько он длился)

    def tick(self, now: float, win: dict | None, idle: float, sensitive: bool = False) -> list[dict]:
        """Один шаг опроса. win - активное окно (dict с полями app и title) или None. Возвращает новые события
        (dict: kind, text, data, опционально urgency_hint). Событие `app_changed` при первом наблюдении не шлётся:
        это не смена, а стартовое состояние."""
        out: list[dict] = []
        app = str(win["app"]) if win else None
        if app is not None and app != self.app:
            if self.app is None:
                self.app, self.app_at = app, now            # стартовое приложение: только запомнить
            elif self.app_at is None or now - self.app_at >= APP_DEBOUNCE_S:
                self.app, self.app_at = app, now
                hint = None if sensitive else str(win.get("title") or "").strip()[:TITLE_HINT_MAX] or None
                text = f"Активное приложение сменилось: {app}" + (f", окно «{hint}»" if hint else "") + "."
                out.append({"kind": "desktop.app_changed", "text": text, "data": {"app": app, "title_hint": hint}})

        if idle >= IDLE_AWAY_S and not self.away:
            self.away = True
            out.append({"kind": "desktop.user_idle", "text": f"Пользователь не трогает клавиатуру и мышь уже "
                        f"{int(idle // 60)} мин.", "data": {"idle_s": round(idle)}})
        elif idle < IDLE_AWAY_S and self.away:
            self.away = False
            out.append({"kind": "desktop.user_back", "text": f"Пользователь вернулся к компьютеру после простоя "
                        f"{int(self.last_idle // 60)} мин.", "data": {"idle_s": round(self.last_idle)}})
        self.last_idle = idle
        return out
