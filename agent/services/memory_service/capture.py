"""MemoryCapture: что запомнить из разговора (ТЗ 29, ARC-5): явное «запомни» (`remember_text`), заметкой в эпизоды.
Остальное запоминает единый агент сам инструментом памяти, а знания из разговора возникают из эпизодов, которые пишет форк её сессии."""
from typing import TYPE_CHECKING
import logging
import re
import time

from services.runtime import fast_brain

if TYPE_CHECKING:
    from services.conversation.companion import Companion

logger = logging.getLogger("companion")

SPEAKER_TAG = re.compile(r"^\[(говорит ([^\]]+)|незнакомый голос)\]\s*")


class MemoryCapture:
    def __init__(self, host):
        self._h: Companion = host

    def store_memories(self, data) -> list[str]:
        """Сохранить воспоминания из ответа модели ({"memories": [...]}); вернуть тексты новых."""
        added: list[str] = []
        for m in (data or {}).get("memories") or []:
            if not isinstance(m, dict) or m.get("kind") not in ("user", "self", "plan"):   # события жизни ведут эпизоды (episodes.db)
                continue
            text = str(m.get("text") or "").strip()
            if len(text) < 6:
                continue
            try:
                importance = max(1, min(5, int(m.get("importance", 3))))
            except (TypeError, ValueError):
                importance = 3
            due = m.get("due_days")
            due = float(due) if isinstance(due, (int, float)) and due > 0 else None
            if self._h._memory_writer.add_fact(text, kind=m["kind"], importance=importance, due_days=due):
                added.append(text)
        return added

    async def remember_text(self, text: str, hint_kind: str | None = None) -> list[str]:
        """Явная просьба «запомни…»: аккуратно разобрать текст на записи правильного вида и в
        правильном лице (о пользователе от третьего, о себе от первого). Сразу, не фоном."""
        h = self._h
        system = ("Пользователь просит тебя запомнить сказанное. Сформулируй 1-2 записи. kind: "
                  "user (факт о нём: пиши от третьего лица, «Любимый цвет пользователя - зелёный»), "
                  "self (о тебе самой, чего он хочет от тебя: пиши от первого лица, «Я называю пользователя "
                  "Аней и реже язвлю»), plan (планы и дела: дедлайны, встречи, экзамены, задачи; due_days - "
                  "через сколько дней, если срок можно понять, иначе null). importance 1-5. "
                  'Ответь ТОЛЬКО JSON: {"memories": [{"kind": "...", "text": "...", "importance": 4, "due_days": null}]}. '
                  f"Сегодня {time.strftime('%A, %d.%m')}.")
        try:
            data = fast_brain._json_of(await h._llm.ask(system, text[:500], timeout=60,
                                                        max_tokens=220, temperature=0.1))
        except Exception as e:
            logger.warning(f"Не удалось разобрать «запомни»: {e}")
            data = None
        stored = h._store_memories(data)
        if not stored:   # модель не помогла: сохраняем как есть, чтобы не потерять просьбу
            kind = hint_kind if hint_kind in ("user", "self", "plan") else "user"
            if h._memory_writer.add_fact(text.strip(), kind=kind, importance=4):
                stored = [text.strip()]
        return stored

    def set_quiet(self, minutes):
        """Пользователь сказал, что занят: сама она не заговаривает это время."""
        h = self._h
        try:
            mins = max(0, min(int(minutes), 240))
        except (TypeError, ValueError):
            return
        if mins > 0:
            h._settings.update(quiet_until=time.time() + mins * 60)
            h._log.log("setting", f"режим «не отвлекать» на {mins} мин (по словам пользователя)")
