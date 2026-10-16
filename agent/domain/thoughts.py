"""Мысли как краткосрочная память (чистые решения, без ввода-вывода).

Мысль рождается при размышлении (`app/reflection.py`) со стадией `short` и оценкой важности 1-5, которую даёт сама модель.
Дальше `plan` решает по возрасту и важности: важное переносится в долговременную память (`long`), мелкое и устаревшее
забывается (`dropped`). Пустые и служебные размышления (лимиты, ошибки, «ничего не делать») в журнал мыслей не попадают
(`worth_keeping`)."""

SHORT, LONG, DROPPED = "short", "long", "dropped"
IMPORTANCE_MIN, IMPORTANCE_MAX, IMPORTANCE_DEFAULT = 1, 5, 2
PROMOTE_AT = 4                    # важность, с которой мысль уходит в долговременную память
KEEP_MIN_IMPORTANCE = 2           # мысль без намерений и ниже этой важности не сохраняется вовсе
MIN_AGE_S = 600.0                 # столько мысль «отлёживается» в краткосрочной памяти до оценки переноса
TTL_S = 24 * 3600.0               # неперенесённая мысль забывается через сутки


def clean_importance(value) -> int:
    """Важность из ответа модели: целое 1-5; нечисловое и булево дают значение по умолчанию."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return IMPORTANCE_DEFAULT
    return max(IMPORTANCE_MIN, min(IMPORTANCE_MAX, int(round(value))))


def worth_keeping(*, status: str, text: str, intents: list[dict], importance: int) -> bool:
    """Стоит ли записывать размышление в журнал. Не стоит: оборвано лимитом или ошибкой, мысли нет, либо ничего не решено
    (все намерения `none`) и важность низкая."""
    if status != "done" or not text.strip():
        return False
    acted = any(i.get("kind") not in (None, "none") for i in intents)
    return acted or importance > KEEP_MIN_IMPORTANCE


def plan(thoughts: list[dict], now: float) -> tuple[list[dict], list[dict]]:
    """(перенести, забыть) среди мыслей стадии `short`. Перенос: важность не ниже PROMOTE_AT и мысль старше MIN_AGE_S.
    Забыть: старше TTL_S, либо важность низкая (1) и старше MIN_AGE_S."""
    promote, drop = [], []
    for t in thoughts:
        if t.get("stage", SHORT) != SHORT:
            continue
        age = now - float(t.get("ts") or now)
        imp = clean_importance(t.get("importance"))
        if imp >= PROMOTE_AT and age >= MIN_AGE_S:
            promote.append(t)
        elif age >= TTL_S or (imp <= IMPORTANCE_MIN and age >= MIN_AGE_S):
            drop.append(t)
    return promote, drop


def memory_text(thought: dict) -> str:
    """Что записать в память: явное «запомни» из намерения, иначе сама мысль."""
    for i in thought.get("intents") or []:
        if i.get("kind") == "remember" and str(i.get("text") or "").strip():
            return str(i["text"]).strip()
    return str(thought.get("text") or "").strip()
