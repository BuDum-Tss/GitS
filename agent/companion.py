"""Ядро-компаньон: разговор, память о пользователе, проактивность.

Не зависит от игры. Каждое сообщение идёт одним путём:
  1. ответ «да/нет» на открытый вопрос о разрешении;
  2. маршрутизатор (router.py): просит ли пользователь действие, и есть ли для него
     инструмент в каком-либо окружении; план и исполнение либо честное «не могу»;
  3. иначе обычный разговор: локальная модель отвечает сама.
Зашитых веток под отдельные команды больше нет: новая возможность это новый инструмент
в окружении (environments/), а не новая регулярка здесь.

Реплики уходят через вывод речи (speech_out.py): модуль голоса, пока он подключён (озвучка и текущее предложение для облачка
питомца идут событиями voice.speech и темой presence), иначе только текст в чате и в presence. Обращения приходят из любых
каналов (conversation_io.py), ответ уходит в канал обращения: голос и чат дашборда как раньше, модуль-канал через deliver.

Звучит речь только через очередь менеджера внимания (attention.py, ТЗ 03): `say` и `_speak_about` кладут кандидата в
очередь, а сам планировщик зовёт их с direct=True. Без менеджера (атрибут `attention` пуст: тесты, запуск до сборки слоя
модулей) они говорят сразу, как раньше. Ответ в канал модуля (Telegram) не речь: он доставляется сразу, мимо очереди.

Companion теперь тонкий фасад (ТЗ 29, ARC-5): состояние и публичный интерфейс здесь, логика в `agent/app/`:
ConversationService (путь обращения), ContextBuilder (контекст), ReplyGenerator (ответ), MemoryCapture (запоминание),
ProactiveLegacy (временная проактивность по таймеру, до ТЗ 30).
"""
import asyncio
import collections
import logging
import time
from pathlib import Path

import claude_quota
import fast_brain
import character
import prompts
from adapters.approver import ApprovalsModule
from adapters.event_log_sqlite import SqliteEventLog
from adapters.llm_lmstudio import LMStudioLLM
from adapters.memory import UserMemoryAdapter
from adapters.people import RelationshipsAdapter
from adapters.settings_provider import SettingsModule
from app.context_builder import ContextBuilder
from app.conversation_service import ConversationService, _sender_ctx  # noqa: F401  (_sender_ctx: прежнее имя)
from app.memory_capture import MemoryCapture
from app.proactive_legacy import CONSULT as _CONSULT, MIN_GAP as _MIN_GAP, ProactiveLegacy  # noqa: F401
from app.reply_generator import ReplyGenerator
from conversation_io import UserInput, current_channel
from speech_out import PresenceTracker, TextOnlySpeech, new_utterance_id
from user_memory import UserMemory

logger = logging.getLogger("companion")


def persona() -> str:
    """Характер компаньона активного персонажа (конституция + prompt.yaml); функция, чтобы переключение подхватывало новый."""
    return prompts.companion_persona()


class Companion:
    def __init__(self, memory_dir: str, rel, speech=None, presence: PresenceTracker | None = None, *,
                 llm=None, memory_reader=None, memory_writer=None, people=None, event_log=None,
                 settings_provider=None, approver=None):
        """Контракты (ТЗ 29, ARC-3, шаг R2) необязательны: без них подставляются адаптеры к текущим модулям ядра.
        llm - LanguageModel, memory_reader/memory_writer - MemoryReader/MemoryWriter, people - PeopleDirectory,
        event_log - EventLog, settings_provider - SettingsProvider, approver - Approver."""
        self._dir = Path(memory_dir)
        self.presence = presence or PresenceTracker()          # тема presence (ставит слушателя ядро)
        self.speech = speech or TextOnlySpeech(self.presence)  # вывод речи (CORE-27, VOICE-15): ядро ставит выбор голос/текст
        self.io = None                                         # ConversationIO: доставка ответов в каналы модулей
        self.attention = None                                  # Attention: очередь речи и фокус (ТЗ 03); None - говорит сразу
        self.last_utterance_id: str | None = None              # id последней реплики, ушедшей в вывод речи
        self.profile_hint = lambda: ""                         # () -> подсказка тона профиля (ProfileManager.hint)
        self._rel = rel
        self._people = people if people is not None else RelationshipsAdapter(lambda: self._rel)
        self._llm = llm if llm is not None else LMStudioLLM()
        self._log = event_log if event_log is not None else SqliteEventLog()
        self._settings = settings_provider if settings_provider is not None else SettingsModule()
        self._approver = approver if approver is not None else ApprovalsModule()
        self.router = None    # Router, ставится ядром после создания реестра окружений
        self.module_notes = lambda: []   # () -> строки о подключённых модулях и их состоянии (ModuleRegistry.notes, ставит ядро)
        self.graph = None        # GraphMemory: факты о людях и жизни в графе
        self._graph_block = ""    # факты графа по теме последней реплики (подмешиваются в контекст)
        self._self_block = ""     # воспоминания персонажа о себе к последней реплике (ТЗ 05, CHR-19)
        self._heard: collections.deque = collections.deque(maxlen=20)   # (время, сводка) услышанного в колонках (voice.heard)
        self.game_order = None  # async (text) -> bool: приказ в игру, ставит контроллер
        self.idle_seconds = lambda: 0.0          # секунды простоя ввода (ставит ядро из state модуля desktop; без модуля 0: за компьютером)
        self.screen_activity = lambda: None      # текущая активность экрана {app, category, summary, ...} (ставит ядро, ТЗ 14 DESK-7)
        self.job_wait = None                     # async (module_id, job_id, timeout) -> итог задачи модуля (ставит ядро, DESK-11)
        if memory_reader is None or memory_writer is None:
            self._mem = UserMemory(memory_dir)                 # личная память на диске (адаптеры по умолчанию)
            default_memory = UserMemoryAdapter(lambda: self._mem, lambda: self.graph)
        else:
            self._mem = None                                   # память передана контрактами: диска не трогаем
            default_memory = None
        self._memory_reader = memory_reader if memory_reader is not None else default_memory
        self._memory_writer = memory_writer if memory_writer is not None else default_memory
        self._last_say = self._load_last_say()   # переживает перезапуски
        self._last_consult = time.time()
        self._away_since: float | None = None
        self._busy = asyncio.Lock()      # один разговор за раз
        self._pending_memory = 0         # сообщений с последнего осмысления беседы
        self._last_user_ts = 0.0
        self._consolidating = False
        self._last_say_text = ""          # последняя реплика персонажа (для поправки «нет, не так», ТЗ 24 TRC-5)
        self._bg_tasks: set = set()       # фоновые проверки поправок (ТЗ 24)
        # компоненты (ТЗ 29, ARC-5): состояние остаётся здесь, логика в agent/app/
        self.context_builder = ContextBuilder(self)
        self.reply_generator = ReplyGenerator(self)
        self.memory_capture = MemoryCapture(self)
        self.conversation = ConversationService(self)
        self.proactive_legacy = ProactiveLegacy(self)

    @property
    def people(self):
        """PeopleDirectory: профили людей (менеджер внимания берёт отсюда отправителей)."""
        return self._people

    @property
    def memory_reader(self):
        """MemoryReader: чтение памяти для ответа."""
        return self._memory_reader

    @property
    def memory_writer(self):
        """MemoryWriter: запись памяти (менеджер внимания кладёт сюда события, которые стоит запомнить)."""
        return self._memory_writer

    def _load_last_say(self) -> float:
        """Когда она в последний раз говорила: из журнала разговора (а не «сейчас»),
        иначе каждый перезапуск откладывал бы её реплики заново."""
        for m in reversed(self._memory_reader.recent_dialogue(60)):
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
        self._memory_writer.add_dialogue(character.active().id, text)   # роль реплики персонажа: его id (данные диалога)
        self._log.log("say", text, emotion=emotion, **({"channel": channel.label()} if channel else {}))
        self._last_say = time.time()
        self._last_say_text = text
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
        logger.info(f"[{character.active().name}] {text}")

    # ── входящее сообщение ─────────────────────────────────────────────────────

    def rebind_memory(self, memory_dir) -> None:
        """Переключение персонажа (ТЗ 05, CHR-7): личная память этого персонажа, та же беседа и тон."""
        self._memory_writer.rebind(memory_dir)

    def is_speaking(self) -> bool:
        return self.speech.is_speaking()

    def note_heard(self, summary: str) -> None:
        """Сводка услышанного в колонках (событие `voice.heard`, VOICE-14): в контекст разговора. Сырой текст остаётся в модуле
        голоса (инструмент recent_audio отдаёт его по запросу)."""
        self._heard.append((time.time(), summary))

    def recent_heard(self, secs: int = 600) -> str:
        cutoff = time.time() - secs
        return " ".join(s for ts, s in self._heard if ts >= cutoff)[-900:]


    async def on_voice(self, text: str, speaker: dict, trace=None):
        await self.conversation.on_voice(text, speaker, trace)

    async def on_user_message(self, text: str, channel=None):
        await self.conversation.on_user_message(text, channel)

    async def on_input(self, ui: UserInput):
        await self.conversation.on_input(ui)

    async def _handle_text(self, text: str):
        await self.conversation.handle_text(text)

    def _check_correction(self, text: str) -> None:
        self.conversation.check_correction(text)

    async def _confirm_correction(self, text: str, ref: float, prev: str) -> None:
        await self.conversation.confirm_correction(text, ref, prev)

    def _log_route(self, text: str, route: str, routed: bool, intent, cls) -> None:
        self.conversation.log_route(text, route, routed, intent, cls)

    async def _act(self, text: str, after_chat: bool = False):
        await self.conversation.act(text, after_chat)

    async def _answer_approval(self, text: str) -> bool:
        """Реплика как ответ на открытый вопрос (да/нет, вариант, свободный текст): см. approvals.try_answer."""
        return await self._approver.try_answer(text)

    # ── разговор ───────────────────────────────────────────────────────────────

    async def speak_about(self, fact: str, user_text: str = "", must: bool = False, question: bool = False,
                          direct: bool = False):
        """Публичный вход (ТЗ 29): менеджер внимания зовёт его вместо приватного имени. Старое имя `_speak_about` ниже."""
        return await self._speak_about_impl(fact, user_text, must, question, direct)

    async def _speak_about(self, fact: str, user_text: str = "", must: bool = False, question: bool = False,
                           direct: bool = False):
        return await self._speak_about_impl(fact, user_text, must, question, direct)

    async def _speak_about_impl(self, fact: str, user_text: str, must: bool, question: bool, direct: bool):
        await self.reply_generator.speak_about(fact, user_text, must, question, direct)

    def _claude_note(self) -> str:
        return self.context_builder.claude_note()

    def _capabilities(self) -> str:
        return self.context_builder.capabilities()

    def _context(self, include_hearing: bool = True, query: str = "") -> str:
        return self.context_builder.build(include_hearing, query)

    def _declutter(self, reply: str) -> str:
        return self.reply_generator.declutter(reply)

    def _owner_name(self) -> str:
        return self.memory_capture.owner_name()

    def _queue_graph(self, text: str):
        self.memory_capture.queue_graph(text)

    async def _refresh_graph(self, query: str):
        await self.context_builder.refresh_graph(query)

    async def _converse(self, text: str, about_self: bool = False, use_memory: bool = True) -> bool:
        return await self.reply_generator.converse(text, about_self, use_memory)

    def _store_memories(self, data) -> list[str]:
        return self.memory_capture.store_memories(data)

    async def remember_text(self, text: str, hint_kind: str | None = None) -> list[str]:
        return await self.memory_capture.remember_text(text, hint_kind)

    async def _learn(self, text: str):
        await self.memory_capture.learn(text)

    async def _consolidate(self):
        await self.memory_capture.consolidate()

    def _set_quiet(self, minutes):
        self.memory_capture.set_quiet(minutes)

    async def run_claude(self, task: str) -> str | None:
        """Сложная задача на Claude (модуль desktop, инструмент claude_task, ТЗ 14 DESK-11): запускается задачей и ждёт её итога.
        Контекст разговора без услышанного подставляет ядро (core_fills). Возвращает итог, "" если добавить нечего,
        None при сбое или если модуль desktop не подключён."""
        if claude_quota.blocked():
            return f"Claude сейчас недоступен: исчерпан лимит до {claude_quota.until_text()}."
        if self.router is None or self.job_wait is None:
            return None
        ok, text, _code = await self.router.run_tool_ex("desktop.claude_task", {"task": task})
        from module_registry import ModuleRegistry
        job_id = ModuleRegistry.job_id_from(text) if ok else None
        if job_id is None:
            return None
        job = await self.job_wait("desktop", job_id, 600)
        if job.get("state") == "done":
            return str(job.get("text") or "")
        return None

    async def _escalate(self, task: str):
        """Запасной путь, когда локальная модель недоступна."""
        result = await self.run_claude(task)
        if result:
            await self.say(result)
        elif result is None:
            await self._speak_about(f"Не удалось выполнить просьбу «{task}»: сбой агента-помощника.", task)
        else:
            await self._speak_about(f"Просьба «{task}» выполнена, добавить нечего.", task)

    # ── проактивность (временный сценарий app/proactive_legacy.py, до ТЗ 30) ──────

    async def proactive_loop(self):
        await self.proactive_legacy.loop()

    def _digest(self, silent_mins: int) -> str:
        return self.proactive_legacy.digest(silent_mins)

    def _repeats_recent(self, text: str) -> bool:
        return self.proactive_legacy.repeats_recent(text)

    async def _maybe_speak(self):
        await self.proactive_legacy.maybe_speak()
