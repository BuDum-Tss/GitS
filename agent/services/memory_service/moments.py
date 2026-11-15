"""Яркие моменты журнала: чистые решения без ввода-вывода (ТЗ 27, замена старой нарезки на сегменты).

Журнал событий остаётся источником правды. Здесь только функции над списками строк журнала:
  event_score       яркость одной строки журнала 0..1 и причины (разговор: лёгкий оценщик по виду и тексту)
  attention_score   яркость по вердикту менеджера внимания (запись kind='attention': решение D0..D4, срочность, маршруты)
  select_moments    пики -> склейка близких -> контекст всех потоков вокруг -> лимиты -> суточный бюджет

Яркость событий модулей берётся из уже работающего механизма внимания (записи `attention`); таблица `MODULE_EVENT_WEIGHTS` это
запасной вариант для события модуля, у которого записи внимания нет. Все веса лежат в словарях ниже, их можно крутить.
Параметры приходят настройками (`MomentCfg.from_settings`, ключи `episodes.moments.*`); часов и настроек здесь нет.
"""
import bisect
import re
from dataclasses import dataclass
from datetime import datetime

from services.memory_service.episodes import estimate_tokens

# ── веса (их можно крутить) ────────────────────────────────────────────────────────────────────────

# решение менеджера внимания -> базовая яркость; прибавки: срочность >= 3, маршрут think или chat
ATTENTION_DECISIONS = {"D0": 0.0, "D1": 0.1, "D2": 0.45, "D3": 0.65, "D4": 0.85, "ANNOUNCE": 0.4}
ATTENTION_URGENCY_BONUS = (3, 0.1)
ATTENTION_ROUTE_BONUS = (("think", "chat"), 0.1)
ATTENTION_CONTEXT_MIN = 0.3          # запись внимания слабее в контекст момента не попадает

# вид события журнала -> яркость (разговор и жизнь без записи внимания)
KIND_WEIGHTS = {"mistake": 0.7, "approval": 0.4, "memory": 0.3, "telegram": 0.5, "hearing": 0.2, "say": 0.1, "game": 0.3}
TASK_STATUS_WEIGHTS = {"done": 0.5, "completed": 0.5, "failed": 0.65, "error": 0.65}
EMOTION_MAX = 0.4                    # эмоция стикера: сила * EMOTION_MAX, фоновая и нейтральная это 0
EMOTION_NEUTRAL = ("idle", "thinking", "looking", "question", "neutral")
USER_BASE, USER_LONG, USER_LONG_CHARS = 0.15, 0.1, 80

# запасная таблица для module_event без записи внимания: `data.event_kind` -> яркость (telegram.* по префиксу)
MODULE_EVENT_WEIGHTS = {"rimworld.death": 0.9, "rimworld.letter": 0.5, "rimworld.game_started": 0.5, "rimworld.run_video": 0.5,
                        "montage.failed": 0.7, "montage.done": 0.6, "voice.heard": 0.35, "self.after_conversation": 0.3}
MODULE_EVENT_PREFIX = (("telegram.", 0.5),)

# признаки в реплике пользователя: (основы слов в нижнем регистре, прибавка)
USER_SIGNS = {
    "просьба запомнить": (("запомни", "не забудь", "запиши себе", "имей в виду", "учти ", "напомни"), 0.35),
    "сильные чувства": (("ненавиж", "обожаю", "люблю", "бесит", "достал", "ужасн", "кошмар", "боюсь", "страшно", "обидно",
                         "грустно", "счастлив", "радуюсь", "устал"), 0.25),
    "благодарность": (("спасибо", "благодарю", "благодар"), 0.2),
    "извинение": (("извини", "прости", "сорри", "виноват"), 0.2),
    "критика": (("неправильно", "не так", "ошибк", "не надо было", "ты зря", "ты не поняла", "исправь", "глупо", "тупо"), 0.3),
    "личное": (("меня зовут", "я живу", "я работаю", "я учусь", "день рождения", "моя мама", "мой брат", "моя сестра",
                "моя девушка", "мой парень", "мне нравится", "у меня есть"), 0.2),
}

THOUGHT_KIND = "thought"

# шум: никогда не пик и не контекст; vision и desktop.* годятся только как «сцена» (фон)
NOISE_KINDS = ("focus", "decision", "trace", "budget_exceeded", "setting", "profile", "error", "vision", "boundary")
SCENE_EVENT_KINDS = ("desktop.app_changed", "desktop.activity")
SCENE_LABELS = ("vision", *SCENE_EVENT_KINDS)       # подписи строк сцены в источниках эпизода
CONTEXT_KINDS = ("user", "say", "mistake", "approval", "memory", "emotion", "game", "telegram", "hearing", "task.status",
                 "attention", "module_event", THOUGHT_KIND)     # thought: её мысль о событии (reflection), контекст, но не пик (яркость 0)
READ_KINDS = frozenset(CONTEXT_KINDS) | {"vision"}          # что читать из журнала
SCENE_MAX = 2                         # строк сцены в моменте
SCENE_LOOKBACK_S = 1800.0             # сцена старше этого перед пиком не берётся


@dataclass(frozen=True)
class MomentCfg:
    threshold: float = 0.5            # яркость, с которой событие считается пиком
    per_day: int = 10                 # моментов в сутки
    merge_s: float = 300.0            # пики ближе склеиваются в один момент
    before_s: float = 240.0           # контекст до пика
    after_s: float = 120.0            # контекст после пика
    max_events: int = 30              # событий в моменте
    max_tokens: int = 1500            # токенов источников в моменте (3 символа на токен)
    instant_score: float = 0.85       # пик не слабее этого закрывает момент быстро (живой режим)
    instant_after_s: float = 20.0     # через сколько секунд тишины закрывается такой момент

    @classmethod
    def from_settings(cls, raw: dict | None) -> "MomentCfg":
        raw = raw if isinstance(raw, dict) else {}
        base = cls()
        out = {}
        for name in ("threshold", "merge_s", "before_s", "after_s", "instant_score", "instant_after_s"):
            v = raw.get(name)
            out[name] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else getattr(base, name)
        for name in ("per_day", "max_events", "max_tokens"):
            v = raw.get(name)
            out[name] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else getattr(base, name)
        return cls(**out)


@dataclass(frozen=True)
class Ev:
    """Событие журнала для момента: оценка и подпись (`label`: вид события или `event_kind` модуля)."""
    id: int
    ts: float
    kind: str
    text: str
    label: str
    score: float = 0.0
    reasons: tuple = ()
    actor: str = ""
    scene: bool = False               # фон, а не событие: вид экрана, смена приложения


@dataclass
class Moment:
    events: list                      # Ev по порядку номеров (контекст и сцена)
    peaks: list                       # номера событий-пиков
    peak_id: int
    salience: float
    day: str
    closed: bool = True

    @property
    def first_id(self) -> int:
        return self.events[0].id

    @property
    def last_id(self) -> int:
        return self.events[-1].id

    @property
    def id(self) -> str:
        return f"m{self.first_id}-{self.last_id}"


# ── оценка событий ─────────────────────────────────────────────────────────────────────────────────

def _num(v, default: float = 0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _data(row: dict) -> dict:
    return row.get("data") if isinstance(row.get("data"), dict) else {}


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def attention_score(row: dict) -> tuple[float, list[str]]:
    """Яркость записи внимания (kind='attention'): решение D0..D4 или ANNOUNCE, прибавки за срочность и маршрут, не меньше
    `data.salience`. Запись только с маршрутом ignore и решением D0/D1 (смена окна, активность) яркости не имеет."""
    d = _data(row)
    decision = str(d.get("decision") or "")
    if decision not in ATTENTION_DECISIONS:
        return 0.0, []
    routes = [str(r) for r in d.get("routes")] if isinstance(d.get("routes"), list) else []
    if routes == ["ignore"] and decision in ("D0", "D1"):
        return 0.0, []
    score, reasons = ATTENTION_DECISIONS[decision], [f"решение {decision}"]
    if _num(d.get("urgency")) >= ATTENTION_URGENCY_BONUS[0]:
        score += ATTENTION_URGENCY_BONUS[1]
        reasons.append("срочно")
    if any(r in routes for r in ATTENTION_ROUTE_BONUS[0]):
        score += ATTENTION_ROUTE_BONUS[1]
        reasons.append("в разговор или мысли")
    sal = _num(d.get("salience"))
    if sal > score:
        score = sal
        reasons.append("яркость внимания")
    return _clip(score), reasons


def user_score(text: str) -> tuple[float, list[str]]:
    """Реплика пользователя: база, длина и признаки по тексту (просьба запомнить, чувства, благодарность, извинение, критика,
    личное)."""
    low = (text or "").lower().replace("ё", "е")
    score, reasons = USER_BASE, []
    if len(text or "") > USER_LONG_CHARS:
        score += USER_LONG
        reasons.append("длинная реплика")
    for name, (stems, bonus) in USER_SIGNS.items():
        if any(s in low for s in stems):
            score += bonus
            reasons.append(name)
    return _clip(score), reasons


def module_event_score(row: dict) -> tuple[float, list[str]]:
    """Запасная оценка события модуля без записи внимания: по `data.event_kind`."""
    ek = str(_data(row).get("event_kind") or "")
    if ek in MODULE_EVENT_WEIGHTS:
        return MODULE_EVENT_WEIGHTS[ek], [ek]
    for prefix, w in MODULE_EVENT_PREFIX:
        if ek.startswith(prefix):
            return w, [ek]
    return 0.0, []


def event_score(row: dict) -> tuple[float, list[str]]:
    """Яркость строки журнала 0..1 и причины. Шум (`NOISE_KINDS`) всегда 0; `data.salience` (ТЗ 24), если есть и больше нуля,
    поднимает оценку по виду."""
    kind = str(row.get("kind") or "")
    d = _data(row)
    if kind in NOISE_KINDS or not str(row.get("text") or "").strip():
        return 0.0, []
    if kind == "attention":
        return attention_score(row)
    if kind == "user":
        score, reasons = user_score(str(row.get("text") or ""))
    elif kind == "module_event":
        score, reasons = module_event_score(row)
    elif kind == "emotion":
        strong = 0.0 if d.get("bg") or str(row.get("text")).strip().lower() in EMOTION_NEUTRAL else _clip(_num(d.get("intensity")))
        score, reasons = strong * EMOTION_MAX, ["эмоция"] if strong else []
    elif kind == "task.status":
        score = TASK_STATUS_WEIGHTS.get(str(d.get("status") or ""), 0.0)
        reasons = [f"задача {d.get('status')}"] if score else []
    elif kind in KIND_WEIGHTS:
        score, reasons = KIND_WEIGHTS[kind], [kind]
    else:
        return 0.0, []
    sal = _num(d.get("salience"))
    if sal > score:
        score, reasons = sal, [*reasons, "яркость события"]
    return _clip(score), reasons


def is_scene(row: dict) -> bool:
    """Строка для «сцены»: вид экрана или смена приложения (фон, не событие)."""
    kind = row.get("kind")
    return kind == "vision" or (kind == "module_event" and str(_data(row).get("event_kind") or "") in SCENE_EVENT_KINDS)


def _norm_text(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower().replace("ё", "е")).strip()[:80]


def _ev(row: dict, score: float, reasons: list[str], scene: bool = False) -> Ev:
    d = _data(row)
    sender = d.get("sender")
    actor = str(sender) if isinstance(sender, str) and row.get("kind") == "module_event" else ""
    label = str(d.get("event_kind") or row.get("kind") or "")
    return Ev(int(row.get("id") or 0), _num(row.get("ts")), str(row.get("kind") or ""), str(row.get("text") or "").strip(), label,
              round(score, 3), tuple(reasons), actor, scene)


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


# ── моменты ────────────────────────────────────────────────────────────────────────────────────────

def select_moments(rows: list[dict], cfg: MomentCfg, now: float, *, taken: dict | None = None, final: bool = False) -> list[Moment]:
    """Строки журнала (старые сверху, с `id`) -> моменты.

    Пики: события с яркостью >= `cfg.threshold`; пики ближе `merge_s` склеиваются. Контекст: значимые события всех потоков в окне
    от `before_s` до `after_s` вокруг пиков и до двух строк сцены перед пиком. Больше `max_events` или `max_tokens` не влезает:
    остаются события ближе к самому яркому пику. Суточный бюджет `per_day` (с учётом `taken`: сколько уже записано за сутки)
    оставляет самые яркие закрытые моменты суток. Момент закрыт, когда после его последнего события прошло больше `after_s` и после
    последнего пика больше `merge_s` (или `final`); очень яркий пик (`instant_score`) закрывает момент уже через `instant_after_s`; открытые возвращаются с `closed=False` и ждут следующего запуска."""
    attended = {_norm_text(r.get("text")) for r in rows if r.get("kind") == "attention"}
    scored: list[Ev] = []
    scene: list[Ev] = []
    for row in rows:
        if is_scene(row) and str(row.get("text") or "").strip():
            scene.append(_ev(row, 0.0, [], True))
        kind = row.get("kind")
        if kind not in CONTEXT_KINDS:
            continue
        if kind == "module_event" and _norm_text(row.get("text")) in attended:
            continue                                          # то же событие уже оценило внимание
        score, reasons = event_score(row)
        if kind in ("attention", "module_event") and score < ATTENTION_CONTEXT_MIN:
            continue
        if kind not in ("user", "say", "thought") and score <= 0:
            continue
        scored.append(_ev(row, score, reasons))
    scored.sort(key=lambda e: (e.ts, e.id))
    peaks = [e for e in scored if e.score >= cfg.threshold]
    groups: list[list[Ev]] = []
    for p in peaks:
        if groups and p.ts - groups[-1][-1].ts < cfg.merge_s:
            groups[-1].append(p)
        else:
            groups.append([p])
    stamps = [e.ts for e in scored]
    scene_stamps = [e.ts for e in scene]
    out: list[Moment] = []
    for g in groups:
        lo = bisect.bisect_left(stamps, g[0].ts - cfg.before_s)
        hi = bisect.bisect_right(stamps, g[-1].ts + cfg.after_s)
        ctx = [e for e in scored[lo:hi] if any(p.ts - cfg.before_s <= e.ts <= p.ts + cfg.after_s for p in g)]
        top = max(g, key=lambda e: (e.score, -e.id))
        events = _trim(ctx, top, cfg)
        events += _scene_before(scene, scene_stamps, g[0].ts, {e.id for e in events}, cfg)
        events.sort(key=lambda e: e.id)
        last_ts = max(e.ts for e in events)
        if top.score >= cfg.instant_score:
            closed = final or now - last_ts > cfg.instant_after_s
        else:
            closed = final or (now - last_ts > cfg.after_s and now - g[-1].ts > cfg.merge_s)
        kept = {e.id for e in events}
        out.append(Moment(events, [p.id for p in g if p.id in kept] or [top.id], top.id, top.score, day_of(top.ts), closed))
    return _budget(out, cfg, taken or {})


def _cost(e: Ev) -> int:
    return estimate_tokens(e.text[:400]) + 8


def _trim(ctx: list[Ev], top: Ev, cfg: MomentCfg) -> list[Ev]:
    """Лимиты момента: события по близости к самому яркому пику, пока влезают в `max_events` и `max_tokens`."""
    pos = next(i for i, e in enumerate(ctx) if e.id == top.id)
    keep, tokens = [], 0
    for i in sorted(range(len(ctx)), key=lambda i: (abs(i - pos), i)):
        cost = _cost(ctx[i])
        if keep and (len(keep) >= cfg.max_events or tokens + cost > cfg.max_tokens):
            break
        keep.append(ctx[i])
        tokens += cost
    return keep


def _scene_before(scene: list[Ev], stamps: list[float], ts: float, have: set, cfg: MomentCfg) -> list[Ev]:
    """До двух последних строк сцены перед пиком (если ещё влезают в лимит событий)."""
    hi = bisect.bisect_right(stamps, ts)
    near = [e for e in scene[:hi] if ts - e.ts <= SCENE_LOOKBACK_S and e.id not in have][-SCENE_MAX:]
    room = max(0, cfg.max_events - len(have))
    return near[-room:] if room else []


def _budget(moments: list[Moment], cfg: MomentCfg, taken: dict) -> list[Moment]:
    """Суточный бюджет: из закрытых моментов суток остаются самые яркие; открытые не режутся (их ещё может склеить)."""
    by_day: dict[str, list[Moment]] = {}
    for m in moments:
        if m.closed:
            by_day.setdefault(m.day, []).append(m)
    drop: set = set()
    for day, ms in by_day.items():
        room = max(0, cfg.per_day - int(taken.get(day, 0)))
        ms.sort(key=lambda m: (-m.salience, m.first_id))
        drop.update(id(m) for m in ms[room:])
    return [m for m in moments if id(m) not in drop]
