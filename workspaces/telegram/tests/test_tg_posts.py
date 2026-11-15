"""Сообщения в канал, приватность, настройки и периодический цикл (ТЗ 10: TG-2..TG-6, TG-13..TG-14 со стороны модуля)."""
import asyncio
import contextlib
import time

import pytest
from aiohttp import web
from tg_rig import Rig

from gits_sdk import ToolError

POST = "Протокол испытания номер семь: куб снова не оправдал ожиданий."


async def test_message_publishes_to_channel_and_reports_event(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.stub_model(monkeypatch, POST)
        out = await rig.core.call_tool("message", {"topic": "понедельник"})
        await rig.core.wait_message("event")
        ev = rig.core.events[-1]
        state = rig.read_state()
    assert out == f"опубликован пост: «{POST}»"
    assert rig.api_calls("sendMessage") == [{"chat_id": "@chan", "text": POST}]
    assert rig.asks == []                                                      # публикация разрешения не просит
    assert state["posts"][-1]["text"] == POST and state["last_post"] > 0
    assert ev["kind"] == "telegram.posted" and POST[:40] in ev["text"]
    assert any(r["text"] == "опубликовано: " + POST for r in await rig.journal())


async def test_private_word_from_core_drops_the_post(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, terms=["Анна Петрова"]) as rig:
        rig.stub_model(monkeypatch, "Анне сегодня снова не повезло с кофе.")             # падеж не спасает
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"topic": "кофе"})
        assert rig.api_calls("sendMessage") == []
    assert e.value.code == "denied" and "приватност" in e.value.message


async def test_filter_does_not_fire_on_words_that_only_contain_a_name(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, terms=["Аня"]) as rig:
        rig.stub_model(monkeypatch, "Баня при лаборатории закрыта на испытания.")
        assert (await rig.core.call_tool("message", {"topic": "баня"})).startswith("опубликован пост")


@pytest.mark.parametrize("bad", ["Смотрите https://example.com", "Пишите @someone", "Звоните +7 999 123 45 67",
                                 "Мой пароль никому не скажу"])
async def test_old_regexes_still_work(tmp_path, monkeypatch, bad):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.stub_model(monkeypatch, bad)
        with pytest.raises(ToolError):
            await rig.core.call_tool("message", {"topic": "тест"})
        assert rig.api_calls("sendMessage") == []


async def test_no_terms_from_core_means_unavailable(tmp_path, monkeypatch):
    """TG-5: пока списка личных слов не было, посты и отправка не делаются."""
    async with Rig(tmp_path, monkeypatch, privacy_ready=False) as rig:
        rig.stub_model(monkeypatch, POST)
        assert not rig.mod.privacy.ready
        for args in ({"topic": "x"}, {"text": "привет"}, {"to": "owner", "text": "привет"}, {"to": "Маша", "text": "привет"}):
            with pytest.raises(ToolError) as e:
                await rig.core.call_tool("message", args)
            assert e.value.code == "unavailable", args
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
        await rig.core.call_tool("message", {"topic": "голос", "voice": True})
        (method, data, files), = rig.uploads
        assert rig.tts_calls == [{"text": POST, "format": "ogg"}]
        assert (method, data["chat_id"], data["caption"], files["voice"][1]) == ("sendVoice", "@chan", POST, b"OggS-voice")
        assert rig.api_calls("sendMessage") == []
        assert not (tmp_path / "tts_test.ogg").exists()                      # забранный файл удалён


async def test_voice_failure_falls_back_to_text(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"voice": True}) as rig:
        rig.stub_model(monkeypatch, POST)

        @rig.core.on_request("core.tts")
        def nope(body):
            raise ToolError("синтез речи не удался", code="unavailable")
        await rig.core.call_tool("message", {"topic": "голос"})
        assert rig.api_calls("sendMessage") == [{"chat_id": "@chan", "text": POST}]


async def test_message_without_token_is_unavailable(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, token=None) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"topic": "тест"})
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
            await rig.core.call_tool("message", {"topic": "тест"})
    assert "проверку" in e.value.message


# ── настройки и фон ──

@contextlib.asynccontextmanager
async def dashboard(monkeypatch, status=200):
    """Дашборд ядра: запоминает записи собственных настроек (POST /api/peers/telegram/settings)."""
    got: list[dict] = []

    async def handler(request):
        got.append(await request.json())
        return web.json_response({"ok": status == 200}, status=status)

    app = web.Application()
    app.router.add_post("/api/peers/telegram/settings", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setenv("GITS_DASHBOARD_URL", f"http://127.0.0.1:{port}")
    try:
        yield got
    finally:
        await runner.cleanup()


async def test_settings_changes_own_setting_through_dashboard(tmp_path, monkeypatch):
    async with dashboard(monkeypatch) as got, Rig(tmp_path, monkeypatch) as rig:
        assert "включены" in await rig.core.call_tool("settings", {"periodic": True})
        assert rig.mod.channel.flag("publish")                                   # значение сразу видно модулю
        assert "выключены" in await rig.core.call_tool("settings", {"periodic": False})
        assert "будет писать" in await rig.core.call_tool("settings", {"after_run": True})
        assert "больше не будет" in await rig.core.call_tool("settings", {"after_run": False})
        assert "периодические посты: выключены" in await rig.core.call_tool("settings", {})     # без параметров: текущие
        with pytest.raises(ToolError):
            await rig.core.call_tool("settings", {"periodic": "да"})
    assert got == [{"key": "publish", "value": True}, {"key": "publish", "value": False},
                   {"key": "after_run", "value": True}, {"key": "after_run", "value": False}]
    assert rig.tool_calls == []                                                  # общие настройки ядра не трогаются


async def test_dashboard_refusal_reaches_the_caller(tmp_path, monkeypatch):
    async with dashboard(monkeypatch, status=400), Rig(tmp_path, monkeypatch) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("settings", {"periodic": True})
        assert e.value.code == "denied" and not rig.mod.channel.flag("publish")


async def test_after_run_post_needs_the_after_run_setting(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.stub_model(monkeypatch, POST)
        out = await rig.core.call_tool("message", {"topic": "забег", "after_run": True})
        assert "выключен" in out and rig.api_calls("sendMessage") == []
    async with Rig(tmp_path / "b", monkeypatch, module_settings={"after_run": True}) as rig:
        rig.stub_model(monkeypatch, POST)
        assert (await rig.core.call_tool("message", {"topic": "забег", "after_run": True})).startswith("опубликован пост")
        assert len(rig.api_calls("sendMessage")) == 1


def tick_settings(**own):
    return dict(module_settings={"every_mins": 30, "post_hours_from": 0, "post_hours_to": 24, **own})


async def test_periodic_tick_posts_when_enabled(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, **tick_settings(publish=True)) as rig:
        rig.stub_model(monkeypatch, POST)
        await rig.mod.channel._tick()
        assert rig.api_calls("sendMessage") == [{"chat_id": "@chan", "text": POST}]
        await rig.mod.channel._tick()                                            # слишком рано для следующего
        assert len(rig.api_calls("sendMessage")) == 1


async def test_periodic_tick_respects_switches(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, **tick_settings(publish=False)) as rig:
        rig.stub_model(monkeypatch, POST)
        await rig.mod.channel._tick()                                            # периодические выключены
        assert rig.api_calls("sendMessage") == []
        await rig.core.update_settings({"dnd": True}, module_settings={"publish": True})
        await rig.core.wait_for(lambda: rig.client.settings.get("dnd") and rig.mod.channel.flag("publish"))
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


async def test_periodic_tick_hours_and_no_confirmation(tmp_path, monkeypatch):
    hour = time.localtime().tm_hour
    off = {"every_mins": 30, "post_hours_from": (hour + 2) % 24, "post_hours_to": (hour + 3) % 24 or 24, "publish": True}
    async with Rig(tmp_path, monkeypatch, module_settings=off) as rig:
        rig.stub_model(monkeypatch, POST)
        await rig.mod.channel._tick()
        assert rig.api_calls("sendMessage") == []                                # ночью (вне часов публикации) молчит
    async with Rig(tmp_path / "b", monkeypatch, **tick_settings(publish=True, confirm=True)) as rig:     # старый confirm игнорируется
        rig.stub_model(monkeypatch, POST)
        rig.ask_answer = False
        await rig.mod.channel._tick()
        assert rig.asks == [] and len(rig.api_calls("sendMessage")) == 1


async def test_every_mins_falls_back_to_environment(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert rig.mod.channel.every_mins() == 180
        monkeypatch.setenv("TELEGRAM_EVERY_MINS", "60")
        assert rig.mod.channel.every_mins() == 60                                # настройка не менялась: запасной вариант
    async with Rig(tmp_path / "c", monkeypatch, module_settings={"every_mins": 90}) as rig:
        assert rig.mod.channel.every_mins() == 90                                # настройка модуля важнее переменной
