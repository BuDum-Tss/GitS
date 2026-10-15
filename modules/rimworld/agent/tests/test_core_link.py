"""CoreLink: реплики, эмоции, присутствие, общие настройки, события и пост-итог в Telegram (ТЗ 11, RW-6, RW-9, RW-12, RW-20)."""
import asyncio

from rigs import Rig

import settings
from core_link import CoreLink


async def test_say_goes_to_core_as_verbatim_with_key_and_expiry(tmp_path):
    async with Rig(tmp_path) as rig:
        assert rig.link.online
        await rig.link.say("Борис снова уснул. Прекрасно.", "angry")
        body = (await rig.core.wait_message("say")).body
        assert body == {"text": "Борис снова уснул. Прекрасно.", "mode": "verbatim", "emotion": "angry",
                        "key": "rimworld.comment", "expires_in_s": 20}


async def test_announce_is_a_fact_with_urgency_three_and_reports_offline(tmp_path):
    async with Rig(tmp_path) as rig:
        assert await rig.link.announce("Ты сама запускаешь RimWorld (не пользователь): запись забега: игры не было") is True
        body = (await rig.core.wait_message("say")).body
        assert body["mode"] == "fact" and body["urgency_hint"] == 3 and body.get("announce") is True and body["text"].startswith("Ты сама запускаешь RimWorld (не пользователь): ")
        await rig.core.set_enabled(False)
        await rig.core.wait_for(lambda: not rig.link.online)
        assert await rig.link.announce("x") is False                   # ядро недоступно: вызывающий пробует HTTP


async def test_emotion_carries_the_cause(tmp_path):
    async with Rig(tmp_path) as rig:
        await rig.link.emotion("pawn_died", "sad")
        assert (await rig.core.wait_message("emotion")).body == {"emotion": "sad", "cause": "pawn_died"}


async def test_presence_topic_feeds_speaking_user_ts_and_repeats(tmp_path):
    async with Rig(tmp_path) as rig:
        assert rig.link.is_speaking() is False and rig.link.last_user_ts() == 0.0
        await rig.core.topic("presence", {"speaking": True, "last_user_ts": 1234.5,
                                          "last_say": "Колонисты строят стену у входа"})
        await rig.core.wait_for(lambda: rig.link.is_speaking())
        assert rig.link.last_user_ts() == 1234.5
        assert rig.link.repeats("Колонисты строят стену у входа в базу") is True      # похоже на недавнее
        assert rig.link.repeats("Начался дождь над пустыней") is False
        assert rig.link.repeats("да") is False                                         # слишком коротко для сравнения
        for i in range(12):                                                            # помнятся только последние десять
            await rig.core.topic("presence", {"speaking": False, "last_say": f"совсем другая фраза номер{i} про луну"})
        await rig.core.wait_for(lambda: not rig.link.is_speaking())
        await asyncio.sleep(0.05)
        assert rig.link.repeats("Колонисты строят стену у входа") is False


async def test_online_requires_enabled(tmp_path):
    async with Rig(tmp_path) as rig:
        await rig.core.set_enabled(False)
        await rig.core.wait_for(lambda: not rig.link.online)
        await rig.link.say("тихо")
        await asyncio.sleep(0.1)
        assert rig.core.messages_of("say") == []


async def test_shared_settings_from_welcome_and_topic_reach_the_settings_file(tmp_path):
    async with Rig(tmp_path, shared={"streamer": True, "game_model": "sonnet"}) as rig:
        await rig.core.wait_for(lambda: settings.load()["streamer"] is True)           # из welcome
        assert settings.load()["game_model"] == "sonnet"
        await rig.core.update_settings({"dnd": True, "quiet_until": 99.0, "record_runs": True})
        await rig.core.wait_for(lambda: settings.load()["dnd"] is True)               # из темы settings
        cfg = settings.load()
        assert cfg["quiet_until"] == 99.0 and cfg["record_runs"] is True and cfg["streamer"] is True


async def test_event_names_sender_free_with_urgency_and_text(tmp_path):
    async with Rig(tmp_path) as rig:
        await rig.link.event("raid", {"label": "налёт"}, text="Угроза колонии: налёт.", urgency=4)
        await rig.link.event("death", {"name": "Борис"}, text="Колонист Борис умер.")
        await rig.link.event("rimworld.fire", {}, text="Пожар.")
        await rig.link.event("colonists", {"pawns": [{}, {}], "colony": "Дом"})
        await rig.core.wait_for(lambda: len(rig.core.events) == 4)
        raid, death, fire, colonists = rig.core.events
        assert raid["kind"] == "rimworld.raid" and raid["urgency_hint"] == 4 and raid["addressed_to_me"] is False
        assert raid["data"] == {"label": "налёт"} and raid["expires_in_s"] == 180
        assert death["urgency_hint"] == 4 and "sender" not in death and "reply_channel" not in death
        assert fire["kind"] == "rimworld.fire"
        assert colonists["kind"] == "rimworld.colonists" and colonists["urgency_hint"] == 0
        assert colonists["text"] == "Колония «Дом»: колонистов 2."
        for e in rig.core.events:
            assert "sender" not in e                                                    # иначе TG-14 заведёт людей на колонистов


async def test_run_ended_posts_to_telegram_through_core_when_enabled(tmp_path):
    async with Rig(tmp_path, shared={"telegram_after_run": True}) as rig:
        await rig.core.wait_for(lambda: rig.client.settings.get("telegram_after_run") is True)
        await rig.link.event("run_ended", {"prompt": "Колония погибла. Напиши итог."}, text="Забег закончен.")
        await rig.core.wait_for(lambda: rig.tool_calls)
        assert rig.tool_calls == [{"tool": "telegram.post", "args": {"topic": "Колония погибла. Напиши итог."}}]
        ended = [e for e in rig.core.events if e["kind"] == "rimworld.run_ended"]
        assert len(ended) == 1 and ended[0]["urgency_hint"] == 3


async def test_run_ended_does_not_post_when_setting_is_off(tmp_path):
    async with Rig(tmp_path, shared={"telegram_after_run": False}) as rig:
        await rig.link.event("run_ended", {"prompt": "Итог."}, text="Забег закончен.")
        await rig.core.wait_message("event")
        await asyncio.sleep(0.15)
        assert rig.tool_calls == []


async def test_failed_post_is_only_logged(tmp_path, monkeypatch):
    import events_log
    from gits_sdk import ToolError
    logged = []
    monkeypatch.setattr(events_log, "log", lambda kind, text="", **kw: logged.append((kind, text)))
    async with Rig(tmp_path, shared={"telegram_after_run": True}) as rig:
        @rig.core.on_request("core.tool.call")
        def refuse(body):
            raise ToolError("модуль Telegram не подключён", code="unavailable")
        await rig.core.wait_for(lambda: rig.client.settings.get("telegram_after_run") is True)
        await rig.link.event("run_ended", {"prompt": "Итог."}, text="Забег закончен.")
        await rig.core.wait_for(lambda: logged)
    assert logged[0][0] == "telegram" and "не опубликован" in logged[0][1]


def test_core_link_has_the_speechlink_interface():
    for name in ("online", "last_user_ts", "is_speaking", "repeats", "say", "emotion", "event", "run"):
        assert hasattr(CoreLink, name), name
