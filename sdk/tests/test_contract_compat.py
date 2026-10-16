"""Версии и совместимость контракта (ТЗ 31, MC-8): старые манифесты и тела сообщений проходят проверку текущей версии.

Примеры ниже ЗАФИКСИРОВАНЫ (копии того, что слали модули в версии контракта 1.0): их нельзя подгонять под новую версию.
Если тест упал, изменение несовместимо: либо верните совместимость, либо поднимите `PROTOCOL` и опишите это в CHANGELOG.
"""
import re

from gits_sdk import contract
from gits_sdk import protocol as P
from gits_sdk.contract import versions

# манифест эпохи 1.0 (ТЗ 01): ни одного поля, появившегося позже
OLD_MANIFEST = {
    "id": "legacy", "title": "Старый модуль", "description": "Модуль, написанный по контракту 1.0.", "version": "1.0.0",
    "capabilities": [], "subscribe": ["presence", "settings"], "shared_settings": ["dnd"],
    "settings_schema": {"poll_seconds": {"type": "integer", "title": "Опрос", "default": 5, "min": 1, "max": 60}},
    "tools": [{"name": "ping", "description": "Ответить pong.", "params": {"text": {"type": "string"}},
               "risk": "auto", "availability": "always"}],
    "emits": [{"kind": "legacy.thing", "description": "Что-то случилось", "default_urgency": 2}],
    "state": True,
}

# тела сообщений эпохи 1.0
OLD_BODIES = {
    "event": [{"kind": "legacy.thing", "text": "что-то случилось"},
              {"kind": "legacy.thing", "text": "сообщение", "data": {"a": 1}, "urgency_hint": 3, "addressed_to_me": True,
               "sender": {"id": "42", "name": "Маша"}, "reply_channel": {"module": "telegram", "target": "42"},
               "dedupe_key": "k", "expires_in_s": 30, "memorable": True}],
    "say": [{"text": "привет"}, {"text": "Готово", "mode": "verbatim", "emotion": "joy", "key": "k", "urgency_hint": 2}],
    "job.update": [{"job_id": "j-0123abcd", "state": "running", "title": "Сборка", "progress": 0.3},
                   {"job_id": "j-0123abcd", "state": "done", "artifacts": [{"path": "C:/out.mp4"}]}],
    "core.ask": [{"kind": "approval", "question": "Можно?"},
                 {"kind": "choice", "question": "Что?", "options": ["а", "б"], "timeout_s": 20, "context": {"x": 1}}],
    "core.tts": [{"text": "привет"}, {"text": "привет", "emotion": "joy", "format": "ogg"}],
    "core.tool.call": [{"tool": "echo.ping", "args": {"text": "x"}}],
    "core.persona": [{}],
    "core.privacy_terms": [{}],
    "core.claude_usage": [{"tokens": 100, "source": "desktop"}, {"tokens": 0, "limit_text": "лимит"}],
    "core.web_search": [{"query": "погода"}],
    "state": [{"summary": "работает", "data": {"n": 1}}],
    "panel.set": [{"blocks": [{"type": "text", "text": "привет"}]}],
    "deliver": [{"target": "42", "text": "ответ"}],
    "tts.synthesize": [{"text": "привет", "emotion": None, "format": "wav"}],
    "panel.action": [{"action_id": "refresh", "args": {}}],
    "tool.call": [{"tool": "ping", "args": {"text": "x"}}],
    "tool.check": [{"tool": "ping", "args": {}}],
    "job.cancel": [{"job_id": "j-0123abcd"}],
    "job.list": [{}],
    "graph.get": [{}],
    "policy.activate": [{"policy": "working", "goal": "g", "task_id": "t-1", "event_id": "e-1", "priority": 2.0, "reason": "r"}],
    "focus.release": [{"reason": "r", "task_id": "t-1", "force": False, "event_id": "e-1", "checkpoint": True}],
    "focus.resume": [{"task_id": "t-1", "policy": "working", "goal": "g", "checkpoint": {}, "subgoals": []}],
}

# имена сообщений версии 1.0: удалять нельзя без повышения PROTOCOL
NAMES_1_0 = {"hello", "event", "say", "emotion", "state", "job.update", "panel.set", "core.ask", "core.tts", "core.tool.call",
             "core.lookup", "core.memory.recall", "core.memory.remember", "core.experience.add", "core.kb.ingest",
             "core.persona", "core.privacy_terms", "core.claude_usage", "core.web_search", "tool.call", "tool.check",
             "job.cancel", "job.list", "graph.get", "deliver", "tts.synthesize", "panel.action", "policy.activate",
             "focus.release", "focus.resume", "welcome", "reject", "job.answer", "speak", "speak.stop", "shutdown",
             "presence", "settings", "profile", "battery", "character", "module"}


def test_old_manifest_still_passes():
    assert P.validate_manifest(OLD_MANIFEST) == []
    hello = P.hello_body(OLD_MANIFEST, "inst0001", 1234)
    assert hello["protocol"] == 1 and P.validate_hello(hello) == []


def test_old_message_bodies_still_pass():
    for name, bodies in OLD_BODIES.items():
        for body in bodies:
            assert contract.validate(name, body) == [], (name, body)


def test_no_message_of_version_1_0_was_removed():
    assert NAMES_1_0 <= set(contract.REGISTRY)
    for name in NAMES_1_0:
        assert contract.get(name).since == "1.0", f"{name}: у элемента 1.0 версия появления должна остаться 1.0"


def test_protocol_and_contract_versions_agree():
    assert P.PROTOCOL == versions.PROTOCOL == 1
    assert re.fullmatch(r"\d+\.\d+", versions.CONTRACT_VERSION)
    assert versions.CONTRACT_VERSION.split(".")[0] == str(P.PROTOCOL)            # мажор контракта = протокол
    cur = tuple(int(x) for x in versions.CONTRACT_VERSION.split("."))
    for m in contract.REGISTRY.values():
        assert tuple(int(x) for x in m.since.split(".")) <= cur, f"{m.name}: появился позже текущей версии"
    assert [v for v, _ in versions.HISTORY][-1] == versions.CONTRACT_VERSION


def test_new_elements_are_marked_with_their_version():
    for name in ("core.llm.ask", "core.events.tail", "core.claude.locate", "log"):
        assert contract.get(name).since == "1.1"


def test_deprecated_entries_have_removal_dates():
    for item, date, replacement in versions.DEPRECATED:
        assert item in contract.REGISTRY or "." in item
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) and replacement
    for m in contract.REGISTRY.values():
        if m.status == "deprecated":
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", m.deprecated_on), f"{m.name}: нужна дата удаления"


def test_unknown_new_message_types_are_ignored_by_the_protocol_table():
    """Получатель игнорирует неизвестный тип сообщения (MOD-10): таблицы протокола не требуют закрытого списка типов."""
    assert not P.is_request("future.request")
    assert "future.note" not in P.MODULE_TO_CORE and "future.note" not in P.CORE_TO_MODULE
