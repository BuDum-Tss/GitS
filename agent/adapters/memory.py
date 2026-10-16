"""MemoryReader и MemoryWriter поверх личной памяти (`UserMemory`, `MemoryStore`) и графа фактов (`GraphMemory`).

`get_mem` и `get_graph` возвращают текущие объекты: память может быть переподключена (переключение персонажа), граф
подставляет ядро уже после создания компаньона. Пока объекта нет (None), запись ничего не делает, чтение отдаёт
пустое значение. Формат файлов памяти этим адаптером не меняется.
"""
from typing import Any, Callable

import character_memory


class UserMemoryAdapter:
    def __init__(self, get_mem: Callable[[], Any], get_graph: Callable[[], Any],
                 get_episodes: Callable[[], Any] | None = None):
        """get_episodes() -> сервис эпизодов (`episode_service`, у него `.recall`) или None: тогда `recall` отдаёт только записи."""
        self._get_mem = get_mem
        self._get_graph = get_graph
        self._get_episodes = get_episodes or (lambda: None)

    def _store(self):
        mem = self._get_mem()
        return mem.store if mem is not None else None

    # ── чтение (MemoryReader) ────────────────────────────────────────────────────────

    def context_text(self, query: str = "") -> str:
        store = self._store()
        return store.context_text(query) if store is not None else ""

    def context_block(self, dialogue: int = 6, query: str = "") -> str:
        mem = self._get_mem()
        return mem.context_block(dialogue=dialogue, query=query) if mem is not None else ""

    async def graph_block(self, query: str) -> str:
        graph = self._get_graph()
        return await graph.context_block(query) if graph is not None else ""

    async def self_block(self, query: str) -> str:
        graph = self._get_graph()
        if graph is None:
            return ""
        return character_memory.block(await graph.self_memories(query))

    def graph_status(self) -> str | None:
        graph = self._get_graph()
        return graph.status_text() if graph is not None else None

    def recent_dialogue(self, n: int = 8) -> list[dict]:
        mem = self._get_mem()
        return mem.recent_dialogue(n) if mem is not None else []

    def recent_activity(self, n: int = 5) -> list[dict]:
        mem = self._get_mem()
        return mem.recent_activity(n) if mem is not None else []

    def facts(self) -> list[dict]:
        mem = self._get_mem()
        return mem.facts() if mem is not None else []

    def relevant(self, query: str = "", k: int = 8) -> list[dict]:
        store = self._store()
        return store.relevant(query, k=k) if store is not None else []

    def search(self, query: str, k: int = 6) -> list[dict]:
        store = self._store()
        return store.search(query, k=k) if store is not None else []

    async def recall(self, query: str, k: int = 6) -> list[dict]:
        """Записи по теме (как `search`) и, если подключены эпизоды, эпизоды и дневник по смыслу (гибридный поиск, ТЗ 27).
        Сбой поиска эпизодов не мешает запискам."""
        import asyncio
        out = [{"text": h.get("text", ""), "kind": "fact", "id": ""} for h in await asyncio.to_thread(self.search, query, k)]
        eps = self._get_episodes()
        if eps is not None:
            try:
                out += [{"text": h["text"], "kind": h["kind"], "id": h["id"]} for h in await eps.recall.find(query, k=max(1, k // 2))]
            except Exception:
                pass
        return out[:k + 3]

    def plans_to_nudge(self) -> list[dict]:
        store = self._store()
        return store.plans_to_nudge() if store is not None else []

    def meta(self, key: str, default: Any = None) -> Any:
        store = self._store()
        return store.meta(key, default) if store is not None else default

    # ── запись (MemoryWriter) ────────────────────────────────────────────────────────

    def add_dialogue(self, role: str, text: str) -> None:
        mem = self._get_mem()
        if mem is not None:
            mem.add_dialogue(role, text)

    def add_fact(self, text: str, kind: str = "user", importance: int = 3, due_days: float | None = None) -> bool:
        mem = self._get_mem()
        return bool(mem.add_fact(text, kind=kind, importance=importance, due_days=due_days)) if mem is not None else False

    def add_activity(self, app: str, category: str, summary: str) -> None:
        mem = self._get_mem()
        if mem is not None:
            mem.add_activity(app, category, summary)

    def enqueue_for_graph(self, speaker: str, text: str, source: str | None = None) -> None:
        graph = self._get_graph()
        if graph is not None:
            if source:
                graph.enqueue(speaker, text, source=source)         # ТЗ 27: факт из эпизода помнит, из какого
            else:
                graph.enqueue(speaker, text)

    def forget_matching(self, text: str) -> int:
        mem = self._get_mem()
        return mem.forget_matching(text) if mem is not None else 0

    def set_meta(self, key: str, value: Any) -> None:
        store = self._store()
        if store is not None:
            store.set_meta(key, value)

    def rebind(self, memory_dir: Any) -> None:
        mem = self._get_mem()
        if mem is not None:
            mem.rebind(memory_dir)


def writer_for_companion(companion):
    """MemoryWriter для менеджера внимания: публичное свойство `memory_writer` компаньона; у заглушек без него - память
    из приватных полей `_mem` и `graph` (читаются при каждом вызове)."""
    writer = getattr(companion, "memory_writer", None)
    if writer is not None:
        return writer
    return UserMemoryAdapter(lambda: getattr(companion, "_mem", None), lambda: getattr(companion, "graph", None))
