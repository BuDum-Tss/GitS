"""Инструменты окружения «Монтаж» (docs/WORKSPACE_CONTRACT.md, разделы 3, 4, 6): work, files в песочнице области, skills, run
(intake, transcribe, voiceover, render, still, compositions, check, final). Ядро, Remotion и контроль качества заглушки."""
import asyncio
import json

import pytest
from gits_sdk import ToolError

import qa as qa_mod
from tests.rig import Rig
from tests.stubs import STORY, FakeRenderer, StubQA, make_plan

pytestmark = pytest.mark.usefixtures("fast")


async def code_of(coro) -> str:
    with pytest.raises(ToolError) as e:
        await coro
    return e.value.code


# ---------------------------------------------------------------- work

async def test_work_create_list_status_cancel(tmp_path):
    rig = Rig(tmp_path)
    made = await rig.tool("work", {"op": "create", "brief": "Ролик про куб", "title": "Куб", "platform": "shorts"})
    jid = made["id"]
    assert jid.startswith("j-") and len(jid) == 10 and made["state"] == "new" and made["title"] == "Куб"
    assert made["paths"]["plan"] == f"{jid}/plan" and made["site"].endswith(f"/job/{jid}")
    job = rig.unit(jid)
    for sub in ("sources", "transcript", "plan", "work", "renders", "qa"):
        assert (job.folder / sub).is_dir()
    assert job.request["platform"] == "shorts" and job.request["format"] == "vertical"        # формат по площадке
    other = await rig.create(title="Второй")
    listed = (await rig.tool("work", {"op": "list"}))["result"]
    assert [u["id"] for u in listed] == [jid, other] and all(set(u) == {"id", "title", "state"} for u in listed)

    st = await rig.tool("work", {"op": "status", "id": jid})
    assert st["state"] == "new" and st["files"]["plan"] is False and st["check"]["fresh"] is False and st["runs"] == {}
    assert (await rig.tool("work", {"op": "status", "id": jid, "set": "waiting_user"}))["state"] == "waiting_user"
    assert await code_of(rig.tool("work", {"op": "status", "id": jid, "set": "done"})) == "invalid"

    res = await rig.tool("work", {"op": "cancel", "id": jid})
    assert res["state"] == "cancelled" and rig.unit(jid).state == "cancelled" and rig.unit(other).state == "new"
    assert await code_of(rig.run("check", jid)) == "unavailable"                         # в отменённой единице команд нет
    assert await code_of(rig.tool("work", {"op": "status", "id": "j-00000000"})) == "unknown"
    assert await code_of(rig.tool("work", {"op": "status", "id": "../x"})) == "invalid"
    assert await code_of(rig.tool("work", {"op": "close"})) == "invalid"


async def test_work_create_checks(tmp_path, monkeypatch):
    import types
    rig = Rig(tmp_path)
    assert await code_of(rig.tool("work", {"op": "create", "brief": "  "})) == "invalid"
    assert await code_of(rig.tool("work", {"op": "create", "brief": "х", "sources": [str(tmp_path / "нет.mp4")]})) == "invalid"
    assert await code_of(rig.tool("work", {"op": "create", "brief": "х", "sources": ["relative/path.mp4"]})) == "invalid"
    assert await code_of(rig.tool("work", {"op": "create", "brief": "х", "platform": "tiktok"})) == "invalid"
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 1000)
    monkeypatch.setattr("manager.shutil.disk_usage", lambda p: types.SimpleNamespace(free=3000, total=10 ** 9, used=0))
    assert await code_of(rig.tool("work", {"op": "create", "brief": "х", "sources": [str(src)], "source_kind": "generic"})) == "unavailable"
    monkeypatch.undo()
    rig.manager.layout = types.SimpleNamespace(missing_fonts=lambda: ["IBMPlexMono-Regular.ttf"], assets=rig.layout.assets)   # MON-24
    assert await code_of(rig.tool("work", {"op": "create", "brief": "х"})) == "unavailable"
    assert rig.manager.store.all() == []                                                  # ни одной единицы не создано


# ---------------------------------------------------------------- files: область и песочница

async def test_files_write_in_writable_and_deny_elsewhere(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    # writable: только три файла в plan/: идея, сценарий, монтажный лист (лист проверяется по схеме)
    plan_text = json.dumps(make_plan(voiceover=False), ensure_ascii=False)
    for path, text in ((f"{jid}/plan/idea.md", "Идея ролика."), (f"{jid}/plan/story.md", "Сцена 1."), (f"{jid}/plan/plan.json", plan_text)):
        res = await rig.files("write", path, content=text)
        assert res["ok"] and res["created"] is True
    assert (await rig.files("read", f"{jid}/plan/idea.md"))["content"] == "Идея ролика."
    rep = await rig.files("replace", f"{jid}/plan/idea.md", old="ролика", new="про куб")
    assert rep["replaced"] == 1
    # код ролика и любые другие имена в plan/ закрыты
    for path in (f"{jid}/work/src/custom/Mine.tsx", f"{jid}/work/src/Main.tsx", f"{jid}/work/public/vo/a.txt",
                 f"{jid}/plan/story.json", f"{jid}/plan/plan.md", f"{jid}/plan/other.txt"):
        assert await code_of(rig.files("write", path, content="x")) == "denied", path
    # неверный монтажный лист отвергается с понятным списком ошибок, прежний файл остаётся
    with pytest.raises(ToolError) as bad:
        await rig.files("write", f"{jid}/plan/plan.json", content='{"version": 2}')
    assert bad.value.code == "invalid" and "не принят" in bad.value.message and "- " in bad.value.message
    assert (await rig.files("read", f"{jid}/plan/plan.json"))["content"] == plan_text
    assert await code_of(rig.files("write", f"{jid}/plan/plan.json", content="не json")) == "invalid"
    assert await code_of(rig.files("replace", f"{jid}/plan/plan.json", old='"version": 1', new='"version": 7')) == "invalid"
    assert (await rig.files("read", f"{jid}/plan/plan.json"))["content"] == plan_text
    # readonly: sources, renders, qa, transcript, work, template, resources
    job = rig.unit(jid)
    for folder, name in ((job.sources, "sources"), (job.renders, "renders"), (job.qa, "qa"), (job.transcript, "transcript")):
        (folder / "x.txt").write_text("данные", encoding="utf-8")
        assert (await rig.files("read", f"{jid}/{name}/x.txt"))["content"] == "данные"
        assert await code_of(rig.files("write", f"{jid}/{name}/x.txt", content="взлом")) == "denied", name
        assert await code_of(rig.files("delete", f"{jid}/{name}/x.txt")) == "denied", name
    assert await code_of(rig.files("write", f"{jid}/work/package.json", content="{}")) == "denied"
    assert await code_of(rig.files("write", "template/src/Main.tsx", content="x")) == "denied"
    assert await code_of(rig.files("write", "resources/APERTURE_STYLE.md", content="x")) == "denied"
    assert await code_of(rig.files("write", f"{jid}/job.json", content="{}")) == "denied"             # служебное вне области
    assert await code_of(rig.files("read", f"{jid}/job.json")) == "denied"
    assert await code_of(rig.files("read", f"{jid}/log.jsonl")) == "denied"
    # выход за область
    for bad in ("../x.txt", f"{jid}/../../x", "/etc/passwd", "C:/Windows/win.ini", f"{jid}/plan/../../job.json"):
        assert await code_of(rig.files("read", bad)) == "denied", bad
    # скрытое
    assert await code_of(rig.files("read", f"{jid}/work/node_modules/zod/package.json")) == "denied"
    assert await code_of(rig.files("write", f"{jid}/work/src/.git/config", content="x")) == "denied"
    assert await code_of(rig.files("read", "template/node_modules/zod/package.json")) == "denied"
    assert await code_of(rig.files("read", "skills/montage-regulation/SKILL.md")) == "denied"      # навыки только через skills


async def test_files_read_template_and_resources_and_list_root(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    root = await rig.files("list", ".")
    names = [e["name"] for e in root["entries"]]
    assert "template" in names and "resources" in names and jid in names and "skills" not in names
    assert "node_modules" not in [e["name"] for e in (await rig.files("list", "template"))["entries"]]
    assert (await rig.files("read", "template/demo/plan.horizontal.json"))["content"].lstrip().startswith("{")
    assert await code_of(rig.files("read", "template/package.json")) == "denied"
    assert await code_of(rig.files("read", f"{jid}/work/src/schema.ts")) == "denied"
    work_entries = (await rig.files("list", f"{jid}/work"))["entries"]
    assert [entry["name"] for entry in work_entries] == ["public"]
    style = await rig.files("read", "resources/APERTURE_STYLE.md")
    assert style["size"] > 0 and style["content"]
    assert {e["name"] for e in (await rig.files("list", "resources"))["entries"]} >= {"assets", "design"}
    unit_view = {e["name"]: e["writable"] for e in (await rig.files("list", jid))["entries"]}
    assert unit_view["plan"] is False and unit_view["sources"] is False and "job.json" not in unit_view


async def test_files_follow_the_template_symlink_only_inside_the_area(tmp_path):
    """Ссылка внутри области на чужое место не открывает его."""
    rig = Rig(tmp_path)
    jid = await rig.create()
    secret = tmp_path / "secret.txt"
    secret.write_text("секрет", encoding="utf-8")
    link = rig.unit(jid).plan_dir / "leak.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("симлинки недоступны")
    assert await code_of(rig.files("read", f"{jid}/plan/leak.txt")) == "denied"


async def test_files_delete_and_move_need_consent_check(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    await rig.files("write", f"{jid}/plan/idea.md", content="a")
    moved = await rig.files("move", f"{jid}/plan/idea.md", dest=f"{jid}/plan/story.md")
    assert moved["path"] == f"{jid}/plan/story.md" and (rig.unit(jid).plan_dir / "story.md").is_file()
    assert await code_of(rig.files("move", f"{jid}/plan/story.md", dest=f"{jid}/plan/b.md")) == "denied"   # чужое имя в plan/
    assert await rig.manager.tools.check_files({"op": "delete", "path": f"{jid}/plan/story.md"})         # ядро спросит согласие
    assert await rig.manager.tools.check_files({"op": "read", "path": "x"}) is None
    assert (await rig.files("delete", f"{jid}/plan/story.md"))["deleted"] is True
    assert await code_of(rig.files("move", f"{jid}/sources/x", dest=f"{jid}/plan/x")) == "denied"


# ---------------------------------------------------------------- skills

async def test_skills_list_and_read_by_parts(tmp_path):
    rig = Rig(tmp_path)
    listed = (await rig.tool("skills"))["result"]
    names = {s["name"] for s in listed}
    assert {"montage-regulation", "remotion-markup", "remotion-render"} <= names and all(s["description"] for s in listed)
    reg = await rig.tool("skills", {"name": "montage-regulation"})
    assert reg["ok"] and "Регламент монтажа" in reg["content"] and reg["skill"] == "montage-regulation"
    craft = await rig.tool("skills", {"name": "montage-regulation", "file": "craft.md"})
    assert "Ремесло монтажа" in craft["content"]
    assert (await rig.tool("skills", {"name": "montage-regulation", "file": "qa-rubric.md"}))["ok"]
    assert await code_of(rig.tool("skills", {"name": "нет-такого"})) == "unknown"
    with pytest.raises(ToolError) as near:                                  # неточное имя: точные и похожие имена
        await rig.tool("skills", {"name": "remotion"})
    assert near.value.code == "unknown" and "Точные имена" in near.value.message and "Похожие: remotion-best-practices" in near.value.message
    assert "remotion-create" in near.value.message
    assert await code_of(rig.tool("skills", {"name": "montage-regulation", "file": "../../remotion-markup/SKILL.md"})) == "denied"
    assert await code_of(rig.tool("skills", {"name": "montage-regulation", "file": "нет.md"})) == "unknown"


# ---------------------------------------------------------------- run: список команд и аргументы

async def test_run_allowlist_and_arguments(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    assert await code_of(rig.run("rm", jid)) == "denied"                                  # не из списка
    assert await code_of(rig.run("npx", jid)) == "denied"
    assert await code_of(rig.run("check")) == "invalid"                                    # нет id единицы
    assert await code_of(rig.run("check", "../../etc")) == "invalid"
    assert await code_of(rig.run("check", "j-00000000")) == "unknown"
    assert await code_of(rig.run("check", jid, "--evil")) == "invalid"                     # лишних аргументов нет
    assert await code_of(rig.run("still", jid, "-1")) == "invalid"
    assert await code_of(rig.run("still", jid, "5", "6")) == "invalid"
    assert await code_of(rig.run("still", jid, "5")) == "unavailable"                      # плана ещё нет
    res = await rig.run("render", jid)                                                      # фон: отказ придёт итогом операции
    assert (await rig.wait_run(res["job_id"]))["error"]["code"] == "unavailable"


async def test_intake_transcribe_voiceover_on_stubs(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create(voiceover="character")
    # intake: единица без исходников (ролик на графике и закадре), фон
    res = await rig.run("intake", jid)
    assert res["background"] is True and res["job_id"].startswith("j-")
    last = await rig.wait_run(res["job_id"])
    assert last["state"] == "done" and "исходники проверены" in last["text"]
    assert (rig.unit(jid).sources / "info.json").is_file() and rig.unit(jid).request["source_kind"] == "narrated"
    assert rig.unit(jid).state == "working" and rig.unit(jid).request["format"] == "vertical"
    # transcribe: для narrated слов нет, файлы записаны
    out = await rig.run("transcribe", jid)
    assert out["background"] is False and out["result"]["words"] == 0
    assert json.loads((rig.unit(jid).transcript / "words.json").read_text(encoding="utf-8")) == []
    # voiceover: реплики её голосом (core.tts) -> wav, длительности в плане, кэш
    await rig.write_plan(jid, make_plan(), STORY)
    res = await rig.run("voiceover", jid)
    last = await rig.wait_run(res["job_id"])
    assert last["state"] == "done" and len(rig.core.tts_calls) == 2 and "заново 2" in last["text"]
    job = rig.unit(jid)
    plan = json.loads((job.plan_dir / "plan.json").read_text(encoding="utf-8"))
    assert all(v["duration_ms"] for v in plan["voiceover"])
    assert (job.work / "public" / "vo" / "vo1.wav").is_file() and (job.work / "public" / "vo" / "vo2.wav").is_file()
    cache = json.loads((job.plan_dir / "voiceover_cache.json").read_text(encoding="utf-8"))
    assert set(cache) == {"vo1", "vo2"} and cache["vo1"]["duration_ms"] == plan["voiceover"][0]["duration_ms"]
    assert (job.plan_dir / "voiceover_fit.json").is_file()
    again = await rig.run("voiceover", jid)
    assert "заново 0" in (await rig.wait_run(again["job_id"]))["text"] and len(rig.core.tts_calls) == 2      # кэш: заново не синтезирует
    plan["voiceover"][0]["text"] = "Протокол нарушен повторно."
    await rig.write_plan(jid, plan)
    third = await rig.run("voiceover", jid)
    assert "заново 1" in (await rig.wait_run(third["job_id"]))["text"] and len(rig.core.tts_calls) == 3     # изменился один текст
    for event in rig.events("montage.done"):
        assert event["text"].startswith("Монтаж «") and event["data"]["job_id"] == jid


async def test_voiceover_without_plan_fails_in_the_background_with_failed_event(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    res = await rig.run("voiceover", jid)
    last = await rig.wait_run(res["job_id"])
    assert last["state"] == "failed" and last["error"]["code"] == "unavailable" and "плана" in last["error"]["message"]
    failed = rig.events("montage.failed")
    assert failed and failed[0]["text"].startswith("Монтаж «") and "не удался" in failed[0]["text"] and failed[0]["urgency_hint"] == 3
    assert rig.unit(jid).state == "failed" and rig.unit(jid).data["runs"]["voiceover"]["status"] == "failed"
    await rig.wait_run((await rig.run("intake", jid))["job_id"])                            # новая операция снова открывает единицу
    assert rig.unit(jid).state == "working" and rig.unit(jid).data["error"] is None


# ---------------------------------------------------------------- render, still, compositions

async def test_render_in_background_reports_job_updates_and_done_event(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    await rig.write_plan(jid, make_plan(voiceover=False), STORY)
    res = await rig.run("render", jid)
    assert res["background"] is True
    last = await rig.wait_run(res["job_id"])
    run_id = res["job_id"]
    assert last["state"] == "done" and last["progress"] == 1.0
    states = rig.states_of(run_id)
    assert states[0] == "queued" and "running" in states and states[-1] == "done"
    assert any(u["job_id"] == run_id and 0 < u.get("progress", 0) <= 1 and "черновик" in (u.get("text") or "") for u in rig.core.updates)
    assert [u for u in rig.core.updates if u["job_id"] == run_id][0]["title"].startswith("Черновик: ")
    assert (rig.unit(jid).renders / "preview.mp4").is_file()
    assert rig.renderer.renders == [{"scale": 0.5, "crf": 28, "out": "preview.mp4"}]
    done = rig.events("montage.done")
    assert len(done) == 1 and "черновик отрендерен" in done[0]["text"] and done[0]["data"]["run"] == run_id
    assert rig.manager.active_jobs() == 0


async def test_one_exclusive_operation_per_unit_and_cancel(tmp_path):
    renderer = FakeRenderer()
    renderer.block = asyncio.Event()                                                        # рендер «висит»
    rig = Rig(tmp_path, renderer=renderer)
    jid = await rig.create()
    await rig.write_plan(jid, make_plan(voiceover=False), STORY)
    res = await rig.run("render", jid)
    await asyncio.sleep(0.1)
    assert await code_of(rig.run("render", jid)) == "busy" and await code_of(rig.run("check", jid)) == "busy"
    assert rig.manager.active_jobs() == 1 and (await rig.tool("work", {"op": "status", "id": jid}))["running"][0]["job_id"] == res["job_id"]
    # отмена операции (job.cancel ядра)
    await rig.manager.cancel(res["job_id"])
    last = await rig.wait_run(res["job_id"])
    assert last["state"] == "cancelled" and rig.manager.active_jobs() == 0
    assert rig.unit(jid).data["runs"]["render"]["status"] == "cancelled" and rig.unit(jid).state == "working"
    # отмена единицы останавливает операции и закрывает её
    res2 = await rig.run("render", jid)
    await asyncio.sleep(0.1)
    await rig.tool("work", {"op": "cancel", "id": jid})
    assert (await rig.wait_run(res2["job_id"]))["state"] == "cancelled" and rig.unit(jid).state == "cancelled"
    renderer.block.set()


async def test_still_and_compositions_are_quick_foreground_commands(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    await rig.write_plan(jid, make_plan(voiceover=False, duration=3000), STORY)
    shot = await rig.run("still", jid, "45")
    assert shot["background"] is False and shot["result"]["path"] == "renders/still_45.png" and rig.renderer.stills == [45]
    assert (rig.unit(jid).renders / "still_45.png").is_file()
    assert await code_of(rig.run("still", jid, "9999")) == "invalid"                        # за пределами ролика
    comps = await rig.run("compositions", jid)
    assert comps["result"]["compositions"] and comps["result"]["compositions"][0].startswith("Main")


# ---------------------------------------------------------------- check и final

async def test_final_refuses_without_a_fresh_passed_check_and_passes_after_it(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create(voiceover="character")
    await rig.write_plan(jid, make_plan(), STORY)
    # без проверки: отказ (в фоне итог failed; необратимая команда ядром всё равно спрашивается заранее)
    assert await rig.manager.tools.check_run({"command": "final"}) and await rig.manager.tools.check_run({"command": "render"}) is None
    res = await rig.run("final", jid)
    last = await rig.wait_run(res["job_id"])
    assert last["state"] == "failed" and last["error"]["code"] == "unavailable" and "check" in last["error"]["message"]
    assert rig.renderer.renders == [] and not (tmp_path / "out").exists()
    # проверка без озвучки не проходит: нет файлов закадра
    chk = (await rig.run("check", jid))["result"]
    assert chk["passed"] is False and any("vo1" in p for p in chk["problems"])
    await rig.wait_run((await rig.run("voiceover", jid))["job_id"])                         # озвучка меняет план: проверка нужна заново
    chk = (await rig.run("check", jid))["result"]
    assert chk["passed"] is True and chk["errors"] == 0 and chk["report"] == "qa/report.md"
    assert (rig.unit(jid).qa / "report.md").is_file() and (rig.unit(jid).qa / "report.json").is_file()
    st = (await rig.tool("work", {"op": "status", "id": jid}))["check"]
    assert st["fresh"] is True and st["passed"] is True
    # правка плана делает проверку несвежей
    plan = json.loads((rig.unit(jid).plan_dir / "plan.json").read_text(encoding="utf-8"))
    plan["title"] = "Другое название"
    await rig.write_plan(jid, plan)
    assert (await rig.tool("work", {"op": "status", "id": jid}))["check"]["fresh"] is False
    res = await rig.run("final", jid)
    assert "изменён после" in (await rig.wait_run(res["job_id"]))["error"]["message"]
    # снова проверка: final проходит
    assert (await rig.run("check", jid))["result"]["passed"] is True
    res = await rig.run("final", jid)
    last = await rig.wait_run(res["job_id"])
    assert last["state"] == "done" and last["progress"] == 1.0
    out = tmp_path / "out" / "final_shorts.mp4"
    assert out.is_file() and rig.unit(jid).state == "done" and rig.unit(jid).data["output"] == str(out)
    assert any(r["out"] == "final_raw.mp4" and r["scale"] is None and r["crf"] == 16 for r in rig.renderer.renders)
    launch = rig.events("montage.launch")
    assert len(launch) == 1 and launch[0]["announce"] is True and "финальный рендер" in launch[0]["text"]
    done = [e for e in rig.events("montage.done") if e["data"].get("path")]
    assert len(done) == 1 and done[0]["text"] == f"Монтаж «{rig.unit(jid).data['title']}» закончен, файл: {out}."
    assert done[0]["urgency_hint"] == 3 and [c["draft"] for c in rig.qa.calls] == [False]          # проверка готового файла
    assert any(a["kind"] == "output" for a in last["artifacts"])


async def test_check_reports_plan_problems_and_blocks_final(tmp_path):
    rig = Rig(tmp_path)
    jid = await rig.create()
    plan = make_plan(voiceover=False)
    plan["inserts"][0]["kind"] = "нет_такой_вставки"
    await rig.write_plan(jid, plan, STORY)
    chk = (await rig.run("check", jid))["result"]
    assert chk["passed"] is False and chk["errors"] >= 1 and chk["problems"]
    assert "остались замечания" in (rig.unit(jid).qa / "report.md").read_text(encoding="utf-8")
    assert rig.unit(jid).data["check"]["passed"] is False
    res = await rig.run("final", jid)
    assert "не пройдена" in (await rig.wait_run(res["job_id"]))["error"]["message"]
    assert await code_of(rig.run("check", "j-00000000")) == "unknown"
    # план не JSON
    (rig.unit(jid).plan_dir / "plan.json").write_text("не json", encoding="utf-8")                 # агент так записать не может: кладём как система
    assert (await rig.run("check", jid))["result"]["passed"] is False


async def test_check_includes_frames_of_a_fresh_preview_and_warns_about_a_missing_one(tmp_path):
    finding = qa_mod.Finding("blackdetect", "error", "чёрный кадр 1.0 с вне затемнения и переходов", 1000)
    rig = Rig(tmp_path, qa=StubQA([[finding], []]))
    jid = await rig.create()
    await rig.write_plan(jid, make_plan(voiceover=False))
    first = (await rig.run("check", jid))["result"]
    assert first["passed"] is True and first["warnings"] >= 1 and rig.qa.calls == []              # черновика нет: только предупреждение
    assert any("черновика нет" in w for w in first["warnings_list"])
    await rig.wait_run((await rig.run("render", jid))["job_id"])
    bad = (await rig.run("check", jid))["result"]                                                  # теперь проверяются кадры и звук черновика
    assert bad["passed"] is False and bad["problems"] == [finding.line()] and rig.qa.calls == [{"draft": True, "video": "preview.mp4"}]
    ok = (await rig.run("check", jid))["result"]
    assert ok["passed"] is True and ok["errors"] == 0
