"""Общие подставки тестов. Тесты не запускают настоящую RimWorld, кроме тестов с маркером `game` (они сами объявляют запуск
пользователю и закрывают игру).

Остальным запуск запрещён: переменная GITS_NO_LAUNCH=1 (её проверяет `game_launcher`; стоит и в окружении процесса pytest,
поэтому её наследуют дочерние процессы, например `main.py` в `test_module_process`), `spawn_detached` заменён заглушкой,
которая роняет тест, если до неё дошли, а `announce` (объявление о запуске: протокол и HTTP на ядро) записывает вызовы
в список `game_launcher.announced` и ничего не делает."""
import os

import pytest

os.environ["GITS_NO_LAUNCH"] = "1"


@pytest.fixture(autouse=True)
def _never_launch_the_real_game(request, monkeypatch):
    import game_launcher
    if request.node.get_closest_marker("game"):
        monkeypatch.delenv("GITS_NO_LAUNCH", raising=False)        # настоящий запуск: тест отвечает за объявление
        return
    monkeypatch.setenv("GITS_NO_LAUNCH", "1")
    announced: list[str] = []
    game_launcher.announced = announced

    def refuse(*args, **kwargs):
        raise AssertionError("тест дошёл до запуска настоящей RimWorld: подмени game_launcher.launch или spawn_detached, "
                             "либо пометь тест @pytest.mark.game")
    monkeypatch.setattr(game_launcher, "spawn_detached", refuse)
    monkeypatch.setattr(game_launcher, "announce", lambda reason: announced.append(reason) or reason)
    monkeypatch.setattr(game_launcher, "announce_fact", lambda text, kind="game": announced.append(text) or text)
