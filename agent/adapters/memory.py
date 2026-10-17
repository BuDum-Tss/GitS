"""MemoryReader и MemoryWriter поверх рабочей памяти разговора (`UserMemory`), памяти-графа (`GraphMemory`), задач и эпизодов.

Куда что пишется (пять мест вместо шести слоёв):
  user    -> профиль владельца в графе (важность 4-5); сюда же self и episode: его пожелания к ней, договорённости и
             то, что она сама сочла важным (собственного профиля у неё нет, характер задают карточка и системный промпт);
  plan    -> задача `plan` в `tasks.db` (срок из `due_days` или из слов текста);
  факты из разговора -> очередь графа (`enqueue_for_graph`), модель выписывает и оценивает важность;
  события жизни -> эпизоды (`episodes.db`) нарезаются из журнала сами, сюда не пишутся.

`get_mem`, `get_graph`, `get_tasks`, `get_episodes` возвращают текущие объекты: память может быть переподключена (переключение
персонажа), граф и задачи подставляет ядро уже после создания компаньона. Пока объекта нет (None), запись ничего не делает,
чтение отдаёт пустое значение.
"""
import asyncio
import hashlib
import re
import time
from typing import Any, Callable

import character_memory
from domain import plan_due

MIN_PLAN_IMPORTANCE = 3          # план записывается и со средней важностью: дедлайны и встречи сами по себе не мелочь


class UserMemoryAdapter:
    def __init__(self, get_mem: Callable[[], Any], get_graph: Callable[[], Any],
                 get_episodes: Callable[[], Any] | None = None, get_tasks: Callable[[], Any] | None = None,
                 get_owner: Callable[[], str] | None = None):
        """get_episodes() -> сервис эпизодов (`episode_service`, у него `.recall`) или None; get_tasks() -> `TaskService` или None
        (без него планы не записываются); get_owner() -> имя владельца для подписи его фактов."""
        self._get_mem = get_mem
        self._get_graph = get_graph
        self._get_episodes = get_episodes or (lambda: None)
        self._get_tasks = get_tasks or (lambda: None)
        self._get_owner = get_owner or (lambda: "")
        self.loop: asyncio.AbstractEventLoop | None = None      # цикл ядра: планы из рабочих потоков (to_thread) ставятся в него

    # ── чтение (MemoryReader) ────────────────────────────────────────────────────────

    def context_block(self, dialogue: int = 6) -> str:
        mem = self._get_mem()
        return mem.context_block(dialogue=dialogue) if mem is not None else ""

    async def graph_block(self, query: str) -> str:
        graph = self._get_graph()
        return await graph.context_block(query) if graph is not None else ""

    async def self_block(self, query: str) -> str:
        graph = self._get_graph()
        if graph is None:
            return ""
        return character_memory.block(await graph.self_memories(query))

    def profile_block(self) -> str:
        """Профиль владельца: всегда в контексте его разговора, вне темы (читается напрямую, без моделей)."""
        fn = getattr(self._get_graph(), "profile_block", None)       # у заглушек графа в тестах метода может не быть
        return fn() if fn is not None else ""

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
        """Профиль владельца: [{text, ts}]."""
        fn = getattr(self._get_graph(), "profile_facts", None)
        return [{"text": f["text"], "ts": f["ts"]} for f in fn("owner", 50)] if fn is not None else []

    async def recall(self, query: str, k: int = 6) -> list[dict]:
        """Факты графа по теме и, если подключены эпизоды, эпизоды и дневник по смыслу (гибридный поиск, ТЗ 27).
        Сбой поиска эпизодов не мешает фактам."""
        out: list[dict] = []
        graph = self._get_graph()
        if graph is not None:
            try:
                out += [{"text": f["fact"], "kind": "fact", "id": f.get("uuid", ""), "importance": 4}
                        for f in await graph.search(query, limit=k)]
            except Exception:
                pass
        eps = self._get_episodes()
        if eps is not None:
            try:
                out += [{"text": h["text"], "kind": h["kind"], "id": h["id"], "importance": 3}
                        for h in await eps.recall.find(query, k=max(1, k // 2))]
            except Exception:
                pass
        return out[:k + 3]

    def meta(self, key: str, default: Any = None) -> Any:
        mem = self._get_mem()
        return mem.meta(key, default) if mem is not None else default

    # ── запись (MemoryWriter) ────────────────────────────────────────────────────────

    def add_dialogue(self, role: str, text: str) -> None:
        mem = self._get_mem()
        if mem is not None:
            mem.add_dialogue(role, text)

    def add_fact(self, text: str, kind: str = "user", importance: int = 3, due_days: float | None = None) -> bool:
        """Запомнить то, что она сама сочла важным. Мелочь (важность ниже порога) не записывается. True, если запись новая."""
        text = re.sub(r"\s+", " ", str(text)).strip()
        if kind == "plan":
            return self._add_plan(text, importance, due_days)
        graph = self._get_graph()
        if getattr(graph, "remember", None) is None:
            return False
        if kind in ("user", "self", "episode"):
            return graph.remember(text, importance, subject=self._get_owner())
        return False

    def _add_plan(self, text: str, importance: int, due_days: float | None) -> bool:
        tasks = self._get_tasks()
        if tasks is None or len(text) < 6 or importance < MIN_PLAN_IMPORTANCE:
            return False
        now = time.time()
        due = now + due_days * 86400 if due_days else plan_due.due_of_text(text, now)
        plan_id = "m-" + hashlib.sha1(" ".join(text.lower().split()).encode("utf-8")).hexdigest()[:8]
        coro = tasks.upsert_plan(plan_id, text[:300], due)
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:                                    # вызов из рабочего потока
            if self.loop is None or self.loop.is_closed():
                coro.close()
                return False
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        return True

    def add_activity(self, app: str, category: str, summary: str) -> None:
        mem = self._get_mem()
        if mem is not None:
            mem.add_activity(app, category, summary)

    def enqueue_for_graph(self, speaker: str, text: str, source: str | None = None, owner: bool = False) -> None:
        graph = self._get_graph()
        if graph is not None:
            extra = {**({"source": source} if source else {}), **({"owner": True} if owner else {})}    # ТЗ 27: факт из эпизода помнит, из какого
            graph.enqueue(speaker, text, **extra)

    def forget_matching(self, text: str) -> int:
        fn = getattr(self._get_graph(), "forget_matching", None)
        return fn(text) if fn is not None else 0

    def set_meta(self, key: str, value: Any) -> None:
        mem = self._get_mem()
        if mem is not None:
            mem.set_meta(key, value)

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
