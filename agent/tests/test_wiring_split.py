"""ТЗ 29, ARC-7 и ARC-8 (шаги R6, R7): корень сборки, шина модулей без домена, дашборд по роутерам."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import dashboard
from adapters.module_events import AttentionEventSink
from contracts import EventSink, RequestHandler
from core_requests import CoreRequests
from module_hub import ModuleHub
from module_outbound import Outbound

AGENT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in tree.body:                                   # верхний уровень: ленивые импорты внутри функций не в счёт
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


# ── R6 ────────────────────────────────────────────────────────────────────────────────────────

def test_hub_does_not_import_attention_or_fast_brain():
    for name in ("module_hub.py", "module_outbound.py"):
        assert not ({"attention", "fast_brain", "companion"} & _imports(AGENT / name)), name


def test_core_service_has_no_event_handlers():
    tree = ast.parse((AGENT / "core_service.py").read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not defined & {"make_voice_handlers", "make_heard_hook", "make_run_lost_hook", "make_colonists_handler",
                          "make_activity_hook", "game_order", "on_utterance", "on_colonists"}


def test_adapter_and_core_requests_satisfy_contracts():
    assert isinstance(AttentionEventSink(SimpleNamespace()), EventSink)
    assert isinstance(CoreRequests(attention=None), RequestHandler)


class FakeSink:
    def __init__(self):
        self.events, self.speech, self.jobs, self.emotions = [], [], [], []

    async def submit(self, module_id, event):
        self.events.append((module_id, event))

    async def submit_speech(self, module_id, say):
        self.speech.append((module_id, say))

    def on_emotion(self, module_id, emotion):
        self.emotions.append((module_id, emotion))
        return emotion == "joy"

    async def on_job_update(self, module_id, job):
        self.jobs.append((module_id, job))

    async def submit_question(self, module_id, job_id, question_id, question):
        return "yes", "user"


def _hub(sink):
    hub = ModuleHub(SimpleNamespace(hub=None, is_enabled=lambda *a, **k: True), sink=sink)
    conn = SimpleNamespace(module_id="echo", spec=SimpleNamespace(emits=[]), warned_kinds=set())
    return hub, conn


async def test_hub_passes_module_messages_to_the_sink():
    from gits_sdk.protocol import Envelope
    sink = FakeSink()
    hub, conn = _hub(sink)
    await hub._on_note(conn, Envelope("event", {"kind": "echo.ping", "text": "привет"}))
    await hub._on_note(conn, Envelope("say", {"text": "скажи", "mode": "fact"}))
    await hub._on_note(conn, Envelope("emotion", {"emotion": "joy"}))
    await hub._on_note(conn, Envelope("emotion", {"emotion": "нет такой"}))
    assert sink.events and sink.events[0][0] == "echo" and sink.events[0][1]["kind"] == "echo.ping"
    assert sink.speech and sink.speech[0][0] == "echo"
    assert [e for _, e in sink.emotions] == ["joy", "нет такой"]


async def test_hub_without_sink_drops_events_quietly():
    from gits_sdk.protocol import Envelope
    hub, conn = _hub(None)
    await hub._on_note(conn, Envelope("event", {"kind": "echo.ping", "text": "привет"}))


def test_outbound_is_the_hub_outbound_and_limits_presence_rate():
    t = [100.0]
    hub, _ = _hub(None)
    hub._monotonic = lambda: t[0]
    hub._conns = {}
    assert isinstance(hub.outbound, Outbound) and hub.outbound is hub.outbound
    sent = []
    hub.outbound.notify = lambda topic, data: sent.append(data)       # рассылка не нужна: смотрим только частоту
    hub.outbound.broadcast_presence({"n": 1})
    hub.outbound.broadcast_presence({"n": 2})                       # в паузе: цикла событий нет, значение ждёт
    assert sent == [{"n": 1}]
    t[0] += 1.0
    hub.outbound.broadcast_presence({"n": 3})
    assert sent[-1] == {"n": 3}


# ── R7 ────────────────────────────────────────────────────────────────────────────────────────

PUBLIC_ROUTES = {
    ("GET", "/"), ("GET", "/api/state"), ("POST", "/api/chat"), ("POST", "/api/settings"), ("POST", "/api/character"),
    ("GET", "/api/core"), ("GET", "/api/events"), ("GET", "/api/latency"), ("GET", "/api/flow"), ("GET", "/api/flow/stream"), ("GET", "/api/flow/node/{node}"), ("GET", "/api/flow/turn/{turn}"), ("POST", "/api/rating"),
    ("GET", "/api/decisions"), ("GET", "/api/capabilities"), ("GET", "/api/modes"), ("POST", "/api/appraisal_label"),
    ("POST", "/api/menu_click"), ("POST", "/api/mode"), ("POST", "/api/approval"), ("POST", "/api/memory/forget"),
    ("GET", "/api/graph"), ("POST", "/api/graph/forget"), ("GET", "/api/chat"), ("GET", "/api/modules"),
    ("POST", "/api/modules/{id}/enabled"), ("POST", "/api/modules/{id}/restart"), ("POST", "/api/modules/{id}/settings"),
    ("GET", "/api/modules/{id}/panel"), ("POST", "/api/modules/{id}/panel/action"),
    ("POST", "/api/modules/{id}/panel/link"), ("GET", "/api/artifact"), ("GET", "/api/attention"),
    ("POST", "/api/attention/rules/clear"), ("GET", "/api/focus"), ("GET", "/api/affect"), ("POST", "/api/affect/reset"),
    ("GET", "/api/thoughts"), ("GET", "/api/episodes"), ("GET", "/api/episodes/{id}"), ("GET", "/api/diary"), ("GET", "/api/sleep"),
    ("POST", "/api/sleep/now"), ("POST", "/api/sleep/interrupt"), ("GET", "/api/lessons"), ("POST", "/api/lessons/seen"), ("POST", "/api/beliefs/{change}/revoke"), ("POST", "/api/focus/pin"), ("POST", "/api/focus/dnd"), ("POST", "/api/focus/allow_once"),
    ("POST", "/api/notify"), ("GET", "/api/jobs"), ("POST", "/api/jobs/{module}/{job_id}/cancel"),
    ("POST", "/api/jobs/{module}/{job_id}/answer"),
}


def test_public_urls_do_not_change():
    app = dashboard.build_app()
    got = {(r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"}
    got = {(m, p) for m, p in got if not p.startswith("/static") and p != "/module/ws"}
    assert got == PUBLIC_ROUTES


def test_legacy_module_names_still_reach_the_context():
    old = dashboard.ctx.memory_dir
    try:
        dashboard._MEMORY_DIR = Path("x-mem")
        assert dashboard.ctx.memory_dir == Path("x-mem") and dashboard._MEMORY_DIR == Path("x-mem")
    finally:
        dashboard._MEMORY_DIR = old


def test_presenters_are_pure():
    from dashboard import presenters
    assert presenters.screen_state(None) is None
    assert presenters.jobs_view([{"module": "a", "job_id": "j", "extra": 1}])[0]["title"] is None
    assert presenters.check_setting_value({"type": "integer", "min": 1}, 0)[1] == "значение меньше 1"
    assert presenters.check_setting_value({"type": "boolean"}, True) == (True, None)
    payload = presenters.state_payload(game={}, emotion=None, settings_dict={}, character={}, presence=None, modules=[])
    assert payload["connected"] is False and payload["modules"] == []
    assert presenters.graph_view({"edges": [{"invalid_at": None}, {"invalid_at": 1}]}, status="ok", pending=0)["outdated"] == [
        {"invalid_at": 1}]


@pytest.mark.parametrize("module", ["context", "presenters", "routes_system", "routes_memory", "routes_modules", "routes_attention"])
def test_dashboard_files_are_small(module):
    path = AGENT / "dashboard" / f"{module}.py"
    assert len(path.read_text(encoding="utf-8").splitlines()) < 320
