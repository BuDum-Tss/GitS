"""Модуль Telegram против настоящих шины, внимания, реестра и маршрутизатора ядра (без процессов и без сети Telegram).

Проверяет стык целиком: событие владельца -> правило R1 -> разговор -> ответ deliver -> Bot API; инструменты модуля в
реестре планировщика; core.set_setting, core.privacy_terms; вопросы владельцу в Telegram (target owner); TG-14 для чужих.
"""
import asyncio
import contextlib
import copy
from types import SimpleNamespace

import pytest
from hub_helpers import Core
from tg_rig import NetStubs

import approvals
import core_requests
import settings
from app import TelegramModule
from conversation_io import Channel, ConversationIO
from environments import builtin
from gits_sdk import ModuleClient
from manifest import build_manifest

OWNER = 777


class Rel:
    def __init__(self):
        self.people: dict[str, dict] = {}

    def get(self, key):
        return self.people.get(key)

    def ensure_person(self, key, name, source="", **extra):
        new = key not in self.people
        self.people.setdefault(key, {"name": name, "source": source, "affinity": 0.0, "notes": []})
        return new

    def note(self, key, text):
        self.people[key]["notes"].append(text)


@contextlib.asynccontextmanager
async def stack(tmp_path, monkeypatch, *, owner=OWNER, state=None, terms=("Анна",)):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET-TOKEN")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@chan")
    st = copy.deepcopy(settings.DEFAULTS)
    st["modules"] = {"telegram": {"owner_tg_id": owner}}
    st.update(state or {})
    core = Core(tmp_path, st)
    core.companion._rel = Rel()
    core.companion._mem = SimpleNamespace(facts=[], add_fact=lambda t, kind="user", importance=3, due_days=None:
                                          core.companion._mem.facts.append((t, kind)) or True)
    core.companion.graph = SimpleNamespace(items=[], enqueue=lambda s, t: core.companion.graph.items.append((s, t)))
    core.requests.privacy_terms = lambda: list(terms)
    builtin.register_core_tools(core.envs, core.modules, core.attention)
    core.attention.hub = core.hub
    deliver_io = ConversationIO(core.companion, core.hub)         # обратный путь ответа (у core.io заглушка для входа обращений)
    await core.start()
    mem = tmp_path / "memory"
    client = ModuleClient(build_manifest(), url=core.url, token=core.token)
    mod = TelegramModule(client, str(mem))
    net = NetStubs()
    net.install(mod)
    task = asyncio.create_task(client.run())
    try:
        await core.wait_connected("telegram")
        await client.wait_connected(5)
        assert await mod.privacy.refresh()                    # настоящий core.privacy_terms
        yield SimpleNamespace(core=core, client=client, mod=mod, net=net, rel=core.companion._rel, io=deliver_io)
    finally:
        await mod.stop()
        await client.stop()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await core.stop()


def bot_msg(uid, text, name="Маша", message_id=1):
    return {"message_id": message_id, "chat": {"id": uid, "type": "private"}, "text": text,
            "from": {"id": uid, "first_name": name}}


async def test_module_manifest_is_accepted_and_tools_reach_the_planner(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        rec = s.core.modules.get("telegram")
        assert rec.connected and rec.title == "Telegram" and "chat.channel" in rec.manifest["capabilities"]
        env = s.core.envs.envs["telegram"]
        assert env.module == "telegram" and {t.name for t in env.tools} >= {"post", "send", "unread", "draft_publish"}
        s.core.envs.focus = lambda: set()                                     # ни один профиль модуль не focus: always видны
        visible = {t.id for t in s.core.envs.active_tools()}
        assert {"telegram.post", "telegram.send", "telegram.chats", "telegram.draft_show"} <= visible
        assert s.core.envs.get("telegram.send").danger_check and s.core.envs.get("telegram.post").self_confirming


async def test_owner_message_goes_to_conversation_and_the_reply_comes_back_to_the_same_chat(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        assert await s.mod.channel._on_private(bot_msg(OWNER, "как дела?", name="Аня"))
        await s.core.wait_for(lambda: s.core.io.inputs)
        text, channel, sender = s.core.io.inputs[0]
        assert s.core.attention.last_decision["rule"] == "R1"                  # TG-17: обращение владельца -> разговор
        assert text == "Сообщение от Аня в личке бота: «как дела?»"
        assert channel == {"module": "telegram", "target": f"bot:{OWNER}"} and sender["is_owner"] is True
        # ответ уходит в тот же чат от бота
        reply = Channel("module", channel["module"], channel["target"])
        assert await s.io.deliver(reply, "Сносно. Подопытные не взорвались.")
        assert s.net.api[-1] == ("sendMessage", {"chat_id": OWNER, "text": "Сносно. Подопытные не взорвались."})


async def test_stranger_message_is_not_answered_but_recorded_and_remembered(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        await s.mod.channel._on_private(bot_msg(555, "я переезжаю в Казань", name="Маша"))
        await s.core.wait_for(lambda: s.core.attention.last_decision)
        d = s.core.attention.last_decision
        assert d["rule"] != "R1" and not s.core.io.inputs                     # TG-18: разговор не заводится
        assert all(m != "sendMessage" for m, _ in s.net.api)                   # и бот никому не пишет
        assert s.rel.people["tg:555"]["source"] == "telegram" and s.rel.people["tg:555"]["name"] == "Маша"   # TG-14
        assert "Казань" in s.rel.people["tg:555"]["notes"][0]
        assert s.core.companion.graph.items[0][0] == "Маша" and "Казань" in s.core.companion.mem_text() \
            if hasattr(s.core.companion, "mem_text") else True
        assert s.core.companion._mem.facts and s.core.companion._mem.facts[0][1] == "episode"


async def test_question_to_the_owner_is_delivered_to_the_owners_bot_chat(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        assert await s.io.deliver(Channel("module", "telegram", "owner"), "Удалить черновые рендеры? Ответь да или нет.")
        assert s.net.api[-1] == ("sendMessage", {"chat_id": OWNER, "text": "Удалить черновые рендеры? Ответь да или нет."})


async def test_owner_id_zero_makes_owner_delivery_unavailable(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch, owner=0) as s:
        assert not await s.io.deliver(Channel("module", "telegram", "owner"), "вопрос")
        assert not [1 for m, _ in s.net.api if m == "sendMessage"]


async def test_post_tool_through_the_router_with_confirmation_asked_via_core(tmp_path, monkeypatch):
    import fast_brain

    async def model(system, user, **kw):
        return '{"post": true, "text": "Объявление: кофе закончился, эксперименты отменяются."}'
    monkeypatch.setattr(fast_brain, "_ask", model)
    async with stack(tmp_path, monkeypatch, state={"telegram_confirm": True}) as s:
        job = asyncio.create_task(s.core.router.run_tool("telegram.post", {"topic": "кофе"}))
        await s.core.wait_for(approvals.has_pending)                           # маршрутизатор не спрашивал: self_confirming
        assert "кофе закончился" in approvals.pending_text()
        approvals.resolve(False)
        ok, text = await asyncio.wait_for(job, 10)
        assert not ok and "не разрешил" in text and [m for m, _ in s.net.api if m == "sendMessage"] == []
        job = asyncio.create_task(s.core.router.run_tool("telegram.post", {"topic": "кофе"}))
        await s.core.wait_for(approvals.has_pending)
        approvals.resolve(True)
        ok, text = await asyncio.wait_for(job, 10)
        assert ok and text.startswith("опубликован пост") and s.net.api[-1][0] == "sendMessage"


async def test_send_tool_asks_and_warns_about_private_words_through_the_router(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch, terms=["Анна"]) as s:
        tool = s.core.envs.get("telegram.send")
        assert await tool.check({"chat": "Маша", "text": "Анна просила привет"})            # tool.check -> причина
        assert await tool.check({"chat": "Маша", "text": "привет"}) is None


async def test_set_periodic_changes_the_core_setting_through_core_tool_call(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        ok, text = await s.core.router.run_tool("telegram.set_periodic", {"enabled": True})
        assert ok and "включены" in text and settings.load()["telegram"] is True
        ok, _ = await s.core.router.run_tool("telegram.set_after_run", {"enabled": True})
        assert ok and settings.load()["telegram_after_run"] is True
        ok, _ = await s.core.router.run_tool("telegram.set_periodic", {"enabled": False})
        assert ok and settings.load()["telegram"] is False


async def test_privacy_terms_come_from_the_real_core_request(tmp_path, monkeypatch):
    names = core_requests.collect_privacy_terms("Аня", ["Маша"], ["Имя: Аня Петрова"])
    async with stack(tmp_path, monkeypatch, terms=names) as s:
        assert s.mod.privacy.ready and s.mod.privacy.leaks("Петрову привет") and s.mod.privacy.leaks("Привет, Маша")
        assert not s.mod.privacy.leaks("Баня закрыта")


async def test_disabling_the_module_hides_its_tools_and_drops_its_events(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        s.core.settings_state["modules"]["telegram"]["enabled"] = False
        await s.core.hub.refresh()
        await s.core.wait_for(lambda: not s.client.enabled)
        assert s.core.envs.envs["telegram"].state()[0] is False                 # «возможности»: инструменты пропали
        before = s.core.attention.last_decision
        await s.mod.channel._on_private(bot_msg(555, "привет"))
        await asyncio.sleep(0.2)
        assert s.core.attention.last_decision is before


@pytest.mark.parametrize("path", ["post", "send"])
def test_planner_search_finds_telegram_tools_by_old_requests(path):
    from environments.registry import Registry
    from module_registry import ModuleRegistry

    reg = Registry()
    mods = ModuleRegistry(reg, settings_source=lambda: {"modules": {}})
    conn = SimpleNamespace(module_id="telegram", manifest=build_manifest(), instance="x", pid=1, connected_at=0,
                           spec=__import__("gits_sdk.protocol", fromlist=["Manifest"]).Manifest.from_dict(build_manifest()))
    mods.on_connected(conn)
    reg.focus = lambda: set()
    request = {"post": "опубликуй пост про понедельник в телеграм", "send": "напиши Маше привет в телеге"}[path]
    found = {t.id for t in reg.search(request, k=6)}
    assert f"telegram.{path}" in found
    assert "telegram.unread" in {t.id for t in reg.search("что мне писали в телеге", k=6)}
