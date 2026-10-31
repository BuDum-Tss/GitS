"""MemoryCapture: что запомнить из разговора (ТЗ 29, ARC-5): явное «запомни» (`remember_text`) и очередь реплик в граф.
Остальное запоминает единый агент сам инструментом памяти, а во сне и в паузе работают эпизоды и размышление."""
import logging
import re
import time

import fast_brain

logger = logging.getLogger("companion")

SPEAKER_TAG = re.compile(r"^\[(говорит ([^\]]+)|незнакомый голос)\]\s*")


class MemoryCapture:
    def __init__(self, host):
        self._h = host

    def owner_name(self) -> str:
        for fact in self._h._memory_reader.facts():
            m = re.match(r"\s*(?:имя(?: пользователя)?|зовут)\s*[:\-]\s*(.+)", fact["text"], re.I)
            names = re.findall(r"[А-Яа-яЁёA-Za-z\-]{2,}", m.group(1)) if m else []
            if names:
                return names[0]
        return ""

    def queue_graph(self, text: str):
        """Реплика человека уходит в очередь на разбор фактов (граф). Незнакомые голоса не запоминаем."""
        tag = SPEAKER_TAG.match(text)
        if tag:
            if not tag.group(2):
                return
            speaker, text = tag.group(2), text[tag.end():]
            owner = speaker.lower() == (self._h._owner_name() or "").lower()
        else:
            speaker = self._h._owner_name() or "Пользователь"
            owner = True
        self._h._memory_writer.enqueue_for_graph(speaker, text, owner=owner)

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
