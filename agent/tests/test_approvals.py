"""approvals.ask (ТЗ 02, CORE-30): approval, choice, text, тайм-аут, очередь вопросов, обёртка request."""
import asyncio

import approvals
import settings


async def _wait_pending():
    for _ in range(200):
        if approvals.has_pending():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("вопрос не появился")


async def test_approval_answered_by_words():
    facts = []

    async def announce(fact):
        facts.append(fact)
    task = asyncio.create_task(approvals.ask("approval", "Удалить черновики?", source="montage", announce=announce))
    await _wait_pending()
    assert approvals.pending_kind() == "approval" and approvals.pending_text() == "Удалить черновики?"
    assert "montage" in facts[0] and "Удалить черновики?" in facts[0]
    assert not await approvals.try_answer("а вообще давай поговорим о погоде завтра утром")      # длинная реплика не ответ
    assert await approvals.try_answer("Да!")
    assert await task == (True, "user")
    assert not approvals.has_pending()


async def test_approval_no_and_dashboard_buttons():
    task = asyncio.create_task(approvals.ask("approval", "Можно?"))
    await _wait_pending()
    assert approvals.resolve(False)
    assert await task == (False, "user")
    task = asyncio.create_task(approvals.ask("approval", "Ещё раз?"))
    await _wait_pending()
    assert not approvals.answer("да")                    # для approval нужен bool
    assert approvals.answer(True)
    assert await task == (True, "user")
    assert not approvals.resolve(True)                   # открытого вопроса нет


async def test_timeouts_give_defaults():
    assert await approvals.ask("approval", "Ждать?", timeout_s=0.05) == (False, "timeout")
    assert await approvals.ask("choice", "Что?", ["а", "б"], timeout_s=0.05) == (None, "timeout")
    assert await approvals.ask("text", "Как назвать?", timeout_s=0.05) == (None, "timeout")
    assert not approvals.has_pending()


async def test_choice_matches_by_word_start_case_insensitive():
    task = asyncio.create_task(approvals.ask("choice", "Утвердить план?", ["утвердить", "переделать"]))
    await _wait_pending()
    assert approvals.pending_options() == ["утвердить", "переделать"]
    assert not await approvals.try_answer("а как же погода")            # не вариант: обычная реплика
    assert await approvals.try_answer("Утвердить, пожалуйста")
    assert await task == ("утвердить", "user")
    task = asyncio.create_task(approvals.ask("choice", "Что дальше?", ["продолжать", "остановить"]))
    await _wait_pending()
    assert await approvals.try_answer("остановить")
    assert await task == ("остановить", "user")
    task = asyncio.create_task(approvals.ask("choice", "Что дальше?", ["продолжать", "остановить"]))
    await _wait_pending()
    assert not approvals.answer("что-то ещё")                           # такого варианта нет
    assert approvals.answer("продолжать")
    assert await task == ("продолжать", "user")


async def test_choice_ambiguity_asks_once_more_then_gives_up():
    facts = []

    async def announce(fact):
        facts.append(fact)
    task = asyncio.create_task(approvals.ask("choice", "Какой?", ["красный круг", "красный квадрат", "синий"],
                                             announce=announce))
    await _wait_pending()
    assert await approvals.try_answer("красный")                       # неоднозначно: переспросили
    await asyncio.sleep(0.05)
    assert len(facts) == 2 and "красный круг" in facts[1] and "красный квадрат" in facts[1]
    assert not await approvals.try_answer("красный")                   # второй раз не переспрашиваем
    assert await approvals.try_answer("синий")
    assert await task == ("синий", "user")


async def test_text_takes_next_reply_whole():
    task = asyncio.create_task(approvals.ask("text", "Как назвать ролик?"))
    await _wait_pending()
    assert await approvals.try_answer("Эксперимент с кубом, часть вторая")
    assert await task == ("Эксперимент с кубом, часть вторая", "user")


async def test_questions_are_asked_one_at_a_time_in_order():
    order = []

    async def announce_a(fact):
        order.append("a")

    async def announce_b(fact):
        order.append("b")
    first = asyncio.create_task(approvals.ask("approval", "Первый?", announce=announce_a, source="m1"))
    await _wait_pending()
    second = asyncio.create_task(approvals.ask("approval", "Второй?", announce=announce_b, source="m2"))
    await asyncio.sleep(0.05)
    assert order == ["a"] and approvals.pending_text() == "Первый?"
    assert approvals.pending_info()["queued"] == 1
    assert await approvals.try_answer("да")
    assert await first == (True, "user")
    for _ in range(200):
        if order == ["a", "b"]:
            break
        await asyncio.sleep(0.005)
    assert order == ["a", "b"] and approvals.pending_text() == "Второй?"
    assert await approvals.try_answer("нет")
    assert await second == (False, "user")


async def test_cancelled_waiter_leaves_queue_clean():
    first = asyncio.create_task(approvals.ask("approval", "Первый?"))
    await _wait_pending()
    second = asyncio.create_task(approvals.ask("approval", "Второй?"))
    await asyncio.sleep(0.02)
    second.cancel()
    await asyncio.sleep(0.02)
    approvals.resolve(True)
    await first
    await asyncio.sleep(0.02)
    assert not approvals.has_pending()
    assert await approvals.ask("approval", "Третий?", timeout_s=0.05) == (False, "timeout")      # очередь не застряла


async def test_request_wrapper_keeps_old_behaviour(tmp_path):
    settings.update(auto_accept=False)
    said = []

    async def announce(text):
        said.append(text)
    task = asyncio.create_task(approvals.request("Хочу выполнить действие.", announce))
    await _wait_pending()
    assert said == ["Хочу выполнить действие. Разрешаешь? Скажи «да» или «нет»."]
    approvals.resolve(True)
    assert await task is True
    settings.update(auto_accept=True)
    assert await approvals.request("Просто действие.", announce) is True            # авто-разрешение
    task = asyncio.create_task(approvals.request("Опасное.", announce, danger="удаление данных"))
    await _wait_pending()                                                           # опасное спрашивается всегда
    assert "Внимание: удаление данных" in approvals.pending_text()
    approvals.resolve(False)
    assert await task is False
