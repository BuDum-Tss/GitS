"""Контракт 3.2: схема workspace (brief, skills, area, commands, site), песочница, WorkspaceTools, версии 3.1 и 3.2."""
import copy
import os
import re

import pytest

from gits_sdk import Sandbox, ToolError, WorkspaceTools
from gits_sdk import contract
from gits_sdk import protocol as P
from gits_sdk.contract import versions
from gits_sdk.protocol import Manifest, WorkspaceCommand, WorkspaceSkill, parse_workspace_spec
from helpers import MANIFEST

WS = {"id": "demo", "title": "Демо", "entry": ["user_message"],
      "brief": "Монтаж роликов.",
      "skills": [{"name": "remotion-markup", "description": "Remotion", "path": "skills/remotion-markup"}],
      "area": {"root": "work", "writable": ["jobs/*/work/src", "jobs/*/plan"], "readonly": ["template"], "hidden": ["node_modules", ".git"]},
      "commands": [{"id": "render", "title": "Рендер", "background": True, "timeout_s": 3600},
                   {"id": "still", "title": "Кадр", "timeout_s": 120},
                   {"id": "final", "title": "Финал", "background": True, "irreversible": True}],
      "site": {"url": "http://127.0.0.1:8790/"}}


def manifest(ws):
    return {**copy.deepcopy(MANIFEST), "workspace": ws}


# ---------------------------------------------------------------- схема workspace

def test_full_workspace_block_is_valid_and_parsed():
    warnings: list[str] = []
    assert P.validate_manifest(manifest(WS), warnings) == [] and warnings == []
    m = Manifest.from_dict(manifest(WS))
    spec = m.workspace_spec
    assert spec.brief == "Монтаж роликов." and spec.site_url == "http://127.0.0.1:8790/"
    assert spec.skills[0].name == "remotion-markup" and spec.area.root == "work"
    assert spec.command("final").irreversible and spec.command("render").background and spec.command("still").timeout_s == 120
    assert spec.to_dict() == {k: WS[k] for k in ("brief", "skills", "area", "commands", "site")}
    assert m.to_dict()["workspace"] == WS                      # манифест уходит как есть


def test_workspace_without_new_fields_has_no_spec():
    plain = {"id": "demo", "title": "Демо"}
    m = Manifest.from_dict(manifest(plain))
    assert m.workspace_spec is None and m.workspace_warnings == []
    assert parse_workspace_spec(plain) == (None, [])
    assert parse_workspace_spec({"brief": "x"})[0].to_dict() == {"brief": "x"}      # пустые поля не выводятся


@pytest.mark.parametrize("field, value", [
    ("brief", "а" * 401), ("brief", ""), ("brief", 5),
    ("skills", "нет"), ("skills", [{"name": "Bad Name", "description": "x", "path": "s"}]),
    ("skills", [{"name": "ok", "description": "", "path": "s"}]),
    ("skills", [{"name": "ok", "description": "x", "path": "../s"}]),
    ("skills", [{"name": "ok", "description": "x", "path": "/abs"}]),
    ("skills", [{"name": "a", "description": "x", "path": "s"}, {"name": "a", "description": "x", "path": "s"}]),
    ("skills", [{"name": "a", "description": "x", "path": "s"}] * 41),
    ("area", {"root": "../x"}), ("area", {"writable": ["a/../b"]}), ("area", {"readonly": ["C:/x"]}), ("area", {"hidden": [3]}),
    ("area", {"writable": "src"}), ("area", {"extra": []}), ("area", []),
    ("commands", [{"id": "Bad", "title": "x"}]), ("commands", [{"id": "ok"}]),
    ("commands", [{"id": "ok", "title": "x", "background": "да"}]), ("commands", [{"id": "ok", "title": "x", "timeout_s": 0}]),
    ("commands", [{"id": "ok", "title": "x", "irreversible": 1}]),
    ("commands", [{"id": "a", "title": "x"}, {"id": "a", "title": "y"}]),
    ("site", {"url": "http://example.com/"}), ("site", {"url": "ftp://127.0.0.1/"}), ("site", "http://127.0.0.1/"), ("site", {}),
])
def test_bad_new_fields_become_warnings_and_do_not_reject(field, value):
    ws = {**WS, field: value}
    warnings: list[str] = []
    assert P.validate_manifest(manifest(ws), warnings) == []               # модуль не отклонён
    assert any(f"workspace.{field}" in w for w in warnings), warnings
    m = Manifest.from_dict(manifest(ws))                                   # и разбирается: плохое поле отброшено, остальное цело
    assert any(f"workspace.{field}" in w for w in m.workspace_warnings)
    good = {k: WS[k] for k in ("brief", "skills", "area", "commands", "site") if k != field}
    assert m.workspace_spec.to_dict() == good


def test_no_warnings_list_means_no_extra_work():
    assert P.validate_manifest(manifest({**WS, "brief": 5})) == []


def test_old_workspace_fields_are_still_checked():
    assert any("workspace.entry" in p for p in P.validate_manifest(manifest({**WS, "entry": ["process_started"]})))
    assert any("неизвестные поля" in p for p in P.validate_manifest(manifest({**WS, "mood": "x"})))


# ---------------------------------------------------------------- песочница

@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "env"
    (root / "jobs/j1/work/src").mkdir(parents=True)
    (root / "jobs/j1/plan").mkdir()
    (root / "jobs/j1/out").mkdir()
    (root / "template").mkdir()
    (root / "node_modules/pkg").mkdir(parents=True)
    (root / "jobs/j1/work/src/a.txt").write_bytes("привет мир\nконец".encode())
    (root / "jobs/j1/out/final.mp4").write_bytes(b"\0\1\2video")
    (root / "template/t.txt").write_text("шаблон", encoding="utf-8")
    (root / "node_modules/pkg/i.js").write_text("x", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("секрет", encoding="utf-8")
    return root


@pytest.fixture
def sb(tree):
    return Sandbox(tree, writable=["jobs/*/work/src", "jobs/*/plan"], readonly=["template"], hidden=["node_modules", ".git"])


def test_sandbox_rejects_traversal_and_absolute(sb, tree):
    for bad in ("../secret.txt", "jobs/../../secret.txt", "..", str(tree / "template/t.txt"), "C:/Windows", "/etc/passwd", "a\0b"):
        with pytest.raises(ToolError) as e:
            sb.resolve(bad)
        assert e.value.code == "denied", bad
    with pytest.raises(ToolError) as e:
        sb.resolve(5)
    assert e.value.code == "invalid"


def test_sandbox_normalizes_and_checks_area(sb, tree):
    assert sb.resolve("template\\t.txt") == tree / "template" / "t.txt"
    assert sb.resolve("./jobs//j1/work/src/a.txt") == tree / "jobs/j1/work/src/a.txt"
    with pytest.raises(ToolError) as e:
        sb.resolve("jobs/j1/out/final.mp4")                       # вне area
    assert e.value.code == "denied"
    with pytest.raises(ToolError):
        sb.resolve(".")                                           # корень не разрешён без listing
    assert sb.resolve(".", listing=True) == tree.resolve()
    assert sb.resolve("jobs/j1", listing=True)                    # ступень к writable


def test_sandbox_hidden_and_readonly_write(sb):
    for p in ("node_modules/pkg/i.js", "jobs/j1/work/src/node_modules/x", ".git/config"):
        with pytest.raises(ToolError) as e:
            sb.resolve(p)
        assert e.value.code == "denied", p
    with pytest.raises(ToolError) as e:
        sb.resolve("template/t.txt", write=True)                  # readonly
    assert e.value.code == "denied"
    assert sb.resolve("jobs/j1/plan/p.json", write=True)
    with pytest.raises(ToolError):
        sb.resolve("jobs/j1/out/x", write=True)


def test_sandbox_symlink_out_of_area_is_denied(sb, tree, tmp_path):
    link = tree / "jobs/j1/work/src/out"
    try:
        os.symlink(tmp_path, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("симлинки недоступны")
    with pytest.raises(ToolError) as e:
        sb.resolve("jobs/j1/work/src/out/secret.txt")
    assert e.value.code == "denied"
    inside = tree / "jobs/j1/work/src/to_out"                      # симлинк в разрешённую, но не записываемую область
    os.symlink(tree / "template", inside, target_is_directory=True)
    assert sb.resolve("jobs/j1/work/src/to_out/t.txt")
    with pytest.raises(ToolError):
        sb.resolve("jobs/j1/work/src/to_out/t.txt", write=True)
    hid = tree / "jobs/j1/work/src/h"                              # симлинк на скрытое
    os.symlink(tree / "node_modules", hid, target_is_directory=True)
    with pytest.raises(ToolError):
        sb.resolve("jobs/j1/work/src/h/pkg/i.js")


def test_sandbox_without_lists_is_read_only_root(tree):
    only = Sandbox(tree)
    assert only.resolve("template/t.txt")
    with pytest.raises(ToolError):
        only.resolve("template/t.txt", write=True)


def test_sandbox_from_area(tree):
    spec = parse_workspace_spec({"area": {"root": "jobs/j1", "writable": ["plan"], "hidden": ["out"]}})[0]
    sb = Sandbox.from_area(tree, spec.area)
    assert sb.resolve("plan/p.json", write=True) == (tree / "jobs/j1/plan/p.json").resolve()
    with pytest.raises(ToolError):
        sb.resolve("out/final.mp4")


# ---------------------------------------------------------------- WorkspaceTools

COMMANDS = [WorkspaceCommand("render", "Рендер", background=True, timeout_s=3600), WorkspaceCommand("still", "Кадр", timeout_s=60),
            WorkspaceCommand("final", "Финал", background=True, irreversible=True)]


@pytest.fixture
def calls():
    return []


@pytest.fixture
def tools(sb, tree, calls):
    (tree / "skills/s1/ref").mkdir(parents=True)
    (tree / "skills/s1/SKILL.md").write_text("# Навык\n" + "я" * 100, encoding="utf-8")
    (tree / "skills/s1/ref/a.md").write_text("ссылка", encoding="utf-8")

    def executor(command, args, background, cwd):
        calls.append((command.id, args, background, cwd))
        return "j-0123abcd" if background else {"stdout": "ok"}

    return WorkspaceTools(sb, [WorkspaceSkill("s1", "Первый навык", "skills/s1")], COMMANDS, executor)


async def fail(coro, code):
    with pytest.raises(ToolError) as e:
        await coro
    assert e.value.code == code, (e.value.code, e.value.message)


async def test_files_list(tools):
    root = await tools.files({"op": "list"})
    assert [e["name"] for e in root["entries"]] == ["jobs", "template"]          # скрытое и вне области не показано
    src = await tools.files({"op": "list", "path": "jobs/j1"})
    assert [e["name"] for e in src["entries"]] == ["plan", "work"]                # out вне area
    files = await tools.files({"op": "list", "path": "jobs/j1/work/src"})
    assert files["entries"] == [{"name": "a.txt", "type": "file", "writable": True, "size": len("привет мир\nконец".encode())}]
    await fail(tools.files({"op": "list", "path": "nope"}), "denied")
    await fail(tools.files({"op": "list", "path": "jobs/j1/plan/none"}), "unknown")
    await fail(tools.files({"op": "list", "path": "template/t.txt"}), "invalid")


async def test_files_read_and_truncate(tools, tree):
    r = await tools.files({"op": "read", "path": "jobs/j1/work/src/a.txt"})
    assert r["content"] == "привет мир\nконец" and r["truncated"] is False and r["size"] > 0
    big = tree / "template/big.txt"
    big.write_text("x" * 40000, encoding="utf-8")
    r = await tools.files({"op": "read", "path": "template/big.txt"})
    assert r["truncated"] is True and r["size"] == 40000 and len(r["content"]) == 32 * 1024
    r = await tools.files({"op": "read", "path": "template/big.txt", "offset": 32768})
    assert r["truncated"] is False and len(r["content"]) == 40000 - 32768
    await fail(tools.files({"op": "read", "path": "template/missing"}), "unknown")
    await fail(tools.files({"op": "read", "path": "template"}), "invalid")
    await fail(tools.files({"op": "read", "path": "node_modules/pkg/i.js"}), "denied")
    await fail(tools.files({"op": "read", "path": "template/big.txt", "offset": -1}), "invalid")


async def test_files_read_binary_returns_metadata(tools, tree):
    (tree / "template/clip.mp4").write_bytes(b"\0\0\0\x18ftypmp42")
    r = await tools.files({"op": "read", "path": "template/clip.mp4"})
    assert r["binary"] is True and r["size"] == 12 and "content" not in r and r["type"] == "video/mp4"


async def test_files_write_replace_delete_move(tools, tree):
    w = await tools.files({"op": "write", "path": "jobs/j1/plan/new/p.json", "content": '{"a": 1}'})
    assert w["created"] is True and (tree / "jobs/j1/plan/new/p.json").read_text(encoding="utf-8") == '{"a": 1}'
    assert (await tools.files({"op": "write", "path": "jobs/j1/plan/new/p.json", "content": "2"}))["created"] is False
    await fail(tools.files({"op": "write", "path": "template/t.txt", "content": "x"}), "denied")          # readonly
    await fail(tools.files({"op": "write", "path": "jobs/j1/out/x", "content": "x"}), "denied")           # вне area
    await fail(tools.files({"op": "write", "path": "../x", "content": "x"}), "denied")
    await fail(tools.files({"op": "write", "path": "jobs/j1/plan", "content": "x"}), "invalid")           # каталог
    await fail(tools.files({"op": "write", "path": "jobs/j1/plan/q"}), "invalid")                        # нет content

    a = "jobs/j1/work/src/a.txt"
    assert (await tools.files({"op": "replace", "path": a, "old": "мир", "new": "свет"}))["replaced"] == 1
    assert (tree / a).read_text(encoding="utf-8") == "привет свет\nконец"
    await fail(tools.files({"op": "replace", "path": a, "old": "нет такого", "new": "x"}), "invalid")
    (tree / a).write_bytes(b"aa aa")
    await fail(tools.files({"op": "replace", "path": a, "old": "aa", "new": "b"}), "invalid")             # неоднозначно
    assert (await tools.files({"op": "replace", "path": a, "old": "aa", "new": "b", "all": True}))["replaced"] == 2
    await fail(tools.files({"op": "replace", "path": a, "old": "", "new": "b"}), "invalid")
    await fail(tools.files({"op": "replace", "path": "template/t.txt", "old": "ш", "new": "x"}), "denied")

    m = await tools.files({"op": "move", "path": a, "dest": "jobs/j1/work/src/sub/b.txt"})
    assert m["path"] == "jobs/j1/work/src/sub/b.txt" and not (tree / a).exists()
    await fail(tools.files({"op": "move", "path": "jobs/j1/work/src/sub/b.txt", "dest": "template/b.txt"}), "denied")
    await fail(tools.files({"op": "move", "path": "template/t.txt", "dest": "jobs/j1/plan/t.txt"}), "denied")
    await fail(tools.files({"op": "move", "path": "jobs/j1/work/src/none", "dest": "jobs/j1/plan/x"}), "unknown")

    await fail(tools.files({"op": "delete", "path": "template/t.txt"}), "denied")
    (tree / "jobs/j1/plan/d").mkdir()
    (tree / "jobs/j1/plan/d/f").write_text("1", encoding="utf-8")
    await fail(tools.files({"op": "delete", "path": "jobs/j1/plan/d"}), "invalid")                       # непустой без recursive
    assert (await tools.files({"op": "delete", "path": "jobs/j1/plan/d", "recursive": True}))["deleted"]
    assert (await tools.files({"op": "delete", "path": "jobs/j1/work/src/sub/b.txt"}))["deleted"]
    await fail(tools.files({"op": "delete", "path": "jobs/j1/work/src/sub/b.txt"}), "unknown")
    await fail(tools.files({"op": "delete", "path": "."}), "denied")
    await fail(tools.files({"op": "frob"}), "invalid")
    await fail(tools.files({}), "invalid")


async def test_skills(tools):
    assert (await tools.skills({}))["result"] == [{"name": "s1", "description": "Первый навык"}]
    assert (await tools.skills(None))["result"][0]["name"] == "s1"
    r = await tools.skills({"name": "s1"})
    assert r["content"].startswith("# Навык") and r["truncated"] is False
    assert (await tools.skills({"name": "s1", "file": "ref/a.md"}))["content"] == "ссылка"
    await fail(tools.skills({"name": "nope"}), "unknown")
    await fail(tools.skills({"name": "s1", "file": "missing.md"}), "unknown")
    await fail(tools.skills({"name": "s1", "file": "../../template/t.txt"}), "denied")
    await fail(tools.skills({"name": "s1", "file": "/etc/passwd"}), "denied")


async def test_run_allowlist_and_background(tools, calls, tree):
    r = await tools.run({"command": "still", "args": ["--frame", "10"]})
    assert r == {"ok": True, "command": "still", "background": False, "result": {"stdout": "ok"}}
    assert calls[-1] == ("still", ["--frame", "10"], False, tree.resolve())
    bg = await tools.run({"command": "render"})                      # background: true в манифесте
    assert bg == {"ok": True, "command": "render", "background": True, "job_id": "j-0123abcd"}
    assert calls[-1][2] is True
    assert (await tools.run({"command": "still", "background": True}))["job_id"] == "j-0123abcd"
    await fail(tools.run({"command": "rm"}), "denied")
    await fail(tools.run({"command": "still; rm -rf /"}), "denied")
    await fail(tools.run({}), "invalid")
    await fail(tools.run({"command": "still", "args": "x"}), "invalid")
    await fail(tools.run({"command": "still", "args": [1]}), "invalid")
    await fail(tools.run({"command": "still", "args": ["a\0"]}), "invalid")
    await fail(tools.run({"command": "still", "background": "да"}), "invalid")
    assert len(calls) == 3                                           # отказы до исполнителя не дошли


async def test_run_async_executor_errors_and_no_executor(sb):
    async def executor(command, args, background, cwd):
        if command.id == "render":
            raise ToolError("рендер уже идёт", code="busy")
        return {"job_id": "j-aaaabbbb"} if background else "готово"

    t = WorkspaceTools(sb, commands=COMMANDS, executor=executor)
    await fail(t.run({"command": "render"}), "busy")
    assert (await t.run({"command": "final"}))["job_id"] == "j-aaaabbbb"
    assert (await t.run({"command": "still"}))["result"] == "готово"
    await fail(WorkspaceTools(sb, commands=COMMANDS).run({"command": "still"}), "unavailable")
    bad = WorkspaceTools(sb, commands=COMMANDS, executor=lambda *a: None)
    await fail(bad.run({"command": "render"}), "unavailable")        # фон без job_id


async def test_danger_checks(tools):
    assert "нельзя отменить" in await tools.check_files({"op": "delete", "path": "jobs/j1/plan/p"})
    assert await tools.check_files({"op": "write", "path": "x"}) is None
    assert "необратима" in await tools.check_run({"command": "final"})
    assert await tools.check_run({"command": "render"}) is None
    assert await tools.check_run({"command": "unknown"}) is None


def test_declared_tools_fit_the_manifest(tools):
    declared = tools.declare_tools()
    assert [t["name"] for t in declared] == ["skills", "files", "run"]
    assert all(t["risk"] == "auto" for t in declared)
    by = {t["name"]: t for t in declared}
    assert by["files"]["danger_check"] is True and by["run"]["danger_check"] is True       # есть необратимая команда
    assert by["run"]["params"]["command"]["enum"] == ["final", "render", "still"] and by["run"]["timeout_s"] == 60
    full = {**copy.deepcopy(MANIFEST), "tools": declared + [t for t in MANIFEST["tools"] if t["name"] not in by]}
    assert P.validate_manifest(full) == []
    plain = WorkspaceTools(tools.sandbox, commands=[WorkspaceCommand("a", "A")]).declare_tools()
    assert {t["name"]: t.get("danger_check") for t in plain}["run"] is False


def test_attach_registers_handlers_and_checks(tools):
    class Fake:
        manifest = {"tools": tools.declare_tools()}

        def __init__(self):
            self.tool_h, self.check_h = {}, {}

        def tool(self, name):
            return lambda fn: self.tool_h.setdefault(name, fn)

        def check(self, name):
            return lambda fn: self.check_h.setdefault(name, fn)

    fake = Fake()
    tools.attach(fake)
    assert set(fake.tool_h) == {"skills", "files", "run"} and set(fake.check_h) == {"files", "run"}


# ---------------------------------------------------------------- версии 3.1 и 3.2

def test_contract_version_is_3_2_and_history_ends_with_it():
    assert versions.CONTRACT_VERSION == "3.4" and P.PROTOCOL == 3
    assert [v for v, _ in versions.HISTORY][-4:] == ["3.1", "3.2", "3.3", "3.4"]


def test_workspace_afk_is_a_new_request_next_to_exit():
    assert P.is_request("workspace.afk") and "workspace.afk" in P.CORE_TO_MODULE
    afk, ex = contract.get("workspace.afk"), contract.get("workspace.exit")
    assert afk.since == "3.2" and ex.since == "1.5"
    assert [f.name for f in afk.request] == [f.name for f in ex.request]
    assert [f.name for f in afk.response] == [f.name for f in ex.response]
    assert contract.validate("workspace.afk", {"reason": "render"}) == [] and contract.validate("workspace.afk", {"force": "да"})


def test_workspace_enter_response_carries_optional_intro():
    names = {f.name for f in contract.get("workspace.enter").response}
    assert {"brief", "skills", "units", "site"} <= names
    # ответы 3.1 без новых полей остаются верными
    assert contract.validate_response("workspace.enter", {"ok": True}) == []
    full = {"ok": True, "brief": "Монтаж", "skills": [{"name": "a", "description": "b"}], "units": [], "site": {"url": "http://127.0.0.1:1/"}}
    assert contract.validate_response("workspace.enter", full) == []
    assert contract.validate_response("workspace.enter", {"skills": "нет"})


def test_3_1_elements_stay_compatible_with_3_2():
    """Элементы 3.1 не изменились: volume_path на месте, старые сообщения workspace.* без правок, версия появления прежняя."""
    assert contract.get("core.tts").since == "1.0" and any(f.name == "volume_path" for f in contract.get("core.tts").response)
    for name in ("workspace.enter", "workspace.exit", "workspace.resume"):
        assert contract.get(name).since == "1.5"
    assert contract.validate("workspace.exit", {"checkpoint": True}) == []
    cur = tuple(int(x) for x in versions.CONTRACT_VERSION.split("."))
    assert all(tuple(int(x) for x in m.since.split(".")) <= cur for m in contract.REGISTRY.values())
    assert re.fullmatch(r"\d+\.\d+", versions.CONTRACT_VERSION)
    assert versions.CONTRACT_VERSION.split(".")[0] == str(P.PROTOCOL)       # мажор тот же: 3.1-участники совместимы
    # манифест 3.1 (без новых полей) проходит проверку 3.2 без предупреждений
    warnings: list[str] = []
    assert P.validate_manifest(manifest({"id": "demo", "title": "Демо", "purpose": "x"}), warnings) == [] and warnings == []


# ---------------------------------------------------------------- журнал действий (контракт, раздел 7а)

@pytest.fixture
def journal(tools):
    rows: list[dict] = []
    tools.on_action = rows.append
    return rows


async def test_on_action_records_skill_files_and_run_in_russian(tools, journal):
    await tools.skills({"name": "s1"})
    await tools.skills({"name": "s1", "file": "ref/a.md"})
    await tools.skills({})
    await tools.files({"op": "read", "path": "jobs/j1/work/src/a.txt"})
    await tools.files({"op": "write", "path": "jobs/j1/plan/p.md", "content": "один\nдва\nтри\n"})
    await tools.files({"op": "write", "path": "jobs/j1/plan/p.md", "content": "один\n"})
    await tools.files({"op": "replace", "path": "jobs/j1/plan/p.md", "old": "один", "new": "раз"})
    await tools.files({"op": "move", "path": "jobs/j1/plan/p.md", "dest": "jobs/j1/plan/q.md"})
    await tools.files({"op": "delete", "path": "jobs/j1/plan/q.md"})
    await tools.run({"command": "still", "args": ["j1", "5"]})
    await tools.run({"command": "render", "args": ["j1"]})
    texts = [r["text"] for r in journal]
    assert texts[0].startswith("Читает навык s1") and texts[1].startswith("Читает файл ref/a.md навыка s1") and texts[2] == "Смотрит список навыков"
    assert texts[3].startswith("Читает jobs/j1/work/src/a.txt (") and "Б)" in texts[3]
    assert texts[4] == "Записала jobs/j1/plan/p.md (+3 строки)" and texts[5] == "Записала jobs/j1/plan/p.md (1 строка)"
    assert texts[6].startswith("Заменила текст в jobs/j1/plan/p.md") and texts[7] == "Перенесла jobs/j1/plan/p.md в jobs/j1/plan/q.md"
    assert texts[8] == "Удалила jobs/j1/plan/q.md"
    assert texts[9].startswith("Выполнила: Кадр j1 5") and texts[10].startswith("Запустила в фоне: Рендер j1")
    assert all(set(r) == {"ts", "tool", "op", "target", "ok", "error", "text"} and r["ok"] and r["error"] is None for r in journal)
    assert [r["tool"] for r in journal[:3]] == ["skills"] * 3 and journal[4]["op"] == "write" and journal[10]["op"] == "background"
    assert not any("один" in str(r.values()) or "два" in str(r.values()) for r in journal)      # содержимое файлов в запись не попадает


async def test_on_action_records_denials_and_errors_and_never_breaks_the_call(tools, journal):
    await fail(tools.files({"op": "write", "path": "jobs/j1/out/x.txt", "content": "x"}), "denied")
    await fail(tools.files({"op": "read", "path": "jobs/j1/work/src/нет.txt"}), "unknown")
    await fail(tools.run({"command": "rm"}), "denied")
    await fail(tools.skills({"name": "нет"}), "unknown")
    assert [r["ok"] for r in journal] == [False] * 4
    assert journal[0]["text"].startswith("Отказано: нельзя записать") and journal[0]["error"].startswith("denied")
    assert journal[1]["text"].startswith("Не вышло: прочитать") and journal[1]["error"].startswith("unknown")
    assert journal[2]["text"].startswith("Отказано") and journal[3]["tool"] == "skills"

    def broken(record):
        raise RuntimeError("сломан")
    tools.on_action = broken
    assert (await tools.files({"op": "read", "path": "jobs/j1/work/src/a.txt"}))["ok"]       # ошибка в on_action вызов не ломает

    seen = []

    async def async_hook(record):
        seen.append(record["tool"])
    tools.on_action = async_hook
    await tools.skills({})
    assert seen == ["skills"]


async def test_unknown_skill_lists_exact_and_similar_names(tools):
    tools.skill_list += [WorkspaceSkill("remotion-create", "c", "skills/s1"), WorkspaceSkill("remotion-render", "r", "skills/s1"),
                         WorkspaceSkill("remotion-best-practices", "b", "skills/s1")]
    with pytest.raises(ToolError) as e:
        await tools.skills({"name": "remotion"})
    assert e.value.code == "unknown"
    msg = e.value.message
    assert "Точные имена: s1, remotion-create, remotion-render, remotion-best-practices" in msg
    assert "Похожие: remotion-create, remotion-render, remotion-best-practices" in msg
    listed = (await tools.skills({}))["result"]
    assert listed[0] == {"name": "s1", "description": "Первый навык"}               # без name: список с описаниями


def test_declared_tools_mark_only_real_parameters_required(tmp_path):
    """Параметр без `required` ядро считает обязательным: опциональные должны быть помечены явно (агент не мог читать файлы)."""
    from gits_sdk import Sandbox, WorkspaceTools
    from gits_sdk.workspace_tools import WorkspaceCommand
    tools = WorkspaceTools(Sandbox(tmp_path, writable=["plan"]), commands=[WorkspaceCommand(id="render", title="r")],
                           executor=None).declare_tools()
    required = {t["name"]: sorted(k for k, v in t["params"].items() if v.get("required")) for t in tools}
    assert required == {"skills": [], "files": ["op"], "run": ["command"]}
    assert all("required" in v for t in tools for v in t["params"].values())
