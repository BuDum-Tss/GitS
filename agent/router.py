"""Исполнитель инструментов: проверка параметров, вопрос о разрешении, вызов, журнал и трасса.

Планирование и разговор делает единый агент (`app/resident.py`): он ищет инструменты через `find_tools` и вызывает их через
`call_tool`, а тот приходит сюда, в `run_tool_ex`. Здесь нет модели: проверка по схеме, риск (`domain/risk.py`), вопрос о
разрешении (`approvals.py`), тайм-аут инструмента, ошибки с кодами MOD-9.
"""
import asyncio
import logging
import time

from adapters.approver import ApprovalsModule
from adapters.event_log_sqlite import SqliteEventLog
from adapters.settings_provider import SettingsModule
import flow_trace
import journal
import tool_trace
from domain import risk as risk_policy
from environments.base import ToolError, validate_args

logger = logging.getLogger("router")

RESULT_CHARS = 600


class Router:
    def __init__(self, registry, say, *, event_log=None, settings_provider=None, approver=None):
        """event_log - EventLog, settings_provider - SettingsProvider, approver - Approver (вопрос о разрешении). Без них
        подставляются адаптеры к текущим модулям ядра (ТЗ 29, ARC-3)."""
        self.registry = registry
        self._say = say                          # как озвучить вопрос о разрешении
        self.after_tool = None                   # async (tool, args, ok, result) -> None: что делать после инструмента (ставит ядро)
        self.auto_ok = lambda: False             # () -> агент сейчас играет в автономной среде: обычные действия без вопроса (ставит ядро)
        self._log = event_log if event_log is not None else SqliteEventLog()
        self._settings = settings_provider if settings_provider is not None else SettingsModule()
        self._approver = approver if approver is not None else ApprovalsModule()

    async def _step(self, step: dict, candidates: dict, executor: str = "fast") -> tuple[bool, str, str, str | None]:
        """(успех, название, результат/причина, код ошибки по MOD-9 или None); шаг пишется в трассу потока (ТЗ 17)
        и в журнал запусков для чата (`tool_trace`; executor: fast - планировщик, external - вызов со стороны)."""
        t0 = time.perf_counter()
        tool = candidates.get(str(step.get("tool", "")))
        trace_id = tool_trace.start(executor, str(step.get("tool", "?")), tool_trace.summarize(step.get("args")))
        res = await self._step_inner(step, candidates)
        tool_trace.finish(trace_id, res[0], res[2])
        if self.after_tool is not None and tool is not None:
            try:
                await self.after_tool(tool, step.get("args") or {}, res[0], res[2])
            except Exception as e:
                logger.warning(f"После инструмента {tool.id}: {e}")
        mod = str(tool.id).split(".", 1)[0] if tool is not None else "?"
        ms = (time.perf_counter() - t0) * 1000
        label = f"{step.get('tool', '?')}:{'ok' if res[0] else 'error'}"
        flow_trace.step(None, "tools", "planner", "tools", "tool", label=label)
        if tool is not None and "." in str(tool.id):
            flow_trace.step(None, "tools", "tools", f"module:{mod}", "tool", ms=ms, label=label)
        return res

    async def _step_inner(self, step: dict, candidates: dict) -> tuple[bool, str, str, str | None]:
        tool = candidates.get(str(step.get("tool", "")))
        if tool is None:
            return False, str(step.get("tool", "?")), "такого инструмента у неё нет", "unknown"
        args, err = validate_args(tool, step.get("args"))
        if err:
            return False, tool.name, f"неверные параметры: {err}", "bad_request"
        danger = tool.danger(args) if tool.danger else None
        if tool.danger_check and tool.check is not None and not danger:
            danger = await tool.check(args)       # модуль сам называет опасность (tool.check, CORE-35)
        public_unconfirmed = tool.risk == "public" and not tool.self_confirming
        needs_ask = risk_policy.needs_ask(danger, tool.risk, tool.self_confirming,
                                          public_unconfirmed and self._settings.load()["telegram_confirm"])
        if needs_ask and not danger and tool.risk == "ask" and self.auto_ok():
            needs_ask = False                    # она играет сама: владельца обычными вопросами не отвлекаем; опасное и публичное спрашивается
        if needs_ask:
            shown = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
            # факт для модели, а не готовая реплика: она сама спросит своими словами (в факт не входит описание инструмента:
            # оно написано для агента и в речь не годится)
            question = f"Единый агент хочет выполнить действие «{tool.name}» из набора «{tool.env}»"
            if shown:
                question += f", параметры: {shown[:200]}"
            if not await self._approver.request(question + ".", self._say, danger=danger):
                journal.mistake("denied", f"{tool.id}: пользователь не разрешил")      # ТЗ 24, TRC-5
                return False, tool.name, "пользователь не разрешил", "denied"
        try:
            # запас над timeout_s: модульный вызов сам вернёт понятную причину тайм-аута раньше (CORE-34)
            result = await asyncio.wait_for(tool.call(args), tool.timeout_s + 2)
        except ToolError as e:
            journal.mistake("tool_failed", f"{tool.id}: {e}"[:200], code=getattr(e, "code", "unavailable"))
            return False, tool.name, str(e), getattr(e, "code", "unavailable")
        except asyncio.TimeoutError:
            journal.mistake("tool_failed", f"{tool.id}: не уложилась по времени", code="timeout")
            return False, tool.name, "не уложилась по времени", "timeout"
        except Exception as e:
            logger.warning(f"Инструмент {tool.id}: {e}")
            journal.mistake("tool_failed", f"{tool.id}: {e}"[:200], code="internal")
            return False, tool.name, f"сбой: {e}", "internal"
        return True, tool.name, str(result)[:RESULT_CHARS], None

    # ── главный вход ───────────────────────────────────────────────────────────

    async def run_tool_ex(self, tool_id: str, args: dict) -> tuple[bool, str, str | None]:
        """Как run_tool, но третьим значением возвращает код ошибки (MOD-9: unknown, bad_request, denied, unavailable,
        timeout, internal) или None при успехе: так отвечает ядро на core.tool.call от модулей."""
        tool = self.registry.get(tool_id)
        if tool is None:
            return False, "такого инструмента нет", "unknown"
        ok, _name, result, code = await self._step({"tool": tool_id, "args": args}, {tool_id: tool}, "external")
        return ok, result, code

    async def call(self, tool_id: str, args: dict) -> tuple[bool, str, str | None]:
        """ToolInvoker (контракт ТЗ 29, ARC-3): то же, что run_tool_ex."""
        return await self.run_tool_ex(tool_id, args)

    async def run_tool(self, tool_id: str, args: dict) -> tuple[bool, str]:
        """Выполнить один инструмент по id с обычным контролем: проверка параметров, вопрос о разрешении, опасное всегда спрашивается.
        Для других агентов (игра) через дашборд. Возвращает (успех, результат или причина)."""
        ok, result, _code = await self.run_tool_ex(tool_id, args)
        return ok, result
