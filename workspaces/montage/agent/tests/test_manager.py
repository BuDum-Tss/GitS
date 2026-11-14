"""Окружение целиком против FakeCore из SDK (MON-7, MON-8, MON-10, MON-12, MON-33; контракт 3.2): настоящий WorkspaceClient и
WebSocket; Remotion и контроль качества заглушки."""
import asyncio

import pytest
from gits_sdk import ToolError
from gits_sdk.testing import FakeCore

import main as main_mod
from manifest import build_manifest
from tests.stubs import STORY, FakeRenderer, StubQA, layout_for, make_plan, write_silence_wav

pytestmark = pytest.mark.usefixtures("fast")


class Env:
    def __init__(self, tmp, core: FakeCore, **kw):
        self.tmp = tmp
        self.core = core
        self.layout = layout_for(tmp)
        self.renderer = kw.pop("renderer", None) or FakeRenderer()
        from gits_sdk import WorkspaceClient
        self.client = WorkspaceClient(build_manifest(), url=core.url, token=core.token)
        self.client, self.manager = main_mod.build(self.layout, client=self.client, renderer=self.renderer, qa=StubQA(), site_port=0)
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(main_mod.run(self.client, self.manager))
        await self.core.wait_connected()
        await self.core.wait_for(lambda: self.core.states.get("montage") is not None, 10)
        return self

    async def __aexit__(self, *exc):
        await self.client.stop()
        await asyncio.wait_for(self.task, 20)

    def jobs(self):
        return self.core.messages_of("job.update")


def core_with_handlers(tmp, **kw):
    core = FakeCore(module_settings={"output_dir": str(tmp / "out")}, **kw)
    kb = []

    @core.on_request("core.persona")
    def persona(body):
        return {"name": "ГЛаДОС", "card": "Сухая учтивость."}

    @core.on_request("core.tts")
    def tts(body):
        seconds = max(0.8, len(body["text"]) * 0.06)
        path = tmp / f"tts_{len(kb)}.wav"
        kb.append(1)
        write_silence_wav(path, seconds)
        return {"path": str(path), "duration_ms": int(seconds * 1000)}

    return core


CREATE = {"op": "create", "brief": "20 секунд: ГЛаДОС объясняет правило", "source_kind": "narrated", "platform": "shorts",
          "duration_s": 12, "voiceover": "character"}


def last_update(core, job_id):
    ups = [m.body for m in core.messages_of("job.update") if m.body["job_id"] == job_id]
    return ups[-1] if ups else {"state": None}


async def test_work_through_the_real_protocol_with_background_final(tmp_path):
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core):
        made = await core.call_tool("work", CREATE)
        jid = made["id"]
        assert jid.startswith("j-") and made["state"] == "new"
        # файлы: запись плана и истории в область через настоящий вызов инструмента
        import json
        await core.call_tool("files", {"op": "write", "path": f"{jid}/plan/plan.json", "content": json.dumps(make_plan(), ensure_ascii=False)})
        (tmp_path / "jobs" / jid / "plan" / "story.json").write_text(json.dumps(STORY, ensure_ascii=False), encoding="utf-8")   # историю кладёт система
        with pytest.raises(ToolError) as e:
            await core.call_tool("files", {"op": "write", "path": f"{jid}/renders/x.mp4", "content": "x"})
        assert e.value.code == "denied"
        # озвучка, проверка, финал
        vo = (await core.call_tool("run", {"command": "voiceover", "args": [jid]}))["job_id"]
        await core.wait_for(lambda: last_update(core, vo)["state"] == "done", 60)
        chk = (await core.call_tool("run", {"command": "check", "args": [jid]}))["result"]
        assert chk["passed"] is True
        # final необратима: ядро спросит согласие (tool.check), причина приходит из окружения
        assert "необратима" in await core.check_tool("run", {"command": "final", "args": [jid]})
        assert await core.check_tool("run", {"command": "render", "args": [jid]}) is None
        assert "нельзя отменить" in await core.check_tool("files", {"op": "delete", "path": f"{jid}/plan/x"})
        fin = (await core.call_tool("run", {"command": "final", "args": [jid]}))["job_id"]
        await core.wait_for(lambda: last_update(core, fin)["state"] == "done", 120)
        assert last_update(core, fin)["progress"] == 1.0 and (tmp_path / "out" / "final_shorts.mp4").is_file()
        await core.wait_for(lambda: any(e["kind"] == "montage.done" and e["data"].get("path") for e in core.events), 10)
        kinds = [e["kind"] for e in core.events]
        assert "montage.launch" in kinds and "montage.done" in kinds
        launch = next(e for e in core.events if e["kind"] == "montage.launch")
        assert launch["announce"] is True and "финальный рендер" in launch["text"]
        # MON-33: state
        await core.wait_for(lambda: core.states["montage"]["data"]["runs"] == [], 10)
        assert core.states["montage"]["data"]["units"] == []                  # единица закрыта (done): в открытых её нет
        listed_jobs = {j["job_id"]: j for j in await core.job_list()}
        assert {vo, fin, jid} <= set(listed_jobs)                              # главная строка: сама единица; операции вложенные
        assert listed_jobs[jid]["state"] == "done" and listed_jobs[jid]["title"]
        listed = await core.call_tool("work", {"op": "list"})
        assert [u["id"] for u in listed["result"]] == [jid]


async def test_job_cancel_stops_the_running_render(tmp_path):
    core = core_with_handlers(tmp_path)
    renderer = FakeRenderer()
    renderer.block = asyncio.Event()                                       # рендер «висит»
    async with core, Env(tmp_path, core, renderer=renderer) as env:
        import json
        jid = (await core.call_tool("work", CREATE))["id"]
        await core.call_tool("files", {"op": "write", "path": f"{jid}/plan/plan.json", "content": json.dumps(make_plan(voiceover=False))})
        run_id = (await core.call_tool("run", {"command": "render", "args": [jid]}))["job_id"]
        await core.wait_for(lambda: last_update(core, run_id)["state"] == "running", 10)
        await core.wait_for(lambda: "операций идёт 1" in core.states["montage"]["summary"], 10)
        with pytest.raises(ToolError) as e:
            await core.call_tool("run", {"command": "render", "args": [jid]})
        assert e.value.code == "busy"
        await core.job_cancel(run_id)
        await core.wait_for(lambda: last_update(core, run_id)["state"] == "cancelled", 30)
        assert (env.layout.jobs / jid).is_dir()                           # папка остаётся: файлы целы
        renderer.block.set()


async def test_enter_answer_carries_brief_skills_units_and_site(tmp_path):
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core) as env:
        jid = (await core.call_tool("work", CREATE))["id"]
        res = await core.workspace("enter", {"workspace": "montage", "goal": "собрать ролик", "task_id": "t1", "reason": "user"})
        assert res["ok"] is True and "montage-regulation" in res["brief"]
        names = {s["name"] for s in res["skills"]}
        assert "montage-regulation" in names and "remotion-markup" in names and all(s["description"] for s in res["skills"])
        assert res["units"] == [{"id": jid, "title": res["units"][0]["title"], "state": "new"}]
        assert res["site"]["url"].startswith("http://127.0.0.1:") and env.manager.site._runner is not None      # сайт поднят при входе
        # ответ проходит схему контракта
        from gits_sdk import contract
        assert contract.validate_response("workspace.enter", res) == []
        await core.wait_for(lambda: core.states["montage"]["data"]["status"]["done"] is False, 5)


async def test_afk_releases_nothing_and_exit_with_running_operations_waits(tmp_path):
    core = core_with_handlers(tmp_path)
    renderer = FakeRenderer()
    renderer.block = asyncio.Event()
    idle = []
    async with core, Env(tmp_path, core, renderer=renderer) as env:
        import json
        jid = (await core.call_tool("work", CREATE))["id"]
        await core.call_tool("files", {"op": "write", "path": f"{jid}/plan/plan.json", "content": json.dumps(make_plan(voiceover=False))})
        await core.workspace("enter", {"workspace": "montage", "goal": "рендер", "task_id": "t1"})
        run_id = (await core.call_tool("run", {"command": "render", "args": [jid]}))["job_id"]
        await core.wait_for(lambda: last_update(core, run_id)["state"] == "running", 10)
        env.manager.on_idle = lambda: idle.append(1)                         # так ставит его lease_host при отпускании
        # afk: фокус снят, всё на месте: операция идёт, сайт работает, ответ ok
        res = await core.request("workspace.afk", {"reason": "render", "checkpoint": True})    # FakeCore.workspace afk пока не знает
        assert res["ok"] is True and res["checkpoint"]["afk"] is True and res["checkpoint"]["running"][0]["job_id"] == run_id
        await core.wait_for(lambda: core.states["montage"]["data"]["status"]["done"] is True, 5)
        assert env.manager.active_jobs() == 1 and env.manager.site._runner is not None and not env.manager.env.releasing and idle == []
        assert last_update(core, run_id)["state"] == "running"
        # возврат тем же enter: без новой аренды, единицы на месте
        back = await core.workspace("enter", {"workspace": "montage", "goal": "вернулась"})
        assert back["ok"] and back["units"][0]["id"] == jid
        # exit с идущей операцией: операция дорабатывает, новые начинать нельзя, сайт гаснет в конце
        res = await core.workspace("exit", {"reason": "done", "checkpoint": True})
        assert res["ok"] and res["checkpoint"]["running"] == [run_id]
        assert env.manager.env.releasing and env.manager.site._runner is not None
        with pytest.raises(ToolError) as e:
            await core.call_tool("run", {"command": "check", "args": [jid]})
        assert e.value.code == "busy"
        with pytest.raises(ToolError) as e:
            await core.call_tool("work", CREATE)
        assert e.value.code == "busy"
        renderer.block.set()
        await core.wait_for(lambda: last_update(core, run_id)["state"] == "done", 30)
        await core.wait_for(lambda: idle, 10)                                # аренда, ждавшая конца операций, может освободиться
        assert env.manager.site._runner is None


async def test_exit_without_operations_stops_the_site_and_resume_restores(tmp_path):
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core) as env:
        await core.workspace("enter", {"workspace": "montage", "goal": "собрать ролик", "task_id": "t1"})
        assert env.manager.site._runner is not None
        status = lambda: core.states["montage"]["data"]["status"]            # noqa: E731
        res = await core.workspace("exit", {"reason": "user", "checkpoint": True, "force": True})
        from gits_sdk import contract
        assert contract.validate_response("workspace.exit", res) == []
        assert res["ok"] and res["checkpoint"]["goal"] == "собрать ролик"
        await core.wait_for(lambda: status()["done"] is True, 5)
        assert env.manager.site._runner is None
        res = await core.workspace("resume", {"task_id": "t1", "goal": "собрать ролик", "checkpoint": {}})
        assert res["ok"] and res["restored"] is True and res["site"]["url"]
        await core.wait_for(lambda: status()["done"] is False, 5)
        assert env.manager.site._runner is not None and not env.manager.env.releasing


async def test_unfinished_units_come_back_after_restart_with_interrupted_runs(tmp_path):
    core = core_with_handlers(tmp_path)
    layout = layout_for(tmp_path)
    from jobs import JobStore
    store = JobStore(layout.jobs)
    job = store.create({"sources": [], "brief": "ролик", "platform": "none"})
    job.data["state"] = "working"
    job.data["runs"] = {"render": {"job_id": "j-aaaaaaaa", "status": "running"}}      # процесс упал посреди рендера
    store.save(job)
    done = store.create({"sources": [], "brief": "готовый", "platform": "none"})
    done.data["state"] = "done"
    store.save(done)
    async with core, Env(tmp_path, core) as env:
        await core.wait_for(lambda: job.job_id in env.manager._jobs, 10)
        assert done.job_id not in env.manager._jobs
        assert store.load(job.job_id).data["runs"]["render"]["status"] == "interrupted"
        res = await core.workspace("enter", {"workspace": "montage"})
        assert [u["id"] for u in res["units"]] == [job.job_id]                    # закрытые единицы во вводную не попадают


async def test_publish_state_survives_run_finishing_during_file_read(tmp_path, monkeypatch):
    """Операция заканчивается, пока publish_state читает файлы: раньше KeyError ронял команду и рвал связь с ядром."""
    import asyncio
    from types import SimpleNamespace
    import manager as manager_mod
    mgr = manager_mod.Manager.__new__(manager_mod.Manager)
    run = SimpleNamespace(run_id="r-1", command="render", job_id="j-1")
    mgr._runs = {"r-1": run}
    mgr._jobs = {}

    def all_jobs():
        mgr._runs.clear()                                          # операция закончилась во время чтения
        return []
    mgr._all_jobs = all_jobs
    mgr.env = SimpleNamespace(status=lambda: {}, releasing=False)
    mgr.layout = SimpleNamespace(jobs=tmp_path)
    mgr.setting = lambda key: None
    mgr.core = SimpleNamespace(client=None)
    mgr.on_idle = None
    mgr.site = SimpleNamespace(stop=lambda: asyncio.sleep(0))
    mgr.describe = lambda j: {}
    await mgr.publish_state()
