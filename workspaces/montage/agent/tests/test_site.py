"""Сайт монтажа: страницы задачи, монтажный лист, защита путей, переход в Studio."""
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jobs import JobStore
from montage_site import Site, safe_file, sheet_rows


class FakeManager:
    def __init__(self, store, owner="glados"):
        self.store = store
        self.layout = SimpleNamespace(owner=owner)

    async def open_studio(self, job_id):
        return {"url": "http://127.0.0.1:8791", "job_id": job_id}


@pytest.fixture
def job(tmp_path):
    store = JobStore(tmp_path)
    j = store.create({"brief": "тест"}, "Ролик про наблюдателя")
    j.plan_dir.mkdir(parents=True, exist_ok=True)
    (j.plan_dir / "story.md").write_text("Сценарий <b>№1</b>", encoding="utf-8")
    (j.plan_dir / "plan.json").write_text(json.dumps({
        "format": {"width": 1080, "height": 1920, "fps": 30}, "duration_ms": 30000,
        "voiceover": [{"id": "vo1", "at_ms": 100, "text": "Привет", "emotion": "idle"}],
        "inserts": [{"id": "i", "kind": "intro", "at_ms": 2500, "dur_ms": 4000, "props": {"subtitle": "ПРОТОКОЛ"}}],
        "sfx": [{"at_ms": 2500, "name": "chime_up", "volume": 0.1}]}), encoding="utf-8")
    (j.work / "node_modules").mkdir(parents=True)
    (j.work / "node_modules" / "x.js").write_text("secret", encoding="utf-8")
    (j.work / ".claude" / "skills").mkdir(parents=True)
    (j.work / ".claude" / "skills" / "a.ts").write_text("x", encoding="utf-8")
    (j.work / "src").mkdir(exist_ok=True)
    (j.work / "src" / "Main.tsx").write_text("export const A = 1", encoding="utf-8")
    return store, j


async def test_pages_files_and_studio_redirect(job):
    store, j = job
    site = Site(FakeManager(store), port=0)
    async with TestClient(TestServer(site.app)) as c:
        index = await (await c.get("/")).text()
        assert "хозяин: glados" in index and "Ролик про наблюдателя" in index
        story = await (await c.get(f"/job/{j.job_id}")).text()
        assert "Сценарий &lt;b&gt;№1" in story and "Remotion Studio" in story        # текст экранируется
        sheet = await (await c.get(f"/job/{j.job_id}?s=sheet")).text()
        assert "ПРОТОКОЛ" in sheet and "chime_up" in sheet and "0:30.00" in sheet
        files = await (await c.get(f"/job/{j.job_id}?s=files")).text()
        assert "work/src/Main.tsx" in files and "node_modules" not in files and ".claude" not in files
        view = await (await c.get(f"/job/{j.job_id}/view/work/src/Main.tsx")).text()
        assert "export const A" in view
        assert (await c.get(f"/job/{j.job_id}/raw/work/node_modules/x.js")).status == 404
        assert (await c.get(f"/job/{j.job_id}/raw/..%2Fjob.json")).status == 404
        assert (await c.get("/job/j-nope")).status == 404
        r = await c.get(f"/job/{j.job_id}/studio", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"].endswith(":8791")


def test_sheet_rows_sorted_by_time_and_safe_file(job):
    store, j = job
    plan = json.loads((j.plan_dir / "plan.json").read_text(encoding="utf-8"))
    assert [r["track"] for r in sheet_rows(plan)] == ["голос", "вставка", "звук"]
    assert safe_file(j.folder, "plan/../../x") is None and safe_file(j.folder, "plan/story.md") is not None


async def test_plan_and_story_pages_fall_back_to_json_when_the_agent_wrote_no_markdown(job):
    """Агент пишет plan.json и story.json; человеческий вид сайт строит сам, если plan.md и story.md нет."""
    from tests.stubs import STORY, make_plan
    store, j = job
    (j.plan_dir / "story.md").unlink()
    (j.plan_dir / "story.json").write_text(json.dumps(STORY, ensure_ascii=False), encoding="utf-8")
    (j.plan_dir / "plan.json").write_text(json.dumps(make_plan(), ensure_ascii=False), encoding="utf-8")
    site = Site(FakeManager(store), port=0)
    async with TestClient(TestServer(site.app)) as c:
        story = await (await c.get(f"/job/{j.job_id}")).text()
        assert "Логлайн" in story and "ГЛаДОС фиксирует нарушение" in story
        plan = await (await c.get(f"/job/{j.job_id}?s=plan")).text()
        assert "План монтажа" in plan and "Протокол нарушен." in plan
        (j.plan_dir / "plan.json").unlink()
        assert "Плана пока нет" in await (await c.get(f"/job/{j.job_id}?s=plan")).text()
