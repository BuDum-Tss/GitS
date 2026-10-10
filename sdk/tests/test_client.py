"""ModuleClient против FakeCore (настоящий aiohttp-сервер на 127.0.0.1): MOD-4…MOD-14, MOD-23…MOD-25."""
import asyncio
import copy
import logging

import pytest
from helpers import MANIFEST, make_client, running

import glados_module.client as client_mod
from glados_module import ManifestError, ModuleClient, ToolError
from glados_module import protocol as P
from glados_module.testing import NO_REPLY, FakeCore

# ---------------------------------------------------------------- рукопожатие


async def test_handshake(core):
    client = make_client(core)
    async with running(client, core):
        hello = core.hellos[0]
        assert hello["module_id"] == "demo" and hello["protocol"] == 1
        assert len(hello["instance"]) == 8 and isinstance(hello["pid"], int)
        assert hello["manifest"]["id"] == "demo" and hello["manifest"]["tools"][0]["name"] == "ping"
        assert client.connected and client.enabled and client.instance == hello["instance"]
        assert client.settings == {"streamer": False, "dnd": False}      # только shared_settings модуля
        assert client.module_settings == {"poll_seconds": 5}              # значения по умолчанию из схемы
        assert client.profile["id"] == "assistant" and client.battery["percent"] == 100
        assert client.core_version == "fake"
    assert core.violations == [] and core.bad_messages == []              # MOD-12: до welcome только hello


async def test_manifest_always_subscribes_to_module_topic(core):
    client = make_client(core)
    async with running(client, core):
        assert "module" in core.hellos[0]["manifest"]["subscribe"]
    assert "module" not in MANIFEST["subscribe"]                         # исходный словарь не меняется


async def test_welcome_with_module_settings_and_disabled():
    async with FakeCore(enabled=False, settings={"streamer": True, "dnd": False, "other": 1},
                        module_settings={"poll_seconds": 9}) as core:
        client = make_client(core)
        async with running(client, core):
            assert not client.enabled
            assert client.settings == {"streamer": True, "dnd": False}
            assert client.module_settings == {"poll_seconds": 9}


async def test_bad_token_gets_401_and_no_hello(core):
    client = make_client(core, token="чужой")
    async with running(client, core, connected=False):
        await core.wait_for(lambda: core.auth_failures >= 2)              # пробует снова
    assert core.hellos == [] and core.connections == 0


async def test_token_from_env_then_file(core, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("GLADOS_MODULE_TOKEN", core.token)
    client = make_client(core, token=None)
    async with running(client, core):
        assert len(core.hellos) == 1
    # без переменной токен читается из файла ядра
    monkeypatch.delenv("GLADOS_MODULE_TOKEN")
    P.write_token_file(core.token)
    client2 = make_client(core, token=None)
    async with running(client2, core):
        assert len(core.hellos) == 2


async def test_no_token_anywhere_keeps_trying(core, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("GLADOS_MODULE_TOKEN", raising=False)
    client = make_client(core, token=None)
    async with running(client, core, connected=False):
        await asyncio.sleep(0.2)
        assert core.connections == 0
        P.write_token_file(core.token)                                     # ядро запустилось и записало токен
        await core.wait_connected()


async def test_url_from_env(core, monkeypatch):
    monkeypatch.setenv("GLADOS_CORE_URL", core.url)
    client = make_client(core)
    client._url = None
    assert client.url == core.url
    async with running(client, core):
        assert client.connected


async def test_token_never_logged(core, caplog):
    caplog.set_level(logging.DEBUG, logger="glados_module")
    client = make_client(core)
    async with running(client, core):
        await core.call_tool("ping", {"text": "x"})
    assert core.token not in caplog.text


# ---------------------------------------------------------------- проверки до подключения (MOD-23)


def test_invalid_manifest_raises_before_connect():
    bad = copy.deepcopy(MANIFEST)
    bad["version"] = "x"
    with pytest.raises(ManifestError) as e:
        ModuleClient(bad)
    assert any("version" in p for p in e.value.problems)


async def test_missing_tool_handler_raises_on_run(core):
    client = make_client(core, handlers=False)
    client.tool("ping")(lambda args: asyncio.sleep(0))
    with pytest.raises(RuntimeError) as e:
        await client.run()
    assert "slow" in str(e.value) and "ping" not in str(e.value)
    assert core.connections == 0


async def test_danger_check_requires_check_handler(core):
    client = make_client(core, handlers=False)
    for name in ("ping", "slow", "refuse", "boom", "huge", "weird", "wipe"):
        client.tool(name)(lambda args: asyncio.sleep(0))
    with pytest.raises(RuntimeError, match="wipe"):
        await client.run()


def test_register_unknown_names_fail_early(core):
    client = make_client(core, handlers=False)
    with pytest.raises(ValueError):
        client.tool("нет_такого")
    with pytest.raises(ValueError):
        client.check("нет_такого")
    with pytest.raises(ValueError):
        client.on_topic("weather")
    m = copy.deepcopy(MANIFEST)
    m["subscribe"] = []
    with pytest.raises(ValueError, match="subscribe"):
        ModuleClient(m).on_topic("presence")


# ---------------------------------------------------------------- инструменты (MOD-8, MOD-9, MOD-24)


async def test_tool_call(core):
    client = make_client(core)
    async with running(client, core):
        assert await core.call_tool("ping", {"text": "привет"}) == {"pong": "привет"}
        assert await core.call_tool("demo.ping", {"text": "и с префиксом"}) == {"pong": "и с префиксом"}
        result = core.messages_of("result")[0]
        assert result.re is not None and result.body["ok"] is True


async def test_tool_error_becomes_error_result(core):
    client = make_client(core)
    async with running(client, core):
        with pytest.raises(ToolError) as e:
            await core.call_tool("refuse")
        assert e.value.code == "denied" and e.value.message == "пользователь не разрешил"
        raw = core.messages_of("result")[-1].body
        assert raw == {"ok": False, "error": {"code": "denied", "message": "пользователь не разрешил"}}


async def test_exception_becomes_internal_without_traceback(core, caplog):
    client = make_client(core)
    with caplog.at_level(logging.ERROR, logger="glados_module"):
        async with running(client, core):
            with pytest.raises(ToolError) as e:
                await core.call_tool("boom")
    assert e.value.code == "internal" and e.value.message == "сломалось внутри"
    assert "Traceback" not in e.value.message
    assert "Traceback" in caplog.text and "ValueError" in caplog.text    # трассировка есть в журнале модуля


async def test_unknown_tool(core):
    client = make_client(core)
    async with running(client, core):
        with pytest.raises(ToolError) as e:
            await core.call_tool("nope")
        assert e.value.code == "unknown"


async def test_bad_args_type(core):
    client = make_client(core)
    async with running(client, core):
        with pytest.raises(ToolError) as e:
            await core.request("tool.call", {"tool": "ping", "args": [1]})
        assert e.value.code == "bad_request"


async def test_long_tool_does_not_block_other_messages(core):
    client = make_client(core)
    async with running(client, core):
        slow = asyncio.create_task(core.call_tool("slow", timeout=5))
        await asyncio.sleep(0.1)
        assert not slow.done()
        assert await core.call_tool("ping", {"text": "быстро"}) == {"pong": "быстро"}   # MOD-24
        client.gate.set()
        assert await slow == "готово"


async def test_unserializable_and_huge_results_become_internal(core):
    client = make_client(core)
    async with running(client, core):
        for name in ("weird", "huge"):
            with pytest.raises(ToolError) as e:
                await core.call_tool(name)
            assert e.value.code == "internal"
        assert await core.call_tool("ping") == {"pong": ""}              # соединение живо


async def test_tool_check(core):
    client = make_client(core)
    async with running(client, core):
        assert await core.check_tool("wipe", {"path": "C:/важное"}) == "это удалит файлы"
        assert await core.check_tool("wipe", {"path": "C:/мусор"}) is None
        assert await core.check_tool("ping") is None                     # обработчика нет: опасности нет


async def test_tool_call_when_disabled_is_unavailable():
    async with FakeCore(enabled=False) as core:
        client = make_client(core)
        async with running(client, core):
            with pytest.raises(ToolError) as e:
                await core.call_tool("ping")
            assert e.value.code == "unavailable"


# ---------------------------------------------------------------- неизвестные сообщения (MOD-10, MOD-11)


async def test_unknown_types_are_ignored_and_unknown_requests_answered(core, caplog):
    client = make_client(core)
    with caplog.at_level(logging.WARNING, logger="glados_module"):
        async with running(client, core):
            await core.send("telepathy", {"a": 1})                       # неизвестный тип: игнор, соединение живо
            with pytest.raises(ToolError) as e:
                await core.request("deliver", {"target": "owner", "text": "т"})      # известный запрос без обработчика
            assert e.value.code == "unknown"
            with pytest.raises(ToolError):
                await core.request("graph.get", {})
            assert await core.call_tool("ping", {"text": "жив"}) == {"pong": "жив"}
    assert "telepathy" in caplog.text


async def test_unknown_body_fields_ignored(core):
    client = make_client(core)
    async with running(client, core):
        assert await core.request("tool.call", {"tool": "ping", "args": {"text": "а"}, "новое": 1}) == {"pong": "а"}


async def test_on_request_handlers(core):
    client = make_client(core)
    got = []

    @client.on_request("deliver")
    async def deliver(body):
        return {"delivered": body["target"] == "owner"}

    @client.on_request("speak")
    async def speak(body):
        got.append(body)

    @client.on_request("graph.get")
    async def graph(body):
        raise ToolError("граф недоступен", code="unavailable")

    async with running(client, core):
        assert await core.request("deliver", {"target": "owner", "text": "т"}) == {"delivered": True}
        await core.send("speak", {"text": "привет"})
        await core.wait_for(lambda: got)
        assert got == [{"text": "привет"}]
        with pytest.raises(ToolError) as e:
            await core.request("graph.get", {})
        assert e.value.code == "unavailable"
    assert len(core.messages_of("result")) == 2                              # speak не запрос: result на него нет


# ---------------------------------------------------------------- темы и состояние


async def test_topics_update_state_and_call_handlers(core):
    client = make_client(core)
    seen = []

    @client.on_topic("presence")
    async def presence(data):
        seen.append(data)

    async with running(client, core):
        await core.topic("presence", {"speaking": True, "busy": False})
        await core.wait_for(lambda: seen)
        assert seen == [{"speaking": True, "busy": False}]
        await core.update_settings(settings={"streamer": True, "secret_key": "не для модуля"},
                                   module_settings={"poll_seconds": 7})
        await core.wait_for(lambda: client.settings.get("streamer") is True)
        assert client.settings == {"streamer": True, "dnd": False} and client.module_settings == {"poll_seconds": 7}
        await core.topic("profile", {"id": "rimworld", "title": "RimWorld", "focus_modules": ["rimworld"]})
        await core.topic("battery", {"percent": 40, "sleeping": False})
        await core.wait_for(lambda: client.profile.get("id") == "rimworld" and client.battery.get("percent") == 40)


async def test_failing_topic_handler_does_not_break_connection(core):
    client = make_client(core)

    @client.on_topic("presence")
    async def presence(data):
        raise RuntimeError("плохой обработчик")

    async with running(client, core):
        await core.topic("presence", {})
        assert await core.call_tool("ping", {"text": "жив"}) == {"pong": "жив"}


# ---------------------------------------------------------------- события, речь, состояние (MOD-14, MOD-23)


async def test_event_say_emotion_state(core):
    client = make_client(core)
    async with running(client, core):
        assert await client.event("demo.thing", "Что-то случилось", urgency_hint=4, data={"x": 1},
                                  sender={"id": "d:1", "name": "Маша"}, memorable=True)
        assert await client.say("Борис уснул.", mode="verbatim", emotion="angry", key="demo.k")
        assert await client.say("Факт.")
        assert await client.emotion("angry", "борис уснул")
        assert await client.state("всё хорошо", {"colonists": 3})
        await core.wait_message("state")
    assert core.events == [{"kind": "demo.thing", "text": "Что-то случилось", "urgency_hint": 4, "data": {"x": 1},
                            "sender": {"id": "d:1", "name": "Маша"}, "memorable": True}]
    says = [m.body for m in core.messages_of("say")]
    assert says[0] == {"text": "Борис уснул.", "mode": "verbatim", "emotion": "angry", "key": "demo.k"}
    assert says[1] == {"text": "Факт.", "mode": "fact"}
    assert core.messages_of("emotion")[0].body == {"emotion": "angry", "cause": "борис уснул"}
    assert core.states["demo"] == {"summary": "всё хорошо", "data": {"colonists": 3}}
    assert core.messages_of("event")[0].id.startswith("m-")


async def test_event_validation_and_clipping(core):
    client = make_client(core)
    async with running(client, core):
        with pytest.raises(ValueError):
            await client.event("other.thing", "чужой модуль")
        with pytest.raises(ValueError):
            await client.event("demo.thing", "")
        with pytest.raises(TypeError):
            await client.event("demo.thing", "x", urgensy=1)
        with pytest.raises(ValueError):
            await client.event("demo.thing", "x", urgency_hint=9)
        with pytest.raises(ValueError):
            await client.say("x", mode="loud")
        await client.event("demo.thing", "я" * 1500)
        await client.state("с" * 500)
        await core.wait_message("state")
    assert len(core.events[0]["text"]) == 1000
    assert len(core.states["demo"]["summary"]) == 300


async def test_disabled_module_drops_events_and_speech():
    async with FakeCore(enabled=False) as core:
        client = make_client(core)
        async with running(client, core):
            assert not client.enabled
            assert not await client.event("demo.thing", "тихо")
            assert not await client.say("тихо")
            assert not await client.emotion("angry", "тихо")
            await client.state("жив", {})                                # сводка для дашборда не молчит
            await core.wait_message("state")
            await asyncio.sleep(0.1)
            assert core.events == [] and core.messages_of("say") == [] and core.messages_of("emotion") == []
            await core.set_enabled(True)                                 # тема module (MOD-14)
            await core.wait_for(lambda: client.enabled)
            assert await client.event("demo.thing", "теперь можно")
            await core.wait_message("event")
            await core.set_enabled(False)
            await core.wait_for(lambda: not client.enabled)
            assert not await client.say("снова тихо")


async def test_sending_without_connection_is_dropped(core):
    client = make_client(core)
    assert not client.connected
    assert not await client.event("demo.thing", "никому")
    assert not await client.say("никому")
    assert not await client.state("никому")
    with pytest.raises(ToolError) as e:
        await client.request("core.persona", {})
    assert e.value.code == "unavailable"
    with pytest.raises(ToolError):
        await client.ask("вопрос")


# ---------------------------------------------------------------- запросы к ядру и вопросы (MOD-25)


async def test_request_returns_value(core):
    @core.on_request("core.tts")
    def tts(body):
        return {"path": "C:\\tmp\\a.wav", "duration_ms": 1200, "echo": body["text"]}

    client = make_client(core)
    async with running(client, core):
        value = await client.request("core.tts", {"text": "привет", "format": "wav"}, timeout=5)
        assert value == {"path": "C:\\tmp\\a.wav", "duration_ms": 1200, "echo": "привет"}


async def test_request_error_raises_tool_error(core):
    @core.on_request("core.tts")
    async def tts(body):
        raise ToolError("голос недоступен", code="unavailable")

    client = make_client(core)
    async with running(client, core):
        with pytest.raises(ToolError) as e:
            await client.request("core.tts", {"text": "x"})
        assert e.value.code == "unavailable" and "голос" in e.value.message
        with pytest.raises(ToolError) as e:
            await client.request("core.lookup", {"query": "q"})           # у FakeCore обработчика нет
        assert e.value.code == "unknown"


async def test_request_timeout(core):
    core.handlers["core.persona"] = lambda body: NO_REPLY
    client = make_client(core)
    async with running(client, core):
        with pytest.raises(asyncio.TimeoutError):
            await client.request("core.persona", {}, timeout=0.2)
        assert client._pending == {}


async def test_ask_answers(core):
    asked = []

    @core.on_request("core.ask")
    def ask(body):
        asked.append(body)
        return {"answer": {"approval": True, "choice": body.get("options", [None])[-1], "text": "Вася"}[body["kind"]],
                "by": "user"}

    client = make_client(core)
    async with running(client, core):
        assert await client.ask("Удалить?") is True
        assert await client.ask("Что делать?", kind="choice", options=["а", "б"], timeout_s=60,
                                context={"job_id": "j-7a1c0000"}, urgency_hint=3) == "б"
        assert await client.ask("Как назвать?", kind="text") == "Вася"
    assert asked[0] == {"kind": "approval", "question": "Удалить?", "timeout_s": 300}
    assert asked[1] == {"kind": "choice", "question": "Что делать?", "options": ["а", "б"], "timeout_s": 60,
                        "context": {"job_id": "j-7a1c0000"}, "urgency_hint": 3}


async def test_ask_core_timeout_gives_false_or_none(core):
    @core.on_request("core.ask")
    def ask(body):
        return {"answer": False if body["kind"] == "approval" else None, "by": "timeout"}

    client = make_client(core)
    async with running(client, core):
        assert await client.ask("Можно?", timeout_s=1) is False
        assert await client.ask("Что?", kind="choice", options=["а"], timeout_s=1) is None


async def test_ask_waits_timeout_plus_grace_then_raises(core, monkeypatch):
    monkeypatch.setattr(client_mod, "ASK_GRACE_S", 0.2)
    core.handlers["core.ask"] = lambda body: NO_REPLY
    client = make_client(core)
    async with running(client, core):
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            await client.ask("Тишина?", timeout_s=1)
        assert 1.15 <= loop.time() - start < 2.5                           # timeout_s + запас (30 с в бою)


async def test_ask_error_and_validation(core):
    @core.on_request("core.ask")
    def ask(body):
        raise ToolError("нет пользователя", code="unavailable")

    client = make_client(core)
    async with running(client, core):
        with pytest.raises(ToolError) as e:
            await client.ask("Есть кто?")
        assert e.value.code == "unavailable"
        with pytest.raises(ValueError):
            await client.ask("Выбери", kind="choice")                      # нет options
        with pytest.raises(ValueError):
            await client.ask("", kind="approval")


async def test_pending_request_fails_when_connection_drops(core):
    core.handlers["core.persona"] = lambda body: NO_REPLY
    client = make_client(core)
    async with running(client, core):
        pending = asyncio.create_task(client.request("core.persona", {}, timeout=10))
        await core.wait_message("core.persona")
        await core.drop()
        with pytest.raises(ToolError) as e:
            await pending
        assert e.value.code == "unavailable"


# ---------------------------------------------------------------- переподключение, вытеснение, reject, shutdown


async def test_reconnect_after_drop_sends_hello_again_and_calls_hook(core):
    client = make_client(core)
    reconnects = []

    @client.on_reconnect
    async def again():
        reconnects.append(1)

    async with running(client, core):
        assert reconnects == []                                            # после первого подключения хук не зовётся
        await client.state("до обрыва", {"n": 1})
        await core.wait_message("state")
        await core.drop()
        await core.wait_connected(count=2)
        assert [h["instance"] for h in core.hellos] == [client.instance] * 2    # тот же процесс: тот же instance
        await core.wait_for(lambda: reconnects)
        assert core.hellos[0]["manifest"] == core.hellos[1]["manifest"]
        assert await core.call_tool("ping", {"text": "снова"}) == {"pong": "снова"}
        await core.wait_message("state", nth=2)                            # последняя сводка переслана сама
        assert core.messages_of("state")[1].body == core.messages_of("state")[0].body


async def test_reconnect_after_abort_without_close_frame(core):
    client = make_client(core)
    async with running(client, core):
        await core.drop(abort=True)
        await core.wait_connected(count=2)
        assert client.connected


async def test_reconnect_when_core_comes_back_later():
    core = FakeCore()
    await core.start()
    client = make_client(core)
    async with running(client, core):
        port = core._port
        await core.stop()
        await asyncio.sleep(0.2)                                           # ядра нет: клиент пробует снова
        assert not client.connected
        core2 = FakeCore(token=core.token)
        core2._port = 0
        # поднимаем ядро на том же порту
        await _start_on_port(core2, port)
        try:
            await core2.wait_connected(timeout=5)
            assert client.connected
        finally:
            await core2.stop()


async def _start_on_port(core: FakeCore, port: int) -> None:
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/module/ws", core._handle)
    core._runner = web.AppRunner(app, shutdown_timeout=1.0)
    await core._runner.setup()
    await web.TCPSite(core._runner, "127.0.0.1", port).start()
    core._port = port


async def test_replaced_connection_exits_without_reconnect(core):
    first = make_client(core)
    second = make_client(core)
    task1 = asyncio.create_task(first.run())
    await core.wait_connected()
    task2 = asyncio.create_task(second.run())
    try:
        code = await asyncio.wait_for(task1, 5)                            # MOD-13: код 4000 -> run() вернулся
        assert code == 0
        await core.wait_for(lambda: len(core.hellos) == 2)
        await asyncio.sleep(0.3)
        assert len(core.hellos) == 2                                       # первый не переподключился
        assert second.connected and not first.connected
    finally:
        await second.stop()
        await asyncio.wait_for(task2, 5)


async def test_kick_with_4000_stops_client(core):
    client = make_client(core)
    task = asyncio.create_task(client.run())
    await core.wait_connected()
    await core.kick()
    assert await asyncio.wait_for(task, 5) == 0
    await asyncio.sleep(0.2)
    assert len(core.hellos) == 1


async def test_reject_about_protocol_exits_with_code_2(core, caplog):
    core.reject_next = ["protocol 2 не поддерживается"]
    client = make_client(core)
    with caplog.at_level(logging.ERROR, logger="glados_module"):
        code = await asyncio.wait_for(client.run(), 5)
    assert code == P.EXIT_REJECTED == 2
    assert len(core.rejected) == 1 and core.hellos == []
    assert "не поддерживается" in caplog.text and "кодом 2" in caplog.text


async def test_reject_about_manifest_exits_with_code_2(core):
    core.reject_next = ["манифест не прошёл проверку: version"]
    assert await asyncio.wait_for(make_client(core).run(), 5) == 2


async def test_other_reject_is_retried(core):
    core.reject_next = ["голос уже занят другим модулем"]
    client = make_client(core)
    async with running(client, core):
        assert len(core.rejected) == 1 and len(core.hellos) == 1 and client.connected


async def test_shutdown_message_stops_client_and_calls_hook(core):
    client = make_client(core)
    reasons = []

    @client.on_shutdown
    async def bye(reason):
        reasons.append(reason)

    task = asyncio.create_task(client.run())
    await core.wait_connected()
    await core.shutdown("выключение ядра")
    assert await asyncio.wait_for(task, 5) == 0
    assert reasons == ["выключение ядра"]
    await asyncio.sleep(0.2)
    assert len(core.hellos) == 1                                           # не переподключился


async def test_stop_closes_connection(core):
    client = make_client(core)
    task = asyncio.create_task(client.run())
    await core.wait_connected()
    await client.stop()
    assert await asyncio.wait_for(task, 5) == 0
    await core.wait_disconnected()


async def test_core_unreachable_keeps_trying_and_stop_works():
    client = ModuleClient(MANIFEST, url="ws://127.0.0.1:1/module/ws", token="t")
    client.reconnect_delays = (0.05,)
    for name in ("ping", "slow", "refuse", "boom", "huge", "weird", "wipe"):
        client.tool(name)(lambda args: asyncio.sleep(0))
    client.check("wipe")(lambda args: asyncio.sleep(0))
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.3)
    assert not task.done()
    await client.stop()
    assert await asyncio.wait_for(task, 5) == 0


# ---------------------------------------------------------------- задачи (раздел 9, MOD-19…MOD-22)


async def test_job_update_merges_and_validates(core):
    client = make_client(core)
    async with running(client, core):
        jid = P.new_job_id()
        assert await client.job_update(jid, title="Работа", state="running", stage="a", progress=0.1, text="идёт")
        body = (await core.wait_message("job.update")).body
        assert body == {"job_id": jid, "title": "Работа", "state": "running", "stage": "a", "progress": 0.1,
                        "text": "идёт"}
        with pytest.raises(ValueError):
            await client.job_update(jid, state="waiting_user", stage="q")      # MOD-20: нужен вопрос
        with pytest.raises(ValueError):
            await client.job_update(jid, state="failed", stage="f")            # MOD-21: нужна ошибка
        with pytest.raises(ValueError):
            await client.job_update("j-1", state="running")
        with pytest.raises(ValueError):
            await client.job_update(P.new_job_id(), progress=0.5)              # у новой задачи нет state
        with pytest.raises(TypeError):
            await client.job_update(jid, colour="red")
        assert client.jobs[jid]["state"] == "running"                          # неудачные вызовы состояние не портят


async def test_job_progress_is_throttled_but_state_changes_go_through(core):
    client = make_client(core)
    client.job_progress_interval = 0.4
    async with running(client, core):
        jid = P.new_job_id()
        assert await client.job_update(jid, title="Т", state="running", stage="a", progress=0.0)
        assert not await client.job_update(jid, progress=0.2)                   # слишком часто
        assert not await client.job_update(jid, progress=0.3, text="ещё")
        question = {"question_id": "q-1", "kind": "approval", "question": "Дальше?"}
        assert await client.job_update(jid, state="waiting_user", question=question)    # смена state: сразу
        assert await client.job_update(jid, state="running", stage="b")                 # смена stage: сразу
        assert not await client.job_update(jid, progress=0.5)
        await asyncio.sleep(0.5)
        assert await client.job_update(jid, progress=0.6)
        assert await client.job_update(jid, state="done", progress=1.0)
        await core.wait_message("job.update", nth=5)
        bodies = [m.body for m in core.messages_of("job.update")]
    # пять отправленных; сжатые обновления прогресса не потеряны, а влиты в следующее сообщение
    assert [b.get("progress") for b in bodies] == [0.0, 0.3, 0.3, 0.6, 1.0]
    assert bodies[1]["state"] == "waiting_user" and bodies[1]["question"]["question_id"] == "q-1"
    assert "question" not in bodies[2]                       # вопрос убран сам при выходе из waiting_user
    assert bodies[-1]["state"] == "done"


async def test_job_answer_cancel_and_list(core):
    client = make_client(core)
    answers, cancelled = [], []

    @client.on_job_answer
    async def answer(job_id, question_id, value):
        answers.append((job_id, question_id, value))

    @client.on_job_cancel
    async def cancel(job_id):
        if job_id != jid:
            raise ToolError("нет такой задачи", code="unknown")
        cancelled.append(job_id)

    jid = P.new_job_id()
    async with running(client, core):
        await client.job_update(jid, title="Т", state="running")
        await client.job_update(P.new_job_id(), title="Готовая", state="done")
        await core.job_answer(jid, "q-1", "утвердить")
        await core.wait_for(lambda: answers)
        assert answers == [(jid, "q-1", "утвердить")]
        assert await core.job_cancel(jid) == {}
        assert cancelled == [jid]
        with pytest.raises(ToolError) as e:
            await core.job_cancel("j-ffffffff")
        assert e.value.code == "unknown"
        jobs = await core.job_list()
        assert {j["state"] for j in jobs} == {"running", "done"} and len(jobs) == 2


async def test_job_cancel_without_handler_is_unknown(core):
    client = make_client(core)
    async with running(client, core):
        with pytest.raises(ToolError) as e:
            await core.job_cancel("j-00000000")
        assert e.value.code == "unknown"


async def test_job_list_keeps_unfinished_and_last_20_finished(core):
    client = make_client(core)
    async with running(client, core):
        running_id = P.new_job_id()
        await client.job_update(running_id, title="Идёт", state="running")
        for _ in range(25):
            await client.job_update(P.new_job_id(), title="Было", state="done")
        jobs = await core.job_list()
        assert len(jobs) == 21 and sum(j["state"] == "running" for j in jobs) == 1


async def test_jobs_are_resent_after_reconnect_by_hook(core):
    client = make_client(core)

    @client.on_reconnect
    async def again():
        await client.resend_jobs()

    async with running(client, core):
        live, finished = P.new_job_id(), P.new_job_id()
        question = {"question_id": "q-1", "kind": "approval", "question": "Дальше?"}
        await client.job_update(live, title="Живая", state="waiting_user", stage="ask", question=question)
        await client.job_update(finished, title="Старая", state="done")
        await core.wait_message("job.update", nth=2)
        await core.drop()
        await core.wait_connected(count=2)
        again_msg = await core.wait_message("job.update", nth=3)           # MOD-22: только незавершённые
        assert again_msg.body["job_id"] == live and again_msg.body["question"]["question_id"] == "q-1"
        await asyncio.sleep(0.2)
        assert len(core.messages_of("job.update")) == 3


async def test_job_update_while_disconnected_is_kept_for_resend(core):
    client = make_client(core)
    jid = P.new_job_id()
    assert not await client.job_update(jid, title="Т", state="running")    # связи нет: не отправлено, но запомнено
    assert client.jobs[jid]["state"] == "running"
    async with running(client, core):
        assert await client.resend_jobs() == 1
        assert (await core.wait_message("job.update")).body["job_id"] == jid


# ---------------------------------------------------------------- ограничения размера


async def test_oversized_outgoing_message_is_refused_locally(core):
    client = make_client(core)
    async with running(client, core):
        with pytest.raises(P.ProtocolError):
            await client.event("demo.thing", "x", data={"blob": "я" * (2 * 1024 * 1024)})
        with pytest.raises(P.ProtocolError):
            await client.request("core.kb.ingest", {"blob": "я" * (2 * 1024 * 1024)})
        assert client.connected
