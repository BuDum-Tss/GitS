"""Ядро после выноса Telegram в модуль (ТЗ 10, TG-12, TG-13, TG-15, TG-16, TG-23): личные слова, настройки для модулей,
пост-итог, запись модуля в modules.yaml, MEMORY_DIR для модулей. Кода Telegram в процессе ядра больше нет."""
import re
import subprocess
import sys
from pathlib import Path

import pytest
from hub_helpers import RawModule, manifest, running_core

import core_requests
import settings
from environments import builtin
from relationships import Relationships
from supervisor import Supervisor, load_specs

AGENT = Path(__file__).resolve().parent.parent


# ── core.privacy_terms (TG-12) ─────────────────────────────────────────────────────────────────

def test_collect_privacy_terms_gathers_owner_people_and_names_from_facts():
    terms = core_requests.collect_privacy_terms(
        "Аня", ["Маша Петрова", "mashka", "аня"],
        ["Имя: Анна Сергеевна", "Любит чай", "Зовут - Аню", "Живёт в Казани", ""])
    assert terms == ["Аня", "Маша Петрова", "mashka", "Анна", "Сергеевна", "Аню"]             # без повторов, порядок сохранён
    assert core_requests.collect_privacy_terms("", [], ["Любит чай"]) == []
    assert core_requests.collect_privacy_terms("Ян", ["Ли"], ["имя: Ян"]) == []                  # короче трёх букв не берём


def test_relationships_people_terms_take_only_people(tmp_path):
    rel = Relationships(str(tmp_path))
    rel.ensure_person("tg:5", "Маша Иванова", source="telegram", handle="mashka")
    rel.ensure_person("tg:6", "Петя", source="telegram")
    rel.adjust("Борис", 5, "помог")                                   # колонист игры: не личное слово
    assert sorted(rel.people_terms()) == sorted(["Маша Иванова", "Петя", "mashka"])


async def test_core_privacy_terms_request(tmp_path):
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            assert (await raw.request("core.privacy_terms", {}))["error"]["code"] == "unavailable"       # провайдера ещё нет
            core.requests.privacy_terms = lambda: ["Аня", "Маша"]
            body = await raw.request("core.privacy_terms", {})
            assert body == {"ok": True, "value": {"terms": ["Аня", "Маша"]}}

            def broken():
                raise OSError("диск недоступен")
            core.requests.privacy_terms = broken
            err = (await raw.request("core.privacy_terms", {}))["error"]
            assert err["code"] == "internal" and "диск" not in err["message"]                            # детали наружу не идут
        finally:
            await raw.close()


# ── core.set_setting (TG-13) ───────────────────────────────────────────────────────────────────

async def call_core_tool(raw, tool, args):
    return await raw.request("core.tool.call", {"tool": tool, "args": args})


async def test_set_setting_changes_only_whitelisted_boolean_settings(tmp_path):
    async with running_core(tmp_path) as core:
        builtin.register_core_tools(core.envs, core.modules)
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            tool = core.envs.get("core.set_setting")
            assert tool.risk == "auto" and tool.availability == "always" and tool.env == "core"
            assert tool.params["key"]["enum"] == ["telegram", "telegram_after_run", "telegram_voice", "telegram_confirm"]
            for key in ("telegram", "telegram_after_run", "telegram_voice", "telegram_confirm"):
                body = await call_core_tool(raw, "core.set_setting", {"key": key, "value": True})
                assert body["ok"] is True, body
                assert settings.load()[key] is True
                assert (await call_core_tool(raw, "core.set_setting", {"key": key, "value": False}))["ok"] is True
                assert settings.load()[key] is False
            before = settings.load()
            for args in ({"key": "auto_accept", "value": True}, {"key": "voice_muted", "value": True},
                         {"key": "telegram", "value": "возможно"}, {"key": "telegram"}, {"value": True}):
                body = await call_core_tool(raw, "core.set_setting", args)
                assert body["ok"] is False and body["error"]["code"] == "bad_request", args
            assert settings.load() == before                                # опасные настройки не тронуты
        finally:
            await raw.close()


async def test_set_setting_direct_call_refuses_other_keys(tmp_path):
    async with running_core(tmp_path) as core:
        builtin.register_core_tools(core.envs, core.modules)
        from environments.base import ToolError
        with pytest.raises(ToolError) as e:
            await core.envs.get("core.set_setting").call({"key": "auto_accept", "value": True})
        assert e.value.code == "denied" and settings.load()["auto_accept"] is False


# ── TG-15: пост-итог после забега ──────────────────────────────────────────────────────────────
# С ТЗ 11 пост-итог запрашивает сам модуль игры (core.tool.call telegram.post, тесты в rimworld-module); в ядре его нет.

def test_core_no_longer_posts_run_summaries():
    import core_service
    assert not hasattr(core_service, "post_run_summary")


# ── TG-16: запись в modules.yaml, MEMORY_DIR ───────────────────────────────────────────────────

def test_shipped_modules_yaml_declares_the_telegram_module():
    spec = {s.id: s for s in load_specs()}["telegram"]
    assert spec.kind == "module" and spec.autostart and spec.restart == "on-failure" and spec.enabled
    assert spec.command == "{PYTHON}" and spec.args == ["-u", "main.py"] and spec.cwd == "{GITS}/modules/telegram"
    sup = Supervisor(load_specs(), token="t" * 64, core_url="ws://x")
    cmd, cwd = sup.resolve("telegram")
    assert cmd == [sys.executable, "-u", "main.py"] and (cwd / "main.py").is_file()


def test_modules_get_absolute_memory_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sup = Supervisor(load_specs(), token="t" * 64, core_url="ws://x", memory_dir="rel-memory")
    env = sup._process_env("telegram")
    assert env["MEMORY_DIR"] == str((tmp_path / "rel-memory").resolve()) and Path(env["MEMORY_DIR"]).is_absolute()
    other = Supervisor(load_specs(), token="t" * 64, core_url="ws://x", env={})
    assert "MEMORY_DIR" not in other._process_env("telegram")                  # папка не задана: модуль решит сам


async def test_wire_modules_hands_core_memory_dir_to_modules():
    import core_service
    from hub_helpers import FakeCompanion
    from environments.registry import Registry
    import speech_out

    companion = FakeCompanion()
    companion.profile_hint = lambda: ""
    companion.io = None
    layer = core_service.wire_modules(companion, Registry(), specs=[], battery=lambda: {"level": 1.0, "sleeping": False})
    assert layer.supervisor.memory_dir == settings.memory_dir().resolve()
    assert companion.module_notes == layer.modules.notes
    assert isinstance(companion.presence, speech_out.PresenceTracker)


def test_module_notes_describe_connected_modules_with_tools():
    from types import SimpleNamespace

    from module_registry import ModuleRecord, ModuleRegistry
    reg = ModuleRegistry(SimpleNamespace(envs={}, load_errors={}), settings_source=lambda: {"modules": {}})
    reg.records["telegram"] = ModuleRecord("telegram", title="Telegram", description="Telegram: канал и бот.", connected=True,
                                           manifest={"tools": [{"name": "post"}]},
                                           state={"summary": "канал: готов, аккаунт: нет сессии", "data": {}, "ts": 1})
    reg.records["quiet"] = ModuleRecord("quiet", title="Тихий", connected=True, manifest={"tools": []})      # без инструментов
    reg.records["off"] = ModuleRecord("off", title="Нет", connected=False, manifest={"tools": [{"name": "x"}]})
    assert reg.notes() == ["Модуль «Telegram»: Telegram: канал и бот. Сейчас: канал: готов, аккаунт: нет сессии."]
    reg._settings_source = lambda: {"modules": {"telegram": {"enabled": False}}}
    reg.invalidate()
    assert reg.notes() == []                                                     # выключенный в контекст не попадает


async def test_companion_context_uses_module_notes_not_telegram_objects():
    from companion import Companion
    from relationships import Relationships

    c = Companion(str(settings.memory_dir()), Relationships(str(settings.memory_dir())))
    assert not hasattr(c, "telegram") and not hasattr(c, "tg_user")
    c.module_notes = lambda: ["Модуль «Telegram»: канал и бот. Сейчас: канал: готов."]
    text = c._capabilities()
    assert "- Модуль «Telegram»: канал и бот. Сейчас: канал: готов." in text


# ── в процессе ядра нет Telegram ──────────────────────────────────────────────────────────────

_FORBIDDEN = ("telethon", "telegram_channel", "telegram_user", "tg_draft")


def test_no_telegram_imports_in_core_sources():
    pattern = re.compile(r"^\s*(?:import|from)\s+(" + "|".join(_FORBIDDEN) + r")\b", re.M)
    offenders = []
    for path in AGENT.rglob("*.py"):
        if any(part in ("memory", "models", "resources", "logs", ".venv") for part in path.relative_to(AGENT).parts):
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(path.relative_to(AGENT)))
    assert offenders == []
    for name in ("telegram_channel.py", "telegram_user.py", "tg_draft.py", "tg_login.py"):
        assert not (AGENT / name).exists(), name                                 # переехали в modules/telegram
    assert not (AGENT / "opus_encoder.py").exists()                              # перекодирование звука ушло в модуль voice (ТЗ 12)


def test_core_import_does_not_pull_telegram_modules():
    code = ("import sys; sys.path[:0] = ['.', '../sdk']; import core_service; "
            f"bad = [m for m in {list(_FORBIDDEN)!r} if m in sys.modules]; print('BAD', bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=AGENT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
