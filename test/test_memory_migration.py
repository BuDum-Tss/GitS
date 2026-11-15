"""Разовый перенос старой памяти memories.json в заметки эпизодов и задачи; сроки планов; старых слоёв памяти больше нет."""
import json
import time

import pytest

from services.memory_service import episodes as dom
from services.memory_service import plan_due
from services.memory_service.migration import BACKUP, FILE, migrate

from test_episode_memory import _Tasks, build


def test_plan_due_follows_words_in_the_text():
    ts = 1_800_000_000.0
    assert plan_due.due_of_text("экзамен завтра", ts) > ts
    assert plan_due.due_of_text("экзамен завтра", ts) < plan_due.due_of_text("экзамен послезавтра", ts)
    assert plan_due.due_of_text("когда-нибудь убраться", ts) == ts + plan_due.UNDATED_DAYS * 86400


def _old_file(folder, now):
    items = [
        {"id": "a", "kind": "user", "text": "Любит зелёный чай по утрам", "importance": 5, "ts": now, "due": None},
        {"id": "b", "kind": "user", "text": "Видел забавную картинку сегодня", "importance": 2, "ts": now, "due": None},
        {"id": "c", "kind": "self", "text": "Я обещала шутить реже", "importance": 4, "ts": now, "due": None},
        {"id": "d", "kind": "episode", "text": "Мы вместе чинили колонию", "importance": 4, "ts": now, "due": None},
        {"id": "e", "kind": "plan", "text": "Сдать отчёт", "importance": 4, "ts": now, "due": now + 86400},
        {"id": "f", "kind": "plan", "text": "Давний просроченный план", "importance": 4, "ts": now - 90 * 86400, "due": now - 80 * 86400},
    ]
    (folder / FILE).write_text(json.dumps({"items": items, "meta": {"consolidated_ts": 123.0}}), encoding="utf-8")


async def test_migration_moves_only_important_to_notes_and_keeps_a_backup(tmp_path):
    now = time.time()
    _old_file(tmp_path, now)
    w, tasks = build(tmp_path), _Tasks()
    done = await migrate(tmp_path, w.memory, tasks, clock=lambda: now)
    assert done == {"owner": 3, "plans": 1, "dropped": 2}
    assert sorted(n["text"] for n in w.store.of_kind(dom.NOTE)) == sorted(
        ["Любит зелёный чай по утрам", "Я обещала шутить реже", "Мы вместе чинили колонию"])
    assert [p[1] for p in tasks.plans] == ["Сдать отчёт"]
    assert not (tmp_path / FILE).exists() and (tmp_path / BACKUP).exists()
    assert json.loads((tmp_path / "memory_meta.json").read_text(encoding="utf-8")) == {"consolidated_ts": 123.0}
    assert await migrate(tmp_path, w.memory, tasks) == {}                                          # повторно ничего не делает


async def test_migration_ignores_unreadable_file(tmp_path):
    (tmp_path / FILE).write_text("не json", encoding="utf-8")
    assert await migrate(tmp_path, build(tmp_path).memory, _Tasks()) == {}
    assert (tmp_path / FILE).exists()


@pytest.mark.parametrize("name", ["memory_store", "knowledge", "lessons", "graph", "fact_store"])
def test_old_memory_layers_are_gone(name):
    import importlib
    for prefix in ("", "services.memory_service."):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(prefix + name)
