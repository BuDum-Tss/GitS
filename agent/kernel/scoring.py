"""Решение по событию внимания: срочность модуля → уровень D0-D4 (чистые функции, без ввода-вывода).

Срочность 0..5 приходит от самого модуля (`urgency_hint`, объявленная в манифесте) либо ставится по обращению к ней. Классификатора,
весов и порогов нет: событие срочности 4+ говорится сразу, остальное копится во входящей очереди (сводке) и уходит модели пачкой.
"""

DECISION_NAMES = ("D0", "D1", "D2", "D3", "D4")
SAY_URGENCY = 4          # с этой срочности событие говорится сразу, ниже оно копится во входящей очереди
MAX_PRIORITY = 100.0


NOISE_KINDS = ("desktop.activity", "desktop.app_changed")    # что делает пользователь на компьютере: вкладки, окна, описание экрана
NO_MODULE_ENVS = ("assistant", "sleep")


def is_assistant_noise(env: str | None, module: str, kind: str) -> bool:
    """Пока она в среде модуля (играет), какую вкладку открыл пользователь и какое окно сменилось, ей не важно: это дело ассистента,
    а не игры. События модуля `desktop` вида активности и смены окна в такой среде выбрасываются (срочность 0)."""
    return bool(env) and env not in NO_MODULE_ENVS and module == "desktop" and kind in NOISE_KINDS


def priority(urgency: float) -> float:
    """Приоритет 0..100 для очереди речи и сортировки сводки: срочность × 20."""
    return max(0.0, min(MAX_PRIORITY, float(urgency) * 20.0))


def decide(urgency: int, quiet: bool, away: bool) -> str:
    """D0 (срочность 0, выбросить), D1 (во входящую очередь), D3 (сказать), D4 (сказать и поставить реакцию). «Не отвлекать»
    гасит всё, кроме срочности 5; отошедшему пользователю говорить некому, всё идёт во входящую очередь."""
    if urgency <= 0:
        return "D0"
    if urgency < SAY_URGENCY or (quiet and urgency < 5) or away:
        return "D1"
    return "D4" if urgency >= 5 else "D3"
