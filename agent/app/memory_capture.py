"""MemoryCapture: что запомнить из разговора (ТЗ 29, ARC-5): явное «запомни», фоновый разбор реплики (`learn`),
осмысление беседы (`consolidate`), очередь реплик в граф, режим «не отвлекать» по словам пользователя.
Счётчики (`_pending_memory`, `_consolidating`) живут на фасаде `Companion`."""
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
        else:
            speaker = self._h._owner_name() or "Пользователь"
        self._h._memory_writer.enqueue_for_graph(speaker, text)

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

    async def learn(self, text: str):
        """Фоном после ответа: что запомнить надолго из этого сообщения и не просил ли он не отвлекать."""
        h = self._h
        system = ("Из сообщения пользователя выдели то, что ты САМА считаешь важным запомнить надолго. Верни JSON: "
                  '{"memories": [{"kind": "user|self|plan", "text": "...", "importance": 3, "due_days": null}], '
                  '"quiet_minutes": 0}. kind=user: устойчивые факты о нём (имя, привычки, предпочтения). '
                  "kind=self: чего он хочет от ТЕБЯ (как с ним говорить, что делать чаще или реже). "
                  "kind=plan: его планы и дела: дедлайны, встречи, экзамены, задачи (due_days: через сколько дней, если названо, иначе null). "
                  "Записи о пользователе пиши от третьего лица, о тебе самой от первого. "
                  "Мелочи и сиюминутное (настроение, усталость, что он делает сейчас, текущий ход игры, просьбы что-то сделать "
                  "прямо сейчас в игре или на компьютере, пересказ разговора) не запоминай: почти всегда memories пустой. "
                  "importance 4-5 только для действительно важного. "
                  "quiet_minutes: если он сказал, что сейчас будет смотреть, работать или заниматься, "
                  "на сколько минут его не отвлекать (например 60-120), иначе 0.")
        try:
            data = fast_brain._json_of(await h._llm.ask(system, text[:400], timeout=60,
                                                        max_tokens=200, temperature=0.1, background=True))
        except Exception as e:
            logger.warning(f"Не удалось разобрать сообщение для памяти: {e}")
            return
        if not data:
            return
        h._store_memories(data)
        h._set_quiet(data.get("quiet_minutes"))

    async def consolidate(self):
        """Фоном: осмыслить недавнюю беседу и сохранить важное (эпизоды, о себе, планы, факты)."""
        h = self._h
        if h._consolidating:
            return
        since = h._memory_reader.meta("consolidated_ts", 0)
        dlg = [m for m in h._memory_reader.recent_dialogue(40) if m["ts"] > since]
        if sum(1 for m in dlg if m["role"] == "user") < 2:
            return
        h._consolidating = True
        try:
            transcript = "\n".join(f"{'Пользователь' if m['role'] == 'user' else 'Ты'}: {m['text']}"
                                   for m in dlg[-24:])
            known = "; ".join(i["text"] for i in h._memory_reader.relevant(transcript[-300:], k=6))
            system = ("Ты осмысляешь недавнюю беседу ассистентки с пользователем. Выдели 0-2 воспоминания, "
                      "которые ты САМА считаешь важными для долгой памяти. kind: user (устойчивый факт о пользователе), "
                      "self (о тебе самой: что ты пообещала, чего он от тебя просит), plan (реальные планы и дела "
                      "пользователя: дедлайны, встречи, экзамены; due_days если срок назван). importance 1-5, "
                      "4-5 только для важного. Не повторяй уже известное. "
                      "Записи о пользователе пиши от третьего лица, о себе от первого. "
                      "Не записывай: пересказ и тон разговора, колкости, настроение, текущий ход игры и временные задачи, "
                      "разовые просьбы (события жизни запоминает отдельный журнал эпизодов). "
                      "Чаще всего ответ пуст. Ответь ТОЛЬКО JSON: "
                      '{"memories": [{"kind": "...", "text": "...", "importance": 3, "due_days": null}]}')
            data = fast_brain._json_of(await h._llm.ask(
                system, f"Уже известно: {known or 'ничего'}\n\nБеседа:\n{transcript}",
                timeout=90, max_tokens=320, temperature=0.2, background=True))
            n = len(h._store_memories(data))
            h._memory_writer.set_meta("consolidated_ts", time.time())
            h._pending_memory = 0
            if n:
                h._log.log("memory", f"осмыслила беседу и запомнила: {n}")
        except Exception as e:
            logger.warning(f"Не удалось осмыслить беседу: {e}")
        finally:
            h._consolidating = False

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
