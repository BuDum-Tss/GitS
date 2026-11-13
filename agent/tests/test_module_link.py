"""ModuleLink: вход, выход и возобновление среды модуля (запросы workspace.*, без политик)."""
from app.module_link import ModuleLink


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
    return ModuleLink(connection_of=lambda m: mod if m == "rimworld" else None)


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
