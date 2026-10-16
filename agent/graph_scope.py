"""Области памяти-графа (ТЗ 04, KB-12, KB-13): к какой области относится факт и в каких областях искать.

Группы графа: `<id персонажа>` - область `global` (люди, отношения, жизнь, предпочтения; все прежние данные) и
`<id>:<область>` - область профиля или модуля (ТЗ 04c, WM-4; тема профиля - его `memory_scopes`). Без графа и без классификатора
ничего не меняется: пишется и ищется в `global`.

Запись: классификатор внимания (контракт `Classifier`) отвечает на вопрос `scope` с вариантами «область профиля / global»;
при сомнении (нет ответа, низкая уверенность, неизвестный вариант) - `global`. Чтение: `global` + области активного профиля;
чужая область добавляется, только если в реплике названа сущность, которая есть в графе лишь в чужой области."""
import re

GLOBAL = "global"
GLOBAL_LABEL = "people, relationships, the user's own life, feelings, preferences, everyday matters"
MIN_CONF = 0.6
MAX_FOREIGN = 2


def scope_questions(scopes: list[str], describe) -> dict:
    """Вопрос классификатору: `describe(область)` - описание темы области (для профиля - его описание)."""
    options = {s: describe(s) for s in scopes if s and s != GLOBAL}
    options[GLOBAL] = GLOBAL_LABEL
    return {"scope": {"type": "choice", "options": options,
                      "instructions": "Which area does this fact belong to?"}}


def parse_scope(answers: dict | None, allowed: list[str], min_conf: float = MIN_CONF) -> str:
    """Область по ответу классификатора; сомнение - `global`."""
    a = (answers or {}).get("scope")
    if not isinstance(a, dict):
        return GLOBAL
    try:
        conf = float(a.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    value = str(a.get("answer") or "")
    return value if value in allowed and conf >= min_conf else GLOBAL


def make_scope_of(classifier, scopes_of, describe, *, timeout: float = 2.0):
    """async (text) -> область. scopes_of() - области активного профиля; describe(область) - её описание."""
    async def scope_of(text: str) -> str:
        scopes = [s for s in scopes_of() if s and s != GLOBAL]
        if not scopes or classifier is None:
            return GLOBAL
        try:
            answers = await classifier.classify(text, scope_questions(scopes, describe), timeout=timeout)
        except Exception:
            return GLOBAL
        return parse_scope(answers, scopes)
    return scope_of


def entity_candidates(query: str) -> list[str]:
    """Слова реплики не короче трёх букв, которые могут быть именем узла (в нижнем регистре; граф сравнивает точное имя)."""
    words = re.findall(r"[\w'-]{3,}", query or "", re.UNICODE)
    return list(dict.fromkeys(w.lower() for w in words))[:12]
