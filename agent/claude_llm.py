"""Быстрая модель на Claude (вместо локальной Gemma): те же вызовы `ask` и `ask_stream`, что у `fast_brain._ask` и `_ask_stream`.

Выбирается настройкой `fast_llm` (`claude` или `local`). Инструментов нет, ход один, настройки и коннекторы пользователя не
подгружаются (иначе запрос раздувается до лимита контекста). Модель берётся из `fast_claude_model` (haiku по умолчанию).
Картинка (`image_b64`) уходит блоком изображения. Лимит подписки: при исчерпании бросает RuntimeError, как недоступный LM Studio.
"""
import asyncio
import base64
import logging

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, StreamEvent, query

import claude_quota
from claude_cli import _CLI_PATH

logger = logging.getLogger("claude_llm")


def _options(system: str, model: str, partial: bool = False) -> ClaudeAgentOptions:
    kwargs: dict = dict(system_prompt=system, model=model, tools=[], allowed_tools=[], max_turns=1, permission_mode="default",
                        setting_sources=[], strict_mcp_config=True, env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
                        include_partial_messages=partial)
    if _CLI_PATH:
        kwargs["cli_path"] = _CLI_PATH
    return ClaudeAgentOptions(**kwargs)


def _prompt(user: str, image_b64: str | None):
    """Строка или поток сообщений: с картинкой SDK принимает только потоковый ввод."""
    if not image_b64:
        return user

    async def stream():
        yield {"type": "user", "session_id": "", "parent_tool_use_id": None, "message": {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": user}]}}
    return stream()


def _check() -> None:
    if claude_quota.blocked():
        raise RuntimeError("лимит Claude исчерпан")


def _model() -> str:
    import settings
    return "sonnet" if settings.load().get("fast_claude_model") == "sonnet" else "haiku"


async def ask(system: str, user: str, timeout: float, image_b64: str | None = None, contour: str = "fast") -> str:
    """Ответ Claude целиком. RuntimeError: лимит или пустой ответ; TimeoutError: не уложился в `timeout`."""
    _check()
    options = _options(system, _model())
    parts: list[str] = []

    async def consume():
        async for message in query(prompt=_prompt(user, image_b64), options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    text = getattr(block, "text", "")
                    if text and not claude_quota.note(text):
                        parts.append(text)
            elif isinstance(message, ResultMessage):
                try:
                    import usage_report
                    usage_report.push_result(message, contour)
                except Exception as e:                       # учёт расхода не должен ломать ответ
                    logger.debug(f"usage: {e}")

    await asyncio.wait_for(consume(), timeout)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("Claude вернул пустой ответ")
    return text


async def ask_stream(system: str, user: str, timeout: float):
    """Ответ Claude потоком кусков текста (первое предложение идёт в речь, пока остальное пишется)."""
    _check()
    options = _options(system, _model(), partial=True)
    got = False
    async with asyncio.timeout(timeout):
        async for message in query(prompt=user, options=options):
            if isinstance(message, StreamEvent):
                ev = message.event or {}
                delta = ev.get("delta") or {}
                if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta" and delta.get("text"):
                    got = True
                    yield delta["text"]
            elif isinstance(message, AssistantMessage) and not got:       # без частичных сообщений: целиком
                for block in message.content:
                    text = getattr(block, "text", "")
                    if text and not claude_quota.note(text):
                        got = True
                        yield text
    if not got:
        raise RuntimeError("Claude вернул пустой ответ")


def image_ok(image_b64: str | None) -> bool:
    """Проверка, что картинка это base64 (иначе SDK упадёт позже, посреди ответа)."""
    try:
        return bool(image_b64) and bool(base64.b64decode(image_b64[:64] + "=" * (-len(image_b64[:64]) % 4)))
    except Exception:
        return False
