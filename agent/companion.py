"""Ядро-компаньон: разговор, память о пользователе, проактивность.

Не зависит от игры. Каждое сообщение идёт одним путём:
  1. ответ «да/нет» на открытый вопрос о разрешении;
  2. маршрутизатор (router.py): просит ли пользователь действие, и есть ли для него
     инструмент в каком-либо окружении; план и исполнение либо честное «не могу»;
  3. иначе обычный разговор: локальная модель отвечает сама.
Зашитых веток под отдельные команды больше нет: новая возможность это новый инструмент
в окружении (environments/), а не новая регулярка здесь.

Реплики уходят через вывод речи (speech_out.py): пока это прежний путь через питомца (memory/say.json, дашборд
отдаёт реплику питомцу, он показывает облачко и озвучивает, если не включена глушилка). Обращения приходят из любых
каналов (conversation_io.py), ответ уходит в канал обращения: голос и чат дашборда как раньше, модуль-канал через deliver.

Звучит речь только через очередь менеджера внимания (attention.py, ТЗ 03): `say` и `_speak_about` кладут кандидата в
очередь, а сам планировщик зовёт их с direct=True. Без менеджера (атрибут `attention` пуст: тесты, запуск до сборки слоя
модулей) они говорят сразу, как раньше. Ответ в канал модуля (Telegram) не речь: он доставляется сразу, мимо очереди.
"""
import asyncio
import collections
import json
import logging
import re
import time
from pathlib import Path

import approvals
import gamenet
import events_log
import fast_brain
import perception
import prompts
import settings
from conversation_io import DASHBOARD, VOICE, UserInput, current_channel
from speech_out import PresenceTracker, TextOnlySpeech, new_utterance_id
from user_memory import UserMemory

logger = logging.getLogger("companion")

PERSONA = prompts.companion_persona()   # конституция + prompt.yaml (agent/resources/prompts)

_MIN_GAP = {"quiet": 30 * 60, "normal": 10 * 60, "chatty": 3 * 60}   # не говорить сама чаще
_CONSULT = {"quiet": 10 * 60, "normal": 4 * 60, "chatty": 90}        # как часто спрашивать себя


class Companion:
    def __init__(self, memory_dir: str, rel, watcher: perception.ScreenWatcher | None = None,
                 speech=None, presence: PresenceTracker | None = None):
        self._dir = Path(memory_dir)
        self.presence = presence or PresenceTracker()          # тема presence (ставит слушателя ядро)
        self.speech = speech or TextOnlySpeech(self.presence)  # вывод речи (CORE-27, VOICE-15): ядро ставит выбор голос/текст
        self.io = None                                         # ConversationIO: доставка ответов в каналы модулей
        self.attention = None                                  # Attention: очередь речи и фокус (ТЗ 03); None - говорит сразу
        self.last_utterance_id: str | None = None              # id последней реплики, ушедшей в вывод речи
        self.profile_hint = lambda: ""                         # () -> подсказка тона профиля (ProfileManager.hint)
        self._rel = rel
        self._watcher = watcher
        self.router = None    # Router, ставится ядром после создания реестра окружений
        self.module_notes = lambda: []   # () -> строки о подключённых модулях и их состоянии (ModuleRegistry.notes, ставит ядро)
        self.graph = None        # GraphMemory: факты о людях и жизни в графе
        self._graph_block = ""    # факты графа по теме последней реплики (подмешиваются в контекст)
        self._heard: collections.deque = collections.deque(maxlen=20)   # (время, сводка) услышанного в колонках (voice.heard)
        self.game_order = None  # async (text) -> bool: приказ в игру, ставит контроллер
        self._mem = UserMemory(memory_dir)
        self._last_say = self._load_last_say()   # переживает перезапуски
        self._last_consult = time.time()
        self._away_since: float | None = None
        self._busy = asyncio.Lock()      # один разговор за раз
        self._pending_memory = 0         # сообщений с последнего осмысления беседы
        self._last_user_ts = 0.0
        self._consolidating = False

    def _load_last_say(self) -> float:
        """Когда она в последний раз говорила: из журнала разговора (а не «сейчас»),
        иначе каждый перезапуск откладывал бы её реплики заново."""
        for m in reversed(self._mem.recent_dialogue(60)):
            if m["role"] != "user":
                return m["ts"]
        return time.time() - 3600

    # ── вывод ────────────────────────────────────────────────────────────────

    def _queued(self, direct: bool) -> bool:
        """Идёт ли речь через очередь внимания: ответ в канал модуля (не речь) и прямой вызов планировщика идут мимо неё."""
        if direct or self.attention is None:
            return False
        channel = current_channel.get()
        return not (channel is not None and channel.kind == "module")

    async def _wait_spoken(self, cand) -> None:
        """Формулировка вопроса считается сказанной, когда она прозвучала (ATT-19); ответ раньше озвучки снимает кандидата."""
        if cand.source == "question" and cand.done is not None:
            try:
                await cand.done
            except asyncio.CancelledError:
                self.attention.speech.discard(cand, "cancelled")
                raise

    async def say(self, text: str, emotion: str | None = None, priority: int | None = None, *, direct: bool = False,
                  source: str | None = None):
        """Сказать реплику: через очередь речи внимания (ATT-13) или сразу (direct=True: так зовёт сам планировщик)."""
        if self._queued(direct):
            await self._wait_spoken(self.attention.enqueue_say(text, emotion, priority, source=source))
            return
        await self._say_now(text, emotion, priority)

    async def _say_now(self, text: str, emotion: str | None = None, priority: int | None = None):
        """Журнал, вывод речи (питомец), эмоция. В канале модуля вместо речи идёт deliver."""
        from speech_text import clean_speech
        text = clean_speech(text)   # без разметки, ремарок в скобках и оборванного хвоста: это произносится вслух
        if not text:
            return
        channel = current_channel.get()
        self._mem.add_dialogue("glados", text)
        events_log.log("say", text, emotion=emotion, **({"channel": channel.label()} if channel else {}))
        self._last_say = time.time()
        self.presence.update(last_say=text, last_say_ts=self._last_say)
        if channel is not None and channel.kind == "module":
            # ответ на обращение из модуля-канала: вслух не произносится (CORE-25); не доставлен - остаётся в чате дашборда
            if self.io is not None:
                await self.io.deliver(channel, text)
        else:
            if emotion:
                self.presence.update(emotion=emotion)
            utterance = new_utterance_id()
            self.last_utterance_id = utterance
            await self.speech.speak(utterance, text, emotion, priority or 50)
        if emotion:
            fast_brain.publish("say", {"emotion": emotion, "action": "none"})
        logger.info(f"[ГЛаДОС] {text}")

    # ── входящее сообщение ─────────────────────────────────────────────────────

    def is_speaking(self) -> bool:
        return self.speech.is_speaking()

    def note_heard(self, summary: str) -> None:
        """Сводка услышанного в колонках (событие `voice.heard`, VOICE-14): в контекст разговора. Сырой текст остаётся в модуле
        голоса (инструмент recent_audio отдаёт его по запросу)."""
        self._heard.append((time.time(), summary))

    def recent_heard(self, secs: int = 600) -> str:
        cutoff = time.time() - secs
        return " ".join(s for ts, s in self._heard if ts >= cutoff)[-900:]

    async def on_voice(self, text: str, speaker: dict):
        """Реплика, сказанная голосом: узнанный владелец идёт как обычное сообщение, остальным ставится пометка,
        кто говорит (имя или «незнакомый голос»), и заводится профиль человека."""
        pid, name = speaker.get("id"), speaker.get("name")
        if speaker.get("owner") or not speaker.get("has_owner"):
            prefix = ""
        elif name:
            prefix = f"[говорит {name}] "
        else:
            prefix = "[незнакомый голос] "
        if pid and not speaker.get("owner") and self._rel is not None and speaker.get("has_owner"):
            self._rel.ensure_person(f"voice:{pid}", name or f"голос {str(pid)[-4:]}", source="voice")
            self._rel.note(f"voice:{pid}", text)
        await self.on_input(UserInput(prefix + text, VOICE, ts=time.time()))

    async def on_user_message(self, text: str, channel=None):
        """Обращение текстом (чат дашборда, строка питомца); channel по умолчанию - дашборд."""
        await self.on_input(UserInput(text, channel or DASHBOARD))

    async def on_input(self, ui: UserInput):
        """Единый вход обращений (CORE-24): ответ на него уходит в канал обращения (current_channel)."""
        token = current_channel.set(ui.channel)
        if self.attention is None:
            self.presence.update(busy=True)         # с менеджером внимания занятость определяет стек фокуса (ATT-23)
        try:
            await self._handle_text(ui.text)
        finally:
            current_channel.reset(token)
            if self.attention is None:
                self.presence.update(busy=False)

    async def _handle_text(self, text: str):
        text = text.strip()
        if not text:
            return
        self._mem.add_dialogue("user", text)
        events_log.log("user", text)
        self._queue_graph(text)
        self._pending_memory += 1
        self._last_user_ts = time.time()
        self.presence.update(last_user_ts=self._last_user_ts)
        fast_brain.publish("chat", {"emotion": "thinking", "action": "none"})   # сразу «задумалась», без вызова модели

        cls = None
        if self.attention is not None:
            await self.attention.on_user_input(text)          # фокус «разговор»; «замолчи» обрывает речь до любых вызовов модели
            cls = await self.attention.classify_input(text)   # намерение (ATT-28); None - классификатора нет, всё как раньше
        if approvals.has_pending() and await self._answer_approval(text):
            return
        if self.game_order is not None:
            try:
                taken = await self.game_order(text)
            except Exception as e:
                events_log.log("error", f"приказ в игру не принят: {e}")
                taken = False
            if taken:
                asyncio.create_task(self._speak_about(
                    "Игрок только что дал тебе срочное указание по игре, ты прямо сейчас берёшься выполнять его "
                    "(действия в игре идут отдельно). Скажи одной короткой фразой в своём характере, что принимаешь.",
                    text, must=True))
                asyncio.create_task(fast_brain.react_chat(text, self._rel))
                return
        intent = cls.intent if cls is not None else None
        # needs_memory классификатора пока смещён в «нет» (замеры ТЗ 03): по нему память не отключаем, только пишем в журнал.
        # Поиск в графе пропускается лишь там, где память заведомо не нужна; вернуть needs_memory после дообучения.
        use_memory = intent not in ("stop", "approval")
        async with self._busy:
            if intent == "about_self":
                await self._converse(text, about_self=True, use_memory=use_memory)   # что она умеет: ответ по перечню окружений
            elif self.router is not None and (self.router.looks_like_action(text) or intent in ("action", "attention_rule")):
                await self._act(text)          # просьба: планировщик, ответ после плана
            else:
                flagged = await self._converse(text, use_memory=use_memory)   # болтовня: один вызов, ответ сразу
                if flagged and self.router is not None:
                    await self._act(text, after_chat=True)   # модель заподозрила просьбу, которую не распознали глаголы
        asyncio.create_task(fast_brain.react_chat(text, self._rel))   # эмоция и отношение: уже после ответа
        if self._pending_memory >= 6:
            asyncio.create_task(self._consolidate())                  # осмыслить беседу и запомнить важное

    async def _act(self, text: str, after_chat: bool = False):
        """Просьба действовать: маршрутизатор решает «могу или нет», выполняет, а итог говорится вслух."""
        outcome = await self.router.handle(text, announce=not after_chat)
        if after_chat and self.router.last_kind == "cannot":
            return   # на ответ-болтовню уже сказано всё, что можно; не повторяем «не могу» вторым сообщением
        if outcome:
            # итог действия или честное «не могу»: пользователь должен узнать результат
            await self._speak_about(outcome, text, must=True)
        elif outcome is None and not after_chat:
            await self._converse(text)    # планировщик решил, что это всё-таки разговор

    async def _answer_approval(self, text: str) -> bool:
        """Реплика как ответ на открытый вопрос (да/нет, вариант, свободный текст): см. approvals.try_answer."""
        return await approvals.try_answer(text)

    # ── разговор ───────────────────────────────────────────────────────────────

    async def _speak_about(self, fact: str, user_text: str = "", must: bool = False, question: bool = False,
                           direct: bool = False):
        """Нет заготовленных фраз: ей сообщают факт, а говорить ли и что — решает сама.
        must=True для итога прямой просьбы пользователя: промолчать нельзя, он должен
        узнать, что получилось (иначе непонятно, сработало ли).
        question=True: факт - вопрос к пользователю (core.ask, вопрос задачи), его нужно задать своими словами.
        direct=False: факт кладётся в очередь речи внимания, реплику сформулирует планировщик, когда придёт очередь;
        direct=True: планировщик уже выбрал этот факт - реплика формулируется и говорится сразу."""
        if self._queued(direct):
            await self._wait_spoken(self.attention.enqueue_fact(fact, user_text, must, question))
            return
        if question:
            rule = ("Пользователю нужно задать вопрос, который описан в факте: задай его своими словами коротко, "
                    "ничего не выдумывая и не отвечая за него. Промолчать нельзя. "
                    'Ответь ТОЛЬКО JSON: {"say": true, "text": "<реплика>"}')
        elif must:
            rule = ("Пользователь сам об этом попросил, поэтому обязательно коротко сообщи ему, "
                    "что получилось. Упомяни суть результата, не выдумывай деталей. "
                    'Ответь ТОЛЬКО JSON: {"say": true, "text": "<реплика>"}')
        else:
            rule = ("Сама реши, нужно ли что-то сказать: можно промолчать. Не пересказывай факт "
                    "дословно и не выдумывай деталей. Ответь ТОЛЬКО JSON: "
                    '{"say": <true или false>, "text": "<реплика>"}')
        system = PERSONA + " Ниже факт о том, что только что произошло. " + rule
        user = (f"{self._context(query=user_text or fact)}\n\nФакт: {fact}"
                + (f"\nПользователь сказал: {user_text}" if user_text else ""))
        try:
            data = fast_brain._json_of(await fast_brain._ask(system, user, timeout=60,
                                                             max_tokens=160, temperature=0.7))
        except Exception as e:
            logger.warning(f"Не удалось решить, что сказать: {e}")
            return
        if data and data.get("say") and data.get("text"):
            await self.say(str(data["text"]), direct=True)

    def _claude_note(self) -> str:
        import claude_quota
        return (f"Claude (игровые ходы, глубокий анализ, помощник) сейчас недоступен: исчерпан лимит до "
                f"{claude_quota.until_text()}. Если просят то, что требует Claude, честно скажи это.") if claude_quota.blocked() else ""

    def _capabilities(self) -> str:
        """Честный список того, что у неё сейчас включено: без него она выдумывает возможности."""
        cfg = settings.load()
        lines = ["Справка о твоих возможностях и их состоянии (для тебя, а не тема разговора: упоминай их, только если спросили; говори о них только по этим данным):",
                 f"- Зрение: {'включено' if cfg['vision'] else 'выключено'}.",
                 f"- Слух (системный звук): {'включён' if cfg['hearing'] else 'выключен'}.",
                 f"- Голос: {'заглушён' if cfg['voice_muted'] else 'включён'}."]
        lines += ["- " + note for note in self.module_notes()]
        lines.append("- Помощник Claude (Клод, Claude Code): ты можешь поручить ему сложные задачи на компьютере: "
                     "установку программ и библиотек, файлы, код, команды, интернет, анализ. Он сам спрашивает "
                     "разрешение на каждое изменение. Просьбы, которые тебе не по силам, уходят к нему.")
        if self.graph is not None:
            lines.append("- " + self.graph.status_text())
        note = self._claude_note()
        if note:
            lines.append("- " + note)
        return "\n".join(lines)

    def _context(self, include_hearing: bool = True, query: str = "") -> str:
        """query: о чём сейчас речь. По нему из долговременной памяти берутся подходящие записи."""
        parts = [self._mem.context_block(dialogue=12, query=query), self._capabilities()]
        if include_hearing and settings.load()["hearing"]:
            heard = self.recent_heard()
            if heard:
                parts.append("Что недавно было слышно из колонок (краткая сводка автоматической расшифровки, "
                             "возможны ошибки; это может быть фильм, видео, музыка, не слова "
                             "пользователя): " + heard)
        if self._graph_block:
            parts.append(self._graph_block)
        rel = self._rel.describe("игрок")
        if rel:
            parts.append(f"Твоё {rel}.")
        return "\n".join(p for p in parts if p)

    _OFFER = re.compile(r"^(ты )?(хочешь|не хочешь|хотела бы|хотел бы)(,? чтобы я)?|^если (хочешь|нужно)|^могу ли я|"
                        r"^нужна ли тебе|^чем (ещё )?(могу )?помочь|^давай я", re.I)

    _PROMISE = re.compile(r"^(сейчас |щас |хорошо, )?(опубликую|отправлю|запущу|включу|выключу|запощу|напишу в)", re.I)

    def _declutter(self, reply: str) -> str:
        """Страховка от навязчивости: убирает хвост-предложение помощи («Хочешь, чтобы я…?») и
        третий вопрос подряд в конце реплики. Применяется, только если останется хотя бы одно предложение."""
        sents = [x for x in re.split(r"(?<=[.!?…])\s+", reply.strip()) if x]
        if len(sents) < 2:
            return reply
        # обещание действия без вызова инструмента (router.last_kind == "done" значит, что он был)
        if (self.router is None or self.router.last_kind != "done") and self._PROMISE.match(sents[0].strip(" «\"")):
            sents = sents[1:]
            reply = " ".join(sents)
            if len(sents) < 2:
                return reply
        last = sents[-1].strip(" «\"")
        recent = [m["text"].rstrip().endswith("?") for m in self._mem.recent_dialogue(10) if m["role"] != "user"][-2:]
        if self._OFFER.match(last) or (sents[-1].rstrip().endswith("?") and len(recent) == 2 and all(recent)):
            return " ".join(sents[:-1])
        return reply

    _SPEAKER_TAG = re.compile(r"^\[(говорит ([^\]]+)|незнакомый голос)\]\s*")

    def _owner_name(self) -> str:
        for fact in self._mem.facts():
            m = re.match(r"\s*(?:имя(?: пользователя)?|зовут)\s*[:\-]\s*(.+)", fact["text"], re.I)
            names = re.findall(r"[А-Яа-яЁёA-Za-z\-]{2,}", m.group(1)) if m else []
            if names:
                return names[0]
        return ""

    def _queue_graph(self, text: str):
        """Реплика человека уходит в очередь на разбор фактов (граф). Незнакомые голоса не запоминаем."""
        if self.graph is None:
            return
        tag = self._SPEAKER_TAG.match(text)
        if tag:
            if not tag.group(2):
                return
            speaker, text = tag.group(2), text[tag.end():]
        else:
            speaker = self._owner_name() or "Пользователь"
        self.graph.enqueue(speaker, text)

    async def _refresh_graph(self, query: str):
        """Факты графа по теме реплики: поиск быстрый, но не дольше трёх секунд, иначе отвечаем без них."""
        if self.graph is None:
            return
        try:
            self._graph_block = await asyncio.wait_for(self.graph.context_block(query), 3.0)
        except (asyncio.TimeoutError, Exception):
            self._graph_block = ""

    async def _converse(self, text: str, about_self: bool = False, use_memory: bool = True) -> bool:
        """Обычный ответ обычным текстом, без JSON: так он приходит вдвое быстрее (локальная
        модель генерирует ~10-15 токенов в секунду, лишние поля стоят секунд).
        Возвращает True, если модель пометила сообщение как просьбу действовать."""
        system = (PERSONA + " " + self.profile_hint() + " Если пользователь просит что-то СДЕЛАТЬ (включить, выключить, найти, написать, "
                  "опубликовать, посмотреть), начни ответ с маркера [ДЕЙСТВИЕ] и коротко скажи, что сейчас "
                  "посмотришь, что можешь, БЕЗ выдуманного результата. Иначе просто ответь.")
        if use_memory:
            await self._refresh_graph(text)
        else:
            self._graph_block = ""          # классификатор: для этой реплики факты о людях и прошлом не нужны (ATT-28)
        user = f"{self._context(query=text)}\n\nПользователь говорит: {text}"
        if about_self and self.router is not None:
            user += ("\n\nПользователь спрашивает о твоих возможностях. Отвечай строго по этому перечню, ничего не выдумывая:\n"
                     + self.router.registry.describe())
        reply, escalate, action_request = "", False, False   # escalate: только если локальная модель недоступна
        try:
            reply = (await fast_brain._ask(system, user, timeout=60, max_tokens=140, temperature=0.7)).strip()
            if reply.startswith("[ДЕЙСТВИЕ]"):
                action_request, reply = True, reply[len("[ДЕЙСТВИЕ]"):].strip()
        except Exception as e:
            logger.warning(f"Локальная модель не ответила: {e}")
            escalate = True    # без локальной модели пусть отвечает Claude
        if reply:
            await self.say(self._declutter(reply))
        if escalate:
            await self._escalate(text)
        if len(text) >= 15:
            asyncio.create_task(self._learn(text))   # фоном, уже после ответа
        return action_request

    def _store_memories(self, data) -> list[str]:
        """Сохранить воспоминания из ответа модели ({"memories": [...]}); вернуть тексты новых."""
        added: list[str] = []
        for m in (data or {}).get("memories") or []:
            if not isinstance(m, dict) or m.get("kind") not in ("user", "self", "plan", "episode"):
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
            if self._mem.add_fact(text, kind=m["kind"], importance=importance, due_days=due):
                added.append(text)
        return added

    async def remember_text(self, text: str, hint_kind: str | None = None) -> list[str]:
        """Явная просьба «запомни…»: аккуратно разобрать текст на записи правильного вида и в
        правильном лице (о пользователе от третьего, о себе от первого). Сразу, не фоном."""
        system = ("Пользователь просит тебя запомнить сказанное. Сформулируй 1-2 записи. kind: "
                  "user (факт о нём: пиши от третьего лица, «Любимый цвет пользователя - зелёный»), "
                  "self (о тебе самой, чего он хочет от тебя: пиши от первого лица, «Я называю пользователя "
                  "Аней и реже язвлю»), plan (планы и дела: дедлайны, встречи, экзамены, задачи; due_days - "
                  "через сколько дней, если срок можно понять, иначе null), episode (событие). importance 1-5. "
                  'Ответь ТОЛЬКО JSON: {"memories": [{"kind": "...", "text": "...", "importance": 4, "due_days": null}]}. '
                  f"Сегодня {time.strftime('%A, %d.%m')}.")
        try:
            data = fast_brain._json_of(await fast_brain._ask(system, text[:500], timeout=60,
                                                             max_tokens=220, temperature=0.1))
        except Exception as e:
            logger.warning(f"Не удалось разобрать «запомни»: {e}")
            data = None
        stored = self._store_memories(data)
        if not stored:   # модель не помогла: сохраняем как есть, чтобы не потерять просьбу
            kind = hint_kind if hint_kind in ("user", "self", "plan", "episode") else "user"
            if self._mem.add_fact(text.strip(), kind=kind, importance=4):
                stored = [text.strip()]
        return stored

    async def _learn(self, text: str):
        """Фоном после ответа: что запомнить надолго из этого сообщения и не просил ли он не отвлекать."""
        system = ("Из сообщения пользователя выдели то, что стоит запомнить надолго. Верни JSON: "
                  '{"memories": [{"kind": "user|self|plan", "text": "...", "importance": 3, "due_days": null}], '
                  '"quiet_minutes": 0}. kind=user: устойчивые факты о нём (имя, привычки, предпочтения). '
                  "kind=self: чего он хочет от ТЕБЯ (как с ним говорить, что делать чаще или реже). "
                  "kind=plan: его планы и дела: дедлайны, встречи, экзамены, задачи (due_days: через сколько дней, если названо, иначе null). "
                  "Записи о пользователе пиши от третьего лица, о тебе самой от первого. "
                  "Мелочи и сиюминутное (настроение, усталость, просьбы что-то сделать прямо сейчас в игре или на "
                  "компьютере) не запоминай: обычно memories пустой. "
                  "quiet_minutes: если он сказал, что сейчас будет смотреть, работать или заниматься, "
                  "на сколько минут его не отвлекать (например 60-120), иначе 0.")
        try:
            data = fast_brain._json_of(await fast_brain._ask(system, text[:400], timeout=60,
                                                             max_tokens=200, temperature=0.1, background=True))
        except Exception as e:
            logger.warning(f"Не удалось разобрать сообщение для памяти: {e}")
            return
        if not data:
            return
        self._store_memories(data)
        self._set_quiet(data.get("quiet_minutes"))

    async def _consolidate(self):
        """Фоном: осмыслить недавнюю беседу и сохранить важное (эпизоды, о себе, планы, факты)."""
        if self._consolidating:
            return
        store = self._mem.store
        since = store.meta("consolidated_ts", 0)
        dlg = [m for m in self._mem.recent_dialogue(40) if m["ts"] > since]
        if sum(1 for m in dlg if m["role"] == "user") < 2:
            return
        self._consolidating = True
        try:
            transcript = "\n".join(f"{'Пользователь' if m['role'] == 'user' else 'Ты'}: {m['text']}"
                                   for m in dlg[-24:])
            known = "; ".join(i["text"] for i in store.relevant(transcript[-300:], k=6))
            system = ("Ты осмысляешь недавнюю беседу ассистентки с пользователем. Выдели 0-3 воспоминания, "
                      "которые стоит сохранить надолго. kind: user (устойчивый факт о пользователе), "
                      "self (о тебе самой: что ты пообещала, какое мнение у тебя сложилось, о чём вы шутили, "
                      "пожелания к тебе), plan (планы и дела пользователя: дедлайны, встречи, экзамены, задачи; due_days если срок назван), episode (заметное "
                      "событие или итог беседы одной фразой). importance 1-5. Не повторяй уже известное. "
                      "Записи о пользователе пиши от третьего лица, о себе от первого. "
                      "Мелочи не нужны (в том числе разовые просьбы сделать что-то прямо сейчас в игре или на компьютере), "
                      "чаще всего ответ пуст. Ответь ТОЛЬКО JSON: "
                      '{"memories": [{"kind": "...", "text": "...", "importance": 3, "due_days": null}]}')
            data = fast_brain._json_of(await fast_brain._ask(
                system, f"Уже известно: {known or 'ничего'}\n\nБеседа:\n{transcript}",
                timeout=90, max_tokens=320, temperature=0.2, background=True))
            n = len(self._store_memories(data))
            store.set_meta("consolidated_ts", time.time())
            self._pending_memory = 0
            if n:
                events_log.log("memory", f"осмыслила беседу и запомнила: {n}")
        except Exception as e:
            logger.warning(f"Не удалось осмыслить беседу: {e}")
        finally:
            self._consolidating = False

    def _set_quiet(self, minutes):
        """Пользователь сказал, что занят: сама она не заговаривает это время."""
        try:
            mins = max(0, min(int(minutes), 240))
        except (TypeError, ValueError):
            return
        if mins > 0:
            settings.update(quiet_until=time.time() + mins * 60)
            events_log.log("setting", f"режим «не отвлекать» на {mins} мин (по словам пользователя)")

    async def run_claude(self, task: str) -> str | None:
        """Сложная задача на Claude с инструментами Claude Code (подтверждения на каждое
        изменение). Услышанное в контекст не передаётся: в облако оно не уходит."""
        import assistant_agent
        return await assistant_agent.run_task(task, self._context(include_hearing=False, query=task), self.say)

    async def _escalate(self, task: str):
        """Запасной путь, когда локальная модель недоступна."""
        result = await self.run_claude(task)
        if result:
            await self.say(result)
        elif result is None:
            await self._speak_about(f"Не удалось выполнить просьбу «{task}»: сбой агента-помощника.", task)
        else:
            await self._speak_about(f"Просьба «{task}» выполнена, добавить нечего.", task)

    # ── проактивность ──────────────────────────────────────────────────────────
    # Жёстких триггеров нет: через паузу (зависит от разговорчивости) она сама
    # спрашивает себя, стоит ли что-то сказать, имея перед глазами дайджест
    # последних занятий. Промолчать — нормальный исход.

    async def proactive_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self._maybe_speak()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Проактивность: {e}")
                events_log.log("error", f"проактивность: {e}")

    def _digest(self, silent_mins: int) -> str:
        """Дайджест для решения «заговорить ли»: время, молчание, занятия за последние минуты."""
        lines = [f"Сейчас {time.strftime('%H:%M')}. Ты молчишь уже {silent_mins} мин."]
        if settings.load()["vision"]:
            acts = self._mem.recent_activity(5)
            if acts:
                lines.append("Что ты видела на экране (от старого к новому):")
                for a in acts:
                    mins = int((time.time() - a["ts"]) / 60)
                    lines.append(f"  {mins} мин назад: {a['summary']}")
        else:
            lines.append("Зрение выключено, на экран ты не смотришь.")
        plans = self._mem.store.plans_to_nudge()[:2]
        if plans:
            lines.append("Скоро у пользователя (напомни ОДИН раз, коротко, и больше к этому не возвращайся):")
            lines += [f"  - {p['text']}" for p in plans]
        return "\n".join(lines)

    def _repeats_recent(self, text: str) -> bool:
        """Не повторять недавние темы: сильное пересечение по словам с её последними репликами."""
        from memory_store import stems
        new = stems(text)
        if len(new) < 3:
            return False
        for m in [m for m in self._mem.recent_dialogue(10) if m["role"] != "user"][-5:]:
            old = stems(m["text"])
            if old and len(new & old) / min(len(new), len(old)) >= 0.5:
                return True
        return False

    async def _maybe_speak(self):
        if self._busy.locked() or approvals.has_pending():
            return
        if self._pending_memory >= 2 and time.time() - self._last_user_ts > 8 * 60:
            asyncio.create_task(self._consolidate())   # пауза в разговоре: самое время осмыслить
        now = time.time()
        idle = perception.idle_seconds()
        # Пользователь отошёл / вернулся
        if idle > 600:
            if self._away_since is None:
                self._away_since = now - idle
            return
        returned_after = None
        if self._away_since is not None:
            away_mins = (now - self._away_since) / 60
            self._away_since = None
            if away_mins >= 15:
                returned_after = int(away_mins)

        cfg = settings.load()
        if settings.quiet_active(cfg):
            return   # пользователь просил не отвлекать (смотрит, работает)
        level = cfg["chattiness"]
        gap = now - self._last_say
        if now - self._last_user_ts < 120:
            return   # только что разговаривали: сама не вклинивается
        if gamenet.port_open() and gap < 45 * 60:
            return   # идёт игра: про игру говорит игровой комментатор, а тут сама не лезет
        cur = self._watcher.current if self._watcher and cfg["vision"] else None
        if (cur and cur["category"] in ("media", "game") and now - cur["ts"] < 600
                and gap < 45 * 60):
            return   # смотрит видео или играет: сама не лезет (если не слишком долго молчала)
        if returned_after is not None and gap > 120:
            situation = f"Пользователь вернулся к компьютеру после перерыва в {returned_after} мин."
        else:
            if gap < _MIN_GAP.get(level, _MIN_GAP["normal"]):
                return
            if now - self._last_consult < _CONSULT.get(level, _CONSULT["normal"]):
                return
            situation = self._digest(int(gap / 60))
        self._last_consult = now

        system = (PERSONA + "\n" + self.profile_hint() + "\nСама решай, стоит ли сейчас вмешаться. Говори, когда есть что-то уместное: "
                  "наблюдение о том, что видишь, шутка про занятие, забота (долго сидит, поздно), вопрос. "
                  "Не навязывайся без повода: если пользователь сейчас занят (игра, работа, видео) или сказать "
                  "нечего, ответь say=false. Не возвращайся к темам, которые уже поднимала в последних "
                  "репликах. Ответь ТОЛЬКО JSON: "
                  '{"say": <true или false>, "text": "<реплика>", "emotion": <одно из '
                  + json.dumps(fast_brain.EMOTIONS) + ">}")
        user = f"{self._context()}\n\n{situation}"
        raw = await fast_brain._ask(system, user, timeout=60, max_tokens=200, temperature=0.8,
                                    background=True)
        data = fast_brain._json_of(raw)
        if data and data.get("say") and data.get("text") and self._repeats_recent(str(data["text"])):
            events_log.log("proactive", situation, said=False, why="повтор недавней темы")
            self._last_say = now - _MIN_GAP.get(level, _MIN_GAP["normal"]) / 2
        elif data and data.get("say") and data.get("text"):
            emo = data.get("emotion") if data.get("emotion") in fast_brain.EMOTIONS else None
            await self.say(str(data["text"]), emo, source="proactive")
            events_log.log("proactive", situation, said=True)
        else:
            events_log.log("proactive", situation, said=False)
