"""Ядро и модуль desktop (ТЗ 14, DESK-18): core_fills, fallback, задача Claude через модуль-заглушку, выбор профиля и простой
по state модуля, алиасы прежних id, запись активности в память, меню через маршрутизатор, в ядре нет кода экрана.
Без экрана, мыши, OCR, LM Studio и Claude: модуль заменён заглушкой шины."""
from core_files import core_sources
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from hub_helpers import FakeCompanion

import core_service
import http_api
from services.peers import registry as peer_registry
from services.conversation.companion import Companion
from environments.base import Environment, Tool
from environments.registry import Registry
from gits_sdk import protocol as P
from services.memory_service.user import UserMemory

AGENT = Path(__file__).resolve().parent.parent / "agent"
JOB = "j-1a2b3c4d"

DESKTOP_TOOLS = [
    {"name": "claude_task", "description": "Сложные задачи на компьютере.", "params": {
        "task": {"type": "string", "description": "что нужно сделать"},
        "context": {"type": "string", "required": False, "description": "контекст (заполняет ядро)"}},
     "risk": "auto", "availability": "always", "job": True, "fallback": True, "announce": True, "core_fills": ["context"],
     "keywords": ["файл", "код"]},
    {"name": "click_text", "description": "Нажать на надпись.", "params": {"text": {"type": "string", "description": "надпись"}},
     "risk": "ask", "availability": "always", "keywords": ["нажми"]},
]


class FakeConn:
    """Заглушка соединения модуля: tool.call отвечает номером задачи, запоминает аргументы."""

    def __init__(self):
        self.calls = []

    async def request(self, msg_type, body, timeout=0):
        self.calls.append((msg_type, body))
        return {"job_id": JOB}


def make_registry():
    reg = peer_registry.PeerRegistry(Registry(), settings_source=lambda: {})
    conn = FakeConn()
    reg.hub = SimpleNamespace(connection=lambda mid: conn, attention=None)
    reg._build_environment(reg.declare("desktop"), P.Manifest.from_dict(
        {"id": "desktop", "role": "workspace", "ownership": "shared", "title": "Компьютер", "description": "Экран и ввод", "version": "1.0.0", "tools": DESKTOP_TOOLS}))
    return reg, conn


def test_core_fills_context_is_hidden_from_planner_and_filled_on_call():
    reg, conn = make_registry()
    reg.context_for = lambda query: f"контекст про «{query}»"
    tool = reg.envs.envs["desktop"].tools[0]
    assert "context" not in tool.params and "task" in tool.params          # планировщик этот параметр не видит
    asyncio.run(tool.call({"task": "найди порт"}))
    assert conn.calls[0][1]["args"] == {"task": "найди порт", "context": "контекст про «найди порт»"}


def test_fallback_tool_is_always_visible_and_announce_flags_come_from_manifest():
    reg, _conn = make_registry()
    claude = reg.envs.get("desktop.claude_task")
    assert claude.always is True and claude.job is True
    assert reg.envs.get("desktop.click_text").always is False
    assert reg.envs.get("desktop.claude_task").always is True                  # отбор search берёт always-инструменты подключённого модуля


def test_job_id_is_read_from_the_tool_answer():
    assert peer_registry.PeerRegistry.job_id_from(f"задача {JOB} запущена") == JOB
    assert peer_registry.PeerRegistry.job_id_from("обычный ответ") is None


async def test_wait_job_returns_the_finished_job_and_fails_on_timeout_or_disconnect():
    reg, _conn = make_registry()
    waiter = asyncio.create_task(reg.wait_job("desktop", JOB, timeout=5))
    await asyncio.sleep(0)
    reg.update_job("desktop", {"job_id": JOB, "state": "running", "text": "работает"})
    assert not waiter.done()
    reg.update_job("desktop", {"job_id": JOB, "state": "done", "text": "Порт 8766."})
    job = await asyncio.wait_for(waiter, 2)
    assert job["state"] == "done" and job["text"] == "Порт 8766."
    assert (await reg.wait_job("desktop", JOB))["state"] == "done"            # уже завершённая: сразу
    slow = await reg.wait_job("desktop", "j-00000001", timeout=0.05)
    assert slow["state"] == "failed" and slow["error"]["code"] == "timeout"
    pending = asyncio.create_task(reg.wait_job("desktop", "j-00000002", timeout=5))
    await asyncio.sleep(0)
    reg.on_disconnected("desktop", "связь оборвалась")
    gone = await asyncio.wait_for(pending, 2)
    assert gone["state"] == "failed" and gone["error"]["code"] == "unavailable"


class _Router:
    def __init__(self, ok=True, text=f"задача {JOB} запущена", code=None):
        self.ok, self.text, self.code, self.calls = ok, text, code, []

    async def run_tool_ex(self, tool_id, args):
        self.calls.append((tool_id, args))
        return self.ok, self.text, self.code


def make_companion(tmp_path):
    return Companion(str(tmp_path / "memory"), rel=None)


def make_layer(tmp_path):
    from services.conversation.speech_out import PresenceTracker
    comp = FakeCompanion()
    comp.presence = PresenceTracker()
    comp.io = None
    comp.profile_hint = lambda: ""
    comp._mem = UserMemory(str(tmp_path / "memory"))
    comp._context = lambda include_hearing=True, query="": f"ctx:{query}"
    return core_service.wire_modules(comp, Registry(), specs=[], port=1)


def test_profile_idle_and_activity_come_from_the_desktop_state(tmp_path):
    layer = make_layer(tmp_path)
    comp = layer.attention.companion
    assert layer.profiles._foreground() is None and comp.idle_seconds() == 0.0 and comp.screen_activity() is None
    manifest = {"id": "desktop", "role": "workspace", "ownership": "shared", "title": "Компьютер", "description": "Экран", "version": "1.0.0", "tools": []}
    layer.modules.declare("desktop")
    layer.modules.on_connected(SimpleNamespace(id="desktop", manifest=manifest, instance="i1", pid=1,
                                               connected_at=0.0, spec=P.Manifest.from_dict(manifest)))
    activity = {"app": "code", "category": "coding", "summary": "пишет код", "ts": 1.0, "since": 1.0}
    layer.modules.set_state("desktop", "код; простой 700 с", {"foreground_app": "code", "sensitive": False,
                                                              "idle_s": 700.0, "activity": activity})
    assert layer.profiles._foreground() == "code"
    assert comp.idle_seconds() == 700.0 and comp.screen_activity() == activity


async def test_activity_event_is_written_to_the_activity_journal(tmp_path):
    comp = FakeCompanion()
    comp._mem = UserMemory(str(tmp_path / "memory"))
    hook = core_service.make_activity_hook(comp)
    await hook(SimpleNamespace(data={"app": "notepad", "category": "docs", "summary": "пишет заметку"}))
    recent = comp._mem.recent_activity(5)
    assert recent and recent[-1]["summary"] == "пишет заметку" and recent[-1]["app"] == "notepad"


def test_legacy_screen_tool_ids_resolve_to_the_desktop_tools():
    reg = Registry()
    tool = Tool(id="desktop.click_text", env="desktop", name="click_text", description="Нажать на надпись.", call=None)
    reg.add(Environment(id="desktop", title="Компьютер", description="Экран", tools=[tool], module="desktop"))
    assert reg.get("system.click_text") is tool
    assert reg.get("assistant.launch_app") is None                            # инструмента нет и в модуле
    assert reg.get("system.nothing_like_this") is None


def test_attention_idle_without_a_module_counts_as_at_the_computer():
    from services.attention import Attention
    assert Attention._default_idle() == 0.0


async def test_menu_click_goes_through_the_router(monkeypatch):
    class Router:
        def __init__(self, answer):
            self.answer = answer

        async def run_tool_ex(self, tool_id, args):
            assert tool_id == "desktop.menu_click"
            return self.answer

    async def body_of(text):
        return SimpleNamespace(json=lambda: asyncio.sleep(0, {"text": text}))

    monkeypatch.setattr(http_api, "_is_local_json", lambda request: True)
    http_api.set_router(Router((True, "нажала «Продолжить»", None)))
    ok = await http_api.handle_api_menu_click(await body_of("Продолжить"))
    assert ok.status == 200 and '"ok": true' in ok.text
    http_api.set_router(Router((False, "такой кнопки в списке разрешённых нет", "denied")))
    denied = await http_api.handle_api_menu_click(await body_of("Удалить"))
    assert denied.status == 403
    http_api.set_router(None)


def test_dashboard_shows_the_desktop_screen_state(tmp_path):
    layer = make_layer(tmp_path)
    http_api.set_modules(layer.modules, layer.hub, layer.supervisor)
    assert http_api._screen_state() is None                                   # модуля нет: честно пусто
    layer.modules.declare("desktop")
    layer.modules.set_state("desktop", "код", {"foreground_app": "code", "sensitive": False, "idle_s": 3.0, "activity": None})
    layer.modules.records["desktop"].connected = True
    assert http_api._screen_state()["foreground_app"] == "code"
    http_api.set_modules(None, None, None)


def test_core_has_no_screen_or_perception_code():
    """ТЗ 14, приёмка 1: в процессе ядра нет импортов экрана, восприятия и ввода (они в модуле desktop)."""
    banned = re.compile(r"^\s*(import|from)\s+(perception|screenshots|screen_operator|app_launcher|assistant_agent|"
                        r"game_launcher|pyautogui|pygetwindow|rapidocr|numpy|environments\.pc_control|pc_control)\b", re.M)
    offenders = [p.name for p in core_sources() if banned.search(p.read_text(encoding="utf-8"))]
    offenders += [p.name for p in (AGENT / "environments").glob("*.py") if banned.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
    assert not (AGENT / "perception.py").exists() and not (AGENT / "environments" / "pc_control.py").exists()
