"""AgentRuntime поверх Claude Agent SDK: одна постоянная сессия (`ClaudeSDKClient`), инструменты ядра как SDK MCP-сервер `core`.

Весь код Claude SDK единого агента живёт здесь (раньше в `app/resident.py`): сессия продолжается после перезапуска ядра
(`resident_session.json`), после `MAX_QUERIES` обращений начинается заново, `abort` это `interrupt`, лимит Claude
запоминает `claude_quota`, расход уходит в `usage_report`. Подмешивать сообщение в идущий ход SDK не умеет (`can_steer = False`).
"""
import json
import logging
from collections.abc import AsyncIterator

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, create_sdk_mcp_server,
                              tool)

import claude_quota
from claude_cli import _CLI_PATH
from contracts import EndEvent, RuntimeEvent, TextEvent, ToolEvent, ToolSpec

logger = logging.getLogger("resident")

MAX_QUERIES = 60            # после стольких обращений сессия начинается заново (контекст не растёт вечно)
AUTH_FAILED = "Failed to authenticate"   # вход Claude CLI истёк: CLI отдаёт это текстом ответа

_PY_TYPES = {"string": str, "object": dict, "array": list, "integer": int, "number": float, "boolean": bool}


def _sdk_schema(parameters: dict) -> dict:
    """JSON Schema параметров -> простая схема SDK `{имя: тип}`."""
    props = (parameters or {}).get("properties") or {}
    return {name: _PY_TYPES.get(str((spec or {}).get("type")), str) for name, spec in props.items()}


class ClaudeRuntime:
    can_steer = False

    def __init__(self):
        self._client: ClaudeSDKClient | None = None
        self._queries = 0
        self._pending_session: str | None = None
        self._system = ""
        self._tools: list[ToolSpec] = []

    async def start(self, system: str, tools: list[ToolSpec]) -> None:
        """Запомнить подсказку и инструменты; сессия поднимается при первом ходе (подсказка нужна только ей)."""
        self._system, self._tools = system, list(tools)

    # ── инструменты ────────────────────────────────────────────────

    def _server(self):
        sdk_tools = []
        for spec in self._tools:
            def make(spec: ToolSpec):
                @tool(spec.name, spec.description, _sdk_schema(spec.parameters))
                async def run_tool(args):
                    ok, text = await spec.handler(args if isinstance(args, dict) else {})
                    out: dict = {"content": [{"type": "text", "text": text}]}
                    if not ok:
                        out["is_error"] = True
                    return out
                return run_tool
            sdk_tools.append(make(spec))
        return create_sdk_mcp_server(name="core", version="1.0.0", tools=sdk_tools)

    # ── сохранённая сессия ─────────────────────────────────────────

    @staticmethod
    def _session_file():
        import settings
        return settings.memory_dir() / "resident_session.json"

    def _saved_session(self) -> str | None:
        """Сессия прошлого запуска: после перезапуска ядра агент продолжает тот же разговор, а не начинает с чистого листа."""
        try:
            data = json.loads(self._session_file().read_text(encoding="utf-8"))
            if data.get("session_id") and int(data.get("queries", 0)) < MAX_QUERIES:
                self._queries = int(data.get("queries", 0))
                return str(data["session_id"])
        except Exception:
            pass
        return None

    def _save_session(self, session_id: str | None) -> None:
        try:
            path = self._session_file()
            if session_id:
                path.write_text(json.dumps({"session_id": session_id, "queries": self._queries}), encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"сохранение сессии: {e}")

    def _options(self, resume: str | None = None) -> ClaudeAgentOptions:
        import settings
        model = "sonnet" if settings.load().get("fast_claude_model") == "sonnet" else "haiku"
        kwargs: dict = dict(
            system_prompt=self._system, model=model, mcp_servers={"core": self._server()},
            allowed_tools=["mcp__core"], tools=[], permission_mode="default", setting_sources=[], strict_mcp_config=True,
            env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"})
        if _CLI_PATH:
            kwargs["cli_path"] = _CLI_PATH
        if resume:
            kwargs["resume"] = resume
        return ClaudeAgentOptions(**kwargs)

    # ── сессия ─────────────────────────────────────────────────────

    async def _ensure(self) -> ClaudeSDKClient:
        if self._client is not None and self._queries >= MAX_QUERIES:
            await self.reset(forget=True)
        if self._client is None:
            saved = self._saved_session()
            try:
                self._client = ClaudeSDKClient(self._options(saved))
                await self._client.connect()
            except Exception as e:
                if not saved:
                    raise
                logger.warning(f"Прошлую сессию агента продолжить не вышло ({e}), начинаю новую")
                self._queries, self._client = 0, None
                self._save_session(None)
                self._client = ClaudeSDKClient(self._options())
                await self._client.connect()
            if not saved:
                self._queries = 0
        return self._client

    async def reset(self, forget: bool = True) -> None:
        """Закрыть сессию. `forget`: и забыть её (сбой, обрыв, лимит обращений); без него сессию подхватит следующий запуск."""
        if forget:
            self._pending_session = None
            self._save_session(None)
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:
                logger.debug(f"disconnect: {e}")

    async def close(self) -> None:
        await self.reset(forget=False)

    async def abort(self) -> None:
        client = self._client
        if client is not None:
            try:
                await client.interrupt()
            except Exception as e:
                logger.debug(f"interrupt: {e}")

    async def steer(self, text: str) -> bool:
        return False

    # ── ход ────────────────────────────────────────────────────────

    async def run(self, prompt: str) -> AsyncIterator[RuntimeEvent]:
        client = await self._ensure()
        await client.query(prompt)
        usage: dict = {}
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    text = getattr(block, "text", "")
                    if text and text.strip().startswith(AUTH_FAILED):      # служебный текст CLI, не реплика: вслух не говорить
                        raise RuntimeError(text.strip()[:200])
                    if text and text.strip() and not claude_quota.note(text):
                        yield TextEvent(text.strip())
                    elif hasattr(block, "name"):
                        args = getattr(block, "input", None)
                        yield ToolEvent(str(block.name).replace("mcp__core__", ""), args if isinstance(args, dict) else {})
            elif isinstance(message, ResultMessage):
                self._pending_session = getattr(message, "session_id", None) or self._pending_session
                usage = dict(getattr(message, "usage", None) or {})
                try:
                    import usage_report
                    usage_report.push_result(message, "resident")
                except Exception as e:
                    logger.debug(f"usage: {e}")
        self._queries += 1
        self._save_session(self._pending_session)
        yield EndEvent("ok", None, usage)
