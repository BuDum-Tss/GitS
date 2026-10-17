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


def test_store_keeps_only_what_she_deems_important(tmp_path):
    """Мелочь (важность 1-3) в долгую память не пишется: память хранит только важное (4-5)."""
    from memory_store import MIN_IMPORTANCE, MemoryStore
    store = MemoryStore(str(tmp_path))
    assert MIN_IMPORTANCE == 4
    assert store.add("user", "Сегодня пользователь устал", importance=3) is False
    assert store.add("episode", "Диалог был наполнен колкостями", importance=2) is False
    assert store.add("user", "Пользователя зовут Аня", importance=4) is True
    assert [i["text"] for i in store.all()] == ["Пользователя зовут Аня"]


def test_capture_does_not_store_episodes_from_the_dialogue():
    """События жизни ведёт журнал эпизодов; разговорный разбор пишет только user, self и plan."""
    from app.memory_capture import MemoryCapture

    class Writer:
        def __init__(self):
            self.kinds = []

        def add_fact(self, text, kind="user", importance=3, due_days=None):
            self.kinds.append(kind)
            return True

    class Host:
        _memory_writer = Writer()

    got = MemoryCapture(Host()).store_memories({"memories": [
        {"kind": "episode", "text": "Беседа закончилась колкостями", "importance": 5},
        {"kind": "user", "text": "Пользователя зовут Аня", "importance": 5}]})
    assert got == ["Пользователя зовут Аня"] and Host._memory_writer.kinds == ["user"]
