"""Протокол (MOD-1…MOD-16, разделы 4–9): манифест, конверт, result, hello, события, речь, вопросы, задачи."""
import copy
import json

import pytest

from glados_module import protocol as P
from glados_module.protocol import Envelope, ManifestError, ProtocolError, ToolError

GOOD = {
    "id": "rimworld",
    "title": "RimWorld",
    "description": "Игра-симулятор колонии: колонисты, строительство, исследования, бой.",
    "version": "1.0.0",
    "capabilities": ["game"],
    "subscribe": ["presence", "settings", "profile", "module"],
    "shared_settings": ["streamer", "dnd", "quiet_until", "game_model", "claude_usage", "voice_muted"],
    "settings_schema": {
        "poll_seconds": {"type": "integer", "title": "Опрос игры, секунд", "default": 5, "min": 1, "max": 60}
    },
    "tools": [{
        "name": "order",
        "description": "Передать игроку-агенту приказ по игре своими словами пользователя.",
        "params": {"text": {"type": "string", "description": "приказ"}},
        "risk": "auto", "availability": "profile", "informative": False, "timeout_s": 15,
        "keywords": ["игра", "колония", "колонист", "римворлд"], "danger_check": False, "job": False,
    }],
    "emits": [
        {"kind": "rimworld.raid", "description": "Начался набег на колонию", "default_urgency": 5},
        {"kind": "rimworld.letter", "description": "Игровое письмо", "default_urgency": 2},
    ],
    "knowledge": [{"collection": "rimworld.wiki", "title": "Вики RimWorld", "scope": "rimworld", "kind": "reference"}],
    "graph": True,
    "state": True,
}


def make(**changes):
    m = copy.deepcopy(GOOD)
    for k, v in changes.items():
        if v is KeyError:
            m.pop(k, None)
        else:
            m[k] = v
    return m


def tool(**changes):
    t = copy.deepcopy(GOOD["tools"][0])
    for k, v in changes.items():
        if v is KeyError:
            t.pop(k, None)
        else:
            t[k] = v
    return t


def problems_of(m):
    return P.validate_manifest(m)


# ---------------------------------------------------------------- манифест: верные

def test_manifest_from_spec_is_valid():
    assert problems_of(GOOD) == []


def test_minimal_manifest_is_valid():
    assert problems_of({"id": "ab", "title": "T", "description": "d", "version": "0.0.1"}) == []


def test_tool_with_all_optional_fields_is_valid():
    t = tool(self_confirming=True, fallback=True, danger_check=True, job=True, timeout_s=120, announce=True,
             core_fills=["context"], params={"text": {"type": "string"}, "context": {"type": "string"}})
    assert problems_of(make(tools=[t])) == []
    assert P.Manifest.from_dict(make(tools=[t])).tools[0].announce is True            # AN-3


@pytest.mark.parametrize("version", ["1.0.0", "0.12.345", "1.0.0-rc.1", "2.1.0+build5"])
def test_semver_ok(version):
    assert problems_of(make(version=version)) == []


# ---------------------------------------------------------------- манифест: неверные, по полям

@pytest.mark.parametrize("manifest, needle", [
    ("не объект", "объектом"),
    (make(id=KeyError), "id"),
    (make(id="R"), "id"),
    (make(id="a"), "id"),                  # короче двух символов
    (make(id="1abc"), "id"),
    (make(id="a" * 33), "id"),
    (make(id="rim-world"), "id"),
    (make(id=5), "id"),
    (make(title=KeyError), "title"),
    (make(title=""), "title"),
    (make(title=3), "title"),
    (make(description=KeyError), "description"),
    (make(description="x" * 301), "description"),
    (make(version=KeyError), "version"),
    (make(version="1.0"), "version"),
    (make(version="v1.0.0"), "version"),
    (make(version=1), "version"),
    (make(capabilities="game"), "capabilities"),
    (make(capabilities=["magic"]), "capabilities"),
    (make(subscribe=["weather"]), "subscribe"),
    (make(subscribe="presence"), "subscribe"),
    (make(shared_settings="streamer"), "shared_settings"),
    (make(shared_settings=[1]), "shared_settings"),
    (make(settings_schema=[]), "settings_schema"),
    (make(tools={}), "tools"),
    (make(emits={}), "emits"),
    (make(knowledge={}), "knowledge"),
    (make(graph="yes"), "graph"),
    (make(state=1), "state"),
])
def test_manifest_invalid(manifest, needle):
    problems = problems_of(manifest)
    assert problems, "ожидались проблемы"
    assert any(needle in p for p in problems), problems


# ---------------------------------------------------------------- инструменты (6.1)

@pytest.mark.parametrize("changes, needle", [
    ({"name": KeyError}, "name"),
    ({"name": "Order"}, "name"),
    ({"name": "o"}, "name"),
    ({"name": "a" * 49}, "name"),
    ({"description": KeyError}, "description"),
    ({"description": ""}, "description"),
    ({"description": "x" * 301}, "description"),
    ({"params": KeyError}, "params"),
    ({"params": []}, "params"),
    ({"params": {"text": "string"}}, "params.text"),
    ({"params": {"text": {"type": "date"}}}, "type"),
    ({"params": {"text": {}}}, "type"),
    ({"params": {"text": {"type": "string", "required": "yes"}}}, "required"),
    ({"params": {"text": {"type": "string", "enum": []}}}, "enum"),
    ({"params": {"text": {"type": "string", "description": 5}}}, "description"),
    ({"risk": KeyError}, "risk"),
    ({"risk": "yolo"}, "risk"),
    ({"availability": KeyError}, "availability"),
    ({"availability": "sometimes"}, "availability"),
    ({"informative": "да"}, "informative"),
    ({"self_confirming": 1}, "self_confirming"),
    ({"danger_check": "x"}, "danger_check"),
    ({"job": None}, "job"),
    ({"fallback": "true"}, "fallback"),
    ({"announce": "да"}, "announce"),
    ({"timeout_s": 0}, "timeout_s"),
    ({"timeout_s": 121}, "timeout_s"),
    ({"timeout_s": 5.5}, "timeout_s"),
    ({"timeout_s": True}, "timeout_s"),
    ({"keywords": "игра"}, "keywords"),
    ({"keywords": [1]}, "keywords"),
    ({"core_fills": "context"}, "core_fills"),
    ({"core_fills": ["screen"]}, "core_fills"),
    ({"core_fills": ["context"]}, "core_fills"),     # параметра context нет в params
])
def test_tool_invalid(changes, needle):
    problems = problems_of(make(tools=[tool(**changes)]))
    assert problems and any(needle in p for p in problems), problems


def test_tool_names_must_be_unique():
    assert any("повторяется" in p for p in problems_of(make(tools=[tool(), tool()])))


def test_tool_must_be_object():
    assert problems_of(make(tools=["order"]))


# ---------------------------------------------------------------- события, знания, настройки

@pytest.mark.parametrize("emit, needle", [
    ({"kind": "raid", "description": "x", "default_urgency": 3}, "kind"),
    ({"kind": "other.raid", "description": "x", "default_urgency": 3}, "rimworld."),    # MOD-16
    ({"kind": "rimworld.Raid", "description": "x", "default_urgency": 3}, "kind"),
    ({"description": "x", "default_urgency": 3}, "kind"),
    ({"kind": "rimworld.raid", "default_urgency": 3}, "description"),
    ({"kind": "rimworld.raid", "description": "x"}, "default_urgency"),
    ({"kind": "rimworld.raid", "description": "x", "default_urgency": 6}, "default_urgency"),
    ({"kind": "rimworld.raid", "description": "x", "default_urgency": -1}, "default_urgency"),
    ({"kind": "rimworld.raid", "description": "x", "default_urgency": "3"}, "default_urgency"),
    ("rimworld.raid", "объектом"),
])
def test_emit_invalid(emit, needle):
    problems = problems_of(make(emits=[emit]))
    assert problems and any(needle in p for p in problems), problems


def test_emit_kinds_unique():
    e = {"kind": "rimworld.raid", "description": "x", "default_urgency": 3}
    assert any("повторяется" in p for p in problems_of(make(emits=[e, dict(e)])))


@pytest.mark.parametrize("missing", ["collection", "title", "scope", "kind"])
def test_knowledge_fields_required(missing):
    coll = dict(GOOD["knowledge"][0])
    coll.pop(missing)
    problems = problems_of(make(knowledge=[coll]))
    assert any(missing in p for p in problems), problems


def test_knowledge_collections_unique():
    coll = dict(GOOD["knowledge"][0])
    assert any("повторяется" in p for p in problems_of(make(knowledge=[coll, dict(coll)])))


@pytest.mark.parametrize("name, spec, needle", [
    ("Bad", {"type": "boolean", "title": "t", "default": True}, "имя"),
    ("a", {"type": "date", "title": "t", "default": 1}, "type"),
    ("a", {"type": "boolean", "default": True}, "title"),
    ("a", {"type": "boolean", "title": "t"}, "default"),
    ("a", {"type": "boolean", "title": "t", "default": 1}, "default"),
    ("a", {"type": "integer", "title": "t", "default": True}, "default"),
    ("a", {"type": "integer", "title": "t", "default": 1.5}, "default"),
    ("a", {"type": "integer", "title": "t", "default": 0, "min": 1}, "меньше min"),
    ("a", {"type": "integer", "title": "t", "default": 9, "max": 5}, "больше max"),
    ("a", {"type": "integer", "title": "t", "default": 3, "min": 5, "max": 1}, "min больше max"),
    ("a", {"type": "string", "title": "t", "default": "x", "min": 1}, "только для"),
    ("a", {"type": "string", "title": "t", "default": "x", "enum": ["y"]}, "enum"),
    ("a", {"type": "string", "title": "t", "default": "x", "enum": []}, "enum"),
    ("a", {"type": "string", "title": "t", "default": "x", "secret": True}, "секрет"),
    ("a", {"type": "string", "title": "t", "default": "x", "secret": "нет"}, "secret"),
    ("a", "строка", "объектом"),
])
def test_settings_schema_invalid(name, spec, needle):
    problems = problems_of(make(settings_schema={name: spec}))
    assert problems and any(needle in p for p in problems), problems


def test_settings_schema_valid_variants():
    schema = {
        "flag": {"type": "boolean", "title": "Флаг", "default": False},
        "ratio": {"type": "number", "title": "Доля", "default": 0.5, "min": 0, "max": 1},
        "mode": {"type": "string", "title": "Режим", "default": "a", "enum": ["a", "b"]},
        "secret_off": {"type": "string", "title": "Не секрет", "default": "", "secret": False},
    }
    assert problems_of(make(settings_schema=schema)) == []


# ---------------------------------------------------------------- Manifest (dataclass)

def test_manifest_dataclass_roundtrip():
    m = P.Manifest.from_dict(GOOD)
    assert m.id == "rimworld" and m.tools[0].full_id("rimworld") == "rimworld.order"
    assert m.tool("order").timeout_s == 15 and m.tool("nope") is None
    assert m.setting_defaults() == {"poll_seconds": 5}
    again = m.to_dict()
    assert P.validate_manifest(again) == []
    assert P.Manifest.from_dict(again) == m


def test_manifest_dataclass_defaults_for_tool():
    m = P.Manifest.from_dict(make(tools=[tool(timeout_s=KeyError)]))
    t = m.tools[0]
    assert t.timeout_s == 30 and not t.fallback and t.core_fills == [] and not t.self_confirming and not t.announce


def test_manifest_dataclass_rejects_invalid():
    with pytest.raises(ManifestError) as e:
        P.Manifest.from_dict(make(id="X"))
    assert e.value.problems


def test_unknown_manifest_fields_are_ignored():
    # MOD-11: неизвестные поля не мешают
    m = make(future_field={"a": 1})
    m["tools"][0]["future_tool_field"] = 1
    assert problems_of(m) == []


# ---------------------------------------------------------------- hello

def test_hello_body_validates():
    body = P.hello_body(GOOD, "c0ffee12", 123)
    assert body["module_id"] == "rimworld" and body["protocol"] == 1
    assert P.validate_hello(body) == []


@pytest.mark.parametrize("changes, needle", [
    ({"protocol": 2}, "protocol"),
    ({"module_id": "other"}, "не совпадает"),
    ({"module_id": "BAD"}, "module_id"),
    ({"instance": ""}, "instance"),
    ({"pid": "1"}, "pid"),
    ({"manifest": None}, "манифест"),
])
def test_hello_invalid(changes, needle):
    body = {**P.hello_body(GOOD, "c0ffee12", 123), **changes}
    problems = P.validate_hello(body)
    assert problems and any(needle in p for p in problems), problems


def test_hello_manifest_problems_are_reported():
    body = P.hello_body(make(version="x"), "c0ffee12", 1)
    assert any("version" in p for p in P.validate_hello(body))


# ---------------------------------------------------------------- конверт и result

def test_envelope_roundtrip():
    env = Envelope("event", {"kind": "echo.x", "text": "привет"})
    assert env.id.startswith("m-") and len(env.id) == 18 and env.v == 1 and env.re is None
    raw = env.dumps()
    assert "привет" in raw                       # UTF-8, а не \u-экранирование
    back = Envelope.loads(raw)
    assert back.to_dict() == env.to_dict()
    assert set(json.loads(raw)) == {"v", "type", "id", "re", "ts", "body"}


def test_envelope_ids_are_unique():
    assert len({P.new_id() for _ in range(200)}) == 200


@pytest.mark.parametrize("raw", [
    "не json",
    "[]",
    "{}",
    json.dumps({"v": 2, "type": "x", "id": "m-1", "re": None, "ts": 1, "body": {}}),
    json.dumps({"v": 1, "type": "", "id": "m-1", "re": None, "ts": 1, "body": {}}),
    json.dumps({"v": 1, "type": "x", "id": "", "re": None, "ts": 1, "body": {}}),
    json.dumps({"v": 1, "type": "x", "id": "m-1", "re": 5, "ts": 1, "body": {}}),
    json.dumps({"v": 1, "type": "x", "id": "m-1", "re": None, "ts": "now", "body": {}}),
    json.dumps({"v": 1, "type": "x", "id": "m-1", "re": None, "ts": 1, "body": []}),
])
def test_envelope_rejects_bad_frames(raw):
    with pytest.raises(ProtocolError):
        Envelope.loads(raw)


def test_envelope_ignores_unknown_fields():
    env = Envelope.loads(json.dumps({"v": 1, "type": "x", "id": "m-1", "re": None, "ts": 1, "body": {"a": 1},
                                     "новое": True}))
    assert env.body == {"a": 1}


def test_envelope_size_limit():
    big = Envelope("event", {"text": "я" * P.MAX_MESSAGE_BYTES})
    with pytest.raises(ProtocolError):
        big.dumps()
    with pytest.raises(ProtocolError):
        Envelope.loads(b"x" * (P.MAX_MESSAGE_BYTES + 1))


def test_envelope_not_json_serializable():
    with pytest.raises(ProtocolError):
        Envelope("result", {"value": object()}).dumps()
    with pytest.raises(ProtocolError):
        Envelope("result", {"value": float("nan")}).dumps()


def test_result_ok_and_error():
    ok = P.result_ok("m-req", {"a": 1})
    assert ok.type == "result" and ok.re == "m-req" and ok.body == {"ok": True, "value": {"a": 1}}
    assert P.unwrap_result(ok) == {"a": 1}
    err = P.result_error("m-req", "timeout", "не успел")
    assert err.body == {"ok": False, "error": {"code": "timeout", "message": "не успел"}}
    with pytest.raises(ToolError) as e:
        P.unwrap_result(err)
    assert e.value.code == "timeout" and e.value.message == "не успел"


def test_error_codes_are_closed_set():
    assert set(P.ERROR_CODES) == {"bad_request", "unknown", "denied", "unavailable", "timeout", "cancelled",
                                  "internal"}
    assert P.result_error("m-1", "weird", "x").body["error"]["code"] == "internal"
    assert ToolError("x", code="weird").code == "internal"
    assert ToolError("x").code in P.ERROR_CODES


def test_request_types():
    for t in ("tool.call", "tool.check", "job.cancel", "job.list", "graph.get", "deliver", "tts.synthesize",
              "core.ask", "core.tts", "core.tool.call", "core.web_search"):
        assert P.is_request(t), t
    for t in ("event", "say", "emotion", "state", "job.update", "job.answer", "speak", "speak.stop", "topic",
              "shutdown", "welcome", "reject", "hello", "result"):
        assert not P.is_request(t), t


def test_reconnect_delays():
    assert [P.reconnect_delay(i) for i in range(9)] == [1, 2, 4, 8, 16, 30, 30, 30, 30]


def test_token_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert P.read_token_file() is None
    token = P.new_token()
    assert len(token) == 64
    path = P.write_token_file(token)
    assert path == tmp_path / "GLaDOS" / "module_token" and P.read_token_file() == token


def test_core_url_env(monkeypatch):
    monkeypatch.delenv("GLADOS_CORE_URL", raising=False)
    assert P.core_url() == "ws://127.0.0.1:8766/module/ws"
    monkeypatch.setenv("GLADOS_CORE_URL", "ws://127.0.0.1:9/x")
    assert P.core_url() == "ws://127.0.0.1:9/x"


# ---------------------------------------------------------------- событие, речь, вопрос, задача

GOOD_EVENT = {
    "kind": "telegram.dm", "text": "Сообщение от Маши в личке: «ты скоро?»", "data": {"chat_id": 123},
    "urgency_hint": 3, "addressed_to_me": True, "sender": {"id": "tg:123", "name": "Маша", "is_owner": False},
    "reply_channel": {"module": "telegram", "target": "tg:123"}, "dedupe_key": "telegram.dm:123:456",
    "expires_in_s": 900, "memorable": True,
}


def test_event_valid_and_dataclass():
    assert P.validate_event(GOOD_EVENT) == []
    assert P.validate_event({"kind": "echo.x", "text": "t"}) == []
    ev = P.Event.from_body(GOOD_EVENT)
    assert ev.to_body() == GOOD_EVENT
    assert P.Event.from_body({"kind": "echo.x", "text": "t"}).to_body() == {"kind": "echo.x", "text": "t"}


@pytest.mark.parametrize("changes, needle", [
    ({"kind": "dm"}, "kind"),
    ({"kind": KeyError}, "kind"),
    ({"text": ""}, "text"),
    ({"text": KeyError}, "text"),
    ({"text": "я" * 1001}, "1000"),
    ({"data": []}, "data"),
    ({"urgency_hint": 6}, "urgency_hint"),
    ({"urgency_hint": "3"}, "urgency_hint"),
    ({"addressed_to_me": "да"}, "addressed_to_me"),
    ({"memorable": 1}, "memorable"),
    ({"sender": {"name": "x"}}, "sender"),
    ({"sender": {"id": "a", "is_owner": "x"}}, "is_owner"),
    ({"reply_channel": {"module": "telegram"}}, "reply_channel"),
    ({"dedupe_key": 1}, "dedupe_key"),
    ({"expires_in_s": 0}, "expires_in_s"),
    ({"expires_in_s": "5"}, "expires_in_s"),
])
def test_event_invalid(changes, needle):
    body = copy.deepcopy(GOOD_EVENT)
    for k, v in changes.items():
        if v is KeyError:
            body.pop(k)
        else:
            body[k] = v
    problems = P.validate_event(body)
    assert problems and any(needle in p for p in problems), problems
    with pytest.raises(ProtocolError):
        P.Event.from_body(body)


def test_say_validation():
    assert P.validate_say({"text": "Борис уснул."}) == []
    assert P.validate_say({"text": "x", "mode": "verbatim", "emotion": "angry", "urgency_hint": 2,
                           "expires_in_s": 30, "key": "rimworld.comment"}) == []
    assert P.Say.from_body({"text": "x"}).to_body() == {"text": "x", "mode": "fact"}
    for bad in ({"text": ""}, {"text": "x", "mode": "loud"}, {"text": "x", "emotion": 1},
                {"text": "x", "urgency_hint": 9}, {"text": "x", "expires_in_s": -1}, {"text": "x", "key": 1},
                {"text": "x", "announce": "да"}, []):
        assert P.validate_say(bad), bad
    assert P.validate_say({"text": "Запускаю игру.", "announce": True}) == []                          # AN-3
    assert P.Say.from_body({"text": "x", "announce": True}).to_body() == {"text": "x", "mode": "fact", "announce": True}


def test_ask_validation():
    assert P.validate_ask({"kind": "approval", "question": "Удалить?"}) == []
    assert P.validate_ask({"kind": "choice", "question": "Что?", "options": ["а", "б"], "timeout_s": 86400}) == []
    assert P.validate_ask({"kind": "text", "question": "Как назвать?", "context": {"job_id": "j-7a1c0000"}}) == []
    for bad in ({"kind": "yesno", "question": "q"}, {"kind": "approval", "question": ""},
                {"kind": "choice", "question": "q"}, {"kind": "choice", "question": "q", "options": []},
                {"kind": "approval", "question": "q", "options": [1]},
                {"kind": "approval", "question": "q", "timeout_s": 0},
                {"kind": "approval", "question": "q", "timeout_s": 86401},
                {"kind": "approval", "question": "q", "context": "x"},
                {"kind": "approval", "question": "q", "urgency_hint": 7}):
        assert P.validate_ask(bad), bad
    assert P.validate_ask({"kind": "approval", "question": "q"}, question_id=True)     # нужен question_id


GOOD_JOB = {
    "job_id": "j-7a1c0000", "title": "Шортс", "state": "waiting_user", "stage": "plan_approval", "progress": 0.35,
    "text": "План готов.",
    "question": {"question_id": "q-1", "kind": "choice", "question": "Утвердить?",
                 "options": ["утвердить", "переделать"], "timeout_s": 86400},
    "artifacts": [{"path": "C:\\jobs\\plan.md", "kind": "plan"}], "error": None,
}


def test_job_update_valid():
    assert P.validate_job_update(GOOD_JOB) == []
    assert P.validate_job_update({"job_id": "j-00000001", "state": "queued"}) == []
    done = P.JobUpdate.from_body({"job_id": "j-00000001", "state": "done", "progress": 1})
    assert done.finished and done.to_body() == {"job_id": "j-00000001", "state": "done", "progress": 1}
    failed = {"job_id": "j-00000001", "state": "failed", "error": {"code": "internal", "message": "сбой"}}
    assert P.validate_job_update(failed) == []


@pytest.mark.parametrize("changes, needle", [
    ({"job_id": "j-1"}, "job_id"),
    ({"job_id": "job1"}, "job_id"),
    ({"job_id": KeyError}, "job_id"),
    ({"state": "paused"}, "state"),
    ({"state": KeyError}, "state"),
    ({"progress": 1.5}, "progress"),
    ({"progress": "half"}, "progress"),
    ({"title": 5}, "title"),
    ({"question": None}, "waiting_user"),                               # MOD-20
    ({"question": {"kind": "choice", "question": "q", "options": ["a"]}}, "question_id"),
    ({"artifacts": [{"kind": "plan"}]}, "artifacts"),
    ({"state": "failed", "question": None}, "error"),                   # MOD-21
    ({"state": "failed", "question": None, "error": {"code": "weird", "message": "x"}}, "error"),
])
def test_job_update_invalid(changes, needle):
    body = copy.deepcopy(GOOD_JOB)
    for k, v in changes.items():
        if v is KeyError:
            body.pop(k)
        else:
            body[k] = v
    problems = P.validate_job_update(body)
    assert problems and any(needle in p for p in problems), problems


def test_new_job_id_format():
    for _ in range(50):
        assert P.JOB_ID_RE.match(P.new_job_id())


def test_clip():
    assert P.clip("abc", 5) == "abc" and P.clip("abcdef", 4) == "abc…" and len(P.clip("я" * 2000, 1000)) == 1000
