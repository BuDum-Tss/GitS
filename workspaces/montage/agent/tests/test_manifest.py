"""Манифест окружения «Монтаж» (MON-37; контракт 3.2): валиден без предупреждений, объявляет инструменты workspace, навыки, область,
команды и сайт."""
from gits_sdk import validate_manifest
from gits_sdk.protocol import Manifest
from manifest import COMMANDS, MANIFEST, build_manifest


def test_manifest_is_valid_without_warnings():
    warnings: list[str] = []
    assert validate_manifest(build_manifest(), warnings) == [] and warnings == []
    spec = Manifest.from_dict(build_manifest())
    assert spec.workspace_warnings == [] and spec.workspace_spec is not None


def test_manifest_declares_workspace_without_policies():
    """ТЗ 32, WS-4: среда агента; политик в манифесте нет."""
    m = build_manifest()
    assert "policies" not in m and "default_policy" not in m
    ws = m["workspace"]
    assert ws["id"] == "montage" and "process_started" not in ws["entry"] and ws["interruptible"] is True
    assert validate_manifest(m) == []


def test_workspace_block_has_brief_skills_area_commands_site():
    spec = Manifest.from_dict(build_manifest()).workspace_spec
    assert spec.brief and "montage-regulation" in spec.brief
    names = [s.name for s in spec.skills]
    assert "montage-regulation" in names and {"remotion-markup", "remotion-render", "remotion-captions"} <= set(names)
    assert all(s.description and s.path == f"skills/{s.name}" for s in spec.skills)
    area = spec.area
    assert area.writable == ["*/plan"]                      # агент пишет только идею, сценарий и монтажный лист
    assert {"template/demo/plan.*.json", "resources", "*/sources", "*/renders", "*/qa"} <= set(area.readonly)
    assert {"node_modules", ".git", ".claude"} == set(area.hidden)
    assert [c.id for c in spec.commands] == ["intake", "transcribe", "voiceover", "render", "still", "compositions", "check", "final"]
    assert spec.command("render").background and spec.command("final").background and spec.command("final").irreversible
    assert not spec.command("still").background and not spec.command("check").irreversible
    assert spec.site_url.startswith("http://127.0.0.1:")
    assert [c.id for c in COMMANDS] == [c.id for c in spec.commands]


def test_manifest_matches_spec():
    m = MANIFEST
    assert m["id"] == "montage" and m["title"] == "Монтаж"
    assert m["subscribe"] == ["settings", "module"]
    assert m["shared_settings"] == ["dnd", "quiet_until"]                     # модель у ядра: настроек pi у окружения нет
    assert m["state"] is True
    schema = m["settings_schema"]
    assert set(schema) == {"output_dir", "keep_jobs"}
    assert (schema["keep_jobs"]["min"], schema["keep_jobs"]["max"], schema["keep_jobs"]["default"]) == (1, 100, 10)
    assert not any(s.get("secret") for s in schema.values())


def test_tools():
    tools = {t["name"]: t for t in MANIFEST["tools"]}
    assert set(tools) == {"skills", "files", "run", "work", "site", "resources"}
    assert not {"start", "revise", "cancel", "open_studio", "status"} & set(tools)       # прежние инструменты конвейера убраны
    assert all(t["availability"] == "always" and t["risk"] == "auto" for t in tools.values())
    assert tools["files"]["danger_check"] is True and tools["run"]["danger_check"] is True      # удаление и final спрашивают согласие
    assert "final" in tools["run"]["params"]["command"]["enum"]
    work = tools["work"]
    assert work["params"]["op"]["enum"] == ["create", "list", "status", "cancel"]
    assert {"монтаж", "смонтируй", "ролик", "видео", "шортс", "рилс", "клип", "субтитры", "нарезка"} <= set(work["keywords"])
    assert work["params"]["sources"]["type"] == "array"
    assert "platform" not in work["params"]
    assert work["params"]["format"]["enum"] == ["vertical", "horizontal"]
    assert tools["site"]["informative"] is True and tools["resources"]["informative"] is True


def test_emits_use_module_prefix():
    assert all(e["kind"].startswith("montage.") for e in MANIFEST["emits"])
    assert {e["kind"] for e in MANIFEST["emits"]} == {"montage.launch", "montage.done", "montage.failed"}


def test_panel_is_declared_and_valid_with_serve_dirs(tmp_path):
    """TZ 15, TAB-4: пространство «Монтаж» только со сводкой и ссылкой на сайт; каталоги раздачи задаются при запуске."""
    from manifest import serve_dirs
    m = build_manifest(serve_dirs(tmp_path / "jobs", str(tmp_path / "out")))
    warnings: list[str] = []
    assert validate_manifest(m, warnings) == [] and warnings == []
    spec = Manifest.from_dict(m)
    assert spec.panel is not None and spec.panel.title == "Монтаж"
    assert [link.id for link in spec.panel.links] == ["site"]                     # у среды свой сайт, в дашборде только ссылка
    assert spec.panel.links[0].kind == "url" and spec.panel.links[0].url.startswith("http://127.0.0.1:")
    assert spec.panel.actions == []                                               # кнопок одобрения нет: согласия идут вопросами ядра
    assert len(spec.panel.serve_dirs) == 2 and all(d for d in spec.panel.serve_dirs)
    assert build_manifest()["panel"].get("serve_dirs") is None          # без каталогов раздача не объявлена
