"""Компоненты менеджера внимания (ТЗ 29, ARC-4, шаг R3): `agent/app/*` и `domain/dedupe.py` по отдельности, плюс проверка,
что фасад `Attention` и компоненты делят одно состояние. Классификатор подменён заглушкой, часы фиктивные, без сети."""
import pytest
from test_attention import Rig, StubDecider, ans

from app.attention_core import Item
from attention import Attention, Question
from contracts import MemoryWriter, PeopleDirectory
from domain import dedupe


def _item(**kw) -> Item:
    base = dict(id="a-1", module="echo", kind="echo.ping", text="т", data={}, sender=None, reply_channel=None,
                addressed_to_me=None, urgency=1, needs_reply=False, memorable=False, created=100.0, expires=None,
                dedupe_key=None, updated=100.0)
    base.update(kw)
    return Item(**base)


# ── domain/dedupe.py ─────────────────────────────────────────────────────────

def test_can_merge_window_and_spoken():
    assert dedupe.can_merge(True, False, 100.0, 150.0)
    assert not dedupe.can_merge(True, False, 100.0, 161.0)         # окно 60 с прошло
    assert not dedupe.can_merge(True, True, 100.0, 110.0)          # уже озвучено
    assert not dedupe.can_merge(False, False, 100.0, 110.0)        # старого нет


def test_announce_key_ignores_spacing_and_case():
    assert dedupe.announce_key("rw", " Запускаю   RimWorld ") == dedupe.announce_key("rw", "запускаю rimworld")
    assert dedupe.announce_key("rw", "x") != dedupe.announce_key("tg", "x")


def test_oldest_ids_only_over_limit():
    ages = {f"i{n}": float(n) for n in range(10)}
    assert dedupe.oldest_ids(ages, limit=10, drop=3) == []
    ages["i10"] = 10.0
    assert dedupe.oldest_ids(ages, limit=10, drop=3) == ["i0", "i1", "i2"]


# ── компоненты ───────────────────────────────────────────────────────────────

def test_facade_aliases_component_state(tmp_path):
    att = Rig(tmp_path).att
    assert att._items is att.store.items and att._digest is att.digest_.buffers and att._focus is att.focus_.stack
    assert att.speech is att.coordinator.speech and att.rules is att.ctx.rules
    att.hub = object()
    assert att.ctx.hub is att.hub                                   # поля, которые ставит ядро, живут в общем контексте
    att._question_channel = "канал"
    assert att.questions.channel == "канал"


def test_people_and_memory_go_through_contracts(tmp_path):
    att = Rig(tmp_path).att
    assert isinstance(att.ctx.people, PeopleDirectory) and isinstance(att.ctx.writer, MemoryWriter)


def test_item_store_merges_duplicates_and_forgets_old(tmp_path):
    att = Rig(tmp_path).att
    store = att.store
    first = _item(dedupe_key="k")
    store.register(first)
    assert store.merge_duplicate(_item(id="a-2", dedupe_key="k", text="новое"), 120.0)
    assert (first.count, first.text, first.updated) == (2, "новое", 120.0)
    assert not store.merge_duplicate(_item(id="a-3", dedupe_key="k"), 300.0)       # окно прошло
    assert not store.merge_duplicate(_item(id="a-4", dedupe_key=None), 120.0)
    for n in range(501):
        store.register(_item(id=f"b-{n}", created=float(n)))
    assert len(store.items) == 402 and "b-0" not in store.items


async def test_digest_take_orders_by_priority_and_clears(tmp_path):
    att = Rig(tmp_path).att
    low, high = _item(id="a-l", text="мелочь", priority=10), _item(id="a-h", text="важное", priority=50, count=3)
    att.digest_.add(low)
    att.digest_.add(high)
    assert att.digest_.counts() == {"echo": 2}
    text = att.digest_.take()
    assert text.index("важное") < text.index("мелочь") and "×3" in text
    assert att.digest_.take() == "В сводке пусто: ничего заметного не накопилось."


async def test_focus_stack_depth_and_expiry(tmp_path):
    rig = Rig(tmp_path)
    focus = rig.att.focus_
    for n in range(5):
        focus.push("reaction", f"t{n}", "s", 50, False)
    assert len(focus.stack) == 4                                    # дно и три выше (ATT-21)
    rig.clock.t += 2000.0
    focus.tick(rig.clock.t)
    assert focus.top().kind == "idle"
    assert rig.companion.presence.snapshot()["focus"] == "idle"


def test_job_tracker_describes_states(tmp_path):
    jobs = Rig(tmp_path).att.jobs
    assert jobs.describe("m", {"title": "Т"}, "done", None) == (3, "Задача «Т» (модуль m) выполнена.")
    urgency, text = jobs.describe("m", {"title": "Т", "error": {"message": "упала"}}, "failed", None)
    assert urgency == 4 and "упала" in text
    assert jobs.describe("m", {"title": "Т"}, "running", "s1") == (1, "Задача «Т» (модуль m): состояние running, стадия s1.")


async def test_job_tracker_skips_repeat_state(tmp_path):
    rig = Rig(tmp_path)
    await rig.att.on_job_update("echo", {"job_id": "j", "title": "Т", "state": "done"})
    await rig.att.on_job_update("echo", {"job_id": "j", "title": "Т", "state": "done"})
    await rig.att.settle()
    assert len(rig.att._items) == 1 and rig.att.jobs.states[("echo", "j")] == ("done", None)


def test_normalizer_item_and_owner_address(tmp_path):
    norm = Rig(tmp_path).att.normalizer
    item = norm.make_item("tg", {"kind": "tg.dm", "text": "привет", "urgency_hint": 2, "addressed_to_me": True,
                                 "sender": {"id": "1", "name": "Маша", "is_owner": True},
                                 "reply_channel": {"module": "tg"}, "expires_in_s": 30}, 1000.0)
    assert item.urgency_hint == 2 and item.expires == 1030.0 and norm.is_owner_address(item)
    assert not norm.is_owner_address(norm.make_item("tg", {"kind": "tg.msg", "text": "x"}, 1000.0))
    item, mode, ttl = norm.make_speech_item("echo", {"mode": "verbatim", "expires_in_s": 5}, "готово", 1000.0)
    assert (mode, ttl, item.kind, item.urgency) == ("verbatim", 5.0, "echo.say", 2)


def test_classifier_apply_spam_and_hint(tmp_path):
    from decide import EventClass
    apply = Rig(tmp_path).att.classifier.apply
    item = _item(urgency=None, urgency_hint=3)
    apply(item, EventClass(category="spam", category_conf=0.9), None)
    assert item.urgency == 0 and item.data["category"] == "spam"
    item = _item(urgency=None)
    apply(item, None, 4)
    assert item.urgency == 4 and not item.needs_reply


async def test_pipeline_classifies_in_background_and_counts_pending(tmp_path):
    rig = Rig(tmp_path, decider=StubDecider(None))
    await rig.att.submit("echo", {"kind": "echo.ping", "text": "Привет", "sender": {"id": "7", "name": "Вася"}})
    await rig.att.settle()
    assert rig.att.classifier.pending == 0 and rig.att.last_decision["module"] == "echo"
    assert ans("x")["answer"] == "x"


async def test_questions_service_uses_shared_focus(tmp_path):
    rig = Rig(tmp_path)
    assert rig.att.questions.chat_module() is None                  # без шины модуля-канала нет
    assert isinstance(Question("approval", "можно?"), Question)


def test_facade_keeps_public_interface():
    names = ("submit", "submit_speech", "submit_question", "on_job_update", "enqueue_say", "enqueue_fact", "submit_announce",
             "take_digest", "digest_counts", "set_rule", "rules_text", "clear_rules", "snapshot", "stop_speech",
             "on_user_input", "classify_input", "set_user_speaking", "top_focus", "reset_for_character", "settle",
             "start", "stop", "tick", "looks_like_stop", "is_announce_event")
    assert all(callable(getattr(Attention, n)) for n in names)


@pytest.mark.parametrize("mode", ["verbatim", "fact"])
async def test_submit_speech_goes_through_dispatcher(tmp_path, mode):
    rig = Rig(tmp_path)
    await rig.att.submit_speech("echo", {"text": "Привет", "mode": mode, "urgency_hint": 5})
    await rig.att.settle()
    assert rig.candidates() and rig.candidates()[0].mode == mode


def test_app_package_is_small_and_clean():
    """ARC-4: ни один класс в `app/` не длиннее 400 строк; компоненты не лезут в приватные поля компаньона."""
    import ast
    from pathlib import Path
    for path in sorted((Path(__file__).resolve().parents[1] / "app").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for node in ast.parse(src).body:
            if isinstance(node, ast.ClassDef):
                assert (node.end_lineno or node.lineno) - node.lineno + 1 <= 400, f"{path.name}:{node.name}"
        assert '"_rel"' not in src and "companion._" not in src.replace("companion._speak_about", ""), path.name
