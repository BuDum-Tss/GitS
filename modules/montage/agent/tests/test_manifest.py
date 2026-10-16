"""Манифест модуля монтажа (MON-37): валиден по контракту ТЗ 01 и совпадает с ТЗ 20 раздел 5."""
from gits_sdk import validate_manifest
from manifest import MANIFEST, build_manifest


def test_manifest_is_valid():
    assert validate_manifest(build_manifest()) == []


def test_manifest_declares_policies_and_workspace():
    """ТЗ 18, FOC-1; ТЗ 32, WS-4: фон по умолчанию (idle), активная политика working и среда агента."""
    m = build_manifest()
    assert m["default_policy"] == "idle"
    assert {p["id"]: p["focus"] for p in m["policies"]} == {"idle": "background", "working": "active"}
    ws = m["workspace"]
    assert ws["id"] == "montage" and "process_started" not in ws["entry"] and ws["interruptible"] is True
    assert validate_manifest(m) == []


def test_manifest_matches_spec():
    m = MANIFEST
    assert m["id"] == "montage" and m["title"] == "Монтаж"
    assert m["subscribe"] == ["settings", "module", "battery"]
    assert m["shared_settings"] == ["dnd", "quiet_until", "claude_usage"]
    assert m["state"] is True
    schema = m["settings_schema"]
    assert schema["approve_plan"]["default"] is True and schema["auto_final"]["default"] is False
    assert (schema["plan_model"]["default"], schema["build_model"]["default"]) == ("opus", "sonnet")
    assert (schema["keep_jobs"]["min"], schema["keep_jobs"]["max"], schema["keep_jobs"]["default"]) == (1, 100, 10)
    assert (schema["max_turns_stage"]["min"], schema["max_turns_stage"]["max"]) == (10, 300)
    assert not any(s.get("secret") for s in schema.values())


def test_tools():
    tools = {t["name"]: t for t in MANIFEST["tools"]}
    assert set(tools) == {"start", "status", "revise", "cancel", "open_studio"}      # open_studio: TZ 15, TAB-4
    assert all(t["availability"] == "always" for t in tools.values())
    start = tools["start"]
    assert start["risk"] == "ask" and start["job"] is True and start["announce"] is True        # MON-5a
    assert {"монтаж", "смонтируй", "ролик", "видео", "шортс", "рилс", "клип", "субтитры", "нарезка"} <= set(start["keywords"])
    assert tools["status"]["informative"] is True
    assert start["params"]["sources"]["type"] == "array"
    assert start["params"]["platform"]["enum"] == ["shorts", "reels", "telegram", "youtube", "none"]


def test_emits_use_module_prefix():
    assert all(e["kind"].startswith("montage.") for e in MANIFEST["emits"])


def test_panel_is_declared_and_valid_with_serve_dirs(tmp_path):
    """TZ 15, TAB-4: пространство «Монтаж» с ссылками и кнопками; каталоги раздачи задаются при запуске."""
    from gits_sdk.protocol import Manifest, validate_manifest
    from manifest import serve_dirs
    m = build_manifest(serve_dirs(tmp_path / "jobs", str(tmp_path / "out")))
    warnings: list[str] = []
    assert validate_manifest(m, warnings) == [] and warnings == []
    spec = Manifest.from_dict(m)
    assert spec.panel is not None and spec.panel.title == "Монтаж"
    assert [link.id for link in spec.panel.links] == ["studio", "folder", "jobs"]
    assert spec.panel.links[0].tool == "open_studio" and spec.panel.links[0].args_from == "job"
    assert spec.panel.actions == ["open_studio", "approve_plan", "rework_plan", "reject_plan", "approve_draft",
                                  "rework_draft", "answer_question"]
    assert len(spec.panel.serve_dirs) == 2 and all(d for d in spec.panel.serve_dirs)
    assert build_manifest()["panel"].get("serve_dirs") is None          # без каталогов раздача не объявлена
