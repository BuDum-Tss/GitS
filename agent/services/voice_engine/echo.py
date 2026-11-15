"""Фильтр эха её собственной речи.

Её голос может вернуться в движок через микрофон участника (колонки владельца, эхо клиента Discord гасится не полностью) и
распознаться как его реплика. Движок помнит недавно озвученное (`SpokenRecord` в `Playback.spoken_log`) и сравнивает с ним каждую
распознанную фразу: если она пришла во время её речи или вскоре после и по словам совпадает с тем, что она говорила, это эхо.

Сравнение по словам (нижний регистр, ё = е, без знаков): доля слов фразы, составляющих общую подпоследовательность с её речью
(слова считаются равными при совпадении или общей основе от 4 букв: распознавание путает окончания). Фразы короче трёх слов
совпадают только целиком и подряд, чтобы «да» и «нет» человека не терялись.
"""
import re
from dataclasses import dataclass, field

RECORD_KEEP_S = 20.0                     # сколько секунд её речь хранится для сравнения
SHORT_WORDS = 3                          # короче этого числа слов фраза проверяется строго
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass
class SpokenRecord:
    """Одна её реплика: когда звучала (часы движка, секунды эпохи) и тексты проигранных предложений."""
    uid: str
    start: float
    end: float
    parts: list[str] = field(default_factory=list)
    active: bool = True                  # реплика ещё звучит: её конец не определён

    @property
    def text(self) -> str:
        return " ".join(self.parts)


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("ё", "е"))


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    n = min(len(a), len(b))
    if n < 5:
        return False
    common = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        common += 1
    return common >= max(4, n - 2)


def _lcs(a: list[str], b: list[str]) -> int:
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if _same(x, y) else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def _contiguous(small: list[str], big: list[str]) -> bool:
    n = len(small)
    return any(all(_same(small[k], big[i + k]) for k in range(n)) for i in range(len(big) - n + 1))


def similar(text: str, reference: str, ratio: float) -> bool:
    """Фраза повторяет её речь: строго для коротких, по доле слов общей подпоследовательности для длинных."""
    cand, ref = words(text), words(reference)
    if not cand or not ref:
        return False
    if len(cand) < SHORT_WORDS:
        return _contiguous(cand, ref)
    return _lcs(cand, ref) / len(cand) >= ratio


def is_echo(text: str, records: list[SpokenRecord], seg_start: float, seg_end: float, window_s: float, ratio: float) -> bool:
    """Фраза говорящего (звучала в [seg_start, seg_end]) это эхо её речи."""
    if window_s <= 0:
        return False
    near = []
    for rec in records:
        end = max(rec.end, seg_end) if rec.active else rec.end
        if seg_end >= rec.start - 0.5 and seg_start <= end + window_s:
            near.append(rec.text)
    return bool(near) and similar(text, " ".join(near), ratio)
