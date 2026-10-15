"""Протокол событий на SQLite: запись, выборки, перенос старого JSONL, выгрузка, запись из нескольких потоков."""
import json
import threading
import time

import pytest

import events_log


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(events_log, "_ready", set())
    events_log.init(str(tmp_path))
    return tmp_path


def test_roundtrip_with_data_and_order(log):
    events_log.log("user", "привет")
    events_log.log("say", "здравствуй", emotion="happy")
    rows = events_log.tail(limit=10)
    assert [r["kind"] for r in rows] == ["user", "say"]            # старые сверху
    assert rows[1]["data"] == {"emotion": "happy"} and "data" not in rows[0]


def test_filters_by_kind_since_and_limit(log):
    for i in range(5):
        events_log.log("game", f"событие {i}")
    events_log.log("error", "сбой")
    assert [r["text"] for r in events_log.tail(limit=2, kinds={"game"})] == ["событие 3", "событие 4"]
    assert events_log.tail(kinds={"error"})[0]["text"] == "сбой"
    assert events_log.tail(since=time.time() + 10) == []
    assert events_log.count("game") == 5 and events_log.count() == 6


def test_text_is_trimmed_and_logging_never_raises(log):
    events_log.log("say", "я" * 2000)
    assert len(events_log.tail()[0]["text"]) == 500
    events_log.log("say", "объект", weird=object())               # непереводимое в JSON не роняет запись
    assert events_log.count() == 2


def test_old_jsonl_is_migrated_once(tmp_path, monkeypatch):
    old = [{"ts": 100.0, "kind": "user", "text": "из старого журнала"},
           {"ts": 101.0, "kind": "game", "text": "событие", "data": {"x": 1}}]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(r) for r in old) + "\nне json\n", encoding="utf-8")
    monkeypatch.setattr(events_log, "_ready", set())
    events_log.init(str(tmp_path))
    rows = events_log.tail()
    assert [r["text"] for r in rows] == ["из старого журнала", "событие"] and rows[1]["data"] == {"x": 1}
    assert not (tmp_path / "events.jsonl").exists() and (tmp_path / "events.jsonl.migrated").exists()
    monkeypatch.setattr(events_log, "_ready", set())               # «перезапуск»: повторного переноса нет
    assert events_log.count() == 2


def test_export_jsonl(log, tmp_path):
    events_log.log("plan", "выполнено", steps=[1])
    out = tmp_path / "reports" / "ev.jsonl"
    assert events_log.export_jsonl(out) == 1
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["data"] == {"steps": [1]}


def test_parallel_writers(log):
    def work(n):
        for i in range(25):
            events_log.log("reflex", f"{n}-{i}")
    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert events_log.count("reflex") == 150
