"""Склейка окружения «Стример»: один манифест на две подсистемы, префиксы инструментов, общее состояние, прежние виды событий."""
import asyncio

from gits_sdk import WorkspaceClient
from gits_sdk.protocol import Manifest, validate_manifest
from gits_sdk.testing import FakeCore

from manifest import build_manifest
from obs.obs_manifest import EMITS as OBS_EMITS
from streamer_module import StreamerModule


def test_one_manifest_has_both_tool_sets_with_prefixes_and_no_duplicates():
    m = build_manifest()
    assert validate_manifest(m) == []
    assert m["id"] == "streamer" and m["role"] == "workspace" and m["ownership"] == "owned"
    names = [t["name"] for t in m["tools"]]
    assert len(names) == len(set(names)) and len(names) == 9 + 8
    assert {n for n in names if n.startswith("twitch_")} == {"twitch_" + x for x in (
        "chat_say", "announce", "stream_status", "set_stream", "clip", "recent_events", "timeout_user", "ban_user", "delete_message")}
    assert {n for n in names if n.startswith("obs_")} == {"obs_" + x for x in (
        "status", "scenes", "switch_scene", "start_stream", "stop_stream", "start_record", "stop_record", "caption")}
    assert len(names) == len([n for n in names if n.startswith(("twitch_", "obs_"))])
    kinds = {e["kind"] for e in m["emits"]}
    assert {"twitch.chat", "twitch.mention", "obs.scene", "obs.stream_state", "obs.record_state"} <= kinds
    assert m["event_prefixes"] == ["twitch", "obs"]
    assert {"chat_events_per_min", "owner_login"} <= set(m["settings_schema"])          # настройки Twitch не потеряны
    assert m["workspace"]["title"] == "Стример" and m["workspace"]["id"] == "streamer" and m["panel"]["title"] == "Стример"
    assert Manifest.from_dict(m).event_prefixes == ["twitch", "obs"]


def test_foreign_event_kind_prefix_is_still_rejected():
    m = build_manifest()
    m["emits"].append({"kind": "telegram.post", "description": "чужой вид", "default_urgency": 1})
    assert any("kind должен начинаться" in p for p in validate_manifest(m))


async def test_streamer_runs_both_subsystems_on_one_client_with_merged_state(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir(exist_ok=True)
    core = FakeCore(settings={"quiet_until": 0.0, "dnd": False})
    await core.start()
    core.on_request("core.privacy_terms")(lambda body: {"terms": []})
    client = WorkspaceClient(build_manifest(), url=core.url, token=core.token)
    client.reconnect_delays = (0.05, 0.05, 0.1)
    mod = StreamerModule(client, str(mem), "ws://127.0.0.1:9", "")           # OBS по этому адресу нет: «не на связи»
    task = asyncio.create_task(client.run())
    try:
        await core.wait_connected()
        await client.wait_connected(5)
        summary, data = mod.snapshot()
        assert summary.startswith("Twitch: не настроен") and "OBS: не на связи" in summary
        assert set(data) == {"twitch", "obs"} and data["obs"]["connected"] is False and data["twitch"]["ready"] is False
        # событие подсистемы Twitch идёт с прежним видом и адресом ответа на окружение
        assert await mod.twitch.on_event("stream.online", {})
        ev = (await core.wait_message("event")).body
        assert ev["kind"] == "twitch.stream_online"
        # событие OBS тоже уходит от того же клиента
        await mod.obs._on_event("CurrentProgramSceneChanged", {"sceneName": "AFK"})
        for _ in range(100):
            kinds = [m.body["kind"] for m in core.messages_of("event")]
            if "obs.scene" in kinds:
                break
            await asyncio.sleep(0.05)
        assert "obs.scene" in kinds
        # инструменты обеих подсистем вызываются по префиксным именам
        assert (await core.call_tool("twitch_recent_events", {}))["events"]
        try:
            await core.call_tool("obs_status", {})
        except Exception as e:                                                  # OBS не на связи: честная ошибка, не «нет инструмента»
            assert "нет инструмента" not in str(e)
        # общее состояние уходит одним сообщением state
        mod.start()
        state = (await core.wait_message("state")).body
        assert "Twitch" in state["summary"] and "OBS" in state["summary"]
    finally:
        await mod.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await core.stop()


def test_obs_emits_are_declared_in_the_merged_manifest():
    kinds = {e["kind"] for e in build_manifest()["emits"]}
    assert {e["kind"] for e in OBS_EMITS} <= kinds


def test_token_file_name_is_unchanged(tmp_path):
    from twitch.twitch_api import Tokens
    tokens = Tokens(str(tmp_path))
    tokens.put("bot", {"access": "a", "refresh": "r", "expires_at": 1.0, "user_id": "1", "login": "b"})
    assert (tmp_path / "twitch_tokens.json").exists()
