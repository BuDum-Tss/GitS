"""Эпизоды и режим сна: чистые решения без ввода-вывода (ТЗ 27, EPI-2, EPI-3, EPI-4, EPI-6, EPI-10).

Журнал событий остаётся источником правды. Здесь только функции над списками записей:
  rec_of          строка журнала -> `Rec` (или None, если событие не для эпизодов); граница -> `Rec(boundary=True)`
  split_segments  структурные границы без LLM: пауза, смена фокуса или профиля, явная запись `boundary`, смена собеседника
  cut             внутри сегмента: потоки, пики яркости, окно вокруг пика (граница, спад яркости, лимит токенов)
  check_summary   проверка резюме по источникам: ссылки на события, имена и числа, число предложений, дословные цитаты
  decide_sleep    пора ли спать (простой, расписание, «догоняющий» сон) и с каким бюджетом
  diary_days      за какие сутки пора писать дневник
  compress_days   какие сутки пора сжать в сводку
Параметры приходят настройками (`CutCfg.from_settings`); часов и настроек здесь нет.
"""
import re
from dataclasses import dataclass, field

# ── статусы ────────────────────────────────────────────────────────────────────────────────────────

PENDING, SUMMARIZED, EMBEDDED, FAILED = "pending", "summarized", "embedded", "failed"
STATUSES = (PENDING, SUMMARIZED, EMBEDDED, FAILED)

# Фоновая политика модуля памяти (ТЗ 18): сон не занимает фокус, прерывается безопасно между эпизодами.
SLEEP_POLICY = {"id": "sleep", "title": "Сон", "focus": "background", "interruptible": True,
                "description": "Фоновая работа с памятью: эпизоды, резюме, дневник. Прерывается между эпизодами."}
MEMORY_MODULE = "memory"            # id внутреннего модуля памяти в менеджере фокуса и в журнале внимания

# Какие виды журнала попадают в эпизоды и в какой поток (разговор, экран, звук, модуль).
CHAT_KINDS = ("user", "say", "emotion", "mistake", "approval", "memory")
STREAM_KINDS = {"game": "game", "telegram": "telegram", "hearing": "hearing", "vision": "screen"}
BOUNDARY_KINDS = ("boundary", "profile")        # `focus` - граница только при переключении (см. rec_of)
PERSON_MODULES = ("telegram",)                  # у этих модулей поток = модуль + собеседник


@dataclass(frozen=True)
class Rec:
    id: int
    ts: float
    kind: str
    text: str
    stream: str = "chat"
    salience: float = 0.0
    actor: str = ""
    boundary: bool = False
    reason: str = ""
    data: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class CutCfg:
    pause_s: float = 600.0           # пауза, после которой сегмент закрыт
    peak: float = 0.5                # яркость, с которой событие считается пиком
    dense_events: int = 8            # столько событий в потоке делают его эпизодом и без пика
    max_events: int = 40             # предел событий в одном эпизоде
    max_tokens: int = 1500           # предел токенов источников в одном эпизоде
    decay: float = 0.4               # событие «спало», если его яркость ниже этой доли яркости пика
    decay_run: int = 3               # столько подряд спавших событий останавливают рост окна
    summary_sentences: tuple = (3, 6)

    @classmethod
    def from_settings(cls, raw: dict | None) -> "CutCfg":
        raw = raw if isinstance(raw, dict) else {}
        base = cls()
        out = {}
        for name in ("pause_s", "peak", "decay"):
            v = raw.get(name)
            out[name] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else getattr(base, name)
        for name in ("dense_events", "max_events", "max_tokens", "decay_run"):
            v = raw.get(name)
            out[name] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else getattr(base, name)
        return cls(**out)


@dataclass
class Candidate:
    """Будущий эпизод: события одного потока в окне вокруг пика."""
    events: list
    stream: str
    peak_id: int
    salience: float
    reason: str                       # peak | dense | whole

    @property
    def first_id(self) -> int:
        return self.events[0].id

    @property
    def last_id(self) -> int:
        return self.events[-1].id

    @property
    def id(self) -> str:
        return f"e{self.first_id}-{self.last_id}"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


# ── записи журнала ─────────────────────────────────────────────────────────────────────────────────

def _num(v, default: float = 0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def fallback_salience(kind: str, data: dict) -> float:
    """Яркость по виду события, если `data.salience` не записана (ТЗ 24 пишет её для разговора и решений)."""
    if kind == "mistake":
        return 0.7
    if kind == "emotion":
        return min(1.0, _num(data.get("intensity")) / 3.0 * 0.6)
    if kind == "approval":
        return 0.4
    if kind == "memory":
        return 0.3
    return 0.0


def rec_of(row: dict) -> Rec | None:
    """Строка журнала (с `id`) -> запись для нарезки. Неизвестный вид и служебные события (trace, decision) отбрасываются."""
    kind = str(row.get("kind") or "")
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    rid, ts = int(row.get("id") or 0), _num(row.get("ts"))
    if kind in BOUNDARY_KINDS or (kind == "focus" and data.get("focus_type") == "switch"):
        return Rec(rid, ts, kind, str(row.get("text") or ""), boundary=True, reason=str(data.get("reason") or kind))
    actor = ""
    if kind in CHAT_KINDS:
        stream = "chat"
    elif kind == "module_event":
        module = str(data.get("module") or "module")
        sender = data.get("sender")
        actor = str(sender) if isinstance(sender, str) else ""
        stream = f"{module}:{actor}" if module in PERSON_MODULES and actor else module
    elif kind in STREAM_KINDS:
        stream = STREAM_KINDS[kind]
    else:
        return None
    text = str(row.get("text") or "").strip()
    if not text:
        return None
    sal = data.get("salience")
    sal = max(0.0, min(1.0, _num(sal))) if isinstance(sal, (int, float)) and not isinstance(sal, bool) \
        else fallback_salience(kind, data)
    return Rec(rid, ts, kind, text, stream, sal, actor, data={k: v for k, v in data.items() if k in ("module", "event_kind", "emotion", "intensity", "type")})


# ── границы и нарезка ──────────────────────────────────────────────────────────────────────────────

def split_segments(recs: list[Rec], cfg: CutCfg, now: float, *, final: bool = False) -> tuple[list[list[Rec]], int]:
    """Сегменты между границами (EPI-2) и номер последнего обработанного события.

    Сегмент закрыт, если за ним граница или пауза больше `pause_s`; последний открытый сегмент (идёт сейчас) остаётся
    нетронутым, пока с его последнего события не прошло `pause_s` (или `final`). Возвращает (закрытые сегменты, upto):
    все события до `upto` включительно обработаны."""
    segments: list[list[Rec]] = []
    cur: list[Rec] = []
    upto = 0
    last_ts = None
    for r in recs:
        if r.boundary:
            if cur:
                segments.append(cur)
            cur = []
            upto = r.id
            last_ts = r.ts
            continue
        if cur and last_ts is not None and r.ts - last_ts > cfg.pause_s:
            segments.append(cur)
            upto = cur[-1].id
            cur = []
        cur.append(r)
        last_ts = r.ts
    if cur and (final or now - cur[-1].ts > cfg.pause_s):
        segments.append(cur)
        upto = cur[-1].id
    return segments, upto


def _tokens(rec: Rec) -> int:
    return estimate_tokens(rec.text) + 8


def _window(part: list[Rec], idx: int, covered: set, cfg: CutCfg) -> tuple[int, int]:
    """Окно вокруг пика `idx`: растёт в обе стороны до границы потока, спада яркости или лимита токенов и событий."""
    lo = hi = idx
    peak = part[idx].salience
    tokens, count = _tokens(part[idx]), 1
    low_l = low_r = 0
    open_l = open_r = True
    while (open_l or open_r) and count < cfg.max_events:
        for side in ("l", "r"):
            if count >= cfg.max_events:
                break
            if side == "l" and open_l:
                nxt = lo - 1
                if nxt < 0 or part[nxt].id in covered:
                    open_l = False
                    continue
                cost = _tokens(part[nxt])
                if tokens + cost > cfg.max_tokens:
                    open_l = False
                    continue
                lo, tokens, count = nxt, tokens + cost, count + 1
                low_l = low_l + 1 if part[nxt].salience < cfg.decay * peak else 0
                open_l = low_l < cfg.decay_run
            elif side == "r" and open_r:
                nxt = hi + 1
                if nxt >= len(part) or part[nxt].id in covered:
                    open_r = False
                    continue
                cost = _tokens(part[nxt])
                if tokens + cost > cfg.max_tokens:
                    open_r = False
                    continue
                hi, tokens, count = nxt, tokens + cost, count + 1
                low_r = low_r + 1 if part[nxt].salience < cfg.decay * peak else 0
                open_r = low_r < cfg.decay_run
    while hi > lo and part[hi].salience < cfg.decay * peak and hi > idx:       # хвост спада не тянем в эпизод
        hi -= 1
    while lo < idx and part[lo].salience < cfg.decay * peak:
        lo += 1
    return lo, hi


def _chunks(part: list[Rec], cfg: CutCfg) -> list[list[Rec]]:
    out, cur, tokens = [], [], 0
    for r in part:
        if cur and (len(cur) >= cfg.max_events or tokens + _tokens(r) > cfg.max_tokens):
            out.append(cur)
            cur, tokens = [], 0
        cur.append(r)
        tokens += _tokens(r)
    if cur:
        out.append(cur)
    return out


def cut(segment: list[Rec], cfg: CutCfg) -> tuple[list[Candidate], list[Rec]]:
    """Сегмент -> (эпизоды-кандидаты, спокойные события для сводки). События разных потоков не смешиваются (EPI-3)."""
    streams: dict[str, list[Rec]] = {}
    for r in segment:
        streams.setdefault(r.stream, []).append(r)
    cands: list[Candidate] = []
    calm: list[Rec] = []
    for stream, part in streams.items():
        top = max(r.salience for r in part)
        has_peak = top >= cfg.peak
        if not has_peak and len(part) < cfg.dense_events:
            calm.extend(part)
            continue
        total = sum(_tokens(r) for r in part)
        if len(part) <= cfg.max_events and total <= cfg.max_tokens:
            peak = max(part, key=lambda r: r.salience)
            cands.append(Candidate(part, stream, peak.id, top, "peak" if has_peak else "dense"))
            continue
        if not has_peak:                                    # плотный и длинный: режем подряд
            for chunk in _chunks(part, cfg):
                peak = max(chunk, key=lambda r: r.salience)
                cands.append(Candidate(chunk, stream, peak.id, peak.salience, "dense"))
            continue
        covered: set = set()
        order = sorted((i for i, r in enumerate(part) if r.salience >= cfg.peak), key=lambda i: -part[i].salience)
        for i in order:
            if part[i].id in covered:
                continue
            lo, hi = _window(part, i, covered, cfg)
            win = part[lo:hi + 1]
            covered.update(r.id for r in win)
            cands.append(Candidate(win, stream, part[i].id, part[i].salience, "peak"))
        calm.extend(r for r in part if r.id not in covered)
    cands.sort(key=lambda c: c.first_id)
    calm.sort(key=lambda r: r.id)
    return cands, calm


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


def check_summary(parsed, recs: list[Rec], *, allow: tuple | list | set = (), sentences_range: tuple = (3, 6)) -> tuple[dict | None, list[str]]:
    """Резюме от модели -> (очищенный dict, замечания). Пустые замечания - резюме принято.

    Проверяется: заголовок и текст есть; число предложений; ссылки `refs` на существующие `event_id` (хотя бы одна); имена и
    числа из резюме встречаются в событиях-источниках (или в `allow`: имя персонажа, владельца); дословные цитаты, не найденные
    в источниках, отбрасываются (замечанием не считаются)."""
    if not isinstance(parsed, dict):
        return None, ["ответ не JSON-объект"]
    problems: list[str] = []
    title = str(parsed.get("title") or "").strip()[:160]
    summary = " ".join(str(parsed.get("summary") or "").split())
    if not title:
        problems.append("нет заголовка title")
    if not summary:
        problems.append("нет резюме summary")
    ids = {r.id for r in recs}
    refs: list[int] = []
    for x in parsed.get("refs") if isinstance(parsed.get("refs"), list) else []:
        try:
            refs.append(int(x))
        except (TypeError, ValueError):
            problems.append(f"ссылка {x!r} не номер события")
    bad = sorted({x for x in refs if x not in ids})
    if bad:
        problems.append("ссылки на несуществующие события: " + ", ".join(str(x) for x in bad[:5]))
    if not [x for x in refs if x in ids]:
        problems.append("нет ссылок refs на события-источники")
    if summary:
        lo, hi = sentences_range
        n = len(sentences(summary))
        lo = min(lo, max(1, len(recs) - 1))
        if n < lo or n > hi:
            problems.append(f"в резюме {n} предложений, нужно от {lo} до {hi}")
        problems += unsupported(summary, " ".join(r.text for r in recs), allow)
    sources = _norm(" ".join(r.text for r in recs))
    quotes = [q.strip() for q in (parsed.get("quotes") if isinstance(parsed.get("quotes"), list) else [])
              if isinstance(q, str) and q.strip() and _norm(q) in sources][:5]
    clean = {"title": title, "summary": summary, "outcome": str(parsed.get("outcome") or "").strip()[:300],
             "quotes": quotes, "refs": sorted({x for x in refs if x in ids}),
             "facts": _strings(parsed.get("facts"), 6, 200), "emotions": _strings(parsed.get("emotions"), 5, 40),
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
