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
