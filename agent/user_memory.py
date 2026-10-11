"""Память ГЛаДОС о тебе: факты, последние разговоры, чем ты занималась.

Лежит в memory/. OCR-текст с экрана НЕ сохраняется никогда — только короткие
выводы вроде «писала код» (см. modules/desktop/perception.py).
"""
import json
import time
from pathlib import Path

from memory_store import MemoryStore

_MAX_FACTS = 100
_MAX_DIALOGUE = 60
_MAX_ACTIVITY = 40


class UserMemory:
    def __init__(self, memory_dir: str = "./memory"):
        self._dir = Path(memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._store = MemoryStore(memory_dir)

    def _load(self, name: str, default):
        try:
            return json.loads((self._dir / name).read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save(self, name: str, data):
        (self._dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                      encoding="utf-8")

    # Долговременная память (виды: user / self / plan / episode) — см. memory_store.py ------

    @property
    def store(self) -> MemoryStore:
        return self._store

    def facts(self) -> list[dict]:
        """Факты о пользователе (для совместимости: [{text, ts}])."""
        return [{"text": i["text"], "ts": i["ts"]} for i in self._store.kind("user")]

    def add_fact(self, text: str, kind: str = "user", importance: int = 3,
                 due_days: float | None = None) -> bool:
        return self._store.add(kind, text, importance, due_days)

    def forget(self, needle: str) -> int:
        """Забыть записи, содержащие фразу (точно)."""
        return self._store.forget_matching(needle, strict=True)

    def forget_matching(self, text: str) -> int:
        """Нестрогое забывание: «забудь что я люблю чай» находит «Любит: чай»."""
        return self._store.forget_matching(text)

    # Разговор ---------------------------------------------------------------

    def add_dialogue(self, role: str, text: str):
        log = self._load("dialogue.json", [])
        log.append({"role": role, "text": text[:600], "ts": time.time()})
        self._save("dialogue.json", log[-_MAX_DIALOGUE:])

    def recent_dialogue(self, n: int = 8) -> list[dict]:
        return self._load("dialogue.json", [])[-n:]

    # Активность (только краткие выводы) ---------------------------------------

    def add_activity(self, app: str, category: str, summary: str):
        log = self._load("activity.json", [])
        log.append({"app": app, "category": category, "summary": summary[:200],
                    "ts": time.time()})
        self._save("activity.json", log[-_MAX_ACTIVITY:])

    def recent_activity(self, n: int = 5) -> list[dict]:
        return self._load("activity.json", [])[-n:]

    # Контекст для моделей -----------------------------------------------------

    def context_block(self, facts_limit: int = 15, dialogue: int = 6, query: str = "") -> str:
        lines = []
        remembered = self._store.context_text(query)
        if remembered:
            lines.append(remembered)
        act = self.recent_activity(3)
        if act:
            last = act[-1]
            mins = int((time.time() - last["ts"]) / 60)
            lines.append(f"Недавно на экране ({mins} мин назад): {last['summary']}")
        dlg = self.recent_dialogue(dialogue)
        if dlg:
            lines.append("Последние реплики разговора:")
            lines += [f"  {'Пользователь' if d['role'] == 'user' else 'ГЛаДОС'}: {d['text']}"
                      for d in dlg]
        return "\n".join(lines)
