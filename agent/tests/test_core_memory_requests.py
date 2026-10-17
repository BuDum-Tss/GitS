"""core.memory.recall / core.memory.remember поверх MemoryReader и MemoryWriter (ТЗ 31, MC-5)."""
import pytest

from core_requests import CoreRequests
from module_hub import ModuleError


class FakeMemory:
    """И читатель, и писатель в памяти процесса."""

    def __init__(self):
        self.items: list[dict] = []

    def search(self, query, k=6):
        return [i for i in self.items if query.lower() in i["text"].lower()][:k]

    async def recall(self, query, k=6):
        return [dict(i, kind="fact") for i in self.search(query, k)]

    def add_fact(self, text, kind="user", importance=3, due_days=None):
        if any(i["text"] == text for i in self.items):
            return False
        self.items.append({"text": text, "kind": kind, "importance": importance})
        return True


async def test_remember_then_recall_and_duplicate():
    mem = FakeMemory()
    req = CoreRequests(memory_reader=mem, memory_writer=mem)
    assert await req.handle(None, "core.memory.remember", {"text": " любит чай "}) == {"stored": True}
    assert await req.handle(None, "core.memory.remember", {"text": "любит чай"}) == {"stored": False}
    got = await req.handle(None, "core.memory.recall", {"query": "чай", "limit": 3})
    assert got == {"facts": [{"text": "любит чай", "kind": "fact", "importance": 4}]}
    assert (await req.handle(None, "core.memory.recall", {"query": "кофе"}))["facts"] == []


async def test_without_memory_unavailable_and_bad_body():
    req = CoreRequests()
    for t, body in (("core.memory.recall", {"query": "x"}), ("core.memory.remember", {"text": "x"})):
        with pytest.raises(ModuleError) as e:
            await req.handle(None, t, body)
        assert e.value.code == "unavailable"
    with pytest.raises(ModuleError) as e:
        await req.handle(None, "core.memory.remember", {"text": ""})
    assert e.value.code == "bad_request"
