"""Контекст реплики: какой запрос ушёл в модель, когда родилась фраза агента (смотреть в дашборде, кнопка «Контекст» у реплики).

Генератор ответа перед вызовом модели кладёт запрос в `begin` (ожидающий), а `Companion._record_say`, когда реплика попала в
диалог, зовёт `attach(text)` и запрос привязывается к тексту. Хранится кольцо последних записей в памяти процесса (на диск
не пишется: в контексте бывают память и экран). Поиск по тексту реплики: `find`. Реплики без модели (поправка, ответ на
разрешение) запроса не имеют, и дашборд говорит, что контекста нет."""
import threading
import time
from collections import deque

RING = 100
PENDING_TTL = 180.0          # ожидающий запрос старше этого не привязывается к чужой реплике
FIELD_MAX = 60000            # потолок длины одного поля: контекст не должен раздувать ответ API

_lock = threading.Lock()
_entries: deque = deque(maxlen=RING)
_pending: dict | None = None


def _clip(value) -> str:
    text = str(value or "")
    return text if len(text) <= FIELD_MAX else text[:FIELD_MAX] + "\n…(обрезано)"


def begin(kind: str, system: str, user: str, **params) -> None:
    """Запрос к модели ушёл: запомнить как ожидающий. kind: converse | fact | quick."""
    global _pending
    with _lock:
        _pending = {"kind": kind, "system": _clip(system), "user": _clip(user), "params": params, "t": time.time()}


def drop() -> None:
    """Модель не ответила или промолчала: ожидающий запрос не нужен."""
    global _pending
    with _lock:
        _pending = None


def attach(text: str, ts: float | None = None) -> bool:
    """Реплика записана в диалог: привязать к ней ожидающий запрос. False, если запроса не было."""
    global _pending
    text = (text or "").strip()
    now = time.time()
    with _lock:
        pend, _pending = _pending, None
        if not text or pend is None or now - pend["t"] > PENDING_TTL:
            return False
        _entries.append({"text": text, "ts": ts or now, "kind": pend["kind"], "system": pend["system"],
                         "user": pend["user"], "params": pend["params"], "asked_ts": pend["t"]})
        return True


def find(text: str) -> dict | None:
    """Последняя запись для этой реплики (точное совпадение текста; запасной путь: реплика входит в запись или наоборот)."""
    text = (text or "").strip()
    if not text:
        return None
    with _lock:
        items = list(_entries)
    for e in reversed(items):
        if e["text"] == text:
            return dict(e)
    for e in reversed(items):
        if text in e["text"] or e["text"] in text:
            return dict(e)
    return None


def recent(n: int = 20) -> list[dict]:
    """Краткий список последних записей (без полных запросов): для проверки и отладки."""
    with _lock:
        items = list(_entries)[-n:]
    return [{"text": e["text"], "ts": e["ts"], "kind": e["kind"]} for e in items]


def clear() -> None:
    global _pending
    with _lock:
        _entries.clear()
        _pending = None
