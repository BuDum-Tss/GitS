"""Одноразовый вызов Claude без инструментов: короткий текст по системной подсказке и запросу (реплики комментатора).

Игровой ход и планирование идут через `glados_agent._run_agent` с инструментами MCP; здесь инструментов нет, ход один."""
import asyncio
import logging

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

import claude_quota
import glados_agent
import usage_report

logger = logging.getLogger("claude_say")


async def ask(system: str, user: str, timeout: float = 60.0, contour: str = "commentator") -> str:
    """Ответ Claude текстом. Бросает RuntimeError при лимите и пустом ответе, TimeoutError по таймауту."""
    if claude_quota.blocked():
        raise RuntimeError("лимит Claude исчерпан")
    kwargs: dict = dict(system_prompt=system, model=glados_agent._game_model(), tools=[], allowed_tools=[], max_turns=1,
                        permission_mode="default", setting_sources=[])
    if glados_agent._CLI_PATH:
        kwargs["cli_path"] = glados_agent._CLI_PATH
    options = ClaudeAgentOptions(**kwargs)
    parts: list[str] = []

    async def _consume():
        async for message in query(prompt=user, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    text = getattr(block, "text", "")
                    if text and not claude_quota.note(text):
                        parts.append(text)
            elif isinstance(message, ResultMessage):
                usage_report.push_result(message, contour)

    await asyncio.wait_for(_consume(), timeout)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("Claude вернул пустой ответ")
    return text
