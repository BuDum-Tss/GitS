"""Соответствие контракту на стороне ядра (ТЗ 31, MC-7): те же примеры, что в `sdk/tests/test_contract_conformance.py`,
через настоящие `module_hub` и `core_requests`. Ядро и FakeCore обязаны отвечать одинаково."""
import pytest
from hub_helpers import RawModule, manifest, running_core

import events_log
import fast_brain
from gits_sdk import contract, testing
from gits_sdk import protocol as P
from gits_sdk.contract import messages as M

REQUESTS = list(M.CORE_REQUEST_MESSAGES)
IMPLEMENTED = [m for m in REQUESTS if m.status != contract.PLANNED]
PLANNED = [m for m in REQUESTS if m.status == contract.PLANNED]


@pytest.fixture(autouse=True)
def _no_external_services(monkeypatch):
    """Тесты не ходят в локальную модель."""
    async def fake_ask(*args, **kwargs):
        return "{}"
    monkeypatch.setattr(fast_brain, "_ask", fake_ask)

    async def fake_search(query):
        return "нашлось"
    import web_search
    monkeypatch.setattr(web_search, "search", fake_search)


def _answering(core):
    """Вопросы пользователю закрываются сразу: ждать человека тест не может."""
    async def answer(q):
        return True, "test"
    core.attention.submit_question = answer


def _code(reply: dict) -> str | None:
    return (reply.get("error") or {}).get("code")


@pytest.mark.parametrize("m", IMPLEMENTED, ids=lambda m: m.name)
async def test_core_rejects_invalid_and_does_not_reject_valid(tmp_path, m):
    async with running_core(tmp_path) as core:
        _answering(core)
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            for body in m.invalid:
                if isinstance(body, dict):
                    assert _code(await raw.request(m.name, body)) == "bad_request", (m.name, body)
            for body in m.valid:
                # зависимостей у тестового ядра нет (нет модели, голоса, списка слов): любой ответ, кроме bad_request
                assert _code(await raw.request(m.name, body, timeout=8)) != "bad_request", (m.name, body)
        finally:
            await raw.close()


@pytest.mark.parametrize("m", PLANNED, ids=lambda m: m.name)
async def test_core_answers_unavailable_for_planned_requests(tmp_path, m):
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            for body in (*m.valid, {}):
                reply = await raw.request(m.name, body)
                assert _code(reply) == "unavailable" and contract.CONTRACT_VERSION in reply["error"]["message"]
        finally:
            await raw.close()


async def test_core_and_fakecore_give_the_same_verdict(tmp_path):
    """Для каждого примера запроса вердикт «bad_request / нет» один у ядра и у FakeCore."""
    async with running_core(tmp_path) as core, testing.FakeCore() as fake:
        _answering(core)
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            fake.install_contract_defaults()
            for m in IMPLEMENTED:
                for body in (b for b in (*m.valid, *m.invalid) if isinstance(b, dict)):
                    core_bad = _code(await raw.request(m.name, body, timeout=8)) == "bad_request"
                    fake_bad = bool(contract.validate(m.name, body))
                    assert core_bad == fake_bad, (m.name, body)
        finally:
            await raw.close()


async def test_core_writes_valid_log_notes_to_the_journal(tmp_path):
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            for body in M._log.valid:
                await raw.send("log", body)
            for body in M._log.invalid:
                if isinstance(body, dict):
                    await raw.send("log", body)
            await core.wait_for(lambda: (events_log.flush(), len(events_log.tail(50, {"error", "telegram"})) >= 2)[1])
            rows = events_log.tail(50, {"error", "telegram"})
            assert [r["text"] for r in rows] == [b["text"] for b in M._log.valid]
            assert not events_log.tail(50, {"user"})                               # чужой вид отброшен
        finally:
            await raw.close()


async def test_llm_ask_goes_through_fast_brain_with_the_requested_parameters(tmp_path, monkeypatch):
    seen = {}

    async def fake_ask(system, user, timeout, max_tokens=40, image_b64=None, temperature=0.3, background=False):
        seen.update(system=system, user=user, timeout=timeout, max_tokens=max_tokens, temperature=temperature,
                    background=background, image=image_b64)
        return 'вот {"summary": "джаз"}'
    monkeypatch.setattr(fast_brain, "_ask", fake_ask)
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            reply = await raw.request("core.llm.ask", {"system": "s", "user": "u", "timeout_s": 30, "max_tokens": 100,
                                                       "temperature": 0.2, "background": True, "json": True,
                                                       "image_b64": "QUJD"})
            assert reply["ok"] and reply["value"]["data"] == {"summary": "джаз"} and "джаз" in reply["value"]["text"]
            assert seen == {"system": "s", "user": "u", "timeout": 30.0, "max_tokens": 100, "temperature": 0.2,
                            "background": True, "image": "QUJD"}
            assert not contract.validate_response("core.llm.ask", reply["value"])

            async def preempted(*a, **k):
                raise fast_brain.Preempted("занято")
            monkeypatch.setattr(fast_brain, "_ask", preempted)
            assert _code(await raw.request("core.llm.ask", {"system": "s", "user": "u"})) == "unavailable"
        finally:
            await raw.close()


async def test_events_tail_returns_only_allowed_kinds(tmp_path):
    async with running_core(tmp_path) as core:
        events_log.log("game", "колонист поймал рыбу")
        events_log.log("user", "личное сообщение")
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            reply = await raw.request("core.events.tail", {"limit": 10, "kinds": ["game"]})
            assert [e["text"] for e in reply["value"]["events"]] == ["колонист поймал рыбу"]
            assert _code(await raw.request("core.events.tail", {"kinds": ["user"]})) == "bad_request"
            texts = [e["text"] for e in (await raw.request("core.events.tail", {}))["value"]["events"]]
            assert "личное сообщение" not in texts                                # без kinds - только безопасные виды
        finally:
            await raw.close()


async def test_claude_locate_returns_an_object(tmp_path, monkeypatch):
    from gits_sdk import claude_path
    monkeypatch.setattr(claude_path, "locate", lambda: "C:/claude.exe")
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            assert (await raw.request("core.claude.locate", {}))["value"] == {"path": "C:/claude.exe"}
            monkeypatch.setattr(claude_path, "locate", lambda: None)
            assert (await raw.request("core.claude.locate", {}))["value"] == {}
        finally:
            await raw.close()


def test_protocol_lists_agree_with_the_contract():
    assert set(P.CORE_REQUESTS) == set(contract.CORE_REQUESTS)
    assert "log" in P.MODULE_TO_CORE_NOTES
