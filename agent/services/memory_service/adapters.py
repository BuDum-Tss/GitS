"""MemoryReader и MemoryWriter поверх рабочей памяти разговора (`UserMemory`), памяти эпизодов (`EpisodeMemory`) и задач.

Память персонажа это эпизоды (`episodes.db`): канон из memories.yaml, заметки «запомни» и события жизни, которые пишет форк её сессии.
Куда что пишется:
  user, self, episode -> заметка `kind=note` в эпизодах (текстом; важность 3-5, мелочь не пишется): его пожелания к ней,
                         договорённости, поправки и то, что она сама сочла важным;
  plan                -> задача `plan` в `tasks.db` (срок из `due_days` или из слов текста);
  события жизни и знания из разговора -> эпизоды нарезаются из журнала и пишутся форком, сюда не пишутся.

`get_mem`, `get_episodes`, `get_tasks` возвращают текущие объекты: память может быть переподключена (переключение персонажа),
эпизоды и задачи подставляет ядро уже после создания компаньона. Пока объекта нет (None), запись ничего не делает, чтение отдаёт
пустое значение. `get_world` - состояние мира (игры) со строкой `block(запрос)`, оно не память и подмешивается в блок «по теме» отдельно.
"""
import asyncio
import hashlib
import re
import time
from typing import Any, Callable

from services.memory_service import plan_due

MIN_PLAN_IMPORTANCE = 3          # план записывается и со средней важностью: дедлайны и встречи сами по себе не мелочь


class UserMemoryAdapter:
    def __init__(self, get_mem: Callable[[], Any], get_episodes: Callable[[], Any] | None = None,
                 get_tasks: Callable[[], Any] | None = None, get_world: Callable[[], Any] | None = None):
        """get_episodes() -> сервис эпизодов (`episode_service.py`, у него `.recall` и `.memory`) или None; get_tasks() -> `TaskService`
        или None (без него планы не записываются); get_world() -> состояние мира или None."""
        self._get_mem = get_mem
        self._get_episodes = get_episodes or (lambda: None)
        self._get_tasks = get_tasks or (lambda: None)
        self._get_world = get_world or (lambda: None)
        self.loop: asyncio.AbstractEventLoop | None = None      # цикл ядра: планы из рабочих потоков (to_thread) ставятся в него

    def _memory(self):
        eps = self._get_episodes()
        return eps.memory if eps is not None else None

    # ── чтение (MemoryReader) ────────────────────────────────────────────────────────

    def context_block(self, dialogue: int = 6) -> str:
        mem = self._get_mem()
        return mem.context_block(dialogue=dialogue) if mem is not None else ""

    async def topic_block(self, query: str) -> str:
        """«Что ты помнишь по теме»: эпизоды и заметки по запросу; состояние мира (игры) отдельной строкой."""
        memory = self._memory()
        parts = [await memory.topic_block(query)] if memory is not None else []
        world = self._get_world()
        parts.append(world.block(query) if world is not None else "")
        return "\n".join(p for p in parts if p)

    async def canon_block(self, query: str) -> str:
        """«Твои воспоминания (это о тебе)»: канон персонажа, подходящий к реплике."""
        memory = self._memory()
        return await memory.canon_block(query) if memory is not None else ""

    def memory_status(self) -> str | None:
        memory = self._memory()
        return memory.status_text() if memory is not None else None

    def recent_dialogue(self, n: int = 8) -> list[dict]:
        mem = self._get_mem()
        return mem.recent_dialogue(n) if mem is not None else []

    def recent_activity(self, n: int = 5) -> list[dict]:
        mem = self._get_mem()
        return mem.recent_activity(n) if mem is not None else []

    async def recall(self, query: str, k: int = 6) -> list[dict]:
        """Эпизоды всех видов (жизнь, канон, заметки) и дневник по смыслу запроса (гибридный поиск, ТЗ 27):
        [{text, kind (episode|canon|note|diary|digest), id, importance}]. Сбой поиска даёт пустой список."""
        memory = self._memory()
        if memory is None:
            return []
        return [{"text": h["text"], "kind": h["kind"], "id": h["id"], "importance": round(h["salience"] * 5) or 3}
                for h in await memory.find(query, k)]

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
        memory = self._memory()
        if memory is None or kind not in ("user", "self", "episode"):
            return False
        return memory.remember(text, importance)

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

    def forget_matching(self, text: str) -> int:
        """Забыть эпизоды и заметки по словам фразы (канон не забывается)."""
        memory = self._memory()
        return memory.forget_matching(text) if memory is not None else 0

    def set_meta(self, key: str, value: Any) -> None:
        mem = self._get_mem()
        if mem is not None:
            mem.set_meta(key, value)

    def rebind(self, memory_dir: Any) -> None:
        mem = self._get_mem()
        if mem is not None:
            mem.rebind(memory_dir)

