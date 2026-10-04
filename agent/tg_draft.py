"""Черновик поста для Telegram-канала: текст и приложенные скриншоты (memory/tg_draft.json).

Она пишет черновик по частям, правит его и только потом публикует. Черновик переживает
перезапуск; публикация идёт через TelegramChannel.publish (фильтр приватности, подтверждение).
"""
import json
import time
from pathlib import Path

import screenshots

MAX_PHOTOS = 4


class TgDraft:
    def __init__(self, memory_dir: str):
        self._file = Path(memory_dir) / "tg_draft.json"

    def _load(self) -> dict:
        try:
            d = json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        return {"text": d.get("text", ""), "photos": [p for p in d.get("photos", []) if screenshots.resolve(p)],
                "updated": d.get("updated", 0)}

    def _save(self, d: dict):
        d["updated"] = time.time()
        self._file.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self) -> dict:
        return self._load()

    def is_empty(self) -> bool:
        d = self._load()
        return not d["text"] and not d["photos"]

    def set_text(self, text: str):
        d = self._load()
        d["text"] = text.strip()
        self._save(d)

    def append(self, text: str):
        d = self._load()
        d["text"] = (d["text"] + "\n\n" + text.strip()).strip()
        self._save(d)

    def replace(self, find: str, repl: str) -> bool:
        d = self._load()
        if find not in d["text"]:
            return False
        d["text"] = d["text"].replace(find, repl, 1)
        self._save(d)
        return True

    def attach(self, name: str) -> str:
        """Приложить снимок (имя файла или «last»). Возвращает имя файла."""
        path = screenshots.last() if name in ("", "last") else screenshots.resolve(name)
        if path is None:
            raise ValueError("такого скриншота нет: сначала сделай снимок")
        d = self._load()
        if path.name in d["photos"]:
            return path.name
        if len(d["photos"]) >= MAX_PHOTOS:
            raise ValueError(f"к посту можно приложить не больше {MAX_PHOTOS} снимков")
        d["photos"].append(path.name)
        self._save(d)
        return path.name

    def detach(self, name: str) -> int:
        d = self._load()
        before = len(d["photos"])
        d["photos"] = [] if name in ("", "all") else [p for p in d["photos"] if p != name]
        self._save(d)
        return before - len(d["photos"])

    def clear(self):
        self._save({"text": "", "photos": []})

    def preview(self) -> str:
        d = self._load()
        if not d["text"] and not d["photos"]:
            return "черновик пуст"
        photos = f"\nСнимки ({len(d['photos'])}): " + ", ".join(d["photos"]) if d["photos"] else "\nСнимков нет."
        return f"Текст ({len(d['text'])} симв.):\n{d['text'] or '(пока нет)'}{photos}"
