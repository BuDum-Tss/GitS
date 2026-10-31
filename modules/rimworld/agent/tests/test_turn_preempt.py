"""Срочное дело обрывает ход игры: единая сессия начинается заново, а не остаётся посреди ответа."""
import asyncio

import glados_agent as g


class HangingClient:
    instances = []

    def __init__(self, options):
        self.disconnected = False
        HangingClient.instances.append(self)

    async def connect(self):
        pass

    async def query(self, prompt):
        pass

    async def receive_response(self):
        await asyncio.sleep(60)
        yield None

    async def disconnect(self):
        self.disconnected = True


def test_cancelled_turn_resets_the_session(monkeypatch):
    monkeypatch.setattr(g, "ClaudeSDKClient", HangingClient)
    monkeypatch.setattr(g.claude_quota, "blocked", lambda: False)
    g._session.client = None

    async def go():
        task = asyncio.create_task(g.run_turn("память", [], 1.0))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False
    assert asyncio.run(go()) is True
    assert g._session.client is None and HangingClient.instances[-1].disconnected
