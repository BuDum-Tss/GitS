"""ModuleLink: вход, выход и возобновление среды модуля (имена workspace.* и прежние имена как откат)."""
import pytest
from workspace_rigs import GAME_POLICIES

from app.module_link import ModuleLink
from module_hub import ModuleError


class Module:
    """Соединение с модулем: журнал запросов; `old=True` - знает только прежние имена."""

    def __init__(self, old=False, refuse_exit=False):
        self.names, self.old, self.refuse_exit = [], old, refuse_exit

    async def request(self, name, body, timeout=0):
        self.names.append(name)
        if self.old and name.startswith("workspace."):
            raise ModuleError("unknown", "модуль не обрабатывает запрос")
        if name in ("workspace.exit", "focus.release"):
            return {"ok": False, "error": "небезопасная точка"} if self.refuse_exit else {"ok": True, "checkpoint": {"step": 3}}
        return {"ok": True, "policy": body.get("policy")}


def link_for(mod):
    return ModuleLink(connection_of=lambda m: mod if m == "rimworld" else None, policies_of=lambda m: GAME_POLICIES)


async def test_activate_release_resume_cycle():
    mod = Module()
    link = link_for(mod)
    assert (await link.activate("rimworld", "колония"))["ok"]
    assert link.holder["module"] == "rimworld" and link.holder["policy"] == "observe"
    assert (await link.release("user"))["ok"] and link.holder is None
    assert link.checkpoints["rimworld"]["payload"] == {"step": 3}
    res = await link.resume("rimworld")
    assert res["ok"] and res["goal"] == "колония" and "rimworld" not in link.checkpoints
    assert mod.names == ["workspace.enter", "workspace.exit", "workspace.resume"]


@pytest.mark.parametrize("old", [False, True])
async def test_old_request_names_are_a_fallback(old):
    mod = Module(old=old)
    link = link_for(mod)
    await link.activate("rimworld", "")
    await link.release("user")
    assert mod.names == (["workspace.enter", "policy.activate", "workspace.exit", "focus.release"] if old
                         else ["workspace.enter", "workspace.exit"])


async def test_refused_release_keeps_the_module_unless_forced():
    mod = Module(refuse_exit=True)
    link = link_for(mod)
    await link.activate("rimworld", "")
    assert not (await link.release("self"))["ok"] and link.holder is not None
    assert (await link.release("disconnect", force=True))["ok"] and link.holder is None


async def test_unconnected_or_policyless_module_is_refused():
    link = link_for(Module())
    assert not (await link.activate("telegram", ""))["ok"]
    link2 = ModuleLink(connection_of=lambda m: Module(), policies_of=lambda m: None)
    assert not (await link2.activate("rimworld", ""))["ok"]
    assert not (await link2.resume("rimworld"))["ok"]
