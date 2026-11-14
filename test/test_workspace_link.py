"""WorkspaceLink: вход, выход и возобновление среды модуля (запросы workspace.*, без политик)."""
from services.spaces.link import WorkspaceLink


class Module:
    """Соединение с модулем: журнал запросов и их тел."""

    def __init__(self, refuse_exit=False):
        self.names, self.bodies, self.refuse_exit = [], [], refuse_exit

    async def request(self, name, body, timeout=0):
        self.names.append(name)
        self.bodies.append(body)
        if name == "workspace.exit":
            return {"ok": False, "error": "небезопасная точка"} if self.refuse_exit else {"ok": True, "checkpoint": {"step": 3}}
        return {"ok": True}


def link_for(mod):
    return WorkspaceLink(connection_of=lambda m: mod if m == "rimworld" else None)


async def test_afk_keeps_the_slot_and_enter_returns_without_a_new_task():
    mod = Module()
    link = link_for(mod)
    await link.activate("rimworld", "колония")
    task_id = link.holder["task_id"]
    assert (await link.afk("rimworld"))["ok"]
    assert link.holder is None and link.focus is None and "rimworld" in link.holders and link.afk_modules() == ["rimworld"]
    res = await link.activate("rimworld", "")
    assert res["ok"] and res["already"] and link.holder["task_id"] == task_id and link.holder["goal"] == "колония"
    assert mod.names == ["workspace.enter", "workspace.afk", "workspace.enter"]
    assert (await link.afk("telegram")) == {"ok": True, "already": True}


async def test_afk_works_with_a_module_that_has_no_afk_handler():
    from services.peers.hub import PeerError

    class Old(Module):
        async def request(self, name, body, timeout=0):
            if name == "workspace.afk":
                raise PeerError("unknown", "нет такого запроса")
            return await super().request(name, body, timeout)

    link = link_for(Old())
    await link.activate("rimworld", "x")
    assert (await link.afk("rimworld"))["ok"] and link.focus is None


async def test_release_of_an_afk_module_leaves_the_focus_on_another():
    mods = {"rimworld": Module(), "montage": Module()}
    link = WorkspaceLink(connection_of=mods.get)
    await link.activate("rimworld", "a")
    await link.activate("montage", "b")
    assert link.focus == "montage"
    assert (await link.release("user", module="rimworld"))["ok"]
    assert set(link.holders) == {"montage"} and link.focus == "montage"


async def test_activate_release_resume_cycle():
    mod = Module()
    link = link_for(mod)
    assert (await link.activate("rimworld", "колония"))["ok"]
    assert link.holder["module"] == "rimworld" and "policy" not in link.holder
    assert (await link.release("user"))["ok"] and link.holder is None
    assert link.checkpoints["rimworld"]["payload"] == {"step": 3}
    res = await link.resume("rimworld")
    assert res["ok"] and res["goal"] == "колония" and "rimworld" not in link.checkpoints
    assert mod.names == ["workspace.enter", "workspace.exit", "workspace.resume"]
    assert all("policy" not in body for body in mod.bodies)


async def test_enter_needs_only_a_connected_module_without_policies():
    link = link_for(Module())
    assert (await link.activate("rimworld", ""))["ok"]


async def test_refused_release_keeps_the_module_unless_forced():
    mod = Module(refuse_exit=True)
    link = link_for(mod)
    await link.activate("rimworld", "")
    assert not (await link.release("self"))["ok"] and link.holder is not None
    assert (await link.release("disconnect", force=True))["ok"] and link.holder is None


async def test_unconnected_module_is_refused():
    link = link_for(Module())
    assert not (await link.activate("telegram", ""))["ok"]
    assert not (await link.resume("rimworld"))["ok"]
