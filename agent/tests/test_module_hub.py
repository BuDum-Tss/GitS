"""Шина модулей (ТЗ 02, CORE-1…CORE-10, CORE-29…CORE-35): рукопожатие, защита, реестр, инструменты, задачи, запросы."""
import asyncio
import time

import aiohttp
import pytest
from echo_module import build_client
from hub_helpers import RawModule, manifest, running_client, running_core, tool

import approvals
import settings
from environments import builtin
from environments.base import ToolError
from gits_sdk import ModuleClient
from gits_sdk import protocol as P


# ── рукопожатие и защита ────────────────────────────────────────────────────────

async def test_handshake_with_echo_module(tmp_path):
    async with running_core(tmp_path) as core:
        assert P.read_token_file(tmp_path / "module_token") == core.token          # CORE-1: токен в файле
        client = build_client(interval=0.01, url=core.url, token=core.token)
        async with running_client(client, core):
            rec = core.modules.get("echo")
            assert rec.connected and rec.version == "1.0.0" and rec.pid and rec.instance == client.instance
            assert client.enabled and client.profile["id"] == "assistant"
            assert "echo" in core.envs.envs and core.envs.envs["echo"].module == "echo"
            assert core.envs.envs["echo"].state() == (True, "доступно")
        await core.wait_for(lambda: not core.modules.get("echo").connected)
        assert core.envs.envs["echo"].state() == (False, "модуль не подключён")               # CORE-9
        assert core.modules.get("echo").last_disconnect_reason


async def test_wrong_or_missing_token_gives_401(tmp_path):
    async with running_core(tmp_path) as core:
        async with aiohttp.ClientSession() as s:
            for headers in ({"Authorization": "Bearer nope"}, {}, {"Authorization": core.token}):
                with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                    await s.ws_connect(core.url, headers=headers)
                assert e.value.status == 401


async def test_not_loopback_gives_403_before_token_check(tmp_path):
    async with running_core(tmp_path, spoof_remote="10.1.2.3") as core:
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                await s.ws_connect(core.url, headers={"Authorization": f"Bearer {core.token}"})
            assert e.value.status == 403


async def test_ipv6_loopback_and_mapped_ipv4_are_allowed(tmp_path):
    for remote in ("::1", "::ffff:127.0.0.1"):
        async with running_core(tmp_path, spoof_remote=remote) as core:
            raw = RawModule(core, manifest())
            try:
                assert (await raw.hello()).type == "welcome"
            finally:
                await raw.close()


async def test_welcome_body_follows_the_contract(tmp_path):
    state = {"modules": {"raw": {"poll": 9}}, "streamer": True, "dnd": False}
    async with running_core(tmp_path, state) as core:
        m = manifest(shared_settings=["streamer", "nonexistent"],
                     settings_schema={"poll": {"type": "integer", "title": "Опрос", "default": 5},
                                      "mode": {"type": "string", "title": "Режим", "default": "a"}})
        raw = RawModule(core, m)
        try:
            welcome = await raw.hello()
            b = welcome.body
            assert b["protocol"] == 1 and b["enabled"] is True and b["core_version"]
            assert b["settings"] == {"streamer": True}                          # только shared_settings модуля
            assert b["module_settings"] == {"poll": 9, "mode": "a"}             # сохранённое поверх умолчаний
            assert set(b["profile"]) == {"id", "title", "focus_modules"} and b["battery"] == {"percent": 100, "sleeping": False}
        finally:
            await raw.close()


async def test_invalid_manifest_and_bad_hello_are_rejected(tmp_path):
    async with running_core(tmp_path) as core:
        bad = manifest()
        bad["tools"] = [{"name": "X"}]
        for raw, words in ((RawModule(core, bad), "манифест"), (RawModule(core, manifest(), protocol=2), "protocol 2"),
                           (RawModule(core, manifest("core")), "зарезервирован"),
                           (RawModule(core, manifest("room", settings_schema={"enabled": {"type": "boolean", "title": "x", "default": True}})), "enabled")):
            try:
                reply = await raw.hello()
                assert reply.type == "reject" and words in reply.body["reason"], reply.body
            finally:
                await raw.close()
        assert core.hub.connections() == []
        # не hello первым сообщением
        raw = RawModule(core, manifest())
        await raw.open()
        await raw.send("event", {"kind": "raw.ping", "text": "рано"})
        assert (await raw.wait("reject")).body["reason"]
        await raw.close()


async def test_silent_connection_is_closed_after_hello_timeout(tmp_path):
    async with running_core(tmp_path) as core:           # hello_timeout=0.5 с
        raw = RawModule(core, manifest())
        await raw.open()
        await asyncio.wait_for(raw._reader, 3)
        assert raw.close_code == 1008
        await raw.close()


async def test_second_voice_output_is_rejected_without_fatal_words(tmp_path):
    async with running_core(tmp_path) as core:
        a = RawModule(core, manifest("voice", capabilities=["voice.output"]))
        b = RawModule(core, manifest("voice2", capabilities=["voice.output"]))
        try:
            assert (await a.hello()).type == "welcome"
            reply = await b.hello()
            assert reply.type == "reject" and "voice" in reply.body["reason"]
            low = reply.body["reason"].lower()
            assert not any(w in low for w in ("протокол", "protocol", "манифест", "manifest"))   # SDK будет повторять попытки
        finally:
            await a.close()
            await b.close()
        # тот же voice при переподключении принимается (вытеснение, не второй владелец)
        again = RawModule(core, manifest("voice", capabilities=["voice.output"]))
        try:
            assert (await again.hello()).type == "welcome"
        finally:
            await again.close()


async def test_duplicate_module_replaces_the_old_connection(tmp_path):
    async with running_core(tmp_path) as core:
        first = build_client(interval=0.01, url=core.url, token=core.token)
        second = build_client(interval=0.01, url=core.url, token=core.token)
        t1 = asyncio.create_task(first.run())
        await core.wait_connected("echo")
        await first.wait_connected(5)         # рукопожатие дошло до модуля: вытеснение посреди него шло бы по пути переподключения
        old = core.hub.connection("echo")
        t2 = asyncio.create_task(second.run())
        await core.wait_for(lambda: core.hub.connection("echo") not in (None, old))
        assert await asyncio.wait_for(t1, 5) == 0                    # SDK при коде 4000 завершается без переподключения
        assert core.hub.connection("echo").instance == second.instance
        assert core.modules.get("echo").connected and core.modules.get("echo").instance == second.instance
        await second.wait_connected(5)        # stop() до конца рукопожатия не закрыл бы ещё не принятое соединение
        await second.stop()
        await asyncio.wait_for(t2, 5)


async def test_sdk_exits_with_code_2_on_fatal_reject(tmp_path):
    async with running_core(tmp_path) as core:
        client = ModuleClient(manifest("core"), url=core.url, token=core.token)
        assert await asyncio.wait_for(client.run(), 5) == 2


# ── журнал ──────────────────────────────────────────────────────────────────────

async def test_journal_has_connect_disconnect_and_reject_without_content(tmp_path):
    import events_log
    async with running_core(tmp_path) as core:
        client = build_client(interval=0.01, url=core.url, token=core.token)
        async with running_client(client, core):
            await client.event("echo.counted", "секретное содержимое события")
        await core.wait_for(lambda: not core.modules.get("echo").connected)
        raw = RawModule(core, manifest("core"))
        await raw.hello()
        await raw.close()
    texts = [e["text"] for e in events_log.tail(100, {"module"})]
    assert any("echo подключён" in t for t in texts) and any("echo отключился" in t for t in texts)
    assert any("отклонён" in t for t in texts)
    assert not any("секретное" in t for t in texts)
    assert not any(core.token in t for t in texts)


# ── инструменты ─────────────────────────────────────────────────────────────────

def _names(core):
    return {t.id for t in core.envs.active_tools()}


async def test_echo_tool_visible_in_any_profile_and_runs_via_router(tmp_path):
    async with running_core(tmp_path) as core:
        focus = {"modules": set()}
        core.envs.focus = lambda: focus["modules"]
        client = build_client(interval=0.01, url=core.url, token=core.token)
        async with running_client(client, core):
            assert {"echo.repeat", "echo.count"} <= _names(core)            # availability=always
            ok, text = await core.router.run_tool("echo.repeat", {"text": "кукуруза"})
            assert (ok, text) == (True, "кукуруза")
            ok, text = await core.router.run_tool("echo.repeat", {})
            assert not ok and "неверные параметры" in text
            sig = core.envs.get("echo.count").signature()
            assert "Запускает долгую задачу: результат придёт позже." in sig
            assert core.envs.get("echo.repeat").timeout_s == 30


async def test_profile_availability_follows_focus_modules(tmp_path):
    async with running_core(tmp_path) as core:
        m = manifest("game", tools=[tool("order", availability="profile"), tool("peek")])
        c = ModuleClient(m, url=core.url, token=core.token)

        @c.tool("order")
        async def order(args):
            return "принято"

        @c.tool("peek")
        async def peek(args):
            return "вижу"
        focus = {"modules": {"desktop"}}
        core.envs.focus = lambda: focus["modules"]
        async with running_client(c, core):
            assert _names(core) == {"game.peek"}
            focus["modules"] = {"game"}
            assert _names(core) == {"game.peek", "game.order"}
            snap = {e["id"]: e for e in core.envs.snapshot()}["game"]
            assert snap["module"] == "game" and {t["name"]: t["availability"] for t in snap["tools"]} == {
                "order": "profile", "peek": "always"}


async def test_disabled_module_is_not_available_and_cannot_be_called(tmp_path):
    state = {"modules": {"echo": {"enabled": False}}}
    async with running_core(tmp_path, state) as core:
        client = build_client(interval=0.01, url=core.url, token=core.token)
        async with running_client(client, core):
            assert not client.enabled
            assert core.envs.envs["echo"].state() == (False, "модуль выключен")
            assert _names(core) == set()
            ok, text = await core.router.run_tool("echo.repeat", {"text": "x"})
            assert not ok                                                    # ни событий, ни вызовов
            state["modules"]["echo"]["enabled"] = True
            await core.hub.refresh()
            await core.wait_for(lambda: client.enabled)
            assert "echo.repeat" in _names(core)


async def test_tool_timeout_becomes_tool_error(tmp_path):
    async with running_core(tmp_path) as core:
        m = manifest("slowpoke", tools=[tool("wait", timeout_s=1)])
        c = ModuleClient(m, url=core.url, token=core.token)

        @c.tool("wait")
        async def wait(args):
            await asyncio.sleep(30)
        async with running_client(c, core):
            t = core.envs.get("slowpoke.wait")
            with pytest.raises(ToolError) as e:
                await t.call({})
            assert e.value.code == "timeout" and "не ответил" in str(e.value)
            ok, text, code = await core.router.run_tool_ex("slowpoke.wait", {})
            assert not ok and code == "timeout"


async def test_module_error_and_internal_error_keep_codes(tmp_path):
    async with running_core(tmp_path) as core:
        m = manifest("flaky", tools=[tool("deny"), tool("crash")])
        c = ModuleClient(m, url=core.url, token=core.token)

        @c.tool("deny")
        async def deny(args):
            raise __import__("gits_sdk").ToolError("нельзя", code="denied")

        @c.tool("crash")
        async def crash(args):
            raise ValueError("сломалось")
        async with running_client(c, core):
            assert (await core.router.run_tool_ex("flaky.deny", {}))[1:] == ("нельзя", "denied")
            ok, text, code = await core.router.run_tool_ex("flaky.crash", {})
            assert not ok and code == "internal" and "сломалось" in text


async def test_danger_check_asks_permission_even_for_auto_tools(tmp_path):
    async with running_core(tmp_path) as core:
        settings.update(auto_accept=True)                      # опасное спрашивается всегда, даже с авто-разрешением
        m = manifest("files", tools=[tool("wipe", danger_check=True, params={"path": {"type": "string"}})])
        c = ModuleClient(m, url=core.url, token=core.token)
        calls = []

        @c.tool("wipe")
        async def wipe(args):
            calls.append(args["path"])
            return "удалено"

        @c.check("wipe")
        async def check(args):
            return "это удалит важные файлы" if "важное" in args["path"] else None
        async with running_client(c, core):
            ok, text = await core.router.run_tool("files.wipe", {"path": "мусор"})
            assert (ok, text) == (True, "удалено") and not approvals.has_pending()      # безопасно: без вопроса
            task = asyncio.create_task(core.router.run_tool("files.wipe", {"path": "важное"}))
            await core.wait_for(approvals.has_pending)
            assert "это удалит важные файлы" in approvals.pending_text()
            approvals.resolve(False)
            assert await task == (False, "пользователь не разрешил") and calls == ["мусор"]
            task = asyncio.create_task(core.router.run_tool("files.wipe", {"path": "важное"}))
            await core.wait_for(approvals.has_pending)
            approvals.resolve(True)
            assert await task == (True, "удалено") and calls == ["мусор", "важное"]


async def test_danger_check_failure_counts_as_dangerous(tmp_path):
    async with running_core(tmp_path) as core:
        m = manifest("files", tools=[tool("wipe", danger_check=True)])
        c = ModuleClient(m, url=core.url, token=core.token)

        @c.tool("wipe")
        async def wipe(args):
            return "удалено"

        @c.check("wipe")
        async def check(args):
            raise RuntimeError("проверка упала")
        async with running_client(c, core):
            task = asyncio.create_task(core.router.run_tool("files.wipe", {}))
            await core.wait_for(approvals.has_pending)
            assert "модуль не ответил на проверку" in approvals.pending_text()
            approvals.resolve(True)
            assert await task == (True, "удалено")


# ── задачи ──────────────────────────────────────────────────────────────────────

async def test_job_flow_question_goes_through_approvals_and_back(tmp_path):
    async with running_core(tmp_path) as core:
        builtin.register_core_tools(core.envs, core.modules)
        client = build_client(interval=0.02, url=core.url, token=core.token)
        async with running_client(client, core):
            ok, text = await core.router.run_tool("echo.count", {"n": 4})
            assert ok and text.startswith("задача j-") and text.endswith("запущена")      # CORE-33
            job_id = text.split()[1]
            await core.wait_for(lambda: core.modules.get("echo").jobs.get(job_id, {}).get("state") == "waiting_user")
            job = core.modules.get("echo").jobs[job_id]
            assert job["title"] == "Счёт до 4" and job["module"] == "echo" and job["question"]["question_id"] == "q-1"
            await core.wait_for(approvals.has_pending)                               # вопрос задан через approvals
            assert approvals.pending_kind() == "choice" and approvals.pending_options() == ["продолжать", "остановить"]
            await core.wait_for(lambda: core.companion.about and core.companion.about[-1][2])     # вслух: факт-вопрос
            assert "Продолжать счёт?" in core.companion.about[-1][0]
            ok, listing = await core.router.run_tool("core.jobs", {})
            assert ok and job_id in listing and "waiting_user" in listing
            # повторный job.update с тем же question_id второго вопроса не порождает (MOD-20)
            await client.job_update(job_id, state="waiting_user", text="ещё раз")
            await asyncio.sleep(0.1)
            assert approvals.pending_info()["queued"] == 0
            assert approvals.answer("продолжать")
            await core.wait_for(lambda: core.modules.get("echo").jobs[job_id]["state"] == "done")
            assert core.modules.get("echo").jobs[job_id]["progress"] == 1.0
            facts = [f for f, _m, q in core.companion.about if not q]
            assert any("выполнена" in f for f in facts)                              # заглушка внимания: факт о завершении
            assert (await core.router.run_tool("core.jobs", {}))[1].count(job_id) == 1


async def test_job_cancel_tool_asks_permission_and_cancels(tmp_path):
    async with running_core(tmp_path) as core:
        builtin.register_core_tools(core.envs, core.modules)
        client = build_client(interval=0.02, url=core.url, token=core.token)
        async with running_client(client, core):
            ok, text = await core.router.run_tool("echo.count", {"n": 100})
            job_id = text.split()[1]
            await core.wait_for(lambda: approvals.pending_kind() == "choice")
            assert approvals.answer("продолжать")
            await core.wait_for(lambda: core.modules.get("echo").jobs[job_id]["state"] == "running" and not approvals.has_pending())
            task = asyncio.create_task(core.router.run_tool("core.job_cancel", {"module": "echo", "job_id": job_id}))
            await core.wait_for(lambda: approvals.pending_kind() == "approval")      # риск ask
            approvals.resolve(True)
            ok, text = await task
            assert ok and "отменена" in text
            await core.wait_for(lambda: core.modules.get("echo").jobs[job_id]["state"] == "cancelled")


async def test_open_question_is_dropped_when_the_job_ends(tmp_path):
    async with running_core(tmp_path) as core:
        client = build_client(interval=0.02, url=core.url, token=core.token)
        async with running_client(client, core):
            ok, text = await core.router.run_tool("echo.count", {"n": 4})
            job_id = text.split()[1]
            await core.wait_for(lambda: approvals.pending_kind() == "choice")
            await core.modules.cancel_job("echo", job_id)                            # задача отменена, вопрос не нужен
            await core.wait_for(lambda: not approvals.has_pending())
            assert core.modules.get("echo").jobs[job_id]["state"] == "cancelled"
            assert not core.hub._job_questions


async def test_job_answer_is_held_while_module_is_offline_and_sent_on_resend(tmp_path):
    async with running_core(tmp_path) as core:
        m = manifest("work")
        raw = RawModule(core, m)
        try:
            await raw.hello()
            q = {"question_id": "q-9", "kind": "choice", "question": "Что?", "options": ["а", "б"], "timeout_s": 60}
            upd = {"job_id": "j-0000abcd", "state": "waiting_user", "title": "Работа", "question": q}
            await raw.send("job.update", upd)
            await core.wait_for(approvals.has_pending)
        finally:
            await raw.close()
        await core.wait_for(lambda: not core.hub.is_connected("work"))
        assert approvals.answer("б")                                  # пользователь ответил, пока модуль офлайн
        await asyncio.sleep(0.1)
        again = RawModule(core, m)
        try:
            await again.hello()
            assert not again.of("job.answer")
            await again.send("job.update", upd)                       # MOD-22: после переподключения модуль повторяет
            ans = await again.wait("job.answer")
            assert ans.body == {"job_id": "j-0000abcd", "question_id": "q-9", "answer": "б"}
        finally:
            await again.close()


# ── события, речь, состояние ────────────────────────────────────────────────────

async def test_events_speech_emotion_state_reach_core(tmp_path, monkeypatch):
    import fast_brain
    published = []
    monkeypatch.setattr(fast_brain, "publish", lambda name, res: published.append((name, res)))
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest("tg", state=True))
        try:
            await raw.hello()
            await raw.send("event", {"kind": "tg.ping", "text": "Пинг", "urgency_hint": 1})       # D1: в сводку, не озвучивается
            await raw.send("event", {"kind": "other.ping", "text": "чужой вид"})          # вид другого модуля
            await raw.send("event", {"kind": "tg.bad", "text": ""})                        # неверное тело
            await raw.send("say", {"text": "Борис уснул.", "mode": "fact"})
            await raw.send("emotion", {"emotion": "happy", "cause": "тест"})
            await raw.send("emotion", {"emotion": "нет-такой", "cause": "тест"})
            await raw.send("state", {"summary": "всё хорошо", "data": {"game_running": True}})
            await core.wait_for(lambda: core.modules.get("tg").state)
            await core.wait_for(lambda: published)
            await asyncio.sleep(0.1)
        finally:
            await raw.close()
        import events_log
        logged = events_log.tail(50, {"module_event"})
        assert [e["data"]["event_kind"] for e in logged] == ["tg.ping"]                       # журнал событий модулей
        assert core.companion.about == [("Борис уснул.", False, False)]                 # fact -> очередь речи -> _speak_about
        assert published == [("module:tg", {"emotion": "happy", "action": "none", "source": "tg"})]
        assert core.modules.get("tg").state["summary"] == "всё хорошо"


async def test_disabled_module_events_and_speech_are_dropped_by_core(tmp_path):
    state = {"modules": {"tg": {"enabled": False}}}
    async with running_core(tmp_path, state) as core:
        raw = RawModule(core, manifest("tg"))
        try:
            welcome = await raw.hello()
            assert welcome.body["enabled"] is False
            await raw.send("event", {"kind": "tg.ping", "text": "Пинг"})
            await raw.send("say", {"text": "Привет", "mode": "fact"})
            reply = await raw.request("core.web_search", {"query": "что-нибудь"})
            assert reply["ok"] is False and reply["error"]["code"] == "unavailable"
            await asyncio.sleep(0.15)
            assert core.companion.about == [] and core.io.inputs == []
        finally:
            await raw.close()


async def test_unknown_and_malformed_messages_do_not_break_connection(tmp_path):
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            await raw.ws.send_str("это не JSON")
            await raw.ws.send_str('{"v": 1, "type": "weird", "id": "m-1", "re": null, "ts": 1, "body": {}}')
            await raw.send("job.update", {"job_id": "плохой", "state": "running"})
            body = await raw.request("core.nothing")                          # неизвестный запрос с core.
            assert body["ok"] is False and body["error"]["code"] == "unknown"
            body = await raw.request("tool.call", {"tool": "x"})              # запрос не того направления
            assert body["error"]["code"] == "unknown"
            assert core.hub.is_connected("raw")
        finally:
            await raw.close()


# ── темы ────────────────────────────────────────────────────────────────────────

async def test_broadcast_reaches_only_subscribed_enabled_modules(tmp_path):
    state = {"modules": {"off": {"enabled": False}}}
    async with running_core(tmp_path, state) as core:
        a = RawModule(core, manifest("aaa", subscribe=["profile"]))
        b = RawModule(core, manifest("bbb", subscribe=["presence"]))
        off = RawModule(core, manifest("off", subscribe=["profile"]))
        try:
            for m in (a, b, off):
                await m.hello()
            await core.hub.broadcast("profile", {"id": "work", "title": "Работа", "focus_modules": ["desktop"]})
            topic = await a.wait("topic")
            assert topic.body == {"topic": "profile", "data": {"id": "work", "title": "Работа", "focus_modules": ["desktop"]}}
            await asyncio.sleep(0.1)
            assert not b.of("topic") and not off.of("topic")          # не подписан / выключен
        finally:
            for m in (a, b, off):
                await m.close()


async def test_presence_is_throttled_to_ten_per_second_last_value_wins(tmp_path):
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest("pet", subscribe=["presence"]))
        try:
            await raw.hello()
            t0 = time.monotonic()
            for i in range(30):
                core.hub.broadcast_presence({"speaking": True, "n": i})
                await asyncio.sleep(0.005)
            elapsed = time.monotonic() - t0
            await core.wait_for(lambda: any(t.body["data"].get("n") == 29 for t in raw.of("topic")))
            topics = [t.body["data"] for t in raw.of("topic") if t.body["topic"] == "presence"]
            assert len(topics) <= elapsed / 0.1 + 2 and topics[-1]["n"] == 29      # не чаще 10/с, а не 30 сообщений
        finally:
            await raw.close()


async def test_settings_and_enabled_changes_are_sent_as_topics(tmp_path):
    state = {"modules": {}, "streamer": False, "dnd": False}
    async with running_core(tmp_path, state) as core:
        m = manifest("cfg", shared_settings=["streamer"],
                     settings_schema={"poll": {"type": "integer", "title": "Опрос", "default": 5}})
        raw = RawModule(core, m)
        try:
            await raw.hello()
            state["streamer"] = True
            state["dnd"] = True                                          # не в shared_settings: не передаётся
            await core.hub.refresh()
            t = await raw.wait("topic", where=lambda e: e.body["topic"] == "settings")
            assert t.body["data"] == {"settings": {"streamer": True}, "module_settings": {"poll": 5}}
            state["modules"]["cfg"] = {"poll": 20}
            await core.hub.refresh()
            await raw.wait("topic", where=lambda e: e.body["topic"] == "settings" and e.body["data"]["module_settings"] == {"poll": 20})
            await core.hub.refresh()                                     # без изменений - без новой темы
            n = len(raw.of("topic"))
            await core.hub.refresh()
            assert len(raw.of("topic")) == n
            state["modules"]["cfg"]["enabled"] = False
            await core.hub.refresh()
            t = await raw.wait("topic", where=lambda e: e.body["topic"] == "module")
            assert t.body["data"] == {"enabled": False}
        finally:
            await raw.close()


# ── запросы модулей к ядру ──────────────────────────────────────────────────────

async def test_core_ask_from_module_all_kinds(tmp_path):
    async with running_core(tmp_path) as core:
        client = build_client(interval=0.01, url=core.url, token=core.token)
        async with running_client(client, core):
            task = asyncio.create_task(client.ask("Удалить черновики?", kind="approval"))
            await core.wait_for(approvals.has_pending)
            await core.wait_for(lambda: core.companion.about)                       # формулировка вопроса идёт через очередь речи
            assert core.companion.about[-1][2] is True and "echo" in core.companion.about[-1][0]
            approvals.resolve(True)
            assert await task is True
            task = asyncio.create_task(client.ask("Как назвать?", kind="text"))
            await core.wait_for(approvals.has_pending)
            assert await approvals.try_answer("Куб")
            assert await task == "Куб"
            assert await client.ask("Ну?", kind="approval", timeout_s=1) is False         # тайм-аут: approval -> False


async def test_core_ask_bad_body_is_bad_request_and_memory_requests_unavailable(tmp_path):
    async with running_core(tmp_path) as core:
        raw = RawModule(core, manifest())
        try:
            await raw.hello()
            assert (await raw.request("core.ask", {"kind": "choice", "question": "?"}))["error"]["code"] == "bad_request"
            assert (await raw.request("core.ask", {"question": "?"}))["error"]["code"] == "bad_request"
            for t in ("core.lookup", "core.experience.add", "core.kb.ingest"):
                body = await raw.request(t, {})
                assert body["error"]["code"] == "unavailable" and "ТЗ 04" in body["error"]["message"]
            for t, body in (("core.memory.recall", {"query": "чай"}), ("core.memory.remember", {"text": "любит чай"})):
                assert (await raw.request(t, body))["error"]["code"] == "unavailable"          # память к тестовому ядру не подключена
            assert (await raw.request("core.privacy_terms", {}))["error"]["code"] == "unavailable"       # список ставит ядро (ТЗ 10)
            assert (await raw.request("core.tool.call", {"tool": 5}))["error"]["code"] == "bad_request"
            assert (await raw.request("core.tool.call", {"tool": "no.such"}))["error"]["code"] == "unknown"
            assert (await raw.request("core.web_search", {}))["error"]["code"] == "bad_request"
            assert (await raw.request("core.claude_usage", {"tokens": -1}))["error"]["code"] == "bad_request"
            assert (await raw.request("core.tts", {"text": "x", "format": "mp3"}))["error"]["code"] == "bad_request"
        finally:
            await raw.close()


async def test_core_tool_call_runs_through_router_with_permissions(tmp_path):
    async with running_core(tmp_path) as core:
        echo = build_client(interval=0.01, url=core.url, token=core.token)
        m = manifest("caller", tools=[tool("risky", risk="ask")])
        caller = ModuleClient(m, url=core.url, token=core.token)

        @caller.tool("risky")
        async def risky(args):
            return "сделано"
        async with running_client(echo, core), running_client(caller, core):
            res = await caller.request("core.tool.call", {"tool": "echo.repeat", "args": {"text": "привет"}})
            assert res == {"text": "привет"}
            with pytest.raises(__import__("gits_sdk").ToolError) as e:
                await caller.request("core.tool.call", {"tool": "echo.repeat", "args": {}})
            assert e.value.code == "bad_request"
            task = asyncio.create_task(caller.request("core.tool.call", {"tool": "caller.risky"}))
            await core.wait_for(approvals.has_pending)                   # обычные разрешения: риск ask спрашивается
            approvals.resolve(False)
            with pytest.raises(__import__("gits_sdk").ToolError) as e:
                await task
            assert e.value.code == "denied"


async def test_core_persona_claude_usage_and_web_search(tmp_path, monkeypatch):
    import claude_battery
    import claude_quota
    import web_search
    recorded, noted = [], []
    monkeypatch.setattr(claude_battery, "record", lambda tokens, source="": recorded.append((tokens, source)))
    monkeypatch.setattr(claude_quota, "note", lambda text: noted.append(text) or True)

    async def fake_search(query):
        return f"нашла: {query}"
    monkeypatch.setattr(web_search, "search", fake_search)
    async with running_core(tmp_path) as core:
        client = build_client(interval=0.01, url=core.url, token=core.token)
        async with running_client(client, core):
            persona = await client.request("core.persona", {})
            assert persona["name"] == "ГЛаДОС" and 0 < len(persona["card"]) <= 2000
            assert "подопытным" in persona["card"]                          # из prompt.yaml, не из конституции
            await client.request("core.claude_usage", {"tokens": 1500, "source": "montage"})
            await client.request("core.claude_usage", {"tokens": 10, "limit_text": "You've hit your session limit"})
            assert recorded == [(1500, "montage"), (10, "echo")] and noted == ["You've hit your session limit"]
            assert await client.request("core.web_search", {"query": "размер комнаты"}) == {"text": "нашла: размер комнаты"}


async def test_core_tts_is_forwarded_to_the_voice_module(tmp_path):
    """VOICE-16: core.tts - это запрос tts.synthesize модулю voice; ядро само не синтезирует."""
    from gits_sdk import ToolError
    async with running_core(tmp_path) as core:
        client = build_client(interval=0.01, url=core.url, token=core.token)
        async with running_client(client, core):
            with pytest.raises(ToolError) as e:                                   # модуля голоса нет
                await client.request("core.tts", {"text": "Привет"})
            assert e.value.code == "unavailable" and "голоса" in e.value.message
            voice = RawModule(core, manifest("voice", capabilities=["voice.output", "voice.input"]))
            asked = []

            def synthesize(body):
                asked.append(body)
                return {"path": str(tmp_path / "tts_1.ogg"), "duration_ms": 1234}
            voice.handlers["tts.synthesize"] = synthesize
            await voice.hello()
            try:
                r = await client.request("core.tts", {"text": "Привет", "emotion": "happy", "format": "ogg"})
                assert r == {"path": str(tmp_path / "tts_1.ogg"), "duration_ms": 1234}
                assert asked == [{"text": "Привет", "emotion": "happy", "format": "ogg"}]
                await client.request("core.tts", {"text": "Ещё"})
                assert asked[-1] == {"text": "Ещё", "emotion": None, "format": "wav"}
                voice.handlers["tts.synthesize"] = lambda body: {"path": 5}
                with pytest.raises(ToolError) as e:
                    await client.request("core.tts", {"text": "Плохо"})
                assert e.value.code == "internal"
                core.settings_state["modules"] = {"voice": {"enabled": False}}      # выключенный модуль голоса не синтезирует
                core.modules.invalidate()
                with pytest.raises(ToolError) as e:
                    await client.request("core.tts", {"text": "Выключен"})
                assert e.value.code == "unavailable"
            finally:
                await voice.close()


async def test_pending_requests_fail_with_unavailable_on_disconnect(tmp_path):
    async with running_core(tmp_path) as core:
        m = manifest("hang", tools=[tool("wait")])
        raw = RawModule(core, m)
        await raw.hello()
        conn = core.hub.connection("hang")
        task = asyncio.create_task(conn.request("tool.call", {"tool": "wait", "args": {}}, timeout=10))
        await raw.wait("tool.call")
        await raw.close()
        with pytest.raises(Exception) as e:
            await task
        assert getattr(e.value, "code", None) == "unavailable"
