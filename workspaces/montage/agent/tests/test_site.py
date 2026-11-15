"""Сайт монтажа: страницы задачи, монтажный лист, защита путей, переход в Studio."""
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from area import MontageSandbox, MontageTools
from jobs import JobStore
from montage_site import Site, safe_file, sheet_rows


class FakeManager:
    def __init__(self, store, owner="glados"):
        self.store = store
        self.layout = SimpleNamespace(owner=owner, character=store.dir / "character")
        self.tools = MontageTools(MontageSandbox(store.dir, {}, readonly=["*/plan", "*/renders"], hidden=["node_modules", ".claude"]))

    async def open_studio(self, job_id):
        return {"url": "http://127.0.0.1:8791", "job_id": job_id}


async def test_site_stays_available_without_lease(job):
    store, j = job
    site = Site(None, port=0, persistent=True)
    async with TestClient(TestServer(site.app)) as c:
        idle = await (await c.get("/")).text()
        assert "никто не использует" in idle and "montage-ui.css" in idle
        assert "cdn." not in idle and "daisyui" not in idle              # сайт работает офлайн
        css = await c.get("/static/montage-ui.css")
        assert css.status == 200 and "--background" in await css.text() and ".dark" in await css.text()
        assert (await c.get("/static/montage-ui.js")).status == 200
        site.manager = FakeManager(store)
        assert (await c.get("/static/other.js")).status == 404
        assert j.job_id in await (await c.get("/")).text()
        await site.stop()
        site.manager = None
        idle_job = await (await c.get(f"/job/{j.job_id}")).text()
        assert "никто не использует" in idle_job and j.job_id not in idle_job


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
        story = await (await c.get(f"/job/{j.job_id}?s=story")).text()
        assert "Сценарий &lt;b&gt;№1" in story and "Remotion Studio" in story        # текст экранируется
        main = await (await c.get(f"/job/{j.job_id}")).text()
        assert all(t in main for t in ("Монтажный лист", "Действия", "Файлы", "Дизайн", "Задачи"))
        assert f"<iframe src='/job/{j.job_id}/studio'" in main
        assert all(f"data-resize='{side}'" in main for side in ("left", "right", "bottom"))
        assert "data-panel='sheet' aria-controls='panel-sheet' aria-expanded='true'" in main
        assert "id='theme-toggle'" in main and "classList.toggle('dark'" in main and "data-fullscreen='#editor-studio'" in main
        assert main.index("id='panel-files'") < main.index("data-panel-group='bottom'")      # рельса нижней панели под панелью
        assert "Агент ещё не завёл план действий" in main and "Сейчас:" in main
        sheet = await (await c.get(f"/job/{j.job_id}?s=sheet")).text()
        assert "ПРОТОКОЛ" in sheet and "Материалы" in sheet and "chime_up" in sheet and "0:30.00" in sheet
        files = await (await c.get(f"/job/{j.job_id}?s=files")).text()
        assert "id='agent-files'" in files
        params = {"path": f"{j.job_id}/plan"}
        tree = await (await c.get("/workspace-files", params=params)).json()
        assert tree == site.manager.tools._op_list({"path": params["path"]})
        assert {entry["name"] for entry in tree["entries"]} == {"story.md", "plan.json"}
        assert (await c.get("/workspace-file", params={"path": f"{j.job_id}/work/src/Main.tsx"})).status == 404
        assert (await c.get("/workspace-file", params={"path": f"{j.job_id}/plan/story.md", "raw": "1"})).status == 200
        view = await (await c.get(f"/job/{j.job_id}/view/work/src/Main.tsx")).text()
        assert "export const A" in view
        assert (await c.get(f"/job/{j.job_id}/raw/work/node_modules/x.js")).status == 404
        assert (await c.get(f"/job/{j.job_id}/raw/..%2Fjob.json")).status == 404
        assert (await c.get("/job/j-nope")).status == 404
        r = await c.get(f"/job/{j.job_id}/studio", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"].endswith(":8791")


async def test_visual_preview_and_isolated_html(job):
    store, j = job
    manager = FakeManager(store)
    design = manager.layout.character / "design"
    design.mkdir(parents=True)
    (design / "preview.html").write_text('<link rel="stylesheet" href="style.css">', encoding="utf-8")
    (design / "style.css").write_text("body { color: red }", encoding="utf-8")
    (design / "heading.ttf").write_bytes(b"font")
    (design / "design-system.json").write_text("{}", encoding="utf-8")
    (design / "icon.svg").write_text("<svg/>", encoding="utf-8")
    intro = design / "components" / "Intro"
    intro.mkdir(parents=True)
    (intro / "README.md").write_text("Описание заставки", encoding="utf-8")
    (intro / "preview.html").write_text('<video src="/_blob/529b6ede200adc229d8cc149e3962526"></video>', encoding="utf-8")
    site = Site(manager, port=0)
    async with TestClient(TestServer(site.app)) as c:
        url = f"/job/{j.job_id}/view/design"
        files = await (await c.get(f"/job/{j.job_id}?s=files")).text()
        assert "id='panel-design'" in files and "Библиотека Aperture" in files
        assert f"{url}/design-system.json" in files
        assert "<th>Название</th><th>Описание</th><th>Просмотр</th>" in files
        assert "class='inline-preview'><iframe" in files
        assert "data-preview='/design/components/Intro/preview.html'" in files and "id='preview-dialog'" in files
        assert "Описание заставки" in files and "/Intro/README.md" not in files
        page = await (await c.get(f"{url}/preview.html")).text()
        assert "sandbox='allow-scripts'" in page and "allow-same-origin" not in page
        assert "aria-current='page'" in page and "/design/heading.ttf" in page
        assert "<img " in await (await c.get(f"{url}/icon.svg")).text()
        assert "PreviewFont" in await (await c.get(f"{url}/heading.ttf")).text()
        raw = await c.get("/design/preview.html")
        assert "sandbox allow-scripts" in raw.headers["Content-Security-Policy"]
        assert "style.css" in await raw.text()
        assert "color: red" in await (await c.get("/design/style.css")).text()
        intro_html = await (await c.get("/design/components/Intro/preview.html")).text()
        assert "/design/components/bundle.css" in intro_html
        assert "/design/assets/Intro/Aperture_Boot_dark.mp4" in intro_html
        assert "id=scene" in intro_html and "ResizeObserver" in intro_html and "scale(" in intro_html      # сцена вписывается в окно
        assert (await c.get("/design/..%2Fsecret.txt")).status == 404


def test_sheet_rows_sorted_by_time_and_safe_file(job):
    store, j = job
    plan = json.loads((j.plan_dir / "plan.json").read_text(encoding="utf-8"))
    assert [r["track"] for r in sheet_rows(plan)] == ["голос", "картинка", "звук"]
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
        story = await (await c.get(f"/job/{j.job_id}?s=story")).text()
        assert "Логлайн" in story and "ГЛаДОС фиксирует нарушение" in story
        sheet = await (await c.get(f"/job/{j.job_id}?s=sheet")).text()
        assert "Монтажного листа" not in sheet or "Протокол нарушен." in sheet
        (j.plan_dir / "plan.json").unlink()
        assert "Монтажного листа пока нет" in await (await c.get(f"/job/{j.job_id}?s=sheet")).text()
        (j.plan_dir / "idea.md").write_text("Идея <i>куба</i>", encoding="utf-8")
        assert "Идея &lt;i&gt;куба" in await (await c.get(f"/job/{j.job_id}?s=idea")).text()
