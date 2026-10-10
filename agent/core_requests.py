"""Запросы модулей к ядру (ТЗ 02, раздел 10): `core.ask`, `core.tts`, `core.tool.call`, `core.persona`,
`core.claude_usage`, `core.web_search`. Запросы памяти и справочника (`core.lookup`, `core.memory.*`,
`core.experience.add`, `core.kb.ingest`) отвечают `unavailable` до ТЗ 04, `core.privacy_terms` - `unknown` до ТЗ 10.

Тело каждого запроса проверяется по схеме (CORE-32): обязательные поля и типы; ошибка - `bad_request`. Ответ ядра
всегда значение для `result.value` или `ModuleError(code, message)`; сообщения по-русски, без секретов.
"""
import asyncio
import logging
import secrets
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable

from glados_module import protocol as P

from module_hub import ModuleConnection, ModuleError

logger = logging.getLogger("core_requests")

TTS_DIR_NAME = "glados_tts"
TTS_KEEP_S = 3600
TTS_TEXT_MAX = 5000
NAME = "ГЛаДОС"

_TO_TZ04 = "появится в ТЗ 04"


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
    def __init__(self, *, attention=None, router=None, tts_dir: Path | None = None,
                 synth: Callable[[str], str | None] | None = None):
        """attention - менеджер внимания (submit_question); router - Router (run_tool_ex); synth - синтез речи в файл
        (по умолчанию tts_engine.synth_file); tts_dir - куда класть результаты core.tts."""
        self.attention = attention
        self.router = router                      # ставится ядром после создания маршрутизатора
        self._tts_dir = tts_dir
        self._synth = synth

    async def handle(self, conn: ModuleConnection, msg_type: str, body: Any) -> Any:
        handler = {
            "core.ask": self._ask, "core.tts": self._tts, "core.tool.call": self._tool_call,
            "core.persona": self._persona, "core.claude_usage": self._claude_usage, "core.web_search": self._web_search,
        }.get(msg_type)
        if handler is not None:
            return await handler(conn, body)
        if msg_type in ("core.lookup", "core.memory.recall", "core.memory.remember", "core.experience.add",
                        "core.kb.ingest"):
            raise ModuleError("unavailable", f"запрос «{msg_type}» {_TO_TZ04}")
        raise ModuleError("unknown", f"ядро не знает запрос «{msg_type}»")      # core.privacy_terms: ТЗ 10

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

    def _out_dir(self) -> Path:
        return self._tts_dir or Path(tempfile.gettempdir()) / TTS_DIR_NAME

    @staticmethod
    def _wav_ms(path: Path) -> int:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / float(w.getframerate()) * 1000)

    def _sweep(self, directory: Path) -> None:
        """Удалить файлы core.tts старше часа: модуль должен был забрать их давно."""
        cutoff = time.time() - TTS_KEEP_S
        for f in directory.glob("tts_*"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    async def _tts(self, conn: ModuleConnection, body: Any) -> dict:
        body = _check_fields(body, {
            "text": (lambda v: _nonempty_str(v) and len(v) <= TTS_TEXT_MAX, True, f"непустая строка до {TTS_TEXT_MAX} символов"),
            "emotion": (lambda v: isinstance(v, str), False, "строка"),
            "format": (lambda v: v in ("wav", "ogg"), False, "wav или ogg"),
        })
        fmt = body.get("format", "wav")
        synth = self._synth
        if synth is None:
            import tts_engine
            synth = tts_engine.synth_file
        src = await asyncio.to_thread(synth, body["text"])           # блокирующий синтез (Fish Speech, запасной edge-tts)
        if not src:
            raise ModuleError("unavailable", "синтез речи не удался")
        out = self._out_dir()
        await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(self._sweep, out)
        name = "tts_" + secrets.token_hex(6)
        wav = out / (name + ".wav")
        try:
            if str(src).lower().endswith(".wav"):
                await asyncio.to_thread(Path(src).replace, wav)
                src = None
            else:
                import opus_encoder
                await opus_encoder.audio_file_to_wav_file(str(src), str(wav))
            duration = await asyncio.to_thread(self._wav_ms, wav)
            if fmt == "wav":
                return {"path": str(wav), "duration_ms": duration}
            import opus_encoder
            data = await opus_encoder.audio_file_to_ogg_opus(str(wav))
            ogg = out / (name + ".ogg")
            await asyncio.to_thread(ogg.write_bytes, data)
            await asyncio.to_thread(wav.unlink)
            return {"path": str(ogg), "duration_ms": duration}
        except ModuleError:
            raise
        except Exception as e:
            raise ModuleError("internal", f"не удалось подготовить звук: {e}") from e
        finally:
            if src:
                await asyncio.to_thread(self._unlink, Path(src))

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

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
        import prompts
        return {"name": NAME, "card": await asyncio.to_thread(prompts.persona_card, 2000)}

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
