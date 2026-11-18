"""Манифест и инструменты модуля Telegram (ТЗ 10: раздел 5, TG-8, TG-22)."""
from tg_rig import Rig

from gits_sdk import protocol as P
from manifest import MANIFEST, build_manifest

TABLE = {"text", "message", "inbox", "announce", "settings"}


def tools():
    return {t["name"]: t for t in MANIFEST["tools"]}


def test_manifest_is_valid_and_matches_the_spec():
    assert P.validate_manifest(build_manifest()) == []
    m = MANIFEST
    assert m["id"] == "telegram" and m["title"] == "Telegram" and m["capabilities"] == ["chat.channel"]
    assert m["subscribe"] == ["settings", "presence", "module", "character"] and m["state"] is True
    assert m["shared_settings"] == ["quiet_until", "dnd"]
    schema = m["settings_schema"]
    assert schema["owner_tg_id"]["default"] == 0
    assert (schema["owner"]["type"], schema["owner"]["default"]) == ("string", "")           # аккаунт владельца: @имя или id
    assert "confirm" not in schema                                                           # разрешения на посты больше нет
    assert {k: (schema[k]["type"], schema[k]["default"]) for k in ("publish", "after_run", "voice")} == {
        k: ("boolean", False) for k in ("publish", "after_run", "voice")}
    assert (schema["every_mins"]["default"], schema["every_mins"]["min"], schema["every_mins"]["max"]) == (180, 30, 1440)
    assert (schema["post_hours_from"]["default"], schema["post_hours_to"]["default"]) == (9, 23)
    assert not any(s.get("secret") for s in schema.values())                 # секретов в настройках нет


def test_showcase_settings_and_events():
    schema = MANIFEST["settings_schema"]
    assert schema["about"]["default"] and all(schema[k]["default"] == "" for k in
                                              ("channel_url", "account_url", "twitch_url", "youtube_url"))
    kinds = {e["kind"] for e in MANIFEST["emits"]}
    assert "telegram.bot_message" not in kinds and {"telegram.dm", "telegram.group", "telegram.posted"} <= kinds


def test_tool_table():
    t = tools()
    assert set(t) == TABLE
    assert all(x["availability"] == "always" and x["risk"] == "auto" for x in t.values())
    assert not any(x.get("self_confirming") for x in t.values())                 # публикация без самоподтверждения и риска public
    assert t["message"]["danger_check"] is True and t["message"]["informative"] is True     # разрешение решает проверка адресата
    assert t["announce"]["params"]["text"].get("required") is not False and t["announce"]["params"]["url"]["required"] is False
    assert not t["inbox"].get("danger_check") and not t["settings"].get("danger_check")
    assert t["message"]["params"]["files"]["type"] == "array" and t["message"]["params"]["reply_to"]["type"] == "integer"
    assert all(p.get("required") is False for p in t["message"]["params"].values())


def test_planner_keywords_cover_old_requests():
    for tool in MANIFEST["tools"]:
        assert {"телеграм", "телега", "тг", "канал", "пост", "написать", "чат"} <= set(tool["keywords"])
    assert {"опубликуй", "запости", "напиши", "отправь"} <= set(tools()["message"]["keywords"])
    assert {"прочитай", "переписка"} <= set(tools()["inbox"]["keywords"])


def test_descriptions_are_short():
    assert all(len(x["description"]) <= 300 for x in tools().values())


async def test_every_tool_has_a_handler(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert set(rig.client._tools) == TABLE and set(rig.client._checks) == {"text", "message"}
        assert rig.core.hellos[0]["manifest"]["id"] == "telegram"
        assert rig.core.violations == [] and rig.core.rejected == []
