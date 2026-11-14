"""Журнал действий агента (docs/WORKSPACE_CONTRACT.md, раздел 7а): записи единицы, «сейчас», главная строка единицы в задачах,
вкладка «Действия» и план действий на сайте, строка «Последнее действие» в панели."""
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from gits_sdk import ToolError

from journal import Journal
from jobs import JobStore
from montage_site import Site
from tests.rig import Rig
from tests.stubs import STORY, make_plan

pytestmark = pytest.mark.usefixtures("fast")

PLAN = {"workspace": "montage", "active": "t1", "tasks": [
    {"id": "t1", "goal": "Ролик про куб", "status": "active", "progress": 0.25, "steps": [
        {"text": "Идея", "status": "done"}, {"text": "Сценарий", "status": "active"},
        {"text": "Монтажный лист", "status": "pending"}, {"text": "Показ", "status": "skipped"}]}]}


def actions(rig, jid):
    return rig.manager.store.read_actions(rig.unit(jid), 300)


async def test_actions_are_logged_for_skill_files_denial_and_run(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    await rig.tool("skills", {"name": "montage-regulation"})
    await rig.files("read", "template/demo/plan.horizontal.json")
    await rig.files("write", f"{jid}/plan/idea.md", content="Идея про куб")
    with pytest.raises(ToolError):
        await rig.files("write", f"{jid}/sources/x.mp4", content="x")
    with pytest.raises(ToolError):
        await rig.run("check", jid)                                                      # плана ещё нет: отказ тоже в журнале
    rows = actions(rig, jid)
    texts = [r["text"] for r in rows]
    assert any(t.startswith("Создала единицу") for t in texts)
    assert "Читает навык montage-regulation" in " ".join(texts)                           # навык: единица текущая после create
    assert any(t.startswith("Читает template/demo/plan.horizontal.json (") for t in texts)
    assert f"Записала {jid}/plan/idea.md (+1 строка)" in texts
    denied = [r for r in rows if not r["ok"]]
    assert denied and denied[0]["text"].startswith("Отказано") and denied[0]["tool"] == "files" and denied[0]["error"].startswith("denied")
    assert any(r["tool"] == "run" and r["target"].startswith("check ") and not r["ok"] for r in rows)
    assert all(r["kind"] == "action" for r in rows)
    assert "Идея про куб" not in (rig.unit(jid).folder / "log.jsonl").read_text(encoding="utf-8")   # содержимого файлов в журнале нет


async def test_calls_before_work_create_go_to_the_environment_log(tmp_path):
    rig = Rig(tmp_path)
    await rig.tool("skills", {"name": "montage-regulation"})
    await rig.tool("resources")
    await rig.tool("work", {"op": "list"})
    env = rig.manager.store.read_env_actions()
    assert [r["tool"] for r in env] == ["skills", "resources", "work"] and env[0]["text"].startswith("Читает навык")
    jid = await rig.create()
    assert any(r["tool"] == "work" and r["op"] == "create" for r in actions(rig, jid))


async def test_now_follows_the_last_action_and_waiting_user(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    await rig.files("list", f"{jid}/plan")
    now = rig.unit(jid).data["now"]
    assert now["text"] == f"смотрит каталог {jid}/plan" and now["at"] > 0
    await rig.tool("work", {"op": "status", "id": jid, "set": "waiting_user"})
    assert rig.unit(jid).data["now"]["text"] == "ждёт ответа пользователя"
    last = [u for u in rig.core.updates if u["job_id"] == jid][-1]
    assert last["stage"] == "ждёт ответа пользователя" and last["state"] == "running"       # waiting_user в протоколе требует вопроса ядра
    await rig.tool("work", {"op": "status", "id": jid, "set": "working"})
    assert rig.unit(jid).data["now"]["text"] == "работает"


async def test_unit_is_the_main_task_line_with_stage_from_commands_and_files(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create(voiceover="character")
    ups = lambda: [u for u in rig.core.updates if u["job_id"] == jid]          # noqa: E731
    assert ups()[0]["title"] and ups()[0]["state"] == "running" and ups()[0]["stage"] == "подготовка"
    res = await rig.run("intake", jid)
    await rig.wait_run(res["job_id"])
    assert any(u.get("stage") == "приём исходников" for u in ups())
    assert ups()[-1]["stage"] == "приём исходников" and ups()[-1]["text"]
    await rig.files("write", f"{jid}/plan/plan.json", content=json.dumps(make_plan(), ensure_ascii=False))
    assert ups()[-1]["stage"] == "план"
    await rig.files("write", f"{jid}/plan/idea.md", content="идея")
    assert ups()[-1]["stage"] == "идея"
    assert all(u["title"] == rig.unit(jid).data["title"] for u in ups())


async def test_unit_stage_and_progress_come_from_the_agent_task_plan(tmp_path):
    rig = Rig(tmp_path)
    rig.core.task_data = PLAN
    jid = await rig.create()
    last = [u for u in rig.core.updates if u["job_id"] == jid][-1]
    assert last["stage"] == "Сценарий" and last["progress"] == 0.25                    # первый незавершённый шаг, done/всего


async def test_unit_update_is_throttled_but_state_changes_go_at_once(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    before = len([u for u in rig.core.updates if u["job_id"] == jid])
    for _ in range(5):
        await rig.files("list", f"{jid}/plan")                                          # быстрые чтения: не чаще раза в секунду
    assert len([u for u in rig.core.updates if u["job_id"] == jid]) - before <= 1
    await rig.tool("work", {"op": "cancel", "id": jid})
    assert [u for u in rig.core.updates if u["job_id"] == jid][-1]["state"] == "cancelled"


async def test_long_commands_write_steps_into_the_journal(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create(voiceover="character")
    await rig.write_plan(jid, make_plan(), STORY)
    res = await rig.run("voiceover", jid)
    await rig.wait_run(res["job_id"])
    texts = [r["text"] for r in actions(rig, jid)]
    assert any(t.startswith("Озвучка: реплика 1 из ") for t in texts) and any(t.startswith("Озвучка: готово") for t in texts)
    steps = [u["text"] for u in rig.core.updates if u["job_id"] == res["job_id"] and u.get("text")]
    assert any(t.startswith("Озвучка: реплика") for t in steps)                          # job.update команды с текстом шага


async def test_panel_shows_the_latest_action_and_the_plan_step(tmp_path):
    rig = Rig(tmp_path)
    blocks = rig.manager.panel_blocks()
    assert [b["type"] for b in blocks] == ["stats", "text"] and blocks[1]["title"] == "Последнее действие"
    assert "Действий пока нет" in blocks[1]["text"]
    await rig.create()
    await rig.files("read", "template/demo/plan.horizontal.json")
    blocks = rig.manager.panel_blocks()
    assert "читает template/demo/plan.horizontal.json" in blocks[1]["text"] and ":" in blocks[1]["text"].split(" · ")[0]
    rig.core.task_data = PLAN
    await rig.manager.journal.plan(fresh=True)
    assert "Шаг 2 из 4: Сценарий" in rig.manager.panel_blocks()[1]["text"]


# ---------------------------------------------------------------- сайт

class SiteManager:
    def __init__(self, store, core=None):
        self.store = store
        self.layout = SimpleNamespace(owner="glados")
        self.core = core or SimpleNamespace()
        self._jobs = {}
        self.journal = Journal(self)

    async def open_studio(self, job_id):
        return {"url": "http://127.0.0.1:8791", "job_id": job_id}


async def test_site_actions_tab_escapes_text_and_orders_fresh_first(tmp_path):
    store = JobStore(tmp_path)
    job = store.create({"brief": "т"}, "Куб")
    store.log(job, "action", tool="files", op="read", target="a", ok=True, error=None, text="Читает <b>старое</b>")
    store.log(job, "action", tool="run", op="check", target="check", ok=False, error="invalid: <img src=x>", text="Не вышло: <script>alert(1)</script>")
    store.log(job, "run", command="x")                                                   # не действие: в таблицу не попадает
    job.data["now"] = {"text": "читает <i>файл</i>", "at": 1700000000}
    store.save(job)
    site = Site(SiteManager(store), port=0)
    async with TestClient(TestServer(site.app)) as c:
        page = await (await c.get(f"/job/{job.job_id}?s=actions")).text()
        assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
        assert page.index("Не вышло") < page.index("Читает &lt;b&gt;старое") and "id=rows" in page
        assert "&#10003;" in page and "&#10007;" in page and "setInterval(t,3000)" in page
        assert "Сейчас: читает &lt;i&gt;файл&lt;/i&gt;" in page and "<i>файл</i>" not in page
        index = await (await c.get("/")).text()
        assert "читает &lt;i&gt;файл&lt;/i&gt;" in index and "Сейчас" in index
        live = await (await c.get(f"/job/{job.job_id}/live")).json()
        assert "&lt;script&gt;" in live["rows"] and live["now"].startswith("Сейчас:") and "Агент ещё не завёл" in live["plan"]


async def test_site_shows_the_agent_plan_as_a_checklist_and_survives_an_error(tmp_path):
    store = JobStore(tmp_path)
    job = store.create({"brief": "т"}, "Куб")
    core = SimpleNamespace(data=PLAN)

    async def tasks():
        if isinstance(core.data, Exception):
            raise core.data
        return core.data
    core.tasks = tasks
    mgr = SiteManager(store, core)
    site = Site(mgr, port=0)
    async with TestClient(TestServer(site.app)) as c:
        page = await (await c.get(f"/job/{job.job_id}")).text()
        assert "План действий" in page and "Ролик про куб" in page and "Агент ещё не завёл" not in page
        assert "&#10003; Идея" in page and "&#9654; Сценарий" in page and "&#9675; Монтажный лист" in page and "&ndash; Показ" in page
        core.data = RuntimeError("ядро не отвечает")
        mgr.journal.cached_plan, mgr.journal._plan_at = None, 0.0
        resp = await c.get(f"/job/{job.job_id}")
        assert resp.status == 200 and "Агент ещё не завёл план действий" in await resp.text()
