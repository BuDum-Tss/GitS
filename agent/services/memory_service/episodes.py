"""Эпизоды и режим сна: чистые решения без ввода-вывода (ТЗ 27, EPI-4, EPI-6, EPI-10).

Журнал событий остаётся источником правды. Выбор ярких моментов лежит в `moments.py`; здесь только функции над данными эпизодов:
  check_summary   проверка текста эпизода от первого лица по источникам: ссылки на события, имена и числа, число предложений
  decide_sleep    пора ли спать (простой, расписание, «догоняющий» сон) и с каким бюджетом
  diary_days      за какие сутки пора писать дневник
  compress_days   какие сутки пора сжать в сводку
Параметры приходят настройками; часов и настроек здесь нет.
"""
import re
from dataclasses import dataclass

# ── статусы ────────────────────────────────────────────────────────────────────────────────────────

PENDING, SUMMARIZED, EMBEDDED, FAILED, SKIPPED = "pending", "summarized", "embedded", "failed", "skipped"
LIFE, CANON, NOTE = "life", "canon", "note"                      # виды эпизодов (см. episode_store)
STATUSES = (PENDING, SUMMARIZED, EMBEDDED, FAILED, SKIPPED)      # skipped: тусклый эпизод отброшен живым циклом (очередь переполнена)

MEMORY_MODULE = "memory"            # id внутреннего модуля памяти в менеджере фокуса и в журнале внимания


@dataclass(frozen=True)
class Rec:
    """Событие-источник эпизода для проверки резюме."""
    id: int
    ts: float
    kind: str
    text: str


SUMMARY_SENTENCES = (2, 8)           # допустимое число предложений текста эпизода (строгая проверка)
TEXT_CHARS = (20, 1500)              # допустимая длина текста у форка (проверка облегчённая)


def index_text(title: str, text: str) -> str:
    """Что индексируется у эпизода (и по словам, и вектором): `заголовок. текст`, целиком и без других полей."""
    title = title.strip().rstrip(".")
    return f"{title}. {text}".strip() if title else text.strip()


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def _num(v, default: float = 0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


# ── проверка резюме (EPI-6) ────────────────────────────────────────────────────────────────────────

_SENT = re.compile(r"[^.!?…]+[.!?…]*")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]*")
_NUM = re.compile(r"\d+(?:[.,:]\d+)*")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.findall(text or "") if re.search(r"\w", s)]


def _stem(word: str) -> str:
    """Основа для сравнения падежных форм (Аня, Ане, Аню): отрезаем до двух букв окончания, но оставляем не меньше двух."""
    w = word.lower().replace("ё", "е")
    return w[:max(2, len(w) - 2)]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def names_in(summary: str) -> list[str]:
    """Слова с заглавной буквы не в начале предложения: имена и названия."""
    out = []
    for s in sentences(summary):
        words = _WORD.findall(s)
        for w in words[1:]:
            if w[0].isupper() and len(w) > 2:
                out.append(w)
    return out


def unsupported(text: str, source: str, allow=()) -> list[str]:
    """Замечания: имена и числа из `text`, которых нет в `source` (и в `allow`: имя персонажа, владельца)."""
    corpus = _norm(source)
    words = set(re.findall(r"[a-zа-я\-]+", corpus))
    allowed = {_stem(a) for a in allow if a}
    known = lambda st: st in allowed or any(w.startswith(st) for w in words)               # noqa: E731
    problems = []
    missing = [w for w in names_in(text) if not known(_stem(w))]
    if missing:
        problems.append("имён нет в событиях: " + ", ".join(sorted(set(missing))[:5]))
    raw = corpus.replace(",", ".")
    lost = [n for n in (x.replace(",", ".") for x in _NUM.findall(text)) if n not in raw]
    if lost:
        problems.append("чисел нет в событиях: " + ", ".join(sorted(set(lost))[:5]))
    return problems


def check_summary(parsed, recs: list[Rec], *, allow: tuple | list | set = (), sentences_range: tuple = SUMMARY_SENTENCES,
                  trusted: bool = False) -> tuple[dict | None, list[str]]:
    """Ответ модели -> (очищенный dict, замечания). Пустые замечания - эпизод принят.

    Эпизод это заголовок `title` и свободный текст `text` от первого лица: что произошло, на что она обратила внимание, как к
    этому относится; отдельных полей нет. Строгая проверка (запасной писатель): число предложений текста; ссылки `refs` на
    существующие номера событий (хотя бы одна); имена и числа из текста встречаются в событиях-источниках (или в `allow`: имя
    персонажа, владельца). `trusted=True` (писал форк её сессии, у которого в контексте весь разговор, а не только события момента):
    заголовок и текст не пусты, длина текста разумная; ссылки, имена и числа не сверяются."""
    if not isinstance(parsed, dict):
        return None, ["ответ не JSON-объект"]
    problems: list[str] = []
    title = " ".join(str(parsed.get("title") or "").split())[:160]
    text = " ".join(str(parsed.get("text") or "").split())
    if not title:
        problems.append("нет заголовка title")
    if not text:
        problems.append("нет текста text")
    ids = {r.id for r in recs}
    refs: list[int] = []
    for x in parsed.get("refs") if isinstance(parsed.get("refs"), list) else []:
        try:
            refs.append(int(x))
        except (TypeError, ValueError):
            problems.append(f"ссылка {x!r} не номер события")
    if text and trusted:
        lo, hi = TEXT_CHARS
        if not lo <= len(text) <= hi:
            problems.append(f"текст text длиной {len(text)} знаков, нужно от {lo} до {hi}")
    elif text:
        bad = sorted({x for x in refs if x not in ids})
        if bad:
            problems.append("ссылки на несуществующие события: " + ", ".join(str(x) for x in bad[:5]))
        if not [x for x in refs if x in ids]:
            problems.append("нет ссылок refs на события-источники")
        lo, hi = sentences_range
        n = len(sentences(text))
        lo = min(lo, max(1, len(recs) - 1))
        if n < lo or n > hi:
            problems.append(f"в тексте {n} предложений, нужно от {lo} до {hi}")
        problems += unsupported(text, " ".join(r.text for r in recs), allow)
    clean = {"title": title, "text": text, "refs": sorted({x for x in refs if x in ids}),
             "participants": _strings(parsed.get("participants"), 8, 60)}
    return (clean if not problems else None), problems


def _strings(v, n: int, size: int) -> list[str]:
    return [str(x).strip()[:size] for x in (v if isinstance(v, list) else []) if str(x).strip()][:n]


# ── сон ────────────────────────────────────────────────────────────────────────────────────────────

FORCED = "forced"                    # принудительный сон: будит только явная команда или переход в другое состояние


@dataclass(frozen=True)
class SleepPlan:
    mode: str                        # idle | scheduled | catchup | forced (по кнопке: ввод и разговор не будят)
    budget_tokens: int | None        # None - без ограничения


def decide_sleep(*, idle_s: float, now: float, hour: int, last_sleep: float, cfg: dict) -> SleepPlan | None:
    """EPI-4: когда спать. `idle` - простой дольше `idle_min` минут; `scheduled` - час из расписания, если сегодня ещё не спала;
    `catchup` - давно не спала (`catchup_after_h`), простоя нет: малый бюджет токенов."""
    if not cfg.get("enabled", True):
        return None
    gap = max(60.0, _num(cfg.get("min_gap_min"), 20.0) * 60.0)
    if now - last_sleep < gap:
        return None
    if idle_s >= _num(cfg.get("idle_min"), 15.0) * 60.0:
        return SleepPlan("idle", None)
    hours = cfg.get("schedule_hours") if isinstance(cfg.get("schedule_hours"), list) else []
    if hour in hours and now - last_sleep > 12 * 3600.0:
        return SleepPlan("scheduled", None)
    after = _num(cfg.get("catchup_after_h"), 12.0) * 3600.0
    if after > 0 and now - last_sleep > after:
        return SleepPlan("catchup", int(_num(cfg.get("catchup_budget_tokens"), 1500)))
    return None


def diary_days(*, today: str, hour: int, days_with_events: list[str], written: set, diary_hour: int = 22, limit: int = 3) -> list[str]:
    """Сутки, за которые пора писать дневник (EPI-8): прошедшие без записи; сегодняшние после `diary_hour`."""
    out = [d for d in sorted(days_with_events) if d not in written and (d < today or (d == today and hour >= diary_hour))]
    return out[:limit]


def compress_days(*, today_ord: int, episodes_by_day: dict, compressed: set, after_days: int) -> list[str]:
    """Сутки старше `after_days` дней, ещё не сжатые в сводку (EPI-10). Ключи - `YYYY-MM-DD`, `today_ord` - номер сегодняшнего дня."""
    if after_days <= 0:
        return []
    from datetime import date
    out = []
    for day in sorted(episodes_by_day):
        if day in compressed:
            continue
        try:
            age = today_ord - date.fromisoformat(day).toordinal()
        except ValueError:
            continue
        if age >= after_days:
            out.append(day)
    return out
