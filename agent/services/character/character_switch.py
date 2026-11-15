"""Переключение персонажа во время работы ядра (ТЗ 05, CHR-7).

Порядок: дождаться конца текущей реплики (не дольше 15 с), сбросить очередь речи и стек фокуса, сохранить выбор, перечитать
промпты, переключить личную память (файлы персонажа) и группу графа, разослать тему `character` модулям, записать событие.
Один переключатель на процесс: параллельные смены ждут друг друга.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from services.character import character
from services.tracing import events_log
from services.memory_service import migration as memory_migration
from services.character import prompts

log = logging.getLogger("character_switch")

SPEECH_WAIT_S = 15.0
CONTAINER_MSG = "в контейнере один персонаж; для другого подними другой контейнер"


class CharacterSwitch:
    def __init__(self, *, companion, rel, attention, hub, mem_root: Path | None = None, speech_wait_s: float = SPEECH_WAIT_S):
        self.companion = companion      # Companion: личная память (UserMemory) и речь
        self.rel = rel                  # Relationships
        self.attention = attention      # Attention: очередь речи, стек фокуса, правила (None в тестах)
        self.hub = hub                  # PeerHub: рассылка темы character (None в тестах)
        self.mem_root = Path(mem_root) if mem_root is not None else None   # None: память персонажа в resources/<Имя>/memory
        self.speech_wait_s = speech_wait_s
        self._lock = asyncio.Lock()

    async def switch(self, char_id: str) -> dict:
        """Сменить персонажа. Неизвестный id или битая папка — character.CharacterError до каких-либо изменений."""
        if character.in_container():
            raise character.CharacterError(CONTAINER_MSG)
        async with self._lock:
            new = character.load(char_id)
            old = character.active()
            if new.id == old.id:
                return {"changed": False, "id": new.id, "name": new.name}
            await self._wait_speech()
            dropped = await self.attention.reset_for_character() if self.attention is not None else 0
            character.set_active(new.id)
            character.persist(new.id)
            prompts.reload()
            mdir = new.memory_dir(self.mem_root)
            mdir.mkdir(parents=True, exist_ok=True)
            self.companion.rebind_memory(mdir)
            self.rel.rebind(mdir)
            if self.attention is not None:
                self.attention.rebind_rules(mdir)
            episodes = getattr(self.companion, "episodes", None)
            if episodes is not None:                  # память нового персонажа открывается по папке компаньона (канон и перенос graph.db)
                try:                                  # у этого персонажа ещё может лежать старая memories.json
                    await memory_migration.migrate(mdir, episodes.memory, getattr(self.companion, "tasks", None))
                except Exception as e:
                    log.warning("перенос старой памяти персонажа: %s", e)
            if self.hub is not None:
                await self.hub.broadcast("character", new.card())
            events_log.log("character", f"персонаж: {old.name} -> {new.name}", character=new.id, dropped=dropped)
            log.info("персонаж переключён: %s -> %s", old.id, new.id)
            return {"changed": True, "id": new.id, "name": new.name}

    async def tool(self, char_id: str) -> str:
        """Инструмент core.set_character: текст для модели о результате."""
        try:
            res = await self.switch(char_id)
        except character.CharacterError as e:
            return f"персонаж не переключён: {e}"
        if not res["changed"]:
            return f"{res['name']} уже активна"
        return f"персонаж переключён: {res['name']}"

    async def _wait_speech(self) -> None:
        deadline = time.monotonic() + self.speech_wait_s
        while self.companion.is_speaking() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
