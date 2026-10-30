"""Один агент на игру: ход, планирование, цели и рефлексия идут в одной постоянной сессии, а не отдельными запросами."""
import asyncio

import glados_agent as g
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


class FakeClient:
    instances: list = []

    def __init__(self, options):
        self.options = options
        self.prompts: list[str] = []
        self.closed = False
        FakeClient.instances.append(self)

    async def connect(self):
        pass

    async def disconnect(self):
        self.closed = True

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text="Готово.")], model="haiku")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
                            total_cost_usd=0.0, usage={}, result="Готово.")


def _fresh(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(g, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(g.claude_quota, "blocked", lambda: False)
    monkeypatch.setattr(g.usage_report, "push_result", lambda *a, **k: None)
    g._session.client, g._session.lock, g._session.queries = None, None, 0


def test_all_roles_share_one_session(monkeypatch):
    _fresh(monkeypatch)

    async def go():
        r1 = await g.run_turn("память", [], 1.0)
        r2 = await g.run_planning("память")
        r3 = await g.run_long_term_planning("память")
        return r1, r2, r3
    r1, r2, r3 = asyncio.run(go())
    assert len(FakeClient.instances) == 1                      # один клиент на всё
    prompts = FakeClient.instances[0].prompts
    assert [p.splitlines()[0] for p in prompts] == [
        "=== РЕЖИМ: ХОД (игровой ход: действуй и в конце коротко скажи вслух) ===",
        "=== РЕЖИМ: ПЛАНИРОВАНИЕ (раскладка целей на задачи) ===", "=== РЕЖИМ: ДОЛГИЕ ЦЕЛИ ==="]
    assert r1["commentary"] == "Готово." and r2["success"] and r3["contour"] == "goals"
    assert "РЕЖИМ: ПЛАНИРОВАНИЕ" in FakeClient.instances[0].options.system_prompt      # роли в одной системной подсказке


def test_session_restarts_after_limit_and_failure(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setattr(g, "SESSION_MAX_QUERIES", 2)

    async def go():
        for _ in range(3):
            await g.run_planning("память")
        await g.reset_session()
        await g.run_planning("память")
    asyncio.run(go())
    assert len(FakeClient.instances) == 3                      # лимит сообщений и явный сброс начинают сессию заново
    assert FakeClient.instances[0].closed and FakeClient.instances[1].closed
