"""Рабочая память разговора: последние реплики, чем занималась на экране, служебные отметки (memory/).

Долговременного здесь нет: профиль владельца и важные факты лежат в графе (`graph_memory.py`), цели и планы
в `tasks.db`, события жизни в `episodes.db`. OCR-текст с экрана НЕ сохраняется никогда, только короткие выводы вроде
«писала код» (см. modules/desktop/perception.py).
"""
import json
import time
from pathlib import Path

import character

_MAX_DIALOGUE = 60
_MAX_ACTIVITY = 40


class UserMemory:
    def __init__(self, memory_dir: str = "./memory"):
        self._dir = Path(memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def rebind(self, memory_dir) -> None:
        """Переключение персонажа (ТЗ 05, CHR-7, CHR-10): та же память, но в папке другого персонажа."""
        self._dir = Path(memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _load(self, name: str, default):
        try:
            return json.loads((self._dir / name).read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save(self, name: str, data):
        (self._dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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
        log.append({"app": app, "category": category, "summary": summary[:200], "ts": time.time()})
        self._save("activity.json", log[-_MAX_ACTIVITY:])

    def recent_activity(self, n: int = 5) -> list[dict]:
        return self._load("activity.json", [])[-n:]

    # Служебные отметки (когда последний раз осмысляли беседу) ---------------------

    def meta(self, key: str, default=None):
        return self._load("memory_meta.json", {}).get(key, default)

    def set_meta(self, key: str, value):
        data = self._load("memory_meta.json", {})
        data[key] = value
        self._save("memory_meta.json", data)

    # Контекст для моделей -----------------------------------------------------

    def context_block(self, dialogue: int = 6) -> str:
        lines = []
        act = self.recent_activity(3)
        if act:
            last = act[-1]
            mins = int((time.time() - last["ts"]) / 60)
            lines.append(f"Недавно на экране ({mins} мин назад): {last['summary']}")
        dlg = self.recent_dialogue(dialogue)
        if dlg:
            lines.append("Последние реплики разговора:")
            lines += [f"  {'Пользователь' if d['role'] == 'user' else character.active().name}: {d['text']}" for d in dlg]
        return "\n".join(lines)
