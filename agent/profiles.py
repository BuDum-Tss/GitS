"""Профили поведения: чем она сейчас занята, определяет тон и поведение (игра, работа, стрим, обычный).

Профиль добавляет к характеру свою подсказку; сам характер (конституция) не меняется. Выбирается автоматически
по активному окну (настройка `profile` = "auto") или вручную по id. После окна без признаков (браузер, чат)
прежний профиль держится ещё две минуты: переключения туда-сюда на каждый чих не нужны.
"""
import time
from dataclasses import dataclass

import settings


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    style: str                # добавляется к характеру в разговоре и самостоятельных репликах
    apps: tuple = ()          # имена процессов (без .exe, в нижнем регистре) в активном окне

    def matches(self, app: str) -> bool:
        return app.lower() in self.apps


PROFILES = (
    Profile("game", "Игра (RimWorld)",
            "Сейчас пользователь играет, а ты играешь вместе с ним: коротко, по делу, с азартом. Не отвлекай длинными "
            "речами, говори о происходящем в колонии.",
            ("rimworldwin64",)),
    Profile("work", "Работа и код",
            "Сейчас пользователь работает (код, терминал, редактор). Говори тихо и по делу, без шуток поверх чужой "
            "сосредоточенности, предлагай помощь только если видишь конкретную проблему.",
            ("code", "devenv", "pycharm64", "idea64", "rider64", "windowsterminal", "powershell", "cmd", "wt", "claude")),
    Profile("stream", "Стрим и монтаж",
            "Сейчас идёт стрим или монтаж видео. Держи живой ритм, говори для зрителей, но не перебивай запись.",
            ("obs64", "obs", "streamlabs obs", "davinciresolve", "resolve", "premiere pro", "vegas")),
    Profile("general", "Обычный режим", "Просто общайся и поддерживай компанию."),
)
_BY_ID = {p.id: p for p in PROFILES}
_STICKY = 120.0
_last = {"id": "general", "ts": 0.0}


def detect(app: str | None, now: float | None = None) -> Profile:
    now = now if now is not None else time.time()
    for p in PROFILES:
        if app and p.matches(app):
            _last.update(id=p.id, ts=now)
            return p
    if now - _last["ts"] < _STICKY:
        return _BY_ID[_last["id"]]
    return _BY_ID["general"]


def current(app: str | None = None) -> Profile:
    """Профиль сейчас: ручной выбор из настроек или автоопределение по активному окну."""
    choice = str(settings.load().get("profile", "auto"))
    if choice in _BY_ID:
        return _BY_ID[choice]
    if app is None:
        try:
            import perception
            app = (perception.foreground() or {}).get("app")
        except Exception:
            app = None
    return detect(app)


def hint() -> str:
    p = current()
    # внутренняя настройка тона: о ней и о смене режима вслух не говорить, это не тема разговора
    return (f"[Внутренняя настройка тона, не тема разговора и не повод что-то сообщать: {p.style} "
            "Никогда не упоминай режимы, профили и их переключение.]")
