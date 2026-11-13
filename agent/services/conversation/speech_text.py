"""Очистка текста перед озвучкой: только произносимые слова."""
import re

_STAGE = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def clean_speech(text: str) -> str:
    """Без ремарок в скобках («(Голос звучит ровно…)»), разметки и оборванного хвоста."""
    text = _STAGE.sub("", text)
    text = re.sub(r"[*`#_]+", "", text)
    text = " ".join(text.split()).strip(' "«»')
    if text and text[-1] not in ".!?…»":                  # модель упёрлась в лимит слов: режем до последнего законченного
        cut = max(text.rfind(c) for c in ".!?…")          # предложения
        text = text[:cut + 1] if cut > 20 else text
    return text


class SentenceSplitter:
    """Разбивка потока токенов на предложения (ТЗ 25, FAST-4): `feed(кусок)` отдаёт предложения, которые уже закончились
    (знак конца и пробел после него: «3.5» без пробела не режется), `flush()` отдаёт остаток. Совсем короткие
    предложения («Да.») склеиваются со следующим: отдельный синтез на два слова даёт рваную речь."""

    _END = re.compile(r"[.!?…]+[\"»”)]*\s")

    def __init__(self, min_chars: int = 8):
        self.min_chars = min_chars
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        pos = 0
        while True:
            m = self._END.search(self._buf, pos)
            if m is None:
                break
            cand = self._buf[:m.end()].strip()
            if len(cand) < self.min_chars:
                pos = m.end()                      # слишком коротко: ищем следующий конец и склеиваем
                continue
            out.append(cand)
            self._buf = self._buf[m.end():]
            pos = 0
        return out

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []
