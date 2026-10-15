"""Лимит Claude: распознать «you've hit your session limit», не жечь запросы впустую и сказать об этом честно.

Игровые ходы, планирование, анализ и помощник работают на подписке Claude Code, у неё есть лимит сессии.
Когда он исчерпан, Claude отвечает текстом «You've hit your session limit · resets 7:40pm». Раньше бот принимал
это за реплику и продолжал стучаться. Теперь: ошибка запоминается (memory/claude_quota.json, видят все процессы),
до сброса новые запуски пропускаются, а в её контексте стоит строка о недоступности Claude.
"""
import json
import os
import re
import time
from pathlib import Path

import events_log

_RESET = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
_MARKS = ("session limit", "usage limit", "rate limit reached", "limit reached")
_FALLBACK_WAIT = 30 * 60


def _file() -> Path:
    base = os.getenv("MEMORY_DIR") or str(Path(__file__).resolve().parent / "memory")
    return Path(base) / "claude_quota.json"


def looks_like_limit(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _MARKS) and len(low) < 400


def _parse_reset(text: str) -> float | None:
    m = _RESET.search(text or "")
    if not m:
        return None
    hour, minute, ap = int(m.group(1)) % 12, int(m.group(2) or 0), m.group(3).lower()
    if ap == "pm":
        hour += 12
    now = time.time()
    lt = time.localtime(now)
    t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
    if t <= now:
        t += 86400
    return t + 60   # минута запаса


def note(text: str) -> bool:
    """Если текст похож на ошибку лимита, запомнить до какого времени Claude недоступен. True, если это лимит."""
    if not looks_like_limit(text):
        return False
    until = _parse_reset(text) or (time.time() + _FALLBACK_WAIT)
    first = until > blocked_until() + 60
    try:
        _file().write_text(json.dumps({"until": until, "text": text[:200], "ts": time.time()}), encoding="utf-8")
    except OSError:
        pass
    if first:
        import usage_report
        usage_report.push(limit_text=text[:200])      # батарейка ассистентки обнуляется сразу
        events_log.log("error", f"лимит Claude исчерпан до {time.strftime('%H:%M', time.localtime(until))}: "
                                "игровые ходы, анализ и помощник на паузе")
    return True


def blocked_until() -> float:
    try:
        return float(json.loads(_file().read_text(encoding="utf-8")).get("until", 0))
    except Exception:
        return 0.0


def blocked() -> bool:
    return blocked_until() > time.time()


def until_text() -> str:
    return time.strftime("%H:%M", time.localtime(blocked_until()))
