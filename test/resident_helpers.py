"""Подделки для тестов единого агента: хозяин (`FakeHost`) и среда исполнения с управляемым ходом (`FakeRuntime`)."""
import asyncio

from interfaces.contracts import EndEvent, TextEvent


class FakeContext:
    async def use_retrieval(self, *a, **k):
        return True

    def start_retrieval(self, text):
        return None


class FakeRegistry:
    def describe(self):
        return "окружения"

    async def search_async(self, text):
        return []


class FakeHost:
    def __init__(self):
        self.said: list[str] = []
        self.stickers: list = []
        self.router = type("R", (), {"registry": FakeRegistry()})()
        self.context_builder = FakeContext()

    async def say(self, text, *a, **k):
        self.said.append(text)
        self.stickers.append(a[0] if a else k.get("sticker"))

    def _declutter(self, text):
        return text

    def _context(self, **k):
        return "[контекст]"

    def profile_hint(self):
        return ""


class FakeRuntime:
    """Среда с управляемым ходом: пока не отпущена `release`, первый ход идёт; события отдаёт из `script`."""

    def __init__(self, can_steer=True, steer_result=True, script=None):
        self.can_steer = can_steer
        self.steer_result = steer_result
        self.script = script if script is not None else [TextEvent("готово")]
        self.prompts, self.steered, self.resets, self.aborts, self.closed = [], [], [], 0, 0
        self.release = asyncio.Event()
        self.hold = False
        self.delay = 0.0
        self.started = asyncio.Event()
        self.system = None
        self.tools = []

    async def start(self, system, tools):
        self.system = system
        self.tools = tools

    async def run(self, prompt):
        self.prompts.append(prompt)
        self.started.set()
        if self.hold and len(self.prompts) == 1:
            await self.release.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        for e in self.script:
            yield e
        yield EndEvent("ok")

    async def steer(self, text):
        self.steered.append(text)
        return self.steer_result

    async def abort(self):
        self.aborts += 1
        self.release.set()

    async def reset(self, forget=True):
        self.resets.append(forget)

    async def close(self):
        self.closed += 1
