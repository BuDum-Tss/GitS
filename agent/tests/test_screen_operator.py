"""Универсальное управление по экрану: цикл на локальной модели с заглушками (без экрана, мыши и LM Studio)."""
import asyncio
import json

import approvals
import fast_brain
import screen_operator as so
from environments import pc_control


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


async def _announce(text, emotion=None):
    pass


def test_click_by_id_then_done(monkeypatch):
    clicks, _ = _setup(monkeypatch, [{"action": "click_id", "id": 1, "reason": "кнопка сохранить"},
                                      {"action": "done", "reason": "сохранено"}])
    out = asyncio.run(so.operate("сохрани файл", _announce))
    assert clicks == [(200, 300)] and out.startswith("Готово")


def test_stops_on_repeated_step_and_bad_id(monkeypatch):
    same = {"action": "click_id", "id": 2}
    clicks, _ = _setup(monkeypatch, [same, same, same])
    out = asyncio.run(so.operate("нажми отмена", _announce))
    assert "один и тот же шаг" in out
    clicks, _ = _setup(monkeypatch, [{"action": "click_id", "id": 99}, {"action": "fail", "reason": "нет кнопки"}])
    assert "нет кнопки" in asyncio.run(so.operate("x", _announce)) and clicks == []


def test_dangerous_text_needs_approval(monkeypatch):
    _, typed = _setup(monkeypatch, [{"action": "type", "text": "rm -rf /"}])
    asked = []

    async def deny(text, announce, danger=None):
        asked.append(danger)
        return False
    monkeypatch.setattr(approvals, "request", deny)
    out = asyncio.run(so.operate("очисти", _announce))
    assert asked and asked[0] and typed == [] and "не разрешил" in out


def test_model_garbage_and_render(monkeypatch):
    queue = ["не json", "тоже не json"]

    async def fake_ask(system, user, timeout, max_tokens=40, **kw):
        return queue.pop(0)
    monkeypatch.setattr(so, "_snapshot", lambda: ("Окно", [("OK", 1, 2)]))
    monkeypatch.setattr(fast_brain, "_ask", fake_ask)
    assert "не смогла выбрать шаг" in asyncio.run(so.operate("цель", _announce))
    text = so._render("Окно", [("Кнопка", 5, 6)])
    assert "0. «Кнопка» (5,6)" in text and "Окно: Окно" in text


def test_tools_registered_and_agent_allowlist():
    import tempfile
    import dashboard
    import settings
    from environments import builtin
    from environments.registry import Registry
    from user_memory import UserMemory

    class Dummy:
        def __getattr__(self, name):
            return None
    d = tempfile.mkdtemp()
    settings.init(d)
    reg = Registry()
    builtin.register(reg, Dummy(), None, UserMemory(d))
    for tool_id in dashboard._AGENT_TOOLS:
        assert reg.get(tool_id) is not None, tool_id
    assert reg.get("system.operate_screen").risk == "ask" and "Запасной уровень" in reg.get("system.click_text").description
