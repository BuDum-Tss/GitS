"""События игры, которые раньше разбирал игровой агент, теперь уходят ядру и попадают в его буфер (входящую очередь)."""
import asyncio

import controller


class FakeLink:
    def __init__(self):
        self.sent = []

    async def event(self, kind, data=None, text=None, urgency=None):
        self.sent.append((kind, text, urgency))


def _controller():
    c = controller.Controller.__new__(controller.Controller)
    c.link = FakeLink()
    return c


def test_state_changes_become_core_events():
    async def run():
        c = _controller()
        for line in ("исследование завершено: Электричество", "запас еды: 2 дня", "Анна упал(а) и лежит", "появился враг: Pirate"):
            c._send_change(line)
        await asyncio.sleep(0)
        return c.link.sent
    kinds = {k for k, _t, _u in asyncio.run(run())}
    assert kinds == {"research_done", "resource_out", "pawn_downed", "enemy_seen"}


def test_game_events_from_the_mod_are_forwarded():
    async def run():
        c = _controller()
        for evt in ({"type": "pawn_downed", "data": {"name": "Анна"}}, {"type": "enemy_killed", "data": {"label": "Пират"}},
                    {"type": "enemy_downed", "data": {"label": "Пират"}}, {"type": "something_else", "data": {}}):
            await c._send_game_event(evt)
        await asyncio.sleep(0)
        return c.link.sent
    sent = asyncio.run(run())
    assert [k for k, _t, _u in sent] == ["pawn_downed", "enemy_down", "enemy_down"]
