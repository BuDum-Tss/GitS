"""Сборка общих задач (ТЗ 32, WS-8): TaskStore + TaskReminders + LlmPlanner + TaskService.

`tasks.db` лежит в папке памяти персонажа (меняется при переключении персонажа, база открывается заново; старый `reminders.json`
переносится в неё один раз при первом открытии). Среда задач берётся у `WorkspaceManager.current`, при смене среды checkpoint и
возобновление идут через `WorkspaceManager.on_change`.
"""
import time
from pathlib import Path

from services.tracing import events_log
from services.character import prompts
from services.tasks.migration import migrate_reminders
from services.tasks.reminders import TaskReminders
from services.tasks.store import FILE, TaskStore
from services.tasks.planner import LlmPlanner
from services.tasks.service import TaskService


def build(companion, workspaces=None, *, clock=None) -> TaskService:
    """companion - Companion (папка памяти, локальная модель), workspaces - WorkspaceManager или None (всё в assistant)."""
    clock = clock or time.time
    store = TaskStore(lambda: Path(companion._dir) / FILE, on_open=migrate_reminders)
    workspace_of = (lambda: workspaces.current) if workspaces is not None else (lambda: "assistant")

    def context_of(ws: str) -> dict:
        spec = workspaces.specs.get(ws) if workspaces is not None else None
        return {"workspace": ws, "title": getattr(spec, "title", ws), "purpose": getattr(spec, "purpose", "")}

    service = TaskService(store, planner=LlmPlanner(companion._llm, prompts.planner), workspace_of=workspace_of, context_of=context_of,
                          clock=clock, log=events_log.log_nowait)
    service.reminders = TaskReminders(store, workspace_of=workspace_of, clock=clock, log=events_log.log_nowait)
    service.store = store
    return service
