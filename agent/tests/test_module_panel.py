"""Пространство модуля на стороне ядра (TZ 15): поле panel доходит до реестра и /api/modules, неверный panel не
отклоняет модуль (TAB-1); блоки panel.set хранятся, лимиты и отказы, действия (TAB-8); раздача файлов артефактов и
отказы на пути и симлинки (TAB-3). Тесты не открывают проводник: reveal подменяется."""
import asyncio
import contextlib
import logging
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from hub_helpers import RawModule, manifest, running_core, tool

import dashboard
import module_panel
from glados_module import protocol as P

PANEL = {
    "title": "Демо",
    "icon": "box",
    "order": 40,
    "links": [
        {"id": "open", "label": "Открыть", "kind": "tool", "tool": "open", "args_from": "job",
         "open_result_url": True},
        {"id": "docs", "label": "Документы", "kind": "url", "url": "https://example.com/docs"},
    ],
    "actions": ["approve"],
}


def paneled(panel=PANEL, tools=None, **extra):
    return manifest("paneled", tools=tools if tools is not None else [tool("open")], panel=panel, **extra)


def blocks_text(text):
    return [{"type": "text", "text": text}]


@contextlib.asynccontextmanager
async def connected(core, module=None):
    raw = RawModule(core, module or paneled())
    await raw.hello()
    try:
        yield raw
    finally:
        await raw.close()


@contextlib.asynccontextmanager
async def panel_http(core):
    """Дашборд на тестовом сервере: только маршруты пространства и раздачи файлов."""
    dashboard.set_modules(core.modules, core.hub, None)
    app = web.Application()
    app.router.add_get("/api/modules/{id}/panel", dashboard.handle_api_module_panel)
    app.router.add_post("/api/modules/{id}/panel/action", dashboard.handle_api_module_panel_action)
    app.router.add_post("/api/modules/{id}/panel/link", dashboard.handle_api_module_panel_link)
    app.router.add_get("/api/artifact", dashboard.handle_api_artifact)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        dashboard.set_modules(None, None, None)


# ---------------------------------------------------------------- TAB-1: поле panel доходит до ядра

async def test_valid_panel_reaches_registry_and_snapshot(tmp_path):
    async with running_core(tmp_path) as core:
        async with connected(core):
            pass
        rec = core.modules.get("paneled")
        assert rec.panel["title"] == "Демо" and rec.panel["order"] == 40
        assert [link["id"] for link in rec.panel["links"]] == ["open", "docs"]
        row = next(r for r in core.modules.snapshot() if r["id"] == "paneled")
        assert row["panel"]["actions"] == ["approve"]


async def test_module_without_panel_has_none(tmp_path):
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest("plain"))
        try:
            await raw.hello()
        finally:
            await raw.close()
        row = next(r for r in core.modules.snapshot() if r["id"] == "plain")
        assert row["panel"] is None


async def test_bad_panel_connects_and_is_logged(tmp_path, caplog):
    """TAB-1: неверный panel не отклоняет модуль; проблемы попадают в журнал ядра, вкладки у модуля нет."""
    bad = dict(PANEL, links=[{"id": "x", "label": "x", "kind": "tool", "tool": "missing_tool"}])
    async with running_core(tmp_path) as core:
        raw = RawModule(core, paneled(bad))
        with caplog.at_level(logging.WARNING):
            try:
                welcome = await raw.hello()
            finally:
                await raw.close()
        assert welcome.type == "welcome"
        assert core.modules.get("paneled").panel is None
        assert any("пространство модуля отброшено" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- TAB-8: panel.set в ядре

async def test_panel_set_is_stored_and_served(tmp_path):
    async with running_core(tmp_path) as core:
        async with connected(core) as raw:
            await raw.send("panel.set", {"blocks": blocks_text("привет")})
            await core.wait_for(lambda: core.modules.get("paneled").panel_blocks)
            async with panel_http(core) as http:
                resp = await http.get("/api/modules/paneled/panel")
                body = await resp.json()
    assert resp.status == 200
    assert body["blocks"] == blocks_text("привет") and body["connected"] is True
    assert body["panel"]["title"] == "Демо"


async def test_panel_set_invalid_is_dropped(tmp_path, caplog):
    async with running_core(tmp_path) as core:
        async with connected(core) as raw:
            with caplog.at_level(logging.WARNING):
                await raw.send("panel.set", {"blocks": [{"type": "html", "text": "<img src=x onerror=1>"}]})
                await core.wait_for(lambda: any("panel.set неверен" in r.getMessage() for r in caplog.records))
    assert core.modules.get("paneled").panel_blocks is None


async def test_panel_set_is_not_kept_for_module_without_panel(tmp_path):
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest("plain"))
        try:
            await raw.hello()
            await raw.send("panel.set", {"blocks": blocks_text("x")})
            await asyncio.sleep(0.2)
        finally:
            await raw.close()
    assert core.modules.get("plain").panel_blocks is None


async def test_panel_set_rate_limit_drops_extra(tmp_path):
    """Не чаще раза в секунду на модуль: второе сообщение сразу за первым отбрасывается."""
    async with running_core(tmp_path) as core:
        async with connected(core) as raw:
            await raw.send("panel.set", {"blocks": blocks_text("первое")})
            await core.wait_for(lambda: core.modules.get("paneled").panel_blocks)
            await raw.send("panel.set", {"blocks": blocks_text("второе")})
            await asyncio.sleep(0.2)
    assert core.modules.get("paneled").panel_blocks["blocks"] == blocks_text("первое")


async def test_panel_set_after_interval_is_kept(tmp_path):
    async with running_core(tmp_path) as core:
        async with connected(core) as raw:
            await raw.send("panel.set", {"blocks": blocks_text("первое")})
            await core.wait_for(lambda: core.modules.get("paneled").panel_blocks)
            core.modules.get("paneled").panel_blocks["ts"] -= 5          # прошло достаточно времени
            await raw.send("panel.set", {"blocks": blocks_text("второе")})
            await core.wait_for(lambda: core.modules.get("paneled").panel_blocks["blocks"] == blocks_text("второе"))


# ---------------------------------------------------------------- TAB-8: действия и ссылки

async def test_panel_action_round_trip_and_open_url_check(tmp_path):
    async with running_core(tmp_path) as core:
        async with connected(core) as raw:
            seen = []

            def approve(body):
                seen.append(body)
                return {"ok": True, "message": "одобрено", "open_url": "http://127.0.0.1:3000/studio"}

            raw.handlers["panel.action"] = approve
            async with panel_http(core) as http:
                resp = await http.post("/api/modules/paneled/panel/action", json={"action_id": "approve",
                                                                                 "args": {"job_id": "j-1a2b3c4d"}})
                body = await resp.json()
            assert resp.status == 200 and body == {"ok": True, "message": "одобрено",
                                                   "open_url": "http://127.0.0.1:3000/studio"}
            assert seen == [{"action_id": "approve", "args": {"job_id": "j-1a2b3c4d"}}]


async def test_panel_action_foreign_host_is_not_opened(tmp_path):
    async with running_core(tmp_path) as core:
        async with connected(core) as raw:
            raw.handlers["panel.action"] = lambda body: {"ok": True, "open_url": "https://evil.example/x"}
            async with panel_http(core) as http:
                resp = await http.post("/api/modules/paneled/panel/action", json={"action_id": "approve"})
                body = await resp.json()
    assert "open_url" not in body and "не открыт" in body["message"]


async def test_panel_action_must_be_declared_and_module_connected(tmp_path):
    async with running_core(tmp_path) as core:
        async with panel_http(core) as http:
            resp = await http.post("/api/modules/nobody/panel/action", json={"action_id": "approve"})
            assert resp.status == 404                             # такого модуля нет
        async with connected(core) as raw:
            raw.handlers["panel.action"] = lambda body: {"ok": True}
            async with panel_http(core) as http:
                resp = await http.post("/api/modules/paneled/panel/action", json={"action_id": "nope"})
                assert resp.status == 400                         # действие не объявлено в panel.actions
                resp = await http.post("/api/modules/paneled/panel/action", json={"action_id": "approve",
                                                                                 "args": "не объект"})
                assert resp.status == 400
    await core.wait_for(lambda: core.hub.connection("paneled") is None)
    async with panel_http(core) as http:
        resp = await http.post("/api/modules/paneled/panel/action", json={"action_id": "approve"})
        assert resp.status == 409                                 # модуль объявлен, но не подключён


async def test_panel_action_refusal_is_an_answer(tmp_path):
    async with running_core(tmp_path) as core:
        async with connected(core) as raw:
            def refuse(body):
                raise RuntimeError("сбой в модуле")
            raw.handlers["panel.action"] = refuse
            async with panel_http(core) as http:
                resp = await http.post("/api/modules/paneled/panel/action", json={"action_id": "approve"})
                body = await resp.json()
    assert resp.status == 200 and body["ok"] is False and body["code"] == "internal"


async def test_tool_link_runs_auto_tool_and_returns_url(tmp_path):
    async with running_core(tmp_path) as core:
        async with connected(core) as raw:
            seen = []

            def open_tool(body):
                seen.append(body)
                return {"url": "http://127.0.0.1:3000/"}

            raw.handlers["tool.call"] = open_tool
            await raw.send("job.update", {"job_id": "j-1a2b3c4d", "state": "running"})
            await core.wait_for(lambda: "j-1a2b3c4d" in core.modules.get("paneled").jobs)
            async with panel_http(core) as http:
                resp = await http.post("/api/modules/paneled/panel/link",
                                       json={"link_id": "open", "job_id": "j-1a2b3c4d"})
                body = await resp.json()
    assert resp.status == 200 and body == {"ok": True, "open_url": "http://127.0.0.1:3000/"}
    assert seen == [{"tool": "open", "args": {"job_id": "j-1a2b3c4d"}}]


async def test_tool_link_needs_a_job_and_auto_risk(tmp_path):
    (tmp_path / "ask").mkdir()
    async with running_core(tmp_path / "ask") as core:
        async with connected(core, paneled(tools=[tool("open", risk="ask")])):
            async with panel_http(core) as http:
                resp = await http.post("/api/modules/paneled/panel/link",
                                       json={"link_id": "open", "job_id": "j-1a2b3c4d"})
                assert resp.status == 403
    (tmp_path / "nojob").mkdir()
    async with running_core(tmp_path / "nojob") as core:
        async with connected(core):
            async with panel_http(core) as http:
                resp = await http.post("/api/modules/paneled/panel/link", json={"link_id": "open"})
                assert resp.status == 400


async def test_url_link_returns_its_address_and_unknown_link_404(tmp_path):
    async with running_core(tmp_path) as core:
        async with connected(core):
            async with panel_http(core) as http:
                resp = await http.post("/api/modules/paneled/panel/link", json={"link_id": "docs"})
                assert (await resp.json()) == {"ok": True, "open_url": "https://example.com/docs"}
                resp = await http.post("/api/modules/paneled/panel/link", json={"link_id": "missing"})
                assert resp.status == 404


async def test_reveal_link_only_inside_serve_dirs(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    (jobs / "j-1a2b3c4d").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    opened: list[Path] = []
    monkeypatch.setattr(module_panel, "reveal", lambda path: opened.append(path))
    panel = {"serve_dirs": [str(jobs)], "links": [
        {"id": "folder", "label": "Папка", "kind": "reveal_path", "path": str(jobs)},
        {"id": "outside", "label": "Чужая", "kind": "reveal_path", "path": str(outside)},
        {"id": "state", "label": "Из состояния", "kind": "reveal_path", "path_from": "jobs_dir"}]}
    async with running_core(tmp_path / "core") as core:
        async with connected(core, paneled(panel)) as raw:
            await raw.send("state", {"summary": "ок", "data": {"jobs_dir": str(outside)}})
            await core.wait_for(lambda: core.modules.get("paneled").state)
            async with panel_http(core) as http:
                ok = await http.post("/api/modules/paneled/panel/link", json={"link_id": "folder"})
                assert ok.status == 200 and opened == [jobs.resolve()]
                no = await http.post("/api/modules/paneled/panel/link", json={"link_id": "outside"})
                assert no.status == 403
                no = await http.post("/api/modules/paneled/panel/link", json={"link_id": "state"})
                assert no.status == 403
    assert len(opened) == 1


# ---------------------------------------------------------------- TAB-3: раздача файлов артефактов

def _video(tmp_path):
    jobs = tmp_path / "jobs"
    job_dir = jobs / "j-1a2b3c4d"
    job_dir.mkdir(parents=True)
    video = job_dir / "preview.mp4"
    video.write_bytes(b"0123456789" * 10)
    secret = tmp_path / "secret.txt"
    secret.write_text("секрет", encoding="utf-8")
    return jobs, video, secret


async def _with_artifacts(core, raw, paths):
    await raw.send("job.update", {"job_id": "j-1a2b3c4d", "state": "done",
                                  "artifacts": [{"path": str(p), "kind": "file"} for p in paths]})
    await core.wait_for(lambda: "j-1a2b3c4d" in core.modules.get("paneled").jobs)


async def test_artifact_served_with_video_type_and_range(tmp_path):
    jobs, video, _ = _video(tmp_path)
    async with running_core(tmp_path / "core") as core:
        async with connected(core, paneled(dict(PANEL, serve_dirs=[str(jobs)]))) as raw:
            await _with_artifacts(core, raw, [video])
            async with panel_http(core) as http:
                resp = await http.get("/api/artifact", params={"module": "paneled", "job": "j-1a2b3c4d",
                                                              "name": "preview.mp4"})
                assert resp.status == 200 and resp.headers["Content-Type"] == "video/mp4"
                assert await resp.read() == video.read_bytes()
                ranged = await http.get("/api/artifact", params={"module": "paneled", "job": "j-1a2b3c4d",
                                                                "name": "preview.mp4"},
                                        headers={"Range": "bytes=0-3"})
                assert ranged.status == 206 and await ranged.read() == b"0123"


async def test_artifact_refusals(tmp_path):
    jobs, video, secret = _video(tmp_path)
    async with running_core(tmp_path / "core") as core:
        async with connected(core, paneled(dict(PANEL, serve_dirs=[str(jobs)]))) as raw:
            await _with_artifacts(core, raw, [video, secret])
            async with panel_http(core) as http:
                async def get(name, job="j-1a2b3c4d"):
                    return await http.get("/api/artifact", params={"module": "paneled", "job": job, "name": name})

                assert (await get("secret.txt")).status == 403              # есть у задачи, но вне serve_dirs
                assert (await get("preview.mp4", job="j-ffffffff")).status == 404
                assert (await get("missing.mp4")).status == 404
                assert (await get("../secret.txt")).status == 400           # `..` в имени
                assert (await get("dir/preview.mp4")).status == 400         # путь вместо имени
                assert (await get("preview.mp4", job="not-a-job")).status == 400
                resp = await http.get("/api/artifact", params={"module": "nobody", "job": "j-1a2b3c4d",
                                                              "name": "preview.mp4"})
                assert resp.status == 404


async def test_artifact_path_with_dotdot_is_refused(tmp_path):
    jobs, video, secret = _video(tmp_path)
    sneaky = jobs / ".." / "secret.txt"                                    # внутри «jobs», но через `..`
    async with running_core(tmp_path / "core") as core:
        async with connected(core, paneled(dict(PANEL, serve_dirs=[str(jobs)]))) as raw:
            await _with_artifacts(core, raw, [sneaky])
            async with panel_http(core) as http:
                resp = await http.get("/api/artifact", params={"module": "paneled", "job": "j-1a2b3c4d",
                                                              "name": "secret.txt"})
                assert resp.status == 403


async def test_artifact_symlink_to_outside_is_refused(tmp_path):
    jobs, video, secret = _video(tmp_path)
    link = jobs / "j-1a2b3c4d" / "link.txt"
    try:
        os.symlink(secret, link)
    except OSError:
        pytest.skip("нет прав на симлинки в этой системе")
    async with running_core(tmp_path / "core") as core:
        async with connected(core, paneled(dict(PANEL, serve_dirs=[str(jobs)]))) as raw:
            await _with_artifacts(core, raw, [link])
            async with panel_http(core) as http:
                resp = await http.get("/api/artifact", params={"module": "paneled", "job": "j-1a2b3c4d",
                                                              "name": "link.txt"})
                assert resp.status == 403


def test_served_file_helpers(tmp_path):
    jobs, video, secret = _video(tmp_path)
    assert module_panel.served_file([str(jobs)], str(video)) == video.resolve()
    assert module_panel.served_file([str(jobs)], str(secret)) is None
    assert module_panel.served_file([str(jobs)], "relative.mp4") is None
    assert module_panel.served_file([], str(video)) is None
    assert module_panel.served_dir([str(jobs)], str(jobs)) == jobs.resolve()
    assert module_panel.served_dir([str(jobs)], str(tmp_path)) is None
    assert module_panel.bad_name("a/b") and module_panel.bad_name("..") and not module_panel.bad_name("a.mp4")


def test_action_result_rules():
    hosts = ["example.com"]
    assert module_panel.action_result({"ok": True, "open_url": "http://localhost:3000"}, hosts)["open_url"]
    assert module_panel.action_result({"ok": True, "open_url": "https://example.com/a"}, hosts)["open_url"]
    assert "open_url" not in module_panel.action_result({"ok": True, "open_url": "file:///C:/x"}, hosts)
    assert module_panel.action_result("не объект", hosts) == {"ok": False, "message": "модуль вернул ответ не по форме"}


def test_panel_spec_has_serve_dirs_for_core():
    spec = P.Manifest.from_dict(paneled(dict(PANEL, serve_dirs=[os.path.abspath(".")])))
    assert spec.panel.serve_dirs
