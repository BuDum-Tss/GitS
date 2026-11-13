"""Теги реакций в тексте ответа: чистая логика, без ввода-вывода.

Модель сама пишет перед предложением тег стикера в квадратных скобках: `Привет! [question] Как тебя зовут?`. Тег
известного стикера начинает новый кусок речи (кусок звучит отдельной репликой, стикер питомца меняется вместе со звуком
этого куска); неизвестный тег отбрасывается (квадратные скобки не произносятся). Эмоций как состояния нет: стикер
выбирает только текст ответа.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Стикеры питомца (файлы `resources/<Имя>/pet/stickers/<тег>.png`). `idle` модель не пишет: это отсутствие реакции
STICKERS: tuple[str, ...] = ("action", "angry", "question", "looking", "sitting", "thinking", "happy", "threat",
                             "resting", "sad", "scared", "proud", "surprised")
IDLE = "idle"
_PARTIAL = re.compile(r"\[[A-Za-zА-Яа-яЁё_ ]*")
TAG_MAX = 20                                  # длиннее тег не бывает: скобка без закрытия дольше этого - обычный текст


class Tagged(str):
    """Предложение с тегом, под которым оно звучит (`sticker` None: тега в этом куске не было)."""

    sticker: str | None

    def __new__(cls, text: str, sticker: str | None = None):
        obj = super().__new__(cls, text)
        obj.sticker = sticker
        return obj


@dataclass(frozen=True)
class Piece:
    """Кусок разобранного ответа: либо тег стикера, либо текст."""

    tag: str | None = None
    text: str = ""


def canonical(tag: str) -> str | None:
    """Имя стикера по тегу или None (регистр не важен)."""
    name = (tag or "").strip().lower()
    return name if name in STICKERS else None


class TagStream:
    """Разбор потока токенов: `feed(кусок)` отдаёт `Piece`. Тег, который ещё приходит, придерживается. Метка в квадратных
    скобках, которой нет среди стикеров (`[ДЕЙСТВИЕ]`, `[smile]`), исчезает из текста: в речь скобки не идут."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[Piece]:
        self._buf += delta
        out: list[Piece] = []
        while True:
            i = self._buf.find("[")
            if i < 0:
                if self._buf:
                    out.append(Piece(text=self._buf))
                self._buf = ""
                break
            if i:
                out.append(Piece(text=self._buf[:i]))
                self._buf = self._buf[i:]
            j = self._buf.find("]")
            if j < 0:
                if len(self._buf) <= TAG_MAX + 1 and _PARTIAL.fullmatch(self._buf):
                    break                                        # тег ещё приходит
                out.append(Piece(text="["))                      # одинокая скобка в обычном тексте
                self._buf = self._buf[1:]
                continue
            if j > TAG_MAX + 1 or not _PARTIAL.fullmatch(self._buf[:j]):
                out.append(Piece(text="["))
                self._buf = self._buf[1:]
                continue
            name = canonical(self._buf[1:j])
            if name:
                out.append(Piece(tag=name))
            self._buf = self._buf[j + 1:]
        return out

    def flush(self) -> list[Piece]:
        """Конец потока: недописанный тег (`[hap`) пропадает."""
        self._buf = ""
        return []


def split(text: str) -> list[tuple[str | None, str]]:
    """Целый ответ -> [(стикер или None, текст куска)]: текст до первого тега идёт с None."""
    stream = TagStream()
    parts: list[tuple[str | None, str]] = []
    current: str | None = None
    buf = ""
    for piece in stream.feed(text) + stream.flush():
        if piece.tag:
            if buf.strip():
                parts.append((current, " ".join(buf.split())))
            current, buf = piece.tag, ""
        else:
            buf += piece.text
    if buf.strip():
        parts.append((current, " ".join(buf.split())))
    return parts


def plain(text: str) -> str:
    """Ответ без тегов (для чата, памяти и каналов, где стикера нет)."""
    return " ".join(p for _, p in split(text))


def first_sticker(text: str) -> str | None:
    """Первый стикер ответа (когда ответ идёт целиком и стикер один на реплику)."""
    for tag, _ in split(text):
        if tag:
            return tag
    return None
