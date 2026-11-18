"""Модуль Telegram против настоящих шины, внимания, реестра и маршрутизатора ядра (без процессов и без сети Telegram).

Проверяет стык целиком: бот-витрина не шлёт событий в ядро; ответ deliver -> Bot API; инструменты модуля в
реестре планировщика; core.set_setting, core.privacy_terms; вопросы владельцу в Telegram (target owner); TG-14 для чужих.
"""
import asyncio
import contextlib
import copy
from types import SimpleNamespace

import pytest
from hub_helpers import Core
from tg_rig import NetStubs

from services.conversation import approvals
from services.peers import requests as core_requests
from host import settings
from telegram_module import TelegramModule
from services.conversation.conversation_io import Channel, ConversationIO
from environments import builtin
from gits_sdk import WorkspaceClient
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
    st["peers"] = {"telegram": {"owner_tg_id": owner}}
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
    client = WorkspaceClient(build_manifest(), url=core.url, token=core.token)
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
        assert env.module == "telegram" and {t.name for t in env.tools} == {"text", "message", "inbox", "announce", "settings"}
        s.core.envs.focus = lambda: set()                                     # ни один профиль модуль не focus: always видны
        visible = {t.id for t in s.core.envs.active_tools()}
        assert {"telegram.message", "telegram.inbox", "telegram.announce", "telegram.settings"} <= visible
        msg = s.core.envs.get("telegram.message")
        assert msg.danger_check and msg.risk == "auto" and not msg.self_confirming      # разрешение решает проверка адресата


async def test_owner_message_to_the_bot_is_not_a_conversation_either(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        assert await s.mod.channel._on_private(bot_msg(OWNER, "как дела?", name="Аня"))
        await asyncio.sleep(0.3)
        assert not s.core.io.inputs                                            # разговоры ведёт её аккаунт, не бот
        assert "только объявляю" in s.net.api[-1][1]["text"]


async def test_stranger_message_to_the_bot_gets_stub_and_never_reaches_the_core(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        await s.mod.channel._on_private(bot_msg(555, "я переезжаю в Казань", name="Маша"))
        await asyncio.sleep(0.3)
        assert not s.core.io.inputs and s.core.attention.last_decision is None       # ни разговора, ни события
        assert "tg:555" not in s.rel.people and not s.core.companion._mem.facts
        assert s.net.api[-1][0] == "sendMessage" and "только объявляю" in s.net.api[-1][1]["text"]


async def test_question_to_the_owner_is_delivered_to_the_owners_bot_chat(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        assert await s.io.deliver(Channel("module", "telegram", "owner"), "Удалить черновые рендеры? Ответь да или нет.")
        assert s.net.api[-1] == ("sendMessage", {"chat_id": OWNER, "text": "Удалить черновые рендеры? Ответь да или нет."})


async def test_owner_id_zero_makes_owner_delivery_unavailable(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch, owner=0) as s:
        assert not await s.io.deliver(Channel("module", "telegram", "owner"), "вопрос")
        assert not [1 for m, _ in s.net.api if m == "sendMessage"]


async def test_message_to_channel_through_the_router_does_not_ask_for_permission(tmp_path, monkeypatch):
    from services.runtime import fast_brain

    async def model(system, user, **kw):
        return '{"text": "Объявление: кофе закончился, эксперименты отменяются."}'
    monkeypatch.setattr(fast_brain, "_ask", model)
    async with stack(tmp_path, monkeypatch) as s:
        ok, text = await asyncio.wait_for(s.core.router.run_tool("telegram.message", {"topic": "кофе"}), 10)
        assert ok and text.startswith("опубликован пост") and not approvals.has_pending()    # публикация без вопроса
        assert s.net.api[-1][0] == "sendMessage"


async def test_message_asks_only_for_unknown_chats_through_the_router(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch, terms=["Анна"]) as s:
        tool = s.core.envs.get("telegram.message")
        assert await tool.check({"to": "Маша", "text": "привет"})                       # незнакомый чат: причина, роутер спросит
        assert await tool.check({"to": "Маша", "text": "Анна просила привет"})          # личные слова: тоже причина
        assert await tool.check({"to": "channel", "text": "привет"}) is None             # канал и владелец: без вопроса
        assert await tool.check({"to": "owner", "text": "привет"}) is None
        s.mod.user.known.add(555, "Маша")                                               # теперь Маша писала её аккаунту
        assert await tool.check({"to": "Маша", "text": "привет"}) is None
        assert await tool.check({"to": "555", "text": "привет"}) is None


async def test_settings_tool_changes_own_setting_of_the_environment(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        async def save(key, value):                       # то, что делает дашборд ядра: запись в peers.telegram
            await asyncio.to_thread(lambda: settings.update(peers={"telegram": {key: value}}))
            s.mod.client.module_settings = {**(s.mod.client.module_settings or {}), key: value}
        s.mod._set_setting = save
        ok, text = await s.core.router.run_tool("telegram.settings", {"periodic": True})
        assert ok and "включены" in text and settings.load()["peers"]["telegram"]["publish"] is True
        ok, _ = await s.core.router.run_tool("telegram.settings", {"after_run": True})
        assert ok and settings.load()["peers"]["telegram"]["after_run"] is True
        ok, _ = await s.core.router.run_tool("telegram.settings", {"periodic": False})
        assert ok and settings.load()["peers"]["telegram"]["publish"] is False
        ok, text = await s.core.router.run_tool("telegram.settings", {})
        assert ok and "периодические посты: выключены" in text and "пост-итог после забега: включён" in text


async def test_privacy_terms_come_from_the_real_core_request(tmp_path, monkeypatch):
    names = core_requests.collect_privacy_terms("Аня", ["Маша"], ["Имя: Аня Петрова"])
    async with stack(tmp_path, monkeypatch, terms=names) as s:
        assert s.mod.privacy.ready and s.mod.privacy.leaks("Петрову привет") and s.mod.privacy.leaks("Привет, Маша")
        assert not s.mod.privacy.leaks("Баня закрыта")


async def test_disabling_the_module_hides_its_tools_and_drops_its_events(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        s.core.settings_state["peers"]["telegram"]["enabled"] = False
        await s.core.hub.refresh()
        await s.core.wait_for(lambda: not s.client.enabled)
        assert s.core.envs.envs["telegram"].state()[0] is False                 # «возможности»: инструменты пропали
        before = s.core.attention.last_decision
        await s.client.event("telegram.dm", "Сообщение от Маша в её личке: «привет»", addressed_to_me=True)
        await asyncio.sleep(0.2)
        assert s.core.attention.last_decision is before


@pytest.mark.parametrize("path", ["message", "inbox"])
def test_planner_search_finds_telegram_tools_by_old_requests(path):
    from environments.registry import Registry
    from services.peers.registry import PeerRegistry

    reg = Registry()
    mods = PeerRegistry(reg, settings_source=lambda: {"peers": {}})
    conn = SimpleNamespace(id="telegram", manifest=build_manifest(), instance="x", pid=1, connected_at=0,
                           spec=__import__("gits_sdk.protocol", fromlist=["Manifest"]).Manifest.from_dict(build_manifest()))
    mods.on_connected(conn)
    reg.focus = lambda: set()
    request = {"message": "опубликуй пост про понедельник в телеграм", "inbox": "что мне писали в телеге"}[path]
    found = {t.id for t in reg.search(request, k=6)}
    assert f"telegram.{path}" in found
    assert "telegram.message" in {t.id for t in reg.search("напиши Маше привет в телеге", k=6)}
