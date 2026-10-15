"""Журнал для эпизодов, настроения и уроков (ТЗ 24, TRC-3, TRC-5, TRC-6): эмоция с интенсивностью, метки ошибок, решения.

Всё пишется через events_log.log_nowait (на цикле событий коммит SQLite не ждём). Трасса (latency_trace) подставляет
trace_id текущего обращения, если он есть. Виды:
  emotion   - метка, интенсивность 0..3, причина (id события или трассы), персонаж; bg=true для фоновых меток
  mistake   - тип ошибки (MISTAKE_TYPES), текст, refs (ссылки на события: их ts)
  decision  - решение диспетчера: маршрут, источник правил и версия, уверенность, эшелон, trace_id
Поведение агента этот модуль не меняет: он только записывает.
"""
import re

import events_log
import latency_trace

BACKGROUND_EMOTIONS = ("idle", "thinking", "looking", "question")     # фоновые метки: пишутся, но помечаются bg
MISTAKE_TYPES = ("correction", "denied", "tool_failed", "plan_failed", "bad_rating", "misroute", "run_lost")
RULES_VERSIONS = {"attention": "R1-R4,D0-D4/1", "conversation": "intent+router/1", "focus": "FOC/1", "router": "plan/1"}
INTENSITY_MAX = 3
DECISIONS_SCAN = 1000         # сколько решений просматриваем, когда выборка идёт по trace_id (фильтр после выборки)

# токены и пароли не попадают в журнал: текст ошибок может нести адрес с токеном бота или ключ в строке запроса
_SECRETS = (re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),
            re.compile(r"(?i)(token|api[_-]?key|password|passwd|secret|пароль)(\s*[=:]\s*)\S+"))


def _clean(text, limit: int) -> str:
    out = str(text)
    out = _SECRETS[0].sub("[токен]", out)
    out = _SECRETS[1].sub(lambda m: m.group(1) + m.group(2) + "[скрыто]", out)
    return out[:limit]


def clamp_intensity(value) -> int:
    """Интенсивность 0..3 из ответа модели; если её нет или она не разбирается, 1 (ТЗ 24, TRC-3)."""
    if isinstance(value, bool):
        return 1
    try:
        v = int(float(value))
    except (TypeError, ValueError, OverflowError):      # JSON умеет «Infinity»: int(inf) не число
        return 1
    return max(0, min(INTENSITY_MAX, v))


def _trace_id() -> str | None:
    t = latency_trace.current()
    return t.id if t is not None else None


def emotion(emotion_label: str, intensity, reason: str, character: str = "", **data) -> None:
    """Эмоция. Причина - id трассы обращения, если он есть, иначе `reason` (имя события, например «chat»)."""
    tid = data.pop("trace_id", None) or _trace_id()
    events_log.log_nowait("emotion", str(emotion_label), emotion=str(emotion_label), intensity=clamp_intensity(intensity),
                          reason=str(tid or reason)[:80], character=character or None,
                          bg=emotion_label in BACKGROUND_EMOTIONS, trace_id=tid, **data)


def mistake(kind: str, text: str, refs: list | None = None, **data) -> None:
    """Метка ошибки (TRC-5). Неизвестный вид не пишется: список видов задан ТЗ."""
    if kind not in MISTAKE_TYPES:
        return
    events_log.log_nowait("mistake", _clean(text, 300), type=kind, refs=[r for r in (refs or []) if r is not None][:10],
                          trace_id=data.pop("trace_id", None) or _trace_id(),
                          **{k: _clean(v, 300) if isinstance(v, str) else v for k, v in data.items()})


def decision(route: str, source: str, text: str = "", *, confidence=None, echelon: str = "rules", **data) -> None:
    """Решение диспетчера (TRC-6): маршрут, источник правил, уверенность, эшелон и версия правил."""
    events_log.log_nowait("decision", _clean(text, 200), route=str(route), source=str(source),
                          confidence=round(float(confidence), 3) if isinstance(confidence, (int, float)) else None,
                          echelon=echelon, rules_version=RULES_VERSIONS.get(source, source),
                          trace_id=data.pop("trace_id", None) or _trace_id(), **data)


# ── поправки пользователя (TRC-5) ──────────────────────────────────────────────

_STRONG = re.compile(r"^\s*(нет[,!.\s]+)?(это )?(не так|не то|неправильно|не верно|неверно|ты (ошиб\w*|неправ\w*)"
                     r"|я (же )?(сказал\w*|говорил\w*|просил\w*) (не|другое)|не то что я)", re.IGNORECASE)
_WEAK = re.compile(r"^\s*(нет|неа|не)\b", re.IGNORECASE)


def correction_level(text: str) -> str:
    """yes - поправка по шаблону; maybe - начало похоже на поправку, нужна проверка моделью; no - нет."""
    if _STRONG.search(text or ""):
        return "yes"
    if _WEAK.match(text or ""):
        return "maybe"
    return "no"


def decisions(limit: int = 100, trace_id: str | None = None) -> list[dict]:
    """Выборка решений диспетчера для разметки (как appraisal_log.recent, ТЗ 24 TRC-6): новые сверху, по trace_id при нужде."""
    limit = max(1, min(int(limit), 1000))
    # по trace_id фильтруем после выборки: сначала берём окно побольше, иначе старое решение не найти
    rows = events_log.tail(limit=DECISIONS_SCAN if trace_id else limit, kinds={"decision"})
    out = [{"ts": r["ts"], **(r.get("data") or {}), "text": r.get("text", "")} for r in reversed(rows)]
    if trace_id:
        out = [r for r in out if r.get("trace_id") == trace_id][:limit]
    return out
