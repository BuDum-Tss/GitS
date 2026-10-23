"""Теги реакций в тексте ответа (domain/stickers.py): разбор потока, разбивка на куски, подсказка в промпте. Эмоций как
состояния нет: стикер выбирает только текст ответа."""
import pytest

import decide
import prompts
from domain import stickers as st


def feed_all(chunks):
    stream, out = st.TagStream(), []
    for c in chunks:
        out += stream.feed(c)
    return out + stream.flush()


def test_split_cuts_at_known_tags_and_text_before_the_first_tag_has_none():
    assert st.split("Привет! [question] Как тебя зовут?") == [(None, "Привет!"), ("question", "Как тебя зовут?")]
    assert st.split("[happy] Рада. [sad] Но грустно.") == [("happy", "Рада."), ("sad", "Но грустно.")]
    assert st.split("Просто текст.") == [(None, "Просто текст.")]
    assert st.split("") == []


def test_tags_are_case_insensitive_unknown_tags_and_brackets_are_dropped():
    assert st.split("[HAPPY] Ура. [smile] Дальше. [idle] И так.") == [("happy", "Ура. Дальше. И так.")]
    assert st.plain("Привет [question] как дела [ДЕЙСТВИЕ]") == "Привет как дела"
    assert st.first_sticker("Привет [angry] стоп") == "angry" and st.first_sticker("нет тегов") is None


def test_stream_holds_a_tag_that_is_still_arriving():
    pieces = feed_all(["Привет! [que", "stion] Как ", "дела?"])
    assert [(p.tag, p.text) for p in pieces if p.tag] == [("question", "")]
    assert " ".join("".join(p.text for p in pieces).split()) == "Привет! Как дела?"
    order = [p.tag or p.text.strip() for p in pieces if p.tag or p.text.strip()]
    assert order == ["Привет!", "question", "Как", "дела?"]


def test_stream_keeps_a_lone_bracket_and_drops_an_unfinished_tag_at_the_end():
    assert "".join(p.text for p in feed_all(["Цена [", " 5 рублей"])) == "Цена [ 5 рублей"
    assert "".join(p.text for p in feed_all(["Конец [hap"])) == "Конец "


@pytest.mark.parametrize("tag", st.STICKERS)
def test_every_sticker_has_an_image_in_the_pet_set(tag):
    from pathlib import Path
    folder = Path(__file__).resolve().parents[2] / "resources" / "GlaDOS" / "pet" / "stickers"
    assert (folder / f"{tag}.png").exists()


def test_prompt_lists_every_tag_and_the_example():
    text = prompts.reaction_tags()
    assert all(tag in text for tag in st.STICKERS) and "[question]" in text


def test_classifier_decides_but_no_longer_recognizes_emotions():
    assert "emotion" not in decide.EVENT_QUESTIONS and "emotion" not in decide.INPUT_QUESTIONS
    assert not hasattr(decide, "EMOTION_OPTIONS")
    assert not hasattr(decide.parse_input({"intent": {"answer": "chat", "confidence": 0.9}}, 0.5), "emotion")
