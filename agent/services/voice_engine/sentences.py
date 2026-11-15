"""Деление реплики на предложения для конвейерного синтеза (docs/VOICE_ENGINE.md, «Потоковый синтез»).

Правила: рвём по `.`, `!`, `?`, `…` и переводам строки; не рвём на сокращениях («т.е.», «т.д.», «им.», «г.», «ул.»), десятичных и
версиях («3.5», «v2.1»), инициалах («А. С. Пушкин»), многоточии внутри предложения (после него строчная буква), закрывающие
кавычки и скобки остаются при своём предложении. Слишком короткие предложения склеиваются со следующим (мелкие запросы и
скачки интонации хуже одного запроса), слишком длинные делятся по `;` `:`, затем по запятым и тире, в крайнем случае по словам.
"""
import re

MIN_CHARS = 25
MAX_CHARS = 220

# сокращения, после которых точка не конец предложения (в нижнем регистре, без внешних точек)
ABBREVIATIONS = frozenset({
    "т.е", "т.д", "т.п", "т.к", "т.н", "т.о", "и т.д", "и др", "и пр", "им", "г", "гг", "ул", "пр", "просп", "пер", "пл", "д", "корп",
    "стр", "кв", "рис", "табл", "см", "ср", "напр", "др", "проф", "акад", "доц", "тов", "гр", "св", "ст", "обл", "р", "оз",
    "руб", "коп", "млн", "млрд", "тыс", "мин", "сек", "ч", "кг", "км", "мм", "тел", "etc", "vs", "mr", "mrs", "dr",
})
_TERMINATORS = ".!?…"
_CLOSERS = "\"'»”)]}"
_OPENERS = "\"'«“([{"
_MAJOR = re.compile(r"(?<=[;:])\s+")
_COMMA = re.compile(r"(?<=,)\s+")
_DASH = re.compile(r"\s+[—–-]\s+")
_WORDS = re.compile(r"\s+")


def _last_token(text: str) -> str:
    parts = text.rstrip().rsplit(None, 1)
    return parts[-1] if parts else ""


def _is_abbreviation(before: str) -> bool:
    token = _last_token(before).strip(_OPENERS).lower()
    if token in ABBREVIATIONS:
        return True
    words = before.rstrip().lower().rsplit(None, 2)
    return len(words) >= 2 and f"{words[-2]} {words[-1].strip(_OPENERS)}" in ABBREVIATIONS


def _is_initial(before: str) -> bool:
    """Инициал: одна заглавная буква перед точкой («А. С. Пушкин»)."""
    token = _last_token(before).strip(_OPENERS)
    return len(token) == 1 and token.isalpha() and token.isupper()


def _split_paragraph(text: str) -> list[str]:
    out, start, i, n = [], 0, 0, len(text)
    while i < n:
        if text[i] not in _TERMINATORS:
            i += 1
            continue
        j = i
        while j < n and text[j] in _TERMINATORS:
            j += 1
        group = text[i:j]
        end = j
        while end < n and text[end] in _CLOSERS:
            end += 1
        rest = text[end:]
        if rest and not rest[0].isspace():                  # «3.5», «v2.1», «ru.wikipedia»: точка внутри слова
            i = end
            continue
        nxt = rest.lstrip()
        if nxt and nxt[0].islower():                        # продолжение с маленькой буквы: не конец предложения
            i = end
            continue
        if group == "." and (_is_abbreviation(text[start:i]) or _is_initial(text[start:i])):
            i = end
            continue
        out.append(text[start:end].strip())
        start, i = end, end
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _pack(pieces: list[str], limit: int) -> list[str]:
    chunks, cur = [], ""
    for piece in pieces:
        if cur and len(cur) + 1 + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def _split_long(text: str, limit: int) -> list[str]:
    """Длинное предложение: по `;` и `:`, затем по запятым, по тире, по словам."""
    if len(text) <= limit:
        return [text]
    for pattern in (_MAJOR, _COMMA, _DASH, _WORDS):
        pieces = [p for p in pattern.split(text) if p.strip()]
        if len(pieces) < 2:
            continue
        out = []
        for chunk in _pack(pieces, limit):
            out.extend(_split_long(chunk, limit) if len(chunk) > limit and pattern is not _WORDS else [chunk])
        return out
    return [text[i:i + limit] for i in range(0, len(text), limit)]        # слово длиннее лимита: режем как есть


def _has_speech(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def split_sentences(text: str, min_chars: int = MIN_CHARS, max_chars: int = MAX_CHARS) -> list[str]:
    """Реплика -> список предложений для синтеза. Пустые и из одних знаков отбрасываются; короткие склеиваются со следующим."""
    raw: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            raw.extend(_split_paragraph(line))
    sentences: list[str] = []
    for part in raw:
        if _has_speech(part):
            sentences.extend(_split_long(part, max_chars))
        elif sentences:                                    # одни знаки («…», «?!») прилипают к предыдущему
            sentences[-1] += part
    merged: list[str] = []
    cur = ""
    for sent in sentences:
        if cur and len(cur) + 1 + len(sent) > max_chars:
            merged.append(cur)                             # склейка не должна раздувать предложение сверх предела
            cur = ""
        cur = f"{cur} {sent}" if cur else sent
        if len(cur) >= min_chars:
            merged.append(cur)
            cur = ""
    if cur:
        if merged and len(merged[-1]) + 1 + len(cur) <= max_chars:
            merged[-1] = f"{merged[-1]} {cur}"
        else:
            merged.append(cur)
    return merged
