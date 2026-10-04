"""Протокол событий: единый журнал всего, что происходит (SQLite: memory/events.db).

Одна строка = одно событие: ts, kind, text, data (JSON). Виды (kind):
  user      — сообщение пользователя
  say       — реплика ассистента
  proactive — решение заговорить самой (в том числе «промолчала»)
  reflex    — быстрая реакция: эмоция и простая команда
  vision    — краткий вывод о том, что на экране (сырой текст экрана не пишется)
  hearing   — краткий вывод о том, что слышно в звуке (расшифровка не пишется)
  game      — событие игры
  telegram  — публикации в Telegram и состояние подключения
  memory    — что она запомнила, осмыслив беседу
  plan      — решения маршрутизатора: могу/не могу, шаги и результаты
  audit     — самоанализ: что нашла и какие выводы сохранила
  approval  — запрос и ответ на разрешение действия
  setting   — смена настроек (голос, зрение, громкость…)
  error     — сбой

Пишут все процессы (ядро, трей, питомец): режим WAL и таймаут ожидания блокировки делают одновременную запись
безопасной. Читает дашборд (/api/events), анализ и сводки. Старые события удаляются по сроку и по числу строк.
Формат был JSONL (events.jsonl): при первом запуске он переносится в базу и файл переименовывается в *.migrated.
Идея хранилища взята из GitS (src/events/store.py).
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

KINDS = ("user", "say", "proactive", "reflex", "vision", "hearing", "game", "telegram", "plan", "memory", "audit",
         "approval", "setting", "error")
KEEP_DAYS = 45
KEEP_ROWS = 300_000
_PRUNE_EVERY = 500            # раз в столько записей проверяем, не пора ли чистить

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    text    TEXT NOT NULL DEFAULT '',
    data    TEXT,
    session TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);
"""

_dir = Path(os.getenv("MEMORY_DIR", "./memory"))
_lock = threading.Lock()
_session = uuid.uuid4().hex[:8]
_ready: set[str] = set()
_counter = [0]


def init(memory_dir: str):
    global _dir
    _dir = Path(memory_dir)


def _db_path() -> Path:
    return _dir / "events.db"


def _connect() -> sqlite3.Connection:
    _dir.mkdir(parents=True, exist_ok=True)
    path = _db_path()
    conn = sqlite3.connect(path, timeout=10)
    if str(path) not in _ready:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _ready.add(str(path))
        _migrate_jsonl(conn)
    return conn


def _migrate_jsonl(conn: sqlite3.Connection):
    """Разовый перенос старого журнала (events.1.jsonl, events.jsonl) в базу."""
    for name in ("events.1.jsonl", "events.jsonl"):
        f = _dir / name
        claimed = f.with_name(name + ".migrating")
        try:
            os.replace(f, claimed)          # атомарно: второй процесс файл уже не найдёт
        except OSError:
            continue
        rows = []
        try:
            for line in claimed.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                rows.append((float(r.get("ts", 0)), str(r.get("kind", "")), str(r.get("text", ""))[:500],
                             json.dumps(r["data"], ensure_ascii=False) if r.get("data") else None, "old"))
            conn.executemany("INSERT INTO events(ts, kind, text, data, session) VALUES (?,?,?,?,?)", rows)
            conn.commit()
            os.replace(claimed, f.with_name(name + ".migrated"))
        except (OSError, sqlite3.Error):
            continue


def _prune(conn: sqlite3.Connection):
    conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
    conn.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (KEEP_ROWS,))
    conn.commit()


def log(kind: str, text: str = "", **data):
    """Записать событие. Никогда не бросает исключений: журнал не должен ронять работу."""
    try:
        with _lock:
            conn = _connect()
            try:
                conn.execute("INSERT INTO events(ts, kind, text, data, session) VALUES (?,?,?,?,?)",
                             (round(time.time(), 2), kind, str(text)[:500],
                              json.dumps(data, ensure_ascii=False, default=str) if data else None, _session))
                conn.commit()
                _counter[0] += 1
                if _counter[0] % _PRUNE_EVERY == 0:
                    _prune(conn)
            finally:
                conn.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass


def _row(r) -> dict:
    rec = {"ts": r[0], "kind": r[1], "text": r[2]}
    if r[3]:
        try:
            rec["data"] = json.loads(r[3])
        except ValueError:
            pass
    return rec


def tail(limit: int = 200, kinds: set | None = None, since: float = 0.0) -> list[dict]:
    """Последние события (старые сверху): не больше limit, только указанных видов, не раньше since."""
    try:
        conn = _connect()
    except (OSError, sqlite3.Error):
        return []
    try:
        sql, args = "SELECT ts, kind, text, data FROM events WHERE ts > ?", [since]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += sorted(kinds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_row(r) for r in reversed(rows)]


def count(kind: str | None = None, since: float = 0.0) -> int:
    try:
        conn = _connect()
        try:
            if kind:
                return conn.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ts>?", (kind, since)).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM events WHERE ts>?", (since,)).fetchone()[0]
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return 0


def export_jsonl(path: str | Path, since: float = 0.0, limit: int = 50_000) -> int:
    """Выгрузить события в JSONL (по строке на событие), чтобы их можно было читать и искать обычными файловыми
    инструментами (агент-аналитик умеет Read/Grep, но не SQL). Возвращает число выгруженных строк."""
    rows = tail(limit=limit, since=since)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)
