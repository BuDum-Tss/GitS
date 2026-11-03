"""Извлечение фактов для графа памяти идёт через Claude без инструментов (`fast_brain._ask`), а не через локальную Gemma."""
import asyncio

import graph_memory


def test_extractor_asks_the_fast_model_with_the_schema_and_parses_json_in_text():
    seen = {}

    async def ask(system, user):
        seen["system"], seen["user"] = system, user
        return 'Вот ответ: {"facts": [{"subject": "Аня", "relation": "любит", "object": "чай", "fact": "Аня любит чай"}]}'
    facts = asyncio.run(graph_memory.FactExtractor(ask=ask).extract("Аня", "я люблю чай"))
    assert [(f.subject, f.relation, f.object) for f in facts] == [("Аня", "ЛЮБИТ", "чай")]
    assert "facts" in seen["system"] and "JSON" in seen["system"] and seen["user"] == "Аня: я люблю чай"


def test_extractor_default_backend_is_the_claude_fast_model(monkeypatch):
    import fast_brain
    calls = []

    async def fake(system, user, timeout, max_tokens=40, image_b64=None, temperature=0.3, background=False, parallel=False):
        calls.append((background, temperature))
        return '{"facts": []}'
    monkeypatch.setattr(fast_brain, "_ask", fake)
    assert asyncio.run(graph_memory.FactExtractor().extract("Аня", "привет")) == []
    assert calls == [(True, 0.1)]                                # фоновый вызов, низкая температура
