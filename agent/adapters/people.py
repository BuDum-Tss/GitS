"""PeopleDirectory поверх профилей людей (`relationships.Relationships`).

`get_rel` возвращает текущий объект профилей: он может смениться (переключение персонажа, тесты), поэтому адаптер
читает его при каждом вызове. Пока профилей нет (None), методы ничего не делают и отдают пустые значения.
"""
from typing import Any, Callable


class RelationshipsAdapter:
    def __init__(self, get_rel: Callable[[], Any]):
        self._get_rel = get_rel

    def ensure_person(self, key: str, name: str, source: str = "", **extra) -> bool:
        fn = getattr(self._get_rel(), "ensure_person", None)
        return bool(fn(key, name, source=source, **extra)) if fn is not None else False

    def note(self, key: str, text: str) -> None:
        fn = getattr(self._get_rel(), "note", None)
        if fn is not None:
            fn(key, text)

    def get(self, subject: str) -> dict | None:
        rel = self._get_rel()
        return rel.get(subject) if rel is not None else None

    def describe(self, subject: str) -> str:
        rel = self._get_rel()
        return rel.describe(subject) if rel is not None else ""

    def adjust(self, subject: str, delta: float, reason: str) -> float:
        rel = self._get_rel()
        return rel.adjust(subject, delta, reason) if rel is not None else 0.0

    def sync_colonists(self, pawns: list[dict]) -> None:
        rel = self._get_rel()
        if rel is not None:
            rel.sync_colonists(pawns)

    def mark_dead(self, subject: str, cause: str = "") -> None:
        rel = self._get_rel()
        if rel is not None:
            rel.mark_dead(subject, cause)


def people_for_companion(companion):
    """PeopleDirectory для менеджера внимания: публичное свойство `people` компаньона; у заглушек без него - профили
    из приватного поля `_rel` (читается при каждом вызове, поэтому подмена поля после создания работает)."""
    people = getattr(companion, "people", None)
    if people is not None:
        return people
    return RelationshipsAdapter(lambda: getattr(companion, "_rel", None))
