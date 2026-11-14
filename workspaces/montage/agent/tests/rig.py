"""Стенд для тестов окружения: менеджер с настоящими песочницей и командами, а ядро, Remotion и контроль качества заглушки.
Клиент к ядру не подключён (`StubCore` ловит `job.update` и события); проход через настоящий протокол в test_manager.py."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gits_sdk import WorkspaceClient

import main as main_mod
from manifest import build_manifest
from tests.stubs import FakeRenderer, StubCore, StubQA, layout_for


class Rig:
    def __init__(self, tmp: Path, *, qa: StubQA | None = None, renderer: FakeRenderer | None = None):
        self.tmp = tmp
        self.layout = layout_for(tmp)
        self.core = StubCore(tmp, {"output_dir": str(tmp / "out")})
        self.renderer = renderer or FakeRenderer()
        self.qa = qa or StubQA()
        self.client = WorkspaceClient(build_manifest())
        self.client, self.manager = main_mod.build(self.layout, client=self.client, renderer=self.renderer, qa=self.qa, site_port=0)
        self.manager.core = self.core
        self.manager.cmds.core = self.core

    # ---- инструменты как их вызывает ядро (обработчики, повешенные на клиента)

    async def tool(self, name: str, args: dict | None = None):
        return await self.client._tools[name](args or {})

    async def run(self, command: str, *args: str, background: bool | None = None):
        body: dict = {"command": command, "args": list(args)}
        if background is not None:
            body["background"] = background
        return await self.tool("run", body)

    async def files(self, op: str, path: str | None = None, **kw):
        return await self.tool("files", {"op": op, **({"path": path} if path is not None else {}), **kw})

    async def create(self, **kw) -> str:
        args = {"op": "create", "brief": "20 секунд: ГЛаДОС объясняет правило", "source_kind": "narrated", "platform": "shorts",
                "duration_s": 12, **kw}
        res = await self.tool("work", args)
        return res["id"]

    def unit(self, job_id: str):
        return self.manager.store.load(job_id)

    # ---- ожидание итога фоновой операции

    async def wait_run(self, run_id: str, timeout: float = 30.0) -> dict:
        """Дождаться итога операции (последний job.update в конечном состоянии и событие, если оно полагается); вернуть его."""
        async def done():
            while True:
                ups = [u for u in self.core.updates if u["job_id"] == run_id]
                if ups and ups[-1].get("state") in ("done", "failed", "cancelled") and run_id not in self.manager._runs:
                    return ups[-1]
                await asyncio.sleep(0.02)
        last = await asyncio.wait_for(done(), timeout)
        await asyncio.sleep(0)                      # событие шлётся сразу после job.update в том же шаге
        return last

    def events(self, kind: str) -> list[dict]:
        return [e for e in self.core.events if e["kind"] == kind]

    def states_of(self, run_id: str) -> list[str | None]:
        return [u.get("state") for u in self.core.updates if u["job_id"] == run_id and u.get("state")]

    async def write_plan(self, job_id: str, plan: dict, story: dict | None = None) -> None:
        """Монтажный лист и историю кладёт сама система (в тестах команд): минуя песочницу агента и её проверку листа."""
        folder = self.unit(job_id).plan_dir
        (folder / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        if story is not None:
            (folder / "story.json").write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")
