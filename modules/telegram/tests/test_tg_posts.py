"""Публикации, черновики, отправка с аккаунта, приватность и настройки (ТЗ 10: TG-2..TG-6, TG-13..TG-14 со стороны модуля)."""
import asyncio
import time

import pytest
from tg_rig import Rig

from glados_module import ToolError

POST = "Протокол испытания номер семь: куб снова не оправдал ожиданий."


async def test_post_publishes_to_channel_and_reports_event(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.stub_model(monkeypatch, POST)
        out = await rig.core.call_tool("post", {"topic": "понедельник"})
        await rig.core.wait_message("event")
        ev = rig.core.events[-1]
        state = rig.read_state()
    assert out == f"опубликован пост: «{POST}»"
    assert rig.api_calls("sendMessage") == [{"chat_id": "@chan", "text": POST}]
    assert rig.asks == []                                                      # telegram_confirm выключен: не спрашивает
    assert state["posts"][-1]["text"] == POST and state["last_post"] > 0
    assert ev["kind"] == "telegram.posted" and POST[:40] in ev["text"]
    assert any(r["text"] == "опубликовано по просьбе: " + POST for r in rig.journal())


async def test_confirm_asks_through_the_core_and_publishes_on_yes(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, settings={"telegram_confirm": True}) as rig:
        rig.stub_model(monkeypatch, POST)
        await rig.core.call_tool("post", {"topic": "понедельник"})
        assert [(a["kind"], POST in a["question"]) for a in rig.asks] == [("approval", True)]
        assert rig.api_calls("sendMessage") == [{"chat_id": "@chan", "text": POST}]


async def test_confirm_refusal_does_not_publish(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, settings={"telegram_confirm": True}) as rig:
        rig.stub_model(monkeypatch, POST)
        rig.ask_answer = False
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("post", {"topic": "понедельник"})
        assert rig.api_calls("sendMessage") == [] and not (rig.mem / "telegram.json").exists()
    assert e.value.code == "denied" and "не разрешил" in e.value.message


async def test_private_word_from_core_drops_the_post(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, terms=["Анна Петрова"]) as rig:
        rig.stub_model(monkeypatch, "Анне сегодня снова не повезло с кофе.")             # падеж не спасает
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("post", {"topic": "кофе"})
        assert rig.api_calls("sendMessage") == []
    assert e.value.code == "denied" and "приватност" in e.value.message


async def test_filter_does_not_fire_on_words_that_only_contain_a_name(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, terms=["Аня"]) as rig:
        rig.stub_model(monkeypatch, "Баня при лаборатории закрыта на испытания.")
        assert (await rig.core.call_tool("post", {})).startswith("опубликован пост")


@pytest.mark.parametrize("bad", ["Смотрите https://example.com", "Пишите @someone", "Звоните +7 999 123 45 67",
                                 "Мой пароль никому не скажу"])
async def test_old_regexes_still_work(tmp_path, monkeypatch, bad):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.stub_model(monkeypatch, bad)
        with pytest.raises(ToolError):
            await rig.core.call_tool("post", {})
        assert rig.api_calls("sendMessage") == []


async def test_no_terms_from_core_means_unavailable(tmp_path, monkeypatch):
    """TG-5: пока списка личных слов не было, посты и отправка не делаются."""
    async with Rig(tmp_path, monkeypatch, privacy_ready=False) as rig:
        rig.stub_model(monkeypatch, POST)
        assert not rig.mod.privacy.ready
        for tool, args in (("post", {}), ("send", {"chat": "Маша", "text": "привет"}), ("draft_write", {"topic": "x"})):
            with pytest.raises(ToolError) as e:
                await rig.core.call_tool(tool, args)
            assert e.value.code == "unavailable", tool
        assert rig.api_calls("sendMessage") == []


async def test_terms_are_refreshed_and_last_list_survives_core_errors(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, terms=["Борис"]) as rig:
        assert rig.mod.privacy.leaks("Борису привет") and not rig.mod.privacy.leaks("привет")
        rig.terms = ["Ольга"]
        assert await rig.mod.privacy.refresh()
        assert rig.mod.privacy.leaks("Ольге привет") and not rig.mod.privacy.leaks("Борису привет")

        @rig.core.on_request("core.privacy_terms")
        def broken(body):
            raise ToolError("ядро занято", code="unavailable")
        assert not await rig.mod.privacy.refresh()
        assert rig.mod.privacy.ready and rig.mod.privacy.leaks("Ольге привет")          # прежний список остался


async def test_privacy_loop_asks_core_right_after_connect(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, privacy_ready=False) as rig:
        task = asyncio.create_task(rig.mod._privacy_loop())
        try:
            await rig.core.wait_for(lambda: rig.mod.privacy.ready)
        finally:
            task.cancel()
        assert [m.type for m in rig.core.messages if m.type == "core.privacy_terms"] == ["core.privacy_terms"]


async def test_voice_post_uses_core_tts_ogg(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.stub_model(monkeypatch, POST)
        await rig.core.call_tool("post", {"voice": True})
        (method, data, files), = rig.uploads
        assert rig.tts_calls == [{"text": POST, "format": "ogg"}]
        assert (method, data["chat_id"], data["caption"], files["voice"][1]) == ("sendVoice", "@chan", POST, b"OggS-voice")
        assert rig.api_calls("sendMessage") == []
        assert not (tmp_path / "tts_test.ogg").exists()                      # забранный файл удалён


async def test_voice_failure_falls_back_to_text(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, settings={"telegram_voice": True}) as rig:
        rig.stub_model(monkeypatch, POST)

        @rig.core.on_request("core.tts")
        def nope(body):
            raise ToolError("синтез речи не удался", code="unavailable")
        await rig.core.call_tool("post", {})
        assert rig.api_calls("sendMessage") == [{"chat_id": "@chan", "text": POST}]


async def test_post_without_token_is_unavailable(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, token=None) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("post", {})
    assert e.value.code == "unavailable" and "не настроен" in e.value.message


async def test_wrong_channel_rights_fail_verification(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        async def call(method, _timeout=15, **params):
            if method == "getMe":
                return {"id": 1, "username": "b"}
            if method == "getChat":
                return {"title": "К"}
            return {"status": "member"}
        rig.mod.channel._call = call
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("post", {})
    assert "проверку" in e.value.message


# ── черновики (TG-6) ──

def make_shot(rig, name="shot_20261010_120000_screen.png", data=b"png"):
    d = rig.mem / "screenshots"
    d.mkdir(exist_ok=True)
    (d / name).write_bytes(data)
    return name


async def test_draft_flow_with_screenshot_from_shared_folder(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        call = rig.core.call_tool
        assert await call("draft_show", {}) == "черновик пуст"
        await call("draft_write", {"text": "Первая мысль про кофе."})
        await call("draft_append", {"text": "Вторая мысль."})
        assert await call("draft_replace", {"find": "Вторая", "replace": "Третья"}) == "заменено"
        name = make_shot(rig)
        assert name in await call("draft_attach", {})                         # shot=last
        shown = await call("draft_show", {})
        assert "Первая мысль про кофе.\n\nТретья мысль." in shown and name in shown
        out = await call("draft_publish", {})
        assert "со снимками: 1" in out
        (method, data, files), = rig.uploads
        assert method == "sendPhoto" and data["caption"].startswith("Первая мысль") and files["photo"][1] == b"png"
        assert await call("draft_show", {}) == "черновик пуст"                  # после публикации очищен


async def test_draft_attach_unknown_and_limit(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("draft_attach", {"shot": "нет.png"})
        assert e.value.code == "bad_request"
        for i in range(4):
            await rig.core.call_tool("draft_attach", {"shot": make_shot(rig, f"shot_{i}.png")})
        with pytest.raises(ToolError):
            await rig.core.call_tool("draft_attach", {"shot": make_shot(rig, "shot_9.png")})
        assert "снято снимков: 4" == await rig.core.call_tool("draft_detach", {})


async def test_module_only_reads_the_screenshots_folder(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert await rig.core.call_tool("draft_show", {}) == "черновик пуст"
        with pytest.raises(ToolError):
            await rig.core.call_tool("draft_attach", {})
        assert not (rig.mem / "screenshots").exists()                          # папку модуль не создаёт


async def test_draft_publish_with_private_word_is_blocked_and_kept(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, terms=["Борис"]) as rig:
        await rig.core.call_tool("draft_write", {"text": "Борис снова проспал испытание."})
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("draft_publish", {})
        assert e.value.code == "denied" and rig.api_calls("sendMessage") == []
        assert "Борис" in await rig.core.call_tool("draft_show", {})           # черновик остался


async def test_draft_publish_empty(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("draft_publish", {})
    assert e.value.code == "bad_request"


async def test_draft_write_and_edit_compose_through_local_model(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.stub_model(monkeypatch, "Новый текст про кофе.")
        assert "Новый текст" in await rig.core.call_tool("draft_write", {"topic": "кофе"})
        rig.stub_model(monkeypatch, "Короче про кофе.")
        assert "Короче" in await rig.core.call_tool("draft_edit", {"instruction": "короче"})
        assert "Короче про кофе." in await rig.core.call_tool("draft_show", {})
        await rig.core.call_tool("draft_clear", {})
        with pytest.raises(ToolError):
            await rig.core.call_tool("draft_edit", {"instruction": "короче"})


# ── отправка с её аккаунта ──

async def test_send_check_reports_private_words(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, terms=["Анна"]) as rig:
        assert await rig.core.check_tool("send", {"chat": "Маша", "text": "Привет, Анна просила передать"}) is not None
        assert await rig.core.check_tool("send", {"chat": "Маша", "text": "Привет"}) is None
        danger = await rig.core.check_tool("send", {"chat": "Маша", "text": "от Анны привет"})
        assert "Анн" not in danger                                              # причина личное не повторяет


async def test_send_blocks_private_words_and_sends_clean_text(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, terms=["Анна"]) as rig:
        async def entity(client, chat):
            return ("chat", chat)
        rig.mod.user._entity = entity
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("send", {"chat": "Маша", "text": "Анна просила привет"})
        assert e.value.code == "denied" and rig.tl.sent == []
        assert await rig.core.call_tool("send", {"chat": "Маша", "text": "привет"}) == "отправлено в «Маша»"
        assert rig.tl.sent == [(("chat", "Маша"), "привет")]


async def test_account_tools_explain_missing_setup(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:                               # ключей и сессии нет
        for tool, args in (("chats", {}), ("read", {"chat": "Маша"}), ("send", {"chat": "Маша", "text": "привет"})):
            with pytest.raises(ToolError) as e:
                await rig.core.call_tool(tool, args)
            assert e.value.code == "unavailable" and "TELEGRAM_API_ID" in e.value.message, tool


# ── настройки и фон ──

async def test_set_periodic_uses_core_set_setting(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert "включены" in await rig.core.call_tool("set_periodic", {"enabled": True})
        assert "выключены" in await rig.core.call_tool("set_periodic", {"enabled": False})
        assert "будет писать" in await rig.core.call_tool("set_after_run", {"enabled": True})
        assert "больше не будет" in await rig.core.call_tool("set_after_run", {"enabled": False})
        with pytest.raises(ToolError):
            await rig.core.call_tool("set_periodic", {"enabled": "да"})
    assert rig.tool_calls == [
        {"tool": "core.set_setting", "args": {"key": "telegram", "value": True}},
        {"tool": "core.set_setting", "args": {"key": "telegram", "value": False}},
        {"tool": "core.set_setting", "args": {"key": "telegram_after_run", "value": True}},
        {"tool": "core.set_setting", "args": {"key": "telegram_after_run", "value": False}}]


async def test_core_set_setting_refusal_reaches_the_caller(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        @rig.core.on_request("core.tool.call")
        def refuse(body):
            raise ToolError("настройка не разрешена", code="denied")
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("set_periodic", {"enabled": True})
    assert e.value.code == "denied"


def tick_settings(**more):
    return dict(module_settings={"every_mins": 30, "post_hours_from": 0, "post_hours_to": 24}, **more)


async def test_periodic_tick_posts_when_enabled(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, settings={"telegram": True}, **tick_settings()) as rig:
        rig.stub_model(monkeypatch, POST)
        await rig.mod.channel._tick()
        assert rig.api_calls("sendMessage") == [{"chat_id": "@chan", "text": POST}]
        await rig.mod.channel._tick()                                            # слишком рано для следующего
        assert len(rig.api_calls("sendMessage")) == 1


async def test_periodic_tick_respects_switches(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, settings={"telegram": False}, **tick_settings()) as rig:
        rig.stub_model(monkeypatch, POST)
        await rig.mod.channel._tick()                                            # периодические выключены
        assert rig.api_calls("sendMessage") == []
        await rig.core.update_settings({"telegram": True, "dnd": True})
        await rig.core.wait_for(lambda: rig.client.settings.get("dnd"))
        await rig.mod.channel._tick()                                            # «не отвлекать»
        assert rig.api_calls("sendMessage") == []
        await rig.core.update_settings({"dnd": False, "quiet_until": time.time() + 600})
        await rig.core.wait_for(lambda: not rig.client.settings.get("dnd"))
        await rig.mod.channel._tick()                                            # режим по таймеру
        assert rig.api_calls("sendMessage") == []
        await rig.core.update_settings({"quiet_until": 0.0})
        await rig.core.wait_for(lambda: rig.client.settings.get("quiet_until") == 0.0)
        await rig.mod.channel._tick()
        assert len(rig.api_calls("sendMessage")) == 1


async def test_periodic_tick_hours_and_confirmation(tmp_path, monkeypatch):
    hour = time.localtime().tm_hour
    off = {"every_mins": 30, "post_hours_from": (hour + 2) % 24, "post_hours_to": (hour + 3) % 24 or 24}
    async with Rig(tmp_path, monkeypatch, settings={"telegram": True}, module_settings=off) as rig:
        rig.stub_model(monkeypatch, POST)
        await rig.mod.channel._tick()
        assert rig.api_calls("sendMessage") == []                                # ночью (вне часов публикации) молчит
    async with Rig(tmp_path / "b", monkeypatch, settings={"telegram": True, "telegram_confirm": True},
                   **tick_settings()) as rig:
        rig.stub_model(monkeypatch, POST)
        rig.ask_answer = False
        await rig.mod.channel._tick()
        assert len(rig.asks) == 1 and rig.api_calls("sendMessage") == []


async def test_every_mins_falls_back_to_environment(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert rig.mod.channel.every_mins() == 180
        monkeypatch.setenv("TELEGRAM_EVERY_MINS", "60")
        assert rig.mod.channel.every_mins() == 60                                # настройка не менялась: запасной вариант
    async with Rig(tmp_path / "c", monkeypatch, module_settings={"every_mins": 90}) as rig:
        assert rig.mod.channel.every_mins() == 90                                # настройка модуля важнее переменной
