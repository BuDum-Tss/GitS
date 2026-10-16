"""ConversationService: путь одного обращения владельца (ТЗ 29, ARC-5): вход, проверки (поправка, ответ на вопрос о
разрешении, приказ в игру), выбор ветки (болтовня / просьба / «что умеешь»), журнал маршрута.
Ответ формулирует ReplyGenerator, память пишет MemoryCapture, остальное зовётся через фасад `Companion`."""
import asyncio
import contextvars
import re
import time
import logging

import fast_brain
import journal
import latency_trace
import salience
from conversation_io import DASHBOARD, VOICE, UserInput, current_channel

from app.memory_capture import SPEAKER_TAG

logger = logging.getLogger("companion")

# отправитель текущего обращения (узнанный голос: новый человек даёт яркость, ТЗ 24 TRC-4)
_sender_ctx: contextvars.ContextVar = contextvars.ContextVar("companion_sender", default=None)

_EXPLICIT_MEMORY = re.compile(r"\b(запомни\w*|не забудь\w*|запиши)\b", re.IGNORECASE)


class ConversationService:
    def __init__(self, host):
        self._h = host

    async def on_voice(self, text: str, speaker: dict, trace=None):
        """Реплика, сказанная голосом: узнанный владелец идёт как обычное сообщение, остальным ставится пометка,
        кто говорит (имя или «незнакомый голос»), и заводится профиль человека."""
        h = self._h
        pid, name = speaker.get("id"), speaker.get("name")
        if speaker.get("owner") or not speaker.get("has_owner"):
            prefix = ""
        elif name:
            prefix = f"[говорит {name}] "
        else:
            prefix = "[незнакомый голос] "
        if pid and not speaker.get("owner") and speaker.get("has_owner"):
            h._people.ensure_person(f"voice:{pid}", name or f"голос {str(pid)[-4:]}", source="voice")
            h._people.note(f"voice:{pid}", text)
        await h.on_input(UserInput(prefix + text, VOICE, sender=speaker, ts=time.time(), trace=trace))

    async def on_user_message(self, text: str, channel=None):
        """Обращение текстом (чат дашборда, строка питомца); channel по умолчанию - дашборд."""
        await self._h.on_input(UserInput(text, channel or DASHBOARD))

    async def on_input(self, ui: UserInput):
        """Единый вход обращений (CORE-24): ответ на него уходит в канал обращения (current_channel)."""
        h = self._h
        token = current_channel.set(ui.channel)
        t = ui.trace if ui.trace is not None else latency_trace.start(ui.channel.kind)   # трасса задержки (ТЗ 24, TRC-1)
        trace_token = latency_trace.bind(t)
        if h.attention is None:
            h.presence.update(busy=True)         # с менеджером внимания занятость определяет стек фокуса (ATT-23)
        try:
            sender_token = _sender_ctx.set(ui.sender)
            try:
                await h._handle_text(ui.text)
            finally:
                _sender_ctx.reset(sender_token)
        finally:
            latency_trace.unbind(trace_token)
            latency_trace.finish_core(t)
            current_channel.reset(token)
            if h.attention is None:
                h.presence.update(busy=False)

    async def handle_text(self, text: str):
        h = self._h
        text = text.strip()
        if not text:
            return
        h._memory_writer.add_dialogue("user", text)
        # яркость реплики (ТЗ 24, TRC-4): явное «запомни» и новый человек (отправитель - из on_input)
        sal, reasons = salience.score(salience.factors_of(memorable=bool(_EXPLICIT_MEMORY.search(text)),
                                                          novelty=bool((_sender_ctx.get() or {}).get("new"))))
        h._log.log_nowait("user", text, salience=sal, reasons=reasons)   # без ожидания коммита (ТЗ 24: трасса)
        h._queue_graph(text)
        h._pending_memory += 1
        h._last_user_ts = time.time()
        h.presence.update(last_user_ts=h._last_user_ts)
        fast_brain.publish("chat", {"emotion": "thinking", "action": "none"})   # сразу «задумалась», без вызова модели

        cls = None
        if h.attention is not None:
            await h.attention.on_user_input(text)          # фокус «разговор»; «замолчи» обрывает речь до любых вызовов модели
            cls = await h.attention.classify_input(text)   # намерение (ATT-28); None - классификатора нет, всё как раньше
        latency_trace.mark("classify_done")
        h._check_correction(text)                           # поправка пользователя (ТЗ 24, TRC-5)
        if h._approver.has_pending() and await h._answer_approval(text):
            return
        if h.game_order is not None:
            try:
                taken = await h.game_order(text)
            except Exception as e:
                h._log.log("error", f"приказ в игру не принят: {e}")
                taken = False
            if taken:
                asyncio.create_task(h._speak_about(
                    "Игрок только что дал тебе срочное указание по игре, ты прямо сейчас берёшься выполнять его "
                    "(действия в игре идут отдельно). Скажи одной короткой фразой в своём характере, что принимаешь.",
                    text, must=True))
                asyncio.create_task(fast_brain.react_chat(text, h._people))
                return
        intent = cls.intent if cls is not None else None
        # needs_memory классификатора пока смещён в «нет» (замеры ТЗ 03): по нему память не отключаем, только пишем в журнал.
        # Поиск в графе пропускается лишь там, где память заведомо не нужна; вернуть needs_memory после дообучения.
        use_memory = intent not in ("stop", "approval")
        route, routed = "chat", False          # routed: вызывался ли планировщик (ТЗ 24, TRC-6)
        async with h._busy:
            if intent == "about_self":
                route = "about_self"
                latency_trace.mark("route")
                await h._converse(text, about_self=True, use_memory=use_memory)   # что она умеет: ответ по перечню окружений
            elif h.router is not None and (h.router.looks_like_action(text) or intent in ("action", "attention_rule")):
                route, routed = "task", True
                latency_trace.mark("route")
                await h._act(text)          # просьба: планировщик, ответ после плана
            else:
                latency_trace.mark("route")
                flagged = await h._converse(text, use_memory=use_memory)   # болтовня: один вызов, ответ сразу
                if flagged and h.router is not None:
                    route, routed = "task", True
                    await h._act(text, after_chat=True)   # модель заподозрила просьбу, которую не распознали глаголы
        h._log_route(text, route, routed, intent, cls)
        h.arm_idle_consolidation()
        asyncio.create_task(fast_brain.react_chat(text, h._people))   # эмоция и отношение: уже после ответа
        if h._pending_memory >= 6:
            asyncio.create_task(h._consolidate())                  # осмыслить беседу и запомнить важное

    def check_correction(self, text: str) -> None:
        """Поправка «нет, не так» (ТЗ 24, TRC-5): по шаблону сразу; неясное начало проверяет модель в фоне."""
        h = self._h
        said = SPEAKER_TAG.sub("", text)
        level = journal.correction_level(said)
        if level == "no":
            return
        ref, prev = h._last_say, h._last_say_text[:200]
        if level == "yes":
            journal.mistake("correction", said[:200], refs=[ref], previous=prev)
        else:
            task = asyncio.create_task(h._confirm_correction(said, ref, prev))
            h._bg_tasks.add(task)                 # сильная ссылка: иначе задачу может собрать сборщик мусора
            task.add_done_callback(h._bg_tasks.discard)

    async def confirm_correction(self, text: str, ref: float, prev: str) -> None:
        system = ("Пользователь ответил на твою прошлую реплику. Определи, поправляет ли он тебя: говорит ли, что ты "
                  'ошиблась или сказала неверно. Ответь ТОЛЬКО JSON: {"correction": <true или false>}')
        try:
            data = fast_brain._json_of(await fast_brain._ask(system, f"Твоя реплика: {prev}\nОтвет: {text[:300]}",
                                                             timeout=20, max_tokens=20, temperature=0.0, background=True))
        except Exception as e:
            logger.info(f"Проверка поправки не выполнена: {e}")
            return
        if data and data.get("correction") is True:
            journal.mistake("correction", text[:200], refs=[ref], previous=prev, checked="llm")

    def log_route(self, text: str, route: str, routed: bool, intent, cls) -> None:
        """Решение маршрута разговора (ТЗ 24, TRC-6) и несовпадение классификатора с путём (TRC-5: misroute)."""
        h = self._h
        conf = cls.intent_conf if cls is not None else None
        journal.decision(route, "conversation", text, confidence=conf, echelon="gliner" if cls is not None else "rules",
                         intent=intent)
        if intent == "chat" and routed and h.router is not None and h.router.last_kind == "done":
            journal.mistake("misroute", text[:200], refs=[h._last_say], source="classifier", intent=intent)
        elif intent == "action" and not routed and h.router is not None:
            journal.mistake("misroute", text[:200], refs=[h._last_say], source="classifier", intent=intent)

    async def act(self, text: str, after_chat: bool = False):
        """Просьба действовать: маршрутизатор решает «могу или нет», выполняет, а итог говорится вслух."""
        h = self._h
        outcome = await h.router.handle(text, announce=not after_chat)
        if after_chat and h.router.last_kind == "cannot":
            return   # на ответ-болтовню уже сказано всё, что можно; не повторяем «не могу» вторым сообщением
        if outcome:
            # итог действия или честное «не могу»: пользователь должен узнать результат
            await h._speak_about(outcome, text, must=True)
        elif outcome is None and not after_chat:
            await h._converse(text)    # планировщик решил, что это всё-таки разговор
