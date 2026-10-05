"""Батарейка: сколько токенов Claude у агента осталось в текущем окне лимита.

У подписки Claude Code лимит по окну времени (около 5 часов). Токены каждого запуска (вход, выход, запись в кэш;
чтение кэша почти бесплатно и не считается) копятся в журнале `memory/claude_battery.json`. Заряд = доля ёмкости,
не потраченная за последнее окно. Ёмкость задаёт настройка `claude_token_budget`: точного числа подписка не
отдаёт, поэтому оно подбирается по опыту. Сообщение «you've hit your session limit» (claude_quota) обнуляет заряд
сразу, как бы ни показывал счётчик.

Когда заряд на нуле, агент уходит в профиль «Сон» (modes.py) и просыпается, когда окно освободится.
Журнал общий для процессов: агент игры присылает свой расход на `POST /api/claude_usage` дашборда.
"""
import json
import os
import time
from pathlib import Path

import claude_quota
import settings

WINDOW_S = 5 * 3600
DEFAULT_CAPACITY = 3_000_000


def _file() -> Path:
    base = os.getenv("MEMORY_DIR") or str(Path(__file__).resolve().parent / "memory")
    return Path(base) / "claude_battery.json"


def _load(now: float) -> list[list]:
    try:
        rows = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:
        return []
    return [r for r in rows if isinstance(r, list) and len(r) == 3 and now - r[0] < WINDOW_S]


def record(tokens: int, source: str = "") -> None:
    """Записать расход одного запуска Claude."""
    if tokens <= 0:
        return
    now = time.time()
    rows = _load(now) + [[now, int(tokens), source[:30]]]
    try:
        _file().write_text(json.dumps(rows[-2000:]), encoding="utf-8")
    except OSError:
        pass


def tokens_of(usage: dict | None) -> int:
    """Токены из поля usage ответа Claude: вход, выход и запись в кэш (чтение кэша не считается)."""
    u = usage or {}
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))


def record_result(message, source: str = "") -> None:
    """Записать расход по ResultMessage из Claude Agent SDK."""
    record(tokens_of(getattr(message, "usage", None)), source)


def capacity() -> int:
    try:
        return max(100_000, int(settings.load().get("claude_token_budget") or DEFAULT_CAPACITY))
    except (TypeError, ValueError):
        return DEFAULT_CAPACITY


def status(now: float | None = None) -> dict:
    """level 0..1, потрачено, ёмкость, спит ли агент и когда заряд вернётся.

    Расход выходит из окна по одному: каждая запись «освобождается» через 5 часов после запуска.
    next_release_at: когда вернётся ближайшая порция, full_at: когда окно опустеет совсем,
    wake_at: когда заряд снова станет больше нуля (если сейчас сон)."""
    now = now if now is not None else time.time()
    rows = _load(now)
    used, cap = sum(r[1] for r in rows), capacity()
    blocked = claude_quota.blocked()
    level = 0.0 if blocked else max(0.0, 1.0 - used / cap)
    wake_at = None
    if blocked:
        wake_at = claude_quota.blocked_until()
    elif level <= 0.0:
        left = used
        for ts, tokens, _src in rows:
            left -= tokens
            if left < cap:
                wake_at = ts + WINDOW_S
                break
    return {"level": round(level, 3), "used": used, "capacity": cap, "blocked": blocked,
            "sleeping": level <= 0.0, "wake_at": wake_at,
            "resets_at": wake_at or (rows[0][0] + WINDOW_S if rows else None),
            "next_release_at": rows[0][0] + WINDOW_S if rows else None,
            "next_release_tokens": rows[0][1] if rows else 0,
            "full_at": rows[-1][0] + WINDOW_S if rows else None}
