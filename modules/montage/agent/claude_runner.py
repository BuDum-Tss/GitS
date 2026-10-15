"""Агент на Claude Agent SDK (ТЗ 20, раздел 8, MON-15, MON-16, MON-17).

`SdkRunner.run` выполняет одну стадию агента: новый клиент SDK на стадию, сессия продолжается по `session_id` из job.json
(параметр `resume`; контекст между стадиями сохраняется). Параметры `ClaudeAgentOptions` (проверено на claude-agent-sdk
0.2.157): `cwd`, `model`, `system_prompt` (файл: промпт длиннее лимита командной строки Windows), `setting_sources=["project"]`
(скиллы из work/.claude/skills), `tools` (какие встроенные инструменты существуют), `allowed_tools=[]` (отступление от
MON-15: всё, что в allowed_tools, SDK выполняет мимо `can_use_tool`, а песочница MON-17 должна видеть каждый вызов),
`can_use_tool` (песочница), `max_turns`, `resume`, `cli_path`, `mcp_servers` (свои инструменты).
Заглушка для тестов реализует тот же интерфейс `AgentRunner` (agent/tests/stubs.py).
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from agent_tools import AgentTools, JobEnv
from glados_module import ToolError
from layout import Layout
from sandbox import Sandbox

log = logging.getLogger("montage.claude")

STAGE_TIMEOUT_S = 40 * 60
BUILTIN_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill", "TodoWrite"]
READONLY_TOOLS = ["Read", "Glob"]
LIMIT_RE = re.compile(r"(usage|rate|session|weekly|5-hour)\s+limit|limit\s+reached|hit your .*limit|credit balance", re.I)


class ClaudeLimitError(Exception):
    """Лимит Claude исчерпан: повторять бессмысленно (MON-9, батарейка)."""


class StageFailed(Exception):
    """Агент не смог выполнить стадию (сообщение для журнала, без трассировки)."""


@dataclass
class StageResult:
    text: str = ""
    tokens: int = 0
    session_id: str | None = None
    turns: int = 0
    hit_turn_limit: bool = False


class AgentRunner(Protocol):
    async def run(self, env: JobEnv, stage: str, prompt: str, *, model: str, max_turns: int, readonly: bool = False,
                  fresh: bool = False) -> StageResult: ...


def find_claude_exe() -> str | None:
    """Путь к claude.exe для SDK (функция скопирована из GitS: claude_cli._find_claude_exe)."""
    patterns = [
        r"C:\Users\*\AppData\Local\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\*\claude.exe",
        r"C:\Users\*\AppData\Local\Programs\Claude\claude.exe",
        r"C:\Program Files\Claude\claude.exe",
        r"C:\Users\*\AppData\Local\uv\cache\archive-v0\*\claude_agent_sdk\_bundled\claude.exe",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=False))
        if matches:
            return matches[-1]
    return None


def tokens_of(usage: dict | None) -> int:
    """Токены из поля usage ответа Claude: вход, выход и запись в кэш (чтение кэша не считается). Как в GitS: claude_battery."""
    u = usage or {}
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))


def build_system_prompt(layout: Layout, persona_card: str) -> str:
    """editor_system.md + карточка персонажа (core.persona) + регламент стиля + руководство ремесла (MON-15)."""
    parts = [(layout.prompts / "editor_system.md").read_text(encoding="utf-8").replace("@@TEMPLATE@@", str(layout.template).replace("\\", "/"))]
    if persona_card.strip():
        parts.append("# Карточка персонажа (core.persona)\n\n" + persona_card.strip())
    parts.append("# Регламент стиля (docs/APERTURE_STYLE.md)\n\n" + (layout.docs / "APERTURE_STYLE.md").read_text(encoding="utf-8"))
    parts.append("# Руководство по ремеслу (docs/EDITING_CRAFT.md)\n\n" + (layout.docs / "EDITING_CRAFT.md").read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def recap_from_log(rows: list[dict]) -> str:
    """Краткий пересказ хода задачи из log.jsonl для новой сессии, когда старая недоступна (MON-8)."""
    lines = []
    for r in rows:
        if r.get("kind") in ("stage", "answer", "revision", "agent_done"):
            lines.append("- " + ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("ts",) and v not in (None, ""))[:240])
    return "\n".join(lines[-30:])


class SdkRunner:
    def __init__(self, layout: Layout, *, cli_path: str | None = None, timeout_s: float = STAGE_TIMEOUT_S):
        self.layout = layout
        self.cli_path = cli_path if cli_path is not None else find_claude_exe()
        self.timeout_s = timeout_s

    # ---- свои инструменты (SDK MCP в процессе)
    def _server(self, tools: AgentTools):
        from claude_agent_sdk import create_sdk_mcp_server, tool

        def wrap(coro_fn):
            async def handler(args):
                try:
                    value = await coro_fn(**args)
                except ToolError as e:
                    return {"content": [{"type": "text", "text": f"{e.code}: {e.message}"}], "is_error": True}
                except TypeError as e:
                    return {"content": [{"type": "text", "text": f"bad_request: {e}"}], "is_error": True}
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                return {"content": [{"type": "text", "text": text}]}
            return handler

        defs = [
            ("ask_user", "Задать вопрос пользователю (через ядро) и получить ответ. Только на стадиях clarify (до 2 раз) и build "
                         "(1 раз). kind: choice (с options) или text.",
             {"question": str, "kind": str, "options": list}, tools.ask_user),
            ("progress", "Сообщить ход работы: progress от 0 до 1 внутри стадии и короткий текст-факт.",
             {"progress": float, "text": str}, tools.progress),
            ("synthesize_voice", "Синтезировать реплику её голосом в work/public/vo/<id>.wav. Возвращает duration_ms. "
                                 "Единственный способ получить закадр.",
             {"id": str, "text": str, "emotion": str}, tools.synthesize_voice),
            ("kb_lookup", "Поиск по справочнику монтажа (регламент и опыт) без интернета.", {"query": str}, tools.kb_lookup),
            ("transcript_words", "Слова расшифровки с таймингами в отрезке from_ms..to_ms (не весь файл).",
             {"from_ms": int, "to_ms": int}, tools.transcript_words),
        ]
        sdk_tools = [tool(name, desc, schema)(wrap(fn)) for name, desc, schema, fn in defs]
        return create_sdk_mcp_server("montage", "1.0.0", sdk_tools)

    async def run(self, env: JobEnv, stage: str, prompt: str, *, model: str, max_turns: int, readonly: bool = False,
                  fresh: bool = False) -> StageResult:
        job = env.job
        session = None if (fresh or readonly) else (job.data.get("claude") or {}).get("session_id")
        try:
            return await asyncio.wait_for(self._once(env, stage, prompt, model, max_turns, readonly, session), self.timeout_s)
        except asyncio.TimeoutError:
            raise StageFailed(f"агент не уложился в {int(self.timeout_s // 60)} минут на стадии {stage}")
        except (ClaudeLimitError, StageFailed, asyncio.CancelledError):
            raise
        except Exception as e:
            if session is None:
                raise StageFailed(_short(e)) from e
            log.warning("сессия %s недоступна (%s): начинаю новую с пересказом", session, _short(e))
            env.store.log(job, "session_lost", session=session, error=_short(e))
            recap = recap_from_log(env.store.read_log(job, 200))
            prompt = ("(Прежняя сессия недоступна. Ход задачи до этого места:\n" + recap + "\nФайлы в work/ и plan/ на месте.)\n\n"
                      + prompt)
            try:
                return await asyncio.wait_for(self._once(env, stage, prompt, model, max_turns, readonly, None), self.timeout_s)
            except asyncio.TimeoutError:
                raise StageFailed(f"агент не уложился в {int(self.timeout_s // 60)} минут на стадии {stage}")
            except (ClaudeLimitError, StageFailed, asyncio.CancelledError):
                raise
            except Exception as e2:
                raise StageFailed(_short(e2)) from e2

    async def _once(self, env: JobEnv, stage: str, prompt: str, model: str, max_turns: int, readonly: bool,
                    session: str | None) -> StageResult:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock,
                                      ToolUseBlock)
        job = env.job
        sandbox = Sandbox(job.folder, self.layout, allow_ask=stage in ("clarify", "build"), readonly=readonly,
                          log=lambda row: env.store.log(job, "sandbox", stage=stage, **row))
        persona = ""
        try:
            persona = json.loads((job.folder / "persona.json").read_text(encoding="utf-8")).get("card", "")
        except (OSError, ValueError):
            pass
        sp = job.folder / "system_prompt.md"
        sp.write_text(build_system_prompt(self.layout, persona), encoding="utf-8")
        kwargs = dict(
            cwd=str(job.work), model=model, system_prompt={"type": "file", "path": str(sp)}, setting_sources=["project"],
            tools=READONLY_TOOLS if readonly else BUILTIN_TOOLS, allowed_tools=[], can_use_tool=sandbox.can_use_tool,
            max_turns=max_turns, permission_mode="default", resume=session,
        )
        if not readonly:
            kwargs["mcp_servers"] = {"montage": self._server(AgentTools(env))}
        if self.cli_path:
            kwargs["cli_path"] = self.cli_path
        text, result, tools_used = "", None, 0
        async with ClaudeSDKClient(options=ClaudeAgentOptions(**kwargs)) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            text = block.text.strip()
                        elif isinstance(block, ToolUseBlock):
                            tools_used += 1
                elif isinstance(msg, ResultMessage):
                    result = msg
                    break
        if result is None:
            raise StageFailed("агент завершился без результата")
        tokens = tokens_of(result.usage)
        limited = bool(LIMIT_RE.search(str(result.result or "") if result.is_error else text[:400]))
        await env.core.claude_usage(tokens, "montage", limit_text=(result.result or text) if limited else None)
        env.store.log(job, "agent_done", stage=stage, model=model, tokens=tokens, turns=result.num_turns, tools=tools_used,
                      subtype=result.subtype, is_error=result.is_error,
                      usage={k: int((result.usage or {}).get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")})
        if limited:
            raise ClaudeLimitError(str(result.result or text)[:300])
        if result.is_error and result.subtype != "error_max_turns":
            raise StageFailed(f"агент вернул ошибку ({result.subtype}): {str(result.result or '')[:200]}")
        return StageResult(text=str(result.result or text or ""), tokens=tokens, session_id=result.session_id,
                           turns=result.num_turns, hit_turn_limit=result.subtype == "error_max_turns")


def _short(e: BaseException) -> str:
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return text[:240]
