"""Запросы модулей к ядру (ТЗ 02, раздел 10): `core.ask`, `core.tts`, `core.tool.call`, `core.persona`,
`core.claude_usage`, `core.web_search`, `core.privacy_terms` (ТЗ 10). Запросы памяти и справочника (`core.lookup`,
`core.memory.*`, `core.experience.add`, `core.kb.ingest`) отвечают `unavailable` до ТЗ 04.

Тело каждого запроса проверяется по схеме (CORE-32): обязательные поля и типы; ошибка - `bad_request`. Ответ ядра
всегда значение для `result.value` или `ModuleError(code, message)`; сообщения по-русски, без секретов.
"""
import asyncio
import logging
import re
from typing import Any, Callable

from gits_sdk import protocol as P

from module_hub import ModuleConnection, ModuleError

logger = logging.getLogger("core_requests")

TTS_TIMEOUT_S = 80            # синтез у модуля голоса: Fish Speech до 30 с на фразу плюс перекодирование
TTS_TEXT_MAX = 5000

_TO_TZ04 = "появится в ТЗ 04"
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


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_fields(body: Any, fields: dict[str, tuple]) -> dict:
    """fields: имя -> (проверка типа, обязательно, пояснение). Возвращает тело; иначе bad_request со всеми проблемами."""
    if not isinstance(body, dict):
        raise _bad("тело запроса должно быть объектом")
    problems = []
    for name, (check, required, hint) in fields.items():
        if name not in body or body[name] is None:
            if required:
                problems.append(f"{name}: обязательное поле ({hint})")
        elif not check(body[name]):
            problems.append(f"{name}: {hint}")
    if problems:
        raise _bad("; ".join(problems))
    return body


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


class CoreRequests:
    def __init__(self, *, attention=None, router=None, privacy_terms: Callable[[], list[str]] | None = None):
        """attention - менеджер внимания (submit_question); router - Router (run_tool_ex); privacy_terms - () -> личные слова
        (core.privacy_terms; ядро ставит после создания памяти). Шина модулей (hub) нужна для core.tts: ставит ModuleHub."""
        self.attention = attention
        self.router = router                      # ставится ядром после создания маршрутизатора
        self.privacy_terms = privacy_terms
        self.hub = None                           # ModuleHub: core.tts передаёт запрос модулю голоса

    async def handle(self, conn: ModuleConnection, msg_type: str, body: Any) -> Any:
        handler = {
            "core.ask": self._ask, "core.tts": self._tts, "core.tool.call": self._tool_call,
            "core.persona": self._persona, "core.claude_usage": self._claude_usage, "core.web_search": self._web_search,
            "core.privacy_terms": self._privacy_terms,
        }.get(msg_type)
        if handler is not None:
            return await handler(conn, body)
        if msg_type in ("core.lookup", "core.memory.recall", "core.memory.remember", "core.experience.add",
                        "core.kb.ingest"):
            raise ModuleError("unavailable", f"запрос «{msg_type}» {_TO_TZ04}")
        raise ModuleError("unknown", f"ядро не знает запрос «{msg_type}»")

    # ── core.ask ────────────────────────────────────────────────────────────

    async def _ask(self, conn: ModuleConnection, body: Any) -> dict:
        problems = P.validate_ask(body)
        if problems:
            raise _bad("; ".join(problems))
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
        body = _check_fields(body, {
            "text": (lambda v: _nonempty_str(v) and len(v) <= TTS_TEXT_MAX, True, f"непустая строка до {TTS_TEXT_MAX} символов"),
            "emotion": (lambda v: isinstance(v, str), False, "строка"),
            "format": (lambda v: v in ("wav", "ogg"), False, "wav или ogg"),
        })
        voice = self._voice_connection()
        if voice is None:
            raise ModuleError("unavailable", "модуль голоса не подключён или выключен")
        value = await voice.request("tts.synthesize", {"text": body["text"], "emotion": body.get("emotion"),
                                                       "format": body.get("format", "wav")}, timeout=TTS_TIMEOUT_S)
        if not (isinstance(value, dict) and isinstance(value.get("path"), str) and _is_int(value.get("duration_ms"))):
            raise ModuleError("internal", "модуль голоса вернул неверный ответ на tts.synthesize")
        return {"path": value["path"], "duration_ms": value["duration_ms"]}
    # ── core.tool.call ───────────────────────────────────────────────────────

    async def _tool_call(self, conn: ModuleConnection, body: Any) -> dict:
        body = _check_fields(body, {
            "tool": (_nonempty_str, True, "id инструмента вида модуль.имя"),
            "args": (lambda v: isinstance(v, dict), False, "объект"),
        })
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
        body = _check_fields(body, {
            "tokens": (lambda v: _is_int(v) and v >= 0, True, "целое число токенов не меньше нуля"),
            "source": (lambda v: isinstance(v, str), False, "строка"),
            "limit_text": (lambda v: isinstance(v, str), False, "строка с текстом исчерпанного лимита"),
        })
        import claude_battery
        import claude_quota
        if body.get("limit_text"):
            claude_quota.note(body["limit_text"])                # лимит исчерпан: батарейка обнуляется сразу
        await asyncio.to_thread(claude_battery.record, body["tokens"], str(body.get("source") or conn.module_id))
        return {}

    async def _web_search(self, conn: ModuleConnection, body: Any) -> dict:
        body = _check_fields(body, {"query": (_nonempty_str, True, "непустая строка")})
        import web_search
        return {"text": await web_search.search(body["query"])}
