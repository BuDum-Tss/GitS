"""ConversationService: путь одного обращения владельца (ТЗ 29, ARC-5): вход, жёсткие правила, запись реплики, ответ на
вопрос о разрешении, затем реплика уходит единому агенту (`services/conversation/resident.py`). Память пишет MemoryCapture, остальное зовётся
через фасад `Companion`."""
from typing import TYPE_CHECKING
import asyncio
import contextvars
import re
import time
import logging

from services.tracing import journal
from services.tools import routes
from services.tracing import latency_trace
from services.tracing import salience
from services.conversation.conversation_io import DASHBOARD, VOICE, UserInput, current_channel

from services.memory_service import SPEAKER_TAG

if TYPE_CHECKING:
    from services.conversation.companion import Companion

logger = logging.getLogger("companion")

# отправитель текущего обращения (узнанный голос: новый человек даёт яркость, ТЗ 24 TRC-4)
_sender_ctx: contextvars.ContextVar = contextvars.ContextVar("companion_sender", default=None)

WRITE_WAIT_S = 0.05      # сколько ответ ждёт запись реплики в диалог (ТЗ 25, FAST-7); дальше запись доходит в фоне
_EXPLICIT_MEMORY = re.compile(r"\b(запомни\w*|не забудь\w*|запиши)\b", re.IGNORECASE)


class ConversationService:
    def __init__(self, host):
        self._h: Companion = host

    async def on_voice(self, text: str, speaker: dict, trace=None):
        """Реплика, сказанная голосом: говорящих различить нельзя (нет голосовых отпечатков), поэтому это реплика владельца
        и идёт как обычное сообщение канала `voice`; `speaker` едет отправителем обращения."""
        await self._h.on_input(UserInput(text, VOICE, sender=speaker, ts=time.time(), trace=trace))

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
        """Единый вход обращения (ТЗ 25, FAST-1): жёсткие правила до моделей -> параллельно классификатор, приказ в игру, поиск
        в памяти и запись реплики -> маршруты (`services/tools/routes.py`) -> ответ потоком, поручение или то и другое."""
        h = self._h
        text = text.strip()
        if not text:
            return
        hard = routes.hard_rules().match(text, pending_question=h._approver.has_pending())
        if hard is not None and hard.kind in ("stop", "emergency"):
            await self._reflex(text, hard)
        # запись реплики (диалог, очередь графа) уходит в поток и идёт параллельно разбору; ответ её дожидается (FAST-7)
        sal, reasons = salience.score(salience.factors_of(memorable=bool(_EXPLICIT_MEMORY.search(text)),
                                                          novelty=bool((_sender_ctx.get() or {}).get("new"))))
        writes = asyncio.ensure_future(asyncio.to_thread(self._write_user, text))
        try:
            await self._handle(text, hard, writes, sal, reasons)
        finally:
            await asyncio.gather(writes, return_exceptions=True)

    def _write_user(self, text: str) -> None:
        h = self._h
        h._memory_writer.add_dialogue("user", text)

    async def _reflex(self, text: str, hard) -> None:
        """Рефлекс жёсткого правила (FAST-2, FAST-5): оборвать генерацию ответа и речь сразу, без вызовов моделей."""
        h = self._h
        h.cancel_reply(hard.kind)
        if h.attention is not None:
            if not h.attention.looks_like_stop(text):          # «замолчи» остановит on_user_input; остальное - здесь
                await h.attention.stop_speech(f"hard_rule:{hard.kind}")
        else:
            await h.speech.stop(None)

    async def _handle(self, text: str, hard, writes, sal, reasons) -> None:
        h = self._h
        h._log.log_nowait("user", text, salience=sal, reasons=reasons)   # без ожидания коммита (ТЗ 24: трасса)
        h._pending_memory += 1
        h._last_user_ts = time.time()
        h.presence.update(last_user_ts=h._last_user_ts)
        h.presence.update(sticker="thinking", sticker_ts=time.time())   # сразу «задумалась», без вызова модели

        if h.attention is not None:
            await h.attention.on_user_input(text)          # фокус «разговор»; «замолчи» обрывает речь до любых вызовов модели
        h._check_correction(text)                           # поправка пользователя (ТЗ 24, TRC-5)
        if h._approver.has_pending() and await h._answer_approval(text):
            await writes
            self.log_decision(text, routes.of_input(intent=None, looks_action=False, hard=hard or routes.HardMatch(
                "approval", "pending_question", "владелец ответил на открытый вопрос о разрешении")))
            return
        await asyncio.wait({writes}, timeout=WRITE_WAIT_S)   # обычно запись уже готова и агент видит реплику в диалоге; медленный диск ответ не держит
        async with h._busy:
            await h.resident.handle_user(text, use_memory=hard is None)
        self.log_route(text, "resident")
        h.arm_idle_consolidation()

    def check_correction(self, text: str) -> None:
        """Поправка «нет, не так» (ТЗ 24, TRC-5): по шаблону, без модели; неясное начало единый агент слышит сам."""
        h = self._h
        said = SPEAKER_TAG.sub("", text)
        if journal.correction_level(said) == "yes":
            journal.mistake("correction", said[:200], refs=[h._last_say], previous=h._last_say_text[:200])

    def log_decision(self, text: str, decision) -> None:
        """Решение без разговора (ответ на вопрос о разрешении): маршруты и правило в журнал решений."""
        journal.decision(decision.routes[-1] if decision.routes else routes.IGNORE, "conversation", text,
                         echelon=decision.echelon, routes=decision.routes, already_done=decision.already_done,
                         hard_rule=decision.rule)

    def log_route(self, text: str, route: str) -> None:
        """Маршрут разговора в журнал решений (ТЗ 24, TRC-6)."""
        journal.decision(route, "conversation", text, echelon="rules")
