"""Манифест и инструменты модуля Telegram (ТЗ 10: раздел 5, TG-8, TG-22)."""
from tg_rig import Rig

from gits_sdk import protocol as P
from manifest import MANIFEST, build_manifest

TABLE = {"post", "draft_show", "draft_write", "draft_edit", "draft_append", "draft_replace", "draft_attach",
         "draft_detach", "draft_clear", "draft_publish", "chats", "read", "unread", "send", "set_periodic",
         "set_after_run"}


def tools():
    return {t["name"]: t for t in MANIFEST["tools"]}


def test_manifest_is_valid_and_matches_the_spec():
    assert P.validate_manifest(build_manifest()) == []
    m = MANIFEST
    assert m["id"] == "telegram" and m["title"] == "Telegram" and m["capabilities"] == ["chat.channel"]
    assert m["subscribe"] == ["settings", "presence", "module"] and m["state"] is True
    assert m["shared_settings"] == ["telegram", "telegram_voice", "telegram_confirm", "quiet_until", "dnd"]
    schema = m["settings_schema"]
    assert schema["owner_tg_id"]["default"] == 0
    assert (schema["every_mins"]["default"], schema["every_mins"]["min"], schema["every_mins"]["max"]) == (180, 30, 1440)
    assert (schema["post_hours_from"]["default"], schema["post_hours_to"]["default"]) == (9, 23)
    assert not any(s.get("secret") for s in schema.values())                 # секретов в настройках нет


def test_tool_table():
    t = tools()
    assert set(t) == TABLE
    assert all(x["availability"] == "always" for x in t.values())
    assert (t["post"]["risk"], t["post"]["self_confirming"]) == ("public", True)
    assert (t["draft_publish"]["risk"], t["draft_publish"]["self_confirming"]) == ("public", True)
    assert t["send"]["risk"] == "ask" and t["send"]["danger_check"] is True
    for name in ("chats", "read", "unread"):
        assert t[name]["risk"] == "auto" and t[name]["informative"] is True
    for name in ("set_periodic", "set_after_run"):
        assert t[name]["risk"] == "auto"
    for name in TABLE - {"post", "draft_publish", "send"}:
        assert t[name]["risk"] == "auto"


def test_planner_keywords_cover_old_requests():
    for tool in MANIFEST["tools"]:
        assert {"телеграм", "телега", "тг", "канал", "пост", "написать", "чат"} <= set(tool["keywords"])
    assert {"опубликуй", "запости"} <= set(tools()["post"]["keywords"])
    assert {"напиши", "отправь"} <= set(tools()["send"]["keywords"])


def test_descriptions_moved_verbatim():
    import re
    t = tools()
    assert t["chats"]["description"] == "Список диалогов её Telegram-аккаунта (личные, группы, каналы) с числом непрочитанных."
    assert t["post"]["description"].startswith("Написать и опубликовать пост в Telegram-канал на заданную тему")
    assert re.search("Каждый раз спрашивает разрешение", t["send"]["description"])
    assert all(len(x["description"]) <= 300 for x in t.values())


async def test_every_tool_has_a_handler(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert set(rig.client._tools) == TABLE and set(rig.client._checks) == {"send"}
        assert rig.core.hellos[0]["manifest"]["id"] == "telegram"
        assert rig.core.violations == [] and rig.core.rejected == []
