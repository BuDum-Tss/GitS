"""Универсальное управление по экрану: цикл на локальной модели с заглушками (без экрана, мыши и LM Studio).
Перенесено из agent/tests/test_screen_operator.py (ТЗ 14): разрешение теперь приходит колбэком ask, а не approvals."""
import asyncio
import json

import fast_brain
import pc_control
import screen_operator as so


def _setup(monkeypatch, replies, items=None):
    items = items if items is not None else [("Файл", 10, 10), ("Сохранить", 200, 300), ("Отмена", 260, 300)]
    clicks, typed = [], []
    monkeypatch.setattr(so, "_snapshot", lambda: ("Блокнот", items))
    monkeypatch.setattr(so, "STEP_PAUSE", 0)
    queue = list(replies)

    async def fake_ask(system, user, timeout, max_tokens=40, **kw):
        return json.dumps(queue.pop(0))
    monkeypatch.setattr(fast_brain, "_ask", fake_ask)
    monkeypatch.setattr(pc_control, "_click_xy", lambda x, y, b, d: clicks.append((x, y)) or "ok")
    monkeypatch.setattr(pc_control, "_type", lambda t: typed.append(t) or "набрала")
    return clicks, typed


async def _no(question, danger=None):
    return False


def test_click_by_id_then_done(monkeypatch):
    clicks, _ = _setup(monkeypatch, [{"action": "click_id", "id": 1, "reason": "кнопка сохранить"},
                                      {"action": "done", "reason": "сохранено"}])
    out = asyncio.run(so.operate("сохрани файл", _no))
    assert clicks == [(200, 300)] and out.startswith("Готово")


def test_stops_on_repeated_step_and_bad_id(monkeypatch):
    same = {"action": "click_id", "id": 2}
    clicks, _ = _setup(monkeypatch, [same, same, same])
    out = asyncio.run(so.operate("нажми отмена", _no))
    assert "один и тот же шаг" in out
    clicks, _ = _setup(monkeypatch, [{"action": "click_id", "id": 99}, {"action": "fail", "reason": "нет кнопки"}])
    assert "нет кнопки" in asyncio.run(so.operate("x", _no)) and clicks == []


def test_dangerous_text_needs_approval(monkeypatch):
    _, typed = _setup(monkeypatch, [{"action": "type", "text": "rm -rf /"}])
    asked = []

    async def deny(question, danger=None):
        asked.append(danger)
        return False
    out = asyncio.run(so.operate("очисти", deny))
    assert asked and asked[0] and typed == [] and "не разрешил" in out


def test_harmless_text_is_typed_without_asking(monkeypatch):
    _, typed = _setup(monkeypatch, [{"action": "type", "text": "привет"}, {"action": "done", "reason": "готово"}])

    async def must_not_ask(question, danger=None):
        raise AssertionError("обычный текст спрашивать не нужно")
    asyncio.run(so.operate("напиши", must_not_ask))
    assert typed == ["привет"]


def test_model_garbage_and_render(monkeypatch):
    queue = ["не json", "тоже не json"]

    async def fake_ask(system, user, timeout, max_tokens=40, **kw):
        return queue.pop(0)
    monkeypatch.setattr(so, "_snapshot", lambda: ("Окно", [("OK", 1, 2)]))
    monkeypatch.setattr(fast_brain, "_ask", fake_ask)
    assert "не смогла выбрать шаг" in asyncio.run(so.operate("цель", _no))
    text = so._render("Окно", [("Кнопка", 5, 6)])
    assert "0. «Кнопка» (5,6)" in text and "Окно: Окно" in text


def test_total_timeout_fits_the_tool_timeout():
    # operate_screen синхронный: тайм-аут инструмента в манифесте 120 с (MOD-17), цикл не должен его переходить
    from manifest import TOOLS
    tool = next(t for t in TOOLS if t["name"] == "operate_screen")
    assert so.TOTAL_TIMEOUT < tool["timeout_s"]
