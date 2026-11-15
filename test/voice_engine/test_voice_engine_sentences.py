"""Деление реплики на предложения для конвейерного синтеза."""
import pytest

from services.voice_engine.sentences import split_sentences as split


def test_plain_sentences_merge_until_min_length():
    assert split("Привет! Как дела? Всё хорошо, спасибо что спросил. Да.") == ["Привет! Как дела? Всё хорошо, спасибо что спросил. Да."]
    assert split("Первое предложение достаточно длинное. Второе предложение тоже достаточно длинное.") == [
        "Первое предложение достаточно длинное.", "Второе предложение тоже достаточно длинное."]


def test_short_sentences_glue_to_next():
    out = split("Да. Нет. Это уже достаточно длинное предложение. Ещё одно длинное предложение здесь.")
    assert out == ["Да. Нет. Это уже достаточно длинное предложение.", "Ещё одно длинное предложение здесь."]


def test_short_tail_glues_to_previous():
    assert split("Это достаточно длинное предложение для теста. Ок.") == ["Это достаточно длинное предложение для теста. Ок."]


@pytest.mark.parametrize("text", [
    "Это сработает, т.е. всё будет в порядке, и т.д. тоже хорошо.",
    "Версия 3.5 вышла вчера, а v2.1 уже устарела совсем.",
    "Он жил на ул. Ленина в г. Москве давно уже.",
    "Стихи написал А. С. Пушкин в Болдинскую осень.",
    "Ну… потом всё пошло как-то само собой, слава богу.",
    "Читайте на ru.wikipedia.org про это подробнее здесь.",
])
def test_no_split_inside(text):
    assert split(text, min_chars=5) == [text]


def test_ellipsis_before_capital_splits():
    assert split("Подожди немного… Теперь можно продолжать работу.", min_chars=5) == ["Подожди немного…", "Теперь можно продолжать работу."]


def test_closing_quotes_and_brackets_stay_with_sentence():
    out = split("Он сказал: «Мы уже идём!» Потом он ушёл прочь. (Это было давно.) Конец истории здесь.", min_chars=5)
    assert out == ["Он сказал: «Мы уже идём!»", "Потом он ушёл прочь.", "(Это было давно.)", "Конец истории здесь."]


def test_newlines_split():
    assert split("Первая строка без точки совсем\nВторая строка тоже без точки", min_chars=5) == [
        "Первая строка без точки совсем", "Вторая строка тоже без точки"]


def test_empty_and_punctuation_only_dropped():
    assert split("") == [] and split("  " + chr(10) + " ... " + chr(10) + " ?! ") == []


def test_punctuation_only_glued_not_spoken_alone():
    out = split("Нормальное длинное предложение здесь.\n...", min_chars=5)
    assert len(out) == 1 and out[0].startswith("Нормальное")


def test_long_sentence_split_by_semicolon_comma_words():
    long = "; ".join(["часть номер %d с достаточно длинным текстом внутри" % i for i in range(8)]) + "."
    out = split(long, min_chars=25, max_chars=120)
    assert len(out) > 1 and all(len(o) <= 120 for o in out) and out[0].endswith(";")
    commas = ", ".join(["слово%d" % i for i in range(60)]) + "."
    out = split(commas, max_chars=100)
    assert len(out) > 1 and all(len(o) <= 100 for o in out) and out[0].endswith(",")
    words = " ".join(["слово"] * 100)
    out = split(words, max_chars=50)
    assert all(len(o) <= 50 for o in out) and " ".join(out) == words
    assert all(len(o) <= 20 for o in split("а" * 70, max_chars=20))


def test_merge_does_not_exceed_max():
    a, b = "а" * 20 + ".", "б" * 100 + "."
    assert split(f"{a} {b}", min_chars=25, max_chars=100) == [a, b]
