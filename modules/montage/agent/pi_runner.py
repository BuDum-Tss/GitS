"""Исполнитель стадии на рабочем агенте pi (`services/agent-runtime`, клиент `gits_sdk.pi_agent`): вторая реализация `AgentRunner`.

Тот же интерфейс и тот же итог (`StageResult`, запись `agent_done`), что у `SdkRunner`. Отличия среды:
  - песочница видит КАЖДЫЙ вызов встроенного инструмента: `PiAgent` присылает имя и поля уже в виде Claude, решение
    принимает `Sandbox.check` (то же `decide`, тот же журнал `sandbox`); отказ уходит модели причиной;
  - свои инструменты монтажа (`ask_user`, `progress`, `synthesize_voice`, `transcript_words`) это `CustomTool`, и каждый вызов тоже
    проходит `Sandbox.check` под именем `mcp__montage__<имя>` (как у Claude), поэтому правило «ask_user только на clarify и build» то же;
  - сессия это файл `pi_session*.jsonl` в папке задачи; его путь хранится там же, где у Claude id сессии (`job.data["claude"]["session_id"]`);
  - названия моделей Claude (`opus`, `sonnet`) на pi не действуют: берётся настройка ядра `pi_model_heavy` для всех стадий;
  - `Skill` и `TodoWrite` у pi нет; скиллы из `work/.claude/skills` подключаются через `skills_dir`;
  - лимита Claude здесь нет: `core.claude_usage` не зовётся, `ClaudeLimitError` не бросается.
`claude_agent_sdk` не нужен.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path

from agent_tools import AgentTools, JobEnv
from claude_runner import (OWN_TOOL_DESCRIPTIONS, STAGE_TIMEOUT_S, AgentRunner, StageFailed, StageResult, _short,
                           build_system_prompt, recap_from_log)
from gits_sdk import ToolError
from layout import Layout
from sandbox import MCP_PREFIX, Sandbox

log = logging.getLogger("montage.pi")

PI_TOOLS = ["read", "write", "edit", "find", "grep", "bash"]
PI_READONLY_TOOLS = ["read", "find"]
DEFAULT_MODEL = "gpt-6-sol"
SESSION_PREFIX = "pi_session"

# JSON-схемы своих инструментов (поля те же, что у ветки Claude)
OWN_TOOL_PARAMETERS = {
    "ask_user": {"type": "object", "properties": {"question": {"type": "string"}, "kind": {"type": "string", "enum": ["choice", "text"]},
                                                   "options": {"type": "array", "items": {"type": "string"}}},
                 "required": ["question"]},
    "progress": {"type": "object", "properties": {"progress": {"type": "number"}, "text": {"type": "string"}},
                 "required": ["progress", "text"]},
    "synthesize_voice": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"},
                                                           "emotion": {"type": "string"}}, "required": ["id", "text"]},
    "transcript_words": {"type": "object", "properties": {"from_ms": {"type": "integer"}, "to_ms": {"type": "integer"}},
                         "required": ["from_ms", "to_ms"]},
}


def use_pi(settings: dict | None) -> bool:
    return (settings or {}).get("agent_runtime") == "pi"


def is_pi_session(value: object) -> bool:
    """Значение `session_id` из job.json: путь к файлу сессии pi (id сессии Claude не годится)."""
    return isinstance(value, str) and Path(value).name.startswith(SESSION_PREFIX) and value.endswith(".jsonl")


class _PiError(Exception):
    """Сбой хода pi (ошибка среды или модели): для `run` это обычное исключение, при продолжении сессии оно ведёт к повтору."""


class PiRunner:
    def __init__(self, layout: Layout, *, settings: Callable[[], dict], timeout_s: float = STAGE_TIMEOUT_S, agent_factory=None):
        self.layout = layout
        self.settings = settings
        self.timeout_s = timeout_s
        self._factory = agent_factory           # подмена PiAgent в тестах

    # ---- свои инструменты

    def _custom_tools(self, env: JobEnv, sandbox: Sandbox, counter: list[int]):
        from gits_sdk.pi_agent import CustomTool
        tools = AgentTools(env)

        def wrap(name: str, fn):
            async def handler(args: dict) -> str:
                allowed, reason = await sandbox.check(MCP_PREFIX + name, args if isinstance(args, dict) else {})
                if not allowed:
                    raise RuntimeError(f"denied: {reason}")
                counter[0] += 1
                try:
                    value = await fn(**(args or {}))
                except ToolError as e:
                    raise RuntimeError(f"{e.code}: {e.message}") from e
                except TypeError as e:
                    raise RuntimeError(f"bad_request: {e}") from e
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return handler

        fns = {"ask_user": tools.ask_user, "progress": tools.progress, "synthesize_voice": tools.synthesize_voice,
               "transcript_words": tools.transcript_words}
        return [CustomTool(name, OWN_TOOL_DESCRIPTIONS[name], OWN_TOOL_PARAMETERS[name], wrap(name, fns[name])) for name in fns]

    # ---- стадия

    async def run(self, env: JobEnv, stage: str, prompt: str, *, model: str, max_turns: int, readonly: bool = False,
                  fresh: bool = False) -> StageResult:
        job = env.job
        stored = (job.data.get("claude") or {}).get("session_id")
        resumed = stored if (not fresh and not readonly and is_pi_session(stored)) else None
        session = resumed or (None if readonly else self._new_session(job.folder))
        try:
            return await asyncio.wait_for(self._once(env, stage, prompt, max_turns, readonly, session), self.timeout_s)
        except asyncio.TimeoutError:
            raise StageFailed(f"агент не уложился в {int(self.timeout_s // 60)} минут на стадии {stage}") from None
        except (StageFailed, asyncio.CancelledError):
            raise
        except Exception as e:
            if resumed is None:
                raise StageFailed(_short(e)) from e
            log.warning("сессия %s недоступна (%s): начинаю новую с пересказом", resumed, _short(e))
            env.store.log(job, "session_lost", session=resumed, error=_short(e))
            recap = recap_from_log(env.store.read_log(job, 200))
            prompt = ("(Прежняя сессия недоступна. Ход задачи до этого места:\n" + recap + "\nФайлы в work/ и plan/ на месте.)\n\n"
                      + prompt)
            try:
                return await asyncio.wait_for(self._once(env, stage, prompt, max_turns, readonly, self._new_session(job.folder)),
                                              self.timeout_s)
            except asyncio.TimeoutError:
                raise StageFailed(f"агент не уложился в {int(self.timeout_s // 60)} минут на стадии {stage}") from None
            except (StageFailed, asyncio.CancelledError):
                raise
            except Exception as e2:
                raise StageFailed(_short(e2)) from e2

    @staticmethod
    def _new_session(folder: Path) -> str:
        return str(folder / f"{SESSION_PREFIX}_{uuid.uuid4().hex[:8]}.jsonl")

    async def _once(self, env: JobEnv, stage: str, prompt: str, max_turns: int, readonly: bool, session: str | None) -> StageResult:
        from gits_sdk.pi_agent import EndEvent, PiAgent, PiAgentError, TextEvent, ToolUse
        job = env.job
        cfg = self.settings() or {}
        model = str(cfg.get("pi_model_heavy") or DEFAULT_MODEL)
        sandbox = Sandbox(job.folder, self.layout, allow_ask=stage in ("clarify", "build"), readonly=readonly,
                          log=lambda row: env.store.log(job, "sandbox", stage=stage, **row))
        persona = ""
        try:
            persona = json.loads((job.folder / "persona.json").read_text(encoding="utf-8")).get("card", "")
        except (OSError, ValueError):
            pass
        sp = job.folder / "system_prompt.md"
        sp.write_text(build_system_prompt(self.layout, persona), encoding="utf-8")
        custom_calls = [0]
        skills = job.work / ".claude" / "skills"
        kwargs = dict(
            cwd=str(job.work), system_file=str(sp), tools=PI_READONLY_TOOLS if readonly else PI_TOOLS,
            custom_tools=None if readonly else self._custom_tools(env, sandbox, custom_calls), model=model,
            thinking=str(cfg.get("pi_thinking") or "minimal"), session_file=session,
            skills_dir=str(skills) if skills.is_dir() else None, max_turns=max_turns, can_use_tool=sandbox.check)
        agent = (self._factory or PiAgent)(**kwargs)
        text, end, tools_used = "", None, 0
        try:
            async for ev in agent.run(prompt):
                if isinstance(ev, TextEvent):
                    if ev.text.strip():
                        text = ev.text.strip()
                elif isinstance(ev, ToolUse):
                    tools_used += 1
                elif isinstance(ev, EndEvent):
                    end = ev
        except PiAgentError as e:
            raise _PiError(_short(e)) from e
        finally:
            await agent.close()
        tools_used += custom_calls[0]
        if end is None:
            raise StageFailed("агент завершился без результата")
        usage = end.usage or {}
        tokens = int(usage.get("input") or 0) + int(usage.get("output") or 0)
        hit = end.status == "max_turns"
        failed = end.status in ("error", "aborted")
        subtype = "error_max_turns" if hit else ("error" if failed else "success")
        env.store.log(job, "agent_done", stage=stage, model=model, tokens=tokens, turns=end.turns, tools=tools_used,
                      subtype=subtype, is_error=failed, runtime="pi",
                      usage={"input_tokens": int(usage.get("input") or 0), "output_tokens": int(usage.get("output") or 0),
                             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        if end.status == "aborted":
            raise StageFailed("агент прерван")
        if failed:
            raise _PiError(f"агент вернул ошибку: {str(end.error or '')[:200]}")
        return StageResult(text=text, tokens=tokens, session_id=session, turns=end.turns, hit_turn_limit=hit)


class RuntimeRunner:
    """Выбор исполнителя на каждую стадию по настройке ядра `agent_runtime` (claude | pi): смена действует со следующей стадии."""

    def __init__(self, claude: AgentRunner, pi: AgentRunner, settings: Callable[[], dict]):
        self.claude, self.pi, self.settings = claude, pi, settings

    def pick(self) -> AgentRunner:
        return self.pi if use_pi(self.settings()) else self.claude

    async def run(self, env: JobEnv, stage: str, prompt: str, **kw) -> StageResult:
        return await self.pick().run(env, stage, prompt, **kw)
