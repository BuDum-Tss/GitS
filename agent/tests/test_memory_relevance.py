"""Контекст ответа не раздувается: записи о пользователе вне темы разговора не идут в промпт, кроме базовых."""
import time

from memory_store import BASE_MAX, MemoryStore


def _store(tmp_path, items):
    s = MemoryStore(str(tmp_path))
    data = s._load()
    now = time.time()
    data["items"] = [{"id": f"i{n}", "kind": kind, "text": text, "importance": imp, "ts": now, "last_used": now, "uses": 0, "due": None}
                     for n, (kind, text, imp) in enumerate(items)]
    s._save(data)
    return s


def test_off_topic_user_facts_are_not_in_context(tmp_path):
    s = _store(tmp_path, [("user", "Любит зелёный чай по утрам", 3), ("user", "Работает над проектом про ракеты", 4),
                          ("user", "Есть кошка по имени Муся", 3)])
    assert s.relevant("как дела с погодой в Казани") == []
    assert s.context_text("как дела с погодой в Казани") == ""


def test_on_topic_fact_is_in_context(tmp_path):
    s = _store(tmp_path, [("user", "Любит зелёный чай по утрам", 3), ("user", "Есть кошка по имени Муся", 3)])
    texts = [i["text"] for i in s.relevant("а что насчёт чай сегодня")]
    assert texts == ["Любит зелёный чай по утрам"]


def test_only_a_few_base_facts_come_without_a_topic(tmp_path):
    s = _store(tmp_path, [("user", f"Базовый факт номер {n} про разное", 5) for n in range(6)] + [("user", "Мелкий факт про носки", 2)])
    got = s.relevant("совсем другая тема")
    assert len(got) == BASE_MAX and all(i["importance"] == 5 for i in got)
