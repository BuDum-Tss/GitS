"""Панель Discord: блок «Голос», карточки голосовых каналов, действия владельца, ошибки, размер и таймаут."""
import asyncio
import json

import discord_module as app_mod
from dc_rig import OWNER_ID, Rig, VoiceChannel

from gits_sdk import protocol as P

OWNER = {"owner": str(OWNER_ID)}


def vworld(rig):
    g, _ = rig.server(1, "Клуб", (("общий", 100),))
    g.me = rig.me
    voice = rig.dc.register(g.add(VoiceChannel(500, "Войс")))
    return g, voice


def flat(blocks):
    out = []
    for b in blocks:
        if b["type"] == "split":
            out += flat(b["left"]) + flat(b["right"])
        else:
            out.append(b)
    return out


async def panel(rig):
    rig.mod.panel.invalidate()
    blocks = await asyncio.wait_for(rig.mod.panel_blocks(), app_mod.PANEL_BUILD_TIMEOUT_S)
    assert P.validate_panel_set({"blocks": blocks}, set(rig.client.manifest["panel"]["actions"])) == [], blocks
    assert len(json.dumps({"blocks": blocks}, ensure_ascii=False).encode()) <= P.PANEL_SET_MAX_BYTES
    return blocks


def voice_text(blocks):
    return [b for b in blocks if b["type"] == "text" and b.get("title") == "Голос"][0]["text"]


def voice_card(blocks):
    return [c for b in flat(blocks) if b["type"] == "cards" for card in b["items"] for c in card.get("children", [])
            if c["title"] == "♪ Войс"][0]


async def test_voice_block_cards_buttons_and_owner_actions(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
        g, voice = vworld(rig)
        voice.members = [rig.dc.user(5, "masha"), rig.dc.user(6, "bot", bot=True)]
        voice.perms.speak = False
        assert "Не в голосовом канале" in voice_text(await panel(rig))
        await rig.core.panel_action("toggle_guild", {"id": "1"})
        blocks = await panel(rig)
        club = [c for b in flat(blocks) if b["type"] == "cards" for c in b["items"]][0]
        v = voice_card(blocks)
        assert "masha" in v["subtitle"] and "+ботов 1" in v["subtitle"] and "нет прав: Speak" in v["subtitle"]
        assert v["action"] == "voice_connect" and v["args"] == {"id": "500"} and v["select"]["args"] == {"scope": "channel", "id": "500"}
        assert club["subtitle"].endswith("голосовых: 1")
        voice.perms.speak = True
        res = await rig.core.panel_action("voice_connect", {"id": "500"})
        assert res["ok"] is True and 1 in rig.voice.sessions
        blocks = await panel(rig)
        text = voice_text(blocks)
        assert "«Клуб / Войс»" in text and "masha" in text and "Движок голоса: подключён" in text and "Говорит: нет" in text
        assert "Объявление" in text
        buttons = [b for b in blocks if b["type"] == "buttons" and b["items"][0]["action"] == "voice_disconnect"]
        assert buttons[0]["items"][0]["args"] == {"id": "1"}
        v = voice_card(blocks)
        assert v["tag"] == "она здесь" and v["action"] == "voice_disconnect"
        rig.engines[0].ready = False
        rig.engines[0].last_error = "ConnectionError: refused"
        assert "нет связи (движок выключен" in voice_text(await panel(rig))
        res = await rig.core.panel_action("voice_disconnect", {"id": "1"})
        assert res["ok"] is True and rig.voice.sessions == {}
        assert (await rig.core.panel_action("voice_disconnect", {"id": "1"}))["message"] == "Она и так не в голосовом канале."


async def test_panel_shows_join_error_and_missing_permissions(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
        g, voice = vworld(rig)
        voice.perms.connect = False
        res = await rig.core.panel_action("voice_connect", {"id": "500"})
        assert res["ok"] is False and "Connect" in res["message"]
        voice.perms.connect = True
        voice.connect_error = type("Forbidden", (Exception,), {})("x")
        await rig.core.panel_action("voice_connect", {"id": "500"})
        assert "Ошибка: нет прав Connect/Speak" in voice_text(await panel(rig))


async def test_panel_in_read_mode_says_she_will_not_speak_and_build_is_fast(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"default_chat_mode": "read"}) as rig:
        g, voice = vworld(rig)
        await rig.voice.join(voice)
        t0 = asyncio.get_running_loop().time()
        text = voice_text(await panel(rig))
        assert asyncio.get_running_loop().time() - t0 < 1 and "Режим «чтение»" in text and "не звучит" in text


async def test_panel_shows_missing_dependencies(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        vworld(rig)

        def importer(name):
            if name == "davey":
                raise ImportError(name)
            return object()

        rig.voice.importer = importer
        assert "Голос недоступен" in voice_text(await panel(rig)) and "davey" in voice_text(await panel(rig))
