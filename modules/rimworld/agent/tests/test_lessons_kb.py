"""Уроки игры как опыт ядра (ТЗ 04, KB-20): подбор по ситуации вместо «последних 15», отправка lessons.json в `core.experience.add`."""
import json

from rigs import Rig

from memory import Memory
import module_manifest


def test_prompt_uses_picker_when_it_gives_lessons_else_last_fifteen(tmp_path):
    mem = Memory(str(tmp_path))
    for i in range(20):
        mem.add_lesson(f"урок {i}")
    block = mem.build_context_block("рейд у ворот")
    assert "урок 19" in block and "урок 4" not in block                       # без подборщика: последние 15
    mem.lesson_picker = lambda s: ["Рейд приходит после 200 очков угрозы"] if "рейд" in s else []
    block = mem.build_context_block("рейд у ворот")
    assert "Рейд приходит" in block and "урок 19" not in block                # подбор по ситуации
    assert "урок 19" in mem.build_context_block("тишина")                     # подборщик пуст: запасной путь
    mem.lesson_picker = lambda s: 1 / 0
    assert "урок 19" in mem.build_context_block("рейд")                       # сбой подборщика не ломает ход


def test_manifest_declares_knowledge_collections():
    manifest = module_manifest.build_manifest([])
    assert {k["collection"] for k in manifest["knowledge"]} == {"rimworld.wiki", "rimworld.defs", "rimworld.experience"}


async def test_sync_lessons_sends_new_ones_once_and_lookup_fills_cache(tmp_path):
    async with Rig(tmp_path) as rig:
        sent = []

        @rig.core.on_request("core.experience.add")
        def add(body):
            sent.append(body)
            return {"stored": True}

        @rig.core.on_request("core.lookup")
        def lookup(body):
            assert body["collections"] == ["rimworld.experience"]
            return {"text": "[rimworld.experience: Урок] Строй стену до рейда"}

        f = tmp_path / "lessons.json"
        f.write_text(json.dumps(["Строй стену до рейда", {"lesson": "Не оставляй еду на улице"}]), encoding="utf-8")
        assert await rig.link.sync_lessons(f) == 2
        assert await rig.link.sync_lessons(f) == 0                           # повторно не отправляет
        assert [b["statement"] for b in sent] == ["Строй стену до рейда", "Не оставляй еду на улице"]
        assert sent[0]["scope"] == "rimworld"
        assert rig.link.lessons_for("рейд у ворот") == []                    # первый вызов запускает поиск в фоне
        await rig.core.wait_for(lambda: rig.link._lessons_cache)
        assert rig.link.lessons_for("рейд у ворот") == ["Строй стену до рейда"]
