"""Отбор инструментов по эмбеддингам (ТЗ 02, CORE-36) на заглушке эмбеддингов: смысл, кэш, откат на слова."""
import asyncio

import tool_search
from environments.base import Environment, Tool
from environments.registry import Registry
from tool_search import ToolEmbeddings

# «смысл» заглушки: ось 0 - экран и скриншоты, ось 1 - почта и письма, ось 2 - музыка
AXES = {"экран": 0, "скриншот": 0, "снимок": 0, "почт": 1, "письм": 1, "музык": 2, "песн": 2}


def vec(text: str) -> list[float]:
    v = [0.01, 0.01, 0.01]
    for word, axis in AXES.items():
        if word in text.lower():
            v[axis] += 1.0
    return v


class Stub:
    def __init__(self):
        self.calls: list[tuple[int, bool]] = []
        self.fail = False

    async def __call__(self, texts, query):
        self.calls.append((len(texts), query))
        if self.fail:
            raise ConnectionError("LM Studio недоступна")
        return [vec(t) for t in texts]


def make_registry(tmp_path, stub, n_filler=20):
    reg = Registry()
    tools = [Tool(id="system.screenshot", env="system", name="screenshot", description="Сделать скриншот экрана.", call=None),
             Tool(id="mail.read", env="system", name="read_mail", description="Прочитать письма в почте.", call=None),
             Tool(id="music.play", env="system", name="play", description="Включить музыку и песню.", call=None)]
    tools += [Tool(id=f"misc.t{i}", env="system", name=f"t{i}", description=f"Прочее действие {i}.", call=None) for i in range(n_filler)]
    reg.add(Environment(id="system", title="ПК", description="Компьютер.", tools=tools))
    reg.embedder = ToolEmbeddings(tmp_path / "emb.json", embed=stub)
    return reg


async def settle(reg):
    if reg.embedder._filling is not None:
        await reg.embedder._filling


async def test_semantic_search_picks_by_meaning_after_cache_is_built(tmp_path):
    stub = Stub()
    reg = make_registry(tmp_path, stub)
    # первая просьба: кэша ещё нет, отбор по словам, а векторы докачиваются в фоне
    first = await reg.search_async("сними картинку", k=3)
    assert len(first) == 3
    await settle(reg)
    assert stub.calls and stub.calls[0][1] is False
    assert (tmp_path / "emb.json").exists()
    ids = [t.id for t in await reg.search_async("сделай снимок экрана", k=3)]
    assert ids[0] == "system.screenshot"                              # по смыслу, хотя слов «скриншот» в просьбе нет
    ids = [t.id for t in await reg.search_async("проверь почту", k=2)]
    assert ids[0] == "mail.read" and len(ids) == 2
    assert stub.calls[-1] == (1, True)                                # на просьбу уходит один вектор запроса


async def test_cache_survives_restart_and_recomputes_only_changed_descriptions(tmp_path):
    stub = Stub()
    reg = make_registry(tmp_path, stub)
    await reg.search_async("экран", k=3)
    await settle(reg)
    docs_total = sum(n for n, q in stub.calls if not q)
    assert docs_total == 23
    stub2 = Stub()
    reg2 = make_registry(tmp_path, stub2)                              # «перезапуск»: кэш читается из файла
    ids = [t.id for t in await reg2.search_async("сделай снимок экрана", k=3)]
    assert ids[0] == "system.screenshot" and all(not q or n == 1 for n, q in stub2.calls) and sum(n for n, q in stub2.calls if not q) == 0
    # изменили одно описание: перевычисляется только оно
    reg2.envs["system"].tools[1].description = "Прочитать почту и письма пользователя."
    await reg2.search_async("почта", k=3)
    await settle(reg2)
    assert [n for n, q in stub2.calls if not q] == [1]


async def test_falls_back_to_word_search_when_embeddings_fail_and_pauses(tmp_path):
    stub = Stub()
    reg = make_registry(tmp_path, stub)
    await reg.search_async("экран", k=3)
    await settle(reg)
    stub.fail = True
    picked = await reg.search_async("прочитай письма", k=3)            # вектор запроса не получен -> по словам, без исключения
    assert len(picked) == 3
    calls = len(stub.calls)
    await reg.search_async("ещё просьба", k=3)
    assert len(stub.calls) == calls                                    # минута тишины: к недоступной LM Studio не ходим
    reg.embedder._clock = lambda: 10 ** 9                              # прошла минута
    stub.fail = False
    ids = [t.id for t in await reg.search_async("включи песню", k=2)]
    assert ids[0] == "music.play"


async def test_fill_failure_does_not_break_search_and_few_tools_skip_embeddings(tmp_path):
    stub = Stub()
    stub.fail = True
    reg = make_registry(tmp_path, stub)
    assert len(await reg.search_async("экран", k=3)) == 3
    await settle(reg)
    assert reg.embedder._down_until > 0
    small_stub = Stub()
    small = make_registry(tmp_path / "x", small_stub, n_filler=0)
    assert len(await small.search_async("что угодно", k=14)) == 3       # инструментов не больше k: возвращаются все
    assert small_stub.calls == []


async def test_always_tools_are_kept_and_no_embedder_means_old_search(tmp_path):
    stub = Stub()
    reg = make_registry(tmp_path, stub)
    reg.envs["system"].tools[2].always = True
    await reg.search_async("экран", k=2)
    await settle(reg)
    ids = [t.id for t in await reg.search_async("сделай снимок экрана", k=2)]
    assert "music.play" in ids and ids[0] == "system.screenshot"        # запасной инструмент не отсеивается
    reg.embedder = None
    assert len(await reg.search_async("экран", k=2)) >= 2


def test_cosine_and_key_helpers():
    assert tool_search._cosine([1, 0], [1, 0]) == 1.0 and tool_search._cosine([1, 0], [0, 1]) == 0.0
    assert tool_search._cosine([0, 0], [1, 1]) == 0.0
    assert tool_search._key("a") != tool_search._key("b") and tool_search._key("a") == tool_search._key("a")


async def test_router_uses_semantic_search(tmp_path, monkeypatch):
    from router import Router
    stub = Stub()
    reg = make_registry(tmp_path, stub)
    await reg.search_async("экран", k=3)
    await settle(reg)
    seen = {}

    async def fake_plan(self, text, tools):
        seen["ids"] = [t.id for t in tools]
        return {"kind": "chat", "steps": []}
    monkeypatch.setattr(Router, "_plan", fake_plan)
    router = Router(reg, lambda t: asyncio.sleep(0), lambda: "")
    await router.handle("сделай снимок экрана")
    assert seen["ids"][0] == "system.screenshot" and len(seen["ids"]) == 14
