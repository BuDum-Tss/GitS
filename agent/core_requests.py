"""Запросы модулей к ядру (ТЗ 02, раздел 10): `core.ask`, `core.tts`, `core.tool.call`, `core.persona`,
`core.claude_usage`, `core.web_search`, `core.privacy_terms` (ТЗ 10), `core.memory.recall` / `core.memory.remember` (поверх
контрактов `MemoryReader` и `MemoryWriter`). Справочник (`core.lookup`, `core.experience.add`, `core.kb.ingest`) отвечает
`unavailable` до ТЗ 04.

Тело каждого запроса проверяется по контракту `gits_sdk.contract` (CORE-32, ТЗ 31 MC-2): обязательные поля и типы; ошибка -
`bad_request`. Запросы со статусом planned (память и справочник) отвечают `unavailable` с версией контракта. Ответ ядра
всегда значение для `result.value` или `ModuleError(code, message)`; сообщения по-русски, без секретов.
"""
import asyncio
import logging
import re
from typing import Any, Callable

from gits_sdk import contract
from gits_sdk import protocol as P

from module_hub import ModuleConnection, ModuleError

logger = logging.getLogger("core_requests")

TTS_TIMEOUT_S = 80            # синтез у модуля голоса: Fish Speech до 30 с на фразу плюс перекодирование
TTS_TEXT_MAX = contract.messages.TTS_TEXT_MAX

_NAME_FACT = re.compile(r"\s*(?:имя(?: пользователя)?|зовут)\s*[:\-]\s*(.+)", re.I)
_NAME_WORD = re.compile(r"[А-Яа-яЁёA-Za-z]{3,}")


def collect_privacy_terms(owner_name: str, people: list[str], facts: list[str]) -> list[str]:
    """Личные слова для фильтров публикации модулей (TG-12): имя владельца, имена и ники людей из relationships и имена из
    фактов о владельце вида «Имя: Аня» (то, что прежде проверял `leaks`). Без повторов, порядок сохраняется."""
    terms: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = (value or "").strip()
        if len(value) >= 3 and value.lower() not in seen:
            seen.add(value.lower())
            terms.append(value)

    add(owner_name)
    for person in people:
        add(person)
    for fact in facts:
        m = _NAME_FACT.match(fact or "")
        if m:
            for word in _NAME_WORD.findall(m.group(1)):
                add(word)
    return terms


def _bad(message: str) -> ModuleError:
    return ModuleError("bad_request", message)


class CoreRequests:
    def __init__(self, *, attention=None, router=None, privacy_terms: Callable[[], list[str]] | None = None,
                 memory_reader=None, memory_writer=None):
        """attention - менеджер внимания (submit_question); router - Router (run_tool_ex); privacy_terms - () -> личные слова
        (core.privacy_terms; ядро ставит после создания памяти). Шина модулей (hub) нужна для core.tts: ставит ModuleHub."""
        self.attention = attention
        self.router = router                      # ставится ядром после создания маршрутизатора
        self.privacy_terms = privacy_terms
        self.memory_reader = memory_reader        # MemoryReader / MemoryWriter ядра (core.memory.*); None: unavailable
        self.memory_writer = memory_writer
        self.hub = None                           # ModuleHub: core.tts передаёт запрос модулю голоса

    async def handle(self, conn: ModuleConnection, msg_type: str, body: Any) -> Any:
        handler = {
            "core.ask": self._ask, "core.tts": self._tts, "core.tool.call": self._tool_call,
            "core.persona": self._persona, "core.claude_usage": self._claude_usage, "core.web_search": self._web_search,
            "core.privacy_terms": self._privacy_terms, "core.llm.ask": self._llm_ask,
            "core.events.tail": self._events_tail, "core.claude.locate": self._claude_locate,
            "core.memory.recall": self._memory_recall, "core.memory.remember": self._memory_remember,
        }.get(msg_type)
        if contract.is_planned(msg_type):                      # MC-5: запрос описан, реализации пока нет
            raise ModuleError("unavailable", contract.planned_message(msg_type))
        if handler is None:
            raise ModuleError("unknown", f"ядро не знает запрос «{msg_type}»")
        problems = contract.validate(msg_type, body)           # MC-2: одна проверка контракта вместо вшитых схем
        if problems:
            raise _bad("; ".join(problems))
        return await handler(conn, body)

    # ── core.ask ────────────────────────────────────────────────────────────

    async def _ask(self, conn: ModuleConnection, body: Any) -> dict:
        if self.attention is None:
            raise ModuleError("unavailable", "менеджер внимания не подключён")
        from attention import Question
        q = Question(kind=body["kind"], question=body["question"], options=body.get("options"),
                     timeout_s=body.get("timeout_s", P.ASK_TIMEOUT_DEFAULT), source=conn.module_id,
                     context=body.get("context") or {}, urgency_hint=body.get("urgency_hint"))
        answer, by = await self.attention.submit_question(q)
        return {"answer": answer, "by": by}

    # ── core.tts ────────────────────────────────────────────────────────────

    def _voice_connection(self):
        """Подключённый включённый модуль с voice.output (VOICE-16) или None."""
        hub = self.hub
        if hub is None:
            return None
        for conn in hub.connections():
            if "voice.output" in (conn.manifest or {}).get("capabilities", []) and hub.modules.is_enabled(conn.module_id):
                return conn
        return None

    async def _tts(self, conn: ModuleConnection, body: Any) -> dict:
        """VOICE-16: синтез её голосом делает модуль голоса (запрос tts.synthesize); ядро только передаёт запрос и ответ
        {path, duration_ms}. Нет модуля голоса: unavailable."""
        voice = self._voice_connection()
        if voice is None:
            raise ModuleError("unavailable", "модуль голоса не подключён или выключен")
        value = await voice.request("tts.synthesize", {"text": body["text"], "emotion": body.get("emotion"),
                                                       "format": body.get("format", "wav")}, timeout=TTS_TIMEOUT_S)
        if contract.validate_response("tts.synthesize", value):
            raise ModuleError("internal", "модуль голоса вернул неверный ответ на tts.synthesize")
        return {"path": value["path"], "duration_ms": value["duration_ms"]}
    # ── core.tool.call ───────────────────────────────────────────────────────

    async def _tool_call(self, conn: ModuleConnection, body: Any) -> dict:
        if self.router is None:
            raise ModuleError("unavailable", "маршрутизатор не подключён")
        tool = self.router.registry.get(body["tool"])
        if tool is None:
            raise ModuleError("unknown", f"инструмента «{body['tool']}» нет")
        env = self.router.registry.envs.get(tool.env)
        if env is not None:
            ok, why = env.state()
            if not ok:
                raise ModuleError("unavailable", f"инструмент недоступен: {why}")
        ok, text, code = await self.router.run_tool_ex(body["tool"], body.get("args") or {})
        if not ok:
            raise ModuleError(code or "internal", text)
        return {"text": text}

    # ── core.memory.* ───────────────────────────────────────────────────────

    async def _memory_recall(self, conn: ModuleConnection, body: Any) -> dict:
        """Поиск по личной памяти (MemoryReader.search): только реально совпавшие записи."""
        reader = self.memory_reader
        if reader is None:
            raise ModuleError("unavailable", "память ядра не подключена")
        items = await asyncio.to_thread(reader.search, body["query"], body.get("limit") or 6)
        return {"facts": [{"text": str(i.get("text", "")), "kind": i.get("kind", "user"),
                           "importance": int(i.get("importance", 3))} for i in items]}

    async def _memory_remember(self, conn: ModuleConnection, body: Any) -> dict:
        """Запись факта в личную память (MemoryWriter.add_fact); дубль сливается и даёт stored=false."""
        writer = self.memory_writer
        if writer is None:
            raise ModuleError("unavailable", "память ядра не подключена")
        stored = await asyncio.to_thread(writer.add_fact, body["text"].strip(), "user", 3)
        return {"stored": bool(stored)}

    # ── прочее ──────────────────────────────────────────────────────────────

    async def _persona(self, conn: ModuleConnection, body: Any) -> dict:
        """ТЗ 05, CHR-6: карточка активного персонажа (имя, алиасы, голос), карточка характера и разделы persona.md."""
        import character
        import prompts
        ch = character.active()
        card = await asyncio.to_thread(prompts.persona_card, 2000)
        sections = await asyncio.to_thread(character.sections, ch.id)
        return {**ch.card(), "name": ch.name, "card": card, "sections": sections}

    async def _privacy_terms(self, conn: ModuleConnection, body: Any) -> dict:
        """TG-12: личные слова (имя владельца, имена знакомых, имена из фактов о владельце) для фильтров публикации."""
        if self.privacy_terms is None:
            raise ModuleError("unavailable", "список личных слов ещё не готов")
        try:
            return {"terms": await asyncio.to_thread(self.privacy_terms)}
        except Exception as e:
            logger.warning(f"core.privacy_terms: {e}")
            raise ModuleError("internal", "не удалось собрать список личных слов") from e

    async def _claude_usage(self, conn: ModuleConnection, body: Any) -> dict:
        import claude_battery
        import claude_quota
        if body.get("limit_text"):
            claude_quota.note(body["limit_text"])                # лимит исчерпан: батарейка обнуляется сразу
        await asyncio.to_thread(claude_battery.record, body["tokens"], str(body.get("source") or conn.module_id))
        return {}

    async def _web_search(self, conn: ModuleConnection, body: Any) -> dict:
        import web_search
        return {"text": await web_search.search(body["query"])}

    # ── ТЗ 31: локальная модель, журнал, путь к Claude ─────────────────────────

    async def _llm_ask(self, conn: ModuleConnection, body: dict) -> dict:
        """Один вызов локальной модели ядра (`fast_brain._ask`). Срочное вытесняет фоновое (`background`)."""
        import fast_brain
        try:
            text = await fast_brain._ask(
                body["system"], body["user"], timeout=float(body.get("timeout_s", 60)),
                max_tokens=int(body.get("max_tokens", 40)), image_b64=body.get("image_b64"),
                temperature=float(body.get("temperature", 0.3)), background=bool(body.get("background", False)))
        except fast_brain.Preempted as e:
            raise ModuleError("unavailable", "локальная модель занята срочными вызовами") from e
        except asyncio.TimeoutError as e:
            raise ModuleError("timeout", "локальная модель не ответила вовремя") from e
        except Exception as e:
            logger.warning(f"core.llm.ask от {conn.module_id}: {type(e).__name__}: {e}")
            raise ModuleError("unavailable", "локальная модель недоступна") from e
        out: dict = {"text": text}
        if body.get("json"):
            data = fast_brain._json_of(text)
            if isinstance(data, dict):
                out["data"] = data
        return out

    async def _events_tail(self, conn: ModuleConnection, body: dict) -> dict:
        import events_log
        kinds = body.get("kinds")
        if kinds:
            bad = [k for k in kinds if k not in contract.LOG_READ_KINDS]
            if bad:
                raise _bad(f"kinds: модулю доступны только {list(contract.LOG_READ_KINDS)}")
        else:
            kinds = list(contract.LOG_READ_KINDS)
        rows = await asyncio.to_thread(events_log.tail, int(body.get("limit", 20)), set(kinds), float(body.get("since", 0)))
        return {"events": rows}

    async def _claude_locate(self, conn: ModuleConnection, body: Any) -> dict:
        from gits_sdk import claude_path
        path = await asyncio.to_thread(claude_path.locate)
        return {"path": path} if path else {}
