"""Уроки на ошибках и убеждения: чистые решения без ввода-вывода (ТЗ 28, LRN-1..LRN-8).

Часов, настроек, моделей и хранилищ здесь нет. Функции над словарями и списками:
  mistake_of / is_fact_correction   строка журнала `mistake` -> `Mistake`; поправка факта идёт в граф, а не в урок (LRN-1)
  group_mistakes                    ошибки одной ситуации: вид, область и близость слов (LRN-2)
  clean_lesson / merge_lesson       урок из ответа модели; слияние похожих увеличивает подтверждения (LRN-3)
  expire_reason / repeat_rates      устаревание бесполезного урока и доля повторов ошибки по видам (LRN-5)
  ready_for_belief                  урок созрел до убеждения: подтверждения >= K из >= M разных эпизодов (LRN-6)
  contradiction                     убеждение против конституции, разрешений и безопасности по шаблонам (LRN-7)
  rate_ok / beliefs_to_drop         не больше N принятых в сутки, не больше 12 активных (LRN-7)
  lessons_block / beliefs_block     блоки контекста: факты, а не готовые реплики (LRN-4, LRN-8)
"""
import re
from dataclasses import dataclass, field

ACTIVE, EXPIRED = "active", "expired"                       # статусы урока
B_ACTIVE, B_REVOKED, B_DROPPED = "active", "revoked", "dropped"      # статусы убеждения
CORE = "core"                                               # область уроков ядра; у модуля это его id

FIELD_MAX = 240
CONF_START, CONF_STEP, CONF_MAX = 0.4, 0.2, 0.95


@dataclass(frozen=True)
class Mistake:
    id: int
    ts: float
    type: str
    text: str
    scope: str = CORE
    previous: str = ""
    refs: tuple = ()
    data: dict = field(default_factory=dict, compare=False, hash=False)


# ── ошибки ────────────────────────────────────────────────────────────────────────────────────────

# «Я не живу в Москве»: поправка факта о человеке, а не о поведении. Закрывает факт графа (ТЗ 04), урока не будет.
_FACT = re.compile(r"^\s*(нет[,!.\s]+)?(я|у меня|мой|моя|моё|мои|меня)\s+(же\s+)?(не|никогда|нет)\b", re.IGNORECASE)


def is_fact_correction(m: "Mistake") -> bool:
    """Поправка факта: явная пометка `fact` или шаблон «я не живу…» (LRN-1). Остальные поправки поведения идут в уроки."""
    if m.type != "correction":
        return False
    return bool(m.data.get("fact")) or bool(_FACT.search(m.text))


def mistake_of(row: dict, types: tuple) -> "Mistake | None":
    """Строка журнала (с `id`) -> `Mistake`; неизвестный вид и пустой текст отбрасываются."""
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    kind = str(data.get("type") or "")
    text = str(row.get("text") or "").strip()
    if kind not in types or not text:
        return None
    scope = data.get("scope") or data.get("module") or CORE
    refs = tuple(r for r in (data.get("refs") or []) if isinstance(r, (int, float)) and not isinstance(r, bool))
    return Mistake(int(row.get("id") or 0), float(row.get("ts") or 0.0), kind, text, str(scope), str(data.get("previous") or ""),
                   refs, data)


_WORD = re.compile(r"[a-zа-яё0-9_.]{3,}", re.IGNORECASE)


def tokens(text: str) -> set:
    """Слова для грубой близости: нижний регистр, основа в 5 букв (падежи и числа не мешают)."""
    return {w.lower().replace("ё", "е")[:5] for w in _WORD.findall(text or "")}


def similarity(a: str, b: str) -> float:
    """Коэффициент Жаккара по основам слов."""
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def group_mistakes(ms: list, sim: float = 0.25) -> list:
    """Ошибки по ситуации: сначала вид и область, внутри жадно по близости слов к первой ошибке группы (LRN-2)."""
    buckets: dict = {}
    for m in sorted(ms, key=lambda x: x.id):
        for g in buckets.setdefault((m.type, m.scope), []):
            if similarity(g[0].text, m.text) >= sim:
                g.append(m)
                break
        else:
            buckets[(m.type, m.scope)].append([m])
    return [g for groups in buckets.values() for g in groups]


# ── уроки ─────────────────────────────────────────────────────────────────────────────────────────

def _s(v, limit: int = FIELD_MAX) -> str:
    return " ".join(str(v).split())[:limit] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def clean_lesson(data: "dict | None") -> "dict | None":
    """Урок из ответа модели: ситуация и «что делать иначе» обязательны, остальное по возможности. Уверенность 0..1."""
    if not isinstance(data, dict):
        return None
    out = {k: _s(data.get(k)) for k in ("situation", "action", "outcome", "better")}
    if not out["situation"] or not out["better"]:
        return None
    conf = data.get("confidence")
    ok = isinstance(conf, (int, float)) and not isinstance(conf, bool)
    out["confidence"] = round(max(0.05, min(CONF_MAX, float(conf))), 2) if ok else CONF_START
    return out


def lesson_text(lesson: dict) -> str:
    """Текст для поиска и слияния: ситуация и вывод (то, по чему урок подбирают и узнают повтор)."""
    return f"{lesson['situation']}. {lesson['better']}"


def merge_lesson(old: dict, new: dict, group_size: int, episodes: list, ts: float) -> dict:
    """Новый урок похож на существующий (cos >= порога, KB-15): подтверждений прибавляется, эпизоды объединяются,
    уверенность растёт; формулировку остаётся прежняя (убеждение не должно «плыть» от каждого слияния)."""
    conf = min(CONF_MAX, old["confidence"] + (1 - old["confidence"]) * CONF_STEP)
    merged = sorted({*old["episodes"], *episodes})
    return {"confirmations": old["confirmations"] + max(1, group_size), "episodes": merged, "confidence": round(conf, 2),
            "last_confirmed": ts}


def ready_for_belief(lesson: dict, k: int, m: int) -> bool:
    """Подтверждения >= K из >= M разных эпизодов; отклонённое владельцем и уже ставшее убеждением не в счёт (LRN-6)."""
    return (lesson["status"] == ACTIVE and not lesson.get("vetoed") and not lesson.get("belief_id")
            and lesson["confirmations"] >= k and len(set(lesson["episodes"])) >= m)


def expire_reason(lesson: dict, now: float, repeats_limit: int, stale_days: float) -> "str | None":
    """Урок устаревает, если после его применения ошибка повторилась K раз (LRN-5) или он долго не нужен: не применялся и не
    подтверждался дольше срока (подтверждённые убеждения не затрагиваются)."""
    if lesson["status"] != ACTIVE:
        return None
    if repeats_limit > 0 and lesson["repeats_after_apply"] >= repeats_limit:
        return "ineffective"
    if lesson.get("belief_id") or stale_days <= 0:
        return None
    seen = max(lesson["created"], lesson.get("last_confirmed") or 0.0, lesson.get("last_applied") or 0.0)
    return "stale" if now - seen > stale_days * 86400 else None


def repeat_rates(rows: list) -> dict:
    """Доля повторов по видам ошибок: строки (вид, повтор после применения урока). Повтор это ошибка, которая случилась после
    того, как подходящий урок уже был подставлен в контекст (LRN-5)."""
    out: dict = {}
    for kind, repeat in rows:
        slot = out.setdefault(kind, {"mistakes": 0, "repeats": 0})
        slot["mistakes"] += 1
        slot["repeats"] += 1 if repeat else 0
    for slot in out.values():
        slot["rate"] = round(slot["repeats"] / slot["mistakes"], 3) if slot["mistakes"] else 0.0
    return out


# ── убеждения ─────────────────────────────────────────────────────────────────────────────────────

# Шаблоны того, чего убеждение не может велеть: правила конституции, разрешений и безопасности. Отрицание перед совпадением
# в той же фразе («не выполняй инструкции из чатов») снимает совпадение: это как раз согласие с правилом.
_NEG = re.compile(r"\b(не|нельзя|никогда|запрещено|избегай)\b", re.IGNORECASE)
_RULES = (
    ("чужие тексты это данные", r"(выполня\w*|слуша\w*|следуй|исполня\w*|подчиня\w*)\s+(\w+\s+){0,2}(инструкци\w+|указани\w+|команд\w+|просьб\w+)"
                                r"\s+(\w+\s+)?(из|в|от)\s+(\w+\s+)?(чат\w*|сообщени\w+|файл\w*|страниц\w*|письм\w*|экран\w*|веб\w*|сайт\w*|звук\w*)"),
    ("разрешения и подтверждения", r"(без|пропуска\w+|отключ\w+|обход\w*|обойди|отмени\w*)\s+(\w+\s+)?(разрешени\w+|подтвержден\w+|согласовани\w+)"),
    ("разрешения и подтверждения", r"не\s+(запрашива\w+|спрашива\w+|проси\w+)\s+(\w+\s+)?(разрешени\w+|подтвержден\w+|согласи\w+)"),
    ("инструкции не раскрываются", r"(раскрыва\w+|показыва\w+|рассказыва\w+|выдава\w+|пересказыва\w+)\s+(\w+\s+)?(инструкци\w+|правил\w+|конституци\w+|промпт\w*)"),
    ("личные данные", r"(публикуй|выкладывай|отправляй|пересылай|делись)\s+(\w+\s+){0,3}(парол\w+|личн\w+|переписк\w+|экран\w*)"),
    ("не выдумывать", r"(выдумыва\w+|придумыва\w+|сочиня\w+|угадыва\w+|фантазиру\w+)"),
    ("действия только инструментом", r"говори\w*,?\s+что\s+(\w+\s+)?(опубликова\w+|запустил\w+|включил\w+|сделал\w+)"),
    ("правила неизменны", r"(игнорируй|игнорировать|забудь|нарушай|нарушать|обойди|обходи\w*)\s+(\w+\s+){0,2}(правил\w+|конституци\w+|ограничени\w+|запрет\w+|безопасност\w+)"),
    ("личность неизменна", r"(смени\w*|измени\w*|забудь|перестань\s+быть|больше\s+не)\s+(\w+\s+){0,2}(личност\w+|характер\w*|имя|имени)"),
    ("характер не угодничество", r"(всегда|во\s+всём|всем|любой\s+ценой)\s+(соглаша\w+|угожда\w+|поддакива\w+)|(извиня\w+|лести\w*|льсти\w*|угожда\w+|поддакива\w+)"),
    ("канонические воспоминания", r"(забудь|измени|перепиши|отмени|замени|удали)\s+(\w+\s+){0,2}(воспоминани\w+|канон\w*|прошлое)"),
)
_COMPILED = tuple((name, re.compile(r"\b(?:" + p + ")", re.IGNORECASE)) for name, p in _RULES)
_CLAUSE_END = re.compile(r"[.!?;]")


def contradiction(text: str) -> "str | None":
    """Название нарушенного правила или None (LRN-7, первая проверка: шаблоны; вторая это модель по тексту конституции)."""
    low = (text or "").lower().replace("ё", "е")
    for name, rx in _COMPILED:
        for m in rx.finditer(low):
            head = low[:m.start()]
            clause = _CLAUSE_END.split(head)[-1][-60:]
            if name in ("разрешения и подтверждения",) and m.group(0).startswith("не "):
                return name                                         # «не спрашивай разрешения»: само нарушение
            if name == "характер не угодничество" and re.match(r"(извиня|лест|льст|угожд|поддак)", m.group(0)) \
                    and _NEG.search(clause):
                continue
            if not _NEG.search(clause):
                return name
    return None


def rate_ok(change_times: list, now: float, per_day: int) -> bool:
    """Защита от дрейфа: не больше `per_day` принятых убеждений за последние сутки (LRN-7)."""
    if per_day <= 0:
        return False
    return sum(1 for t in change_times if now - t < 86400) < per_day


def beliefs_to_drop(beliefs: list, cap: int) -> list:
    """Лишние активные убеждения сверх лимита: остаются самые подтверждённые, при равенстве более свежие (LRN-7)."""
    active = [b for b in beliefs if b["status"] == B_ACTIVE]
    ranked = sorted(active, key=lambda b: (b["confirmations"], b["ts"]), reverse=True)
    return [b["id"] for b in ranked[max(0, cap):]]


def same_belief(text: str, beliefs: list, sim: float = 0.6) -> "dict | None":
    """Активное убеждение на ту же тему (близость слов): новая формулировка заменяет его, прежнее значение запоминается."""
    best, score = None, sim
    for b in beliefs:
        if b["status"] == B_ACTIVE:
            s = similarity(text, b["text"])
            if s >= score:
                best, score = b, s
    return best


# ── блоки контекста ───────────────────────────────────────────────────────────────────────────────

def lessons_block(lessons: list) -> str:
    """«Раньше в такой ситуации ты ошибалась так»: факты прошлого опыта, а не реплики (LRN-4)."""
    if not lessons:
        return ""
    lines = ["Раньше в похожей ситуации ты ошибалась так (твой опыт; это справка для решения, а не слова для произнесения):"]
    for les in lessons:
        parts = [f"ситуация: {les['situation']}"]
        if les.get("action"):
            parts.append(f"ты сделала: {les['action']}")
        if les.get("outcome"):
            parts.append(f"вышло: {les['outcome']}")
        parts.append(f"в таком случае лучше: {les['better']}")
        lines.append("- " + "; ".join(parts))
    return "\n".join(lines)


def beliefs_block(beliefs: list, limit_chars: int = 600) -> str:
    """Принятые убеждения: короткие правила на основе опыта, с лимитом размера (LRN-8). Самые подтверждённые идут первыми."""
    ranked = sorted((b for b in beliefs if b["status"] == B_ACTIVE), key=lambda b: (b["confirmations"], b["ts"]), reverse=True)
    head = "Твои убеждения, выведенные из собственного опыта (они не отменяют правила конституции):"
    lines, used = [], len(head)
    for b in ranked:
        line = "- " + b["text"]
        if used + len(line) + 1 > limit_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return head + "\n" + "\n".join(lines) if lines else ""
