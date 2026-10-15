"""Одобрение по этапам в пространстве «Монтаж» (ТЗ 15b): подпись этапа, файлы и кнопки именно этого этапа, ответ доходит
до конвейера тем же путём, что и из обзора. Конвейер и режимы приёмки не меняются: проверяем только панель и ответы."""
import asyncio
import json

import pytest
from gits_sdk.protocol import validate_panel_set

import approval_panel as ap
from manifest import build_manifest
from pipeline import APPROVE, EDITS, READY, REDO_PLAN, REDO_STORY
from tests.stubs import StubCore
from tests.test_studio_panel import manager_for, make_job

PLAN_Q = {"question_id": "q-1", "kind": "choice", "question": "Утвердить монтаж?",
          "options": [APPROVE, REDO_PLAN, REDO_STORY]}
DRAFT_Q = {"question_id": "q-1", "kind": "choice", "question": "Черновик готов?", "options": [READY, EDITS]}
ACTIONS = set(build_manifest()["panel"]["actions"])


def open_question(manager, job, question, stage):
    """Задача ждёт ответа: состояние, вопрос в задаче и ожидающий ответа (как у конвейера в `ask`)."""
    job = manager._find(job.job_id)
    job.data.update(state="waiting_user", stage=stage, question=question)
    manager.store.save(job)
    fut = asyncio.get_running_loop().create_future()
    manager.pipeline._waiters[job.job_id] = (question["question_id"], fut)
    return job, fut


def buttons_of(blocks):
    return [b for b in blocks if b["type"] == "buttons"]


def job_blocks(manager, job):
    """Блоки панели только этой задачи (пространство собирает все задачи)."""
    return ap.waiting_blocks(job, job.data["question"])


async def test_plan_question_shows_stage_files_and_its_three_buttons(tmp_path):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Куб", "waiting_user", plan=True)
    (job.plan_dir / "story.md").write_text("# История", encoding="utf-8")
    (job.plan_dir / "plan.md").write_text("# План", encoding="utf-8")
    job, _ = open_question(manager, job, PLAN_Q, "plan_approval")
    blocks = manager.panel_blocks()
    caption = next(b for b in blocks if b.get("title") == "Ждёт решения: история и план")
    assert "Утвердить монтаж?" in caption["text"] and "Вернуть на доработку" in caption["text"]
    links = [b for b in blocks if b["type"] == "link"]
    assert [b["label"] for b in links] == ["История: story.md", "План: plan.md"]
    assert f"job={job.job_id}" in links[0]["url"] and "name=story.md" in links[0]["url"]
    assert "module=montage" in links[0]["url"] and "/api/artifact?" in links[0]["url"]
    (group,) = buttons_of(blocks)
    assert [i["action"] for i in group["items"]] == ["approve_plan", "rework_plan", "reject_plan"]
    assert [i["label"] for i in group["items"]] == ["Одобрить историю и план", "Вернуть на доработку",
                                                    "Отклонить (переделать историю)"]
    assert all(i["args"] == {"job_id": job.job_id, "question_id": "q-1"} for i in group["items"])
    assert group["items"][2]["style"] == "danger"
    assert validate_panel_set({"blocks": blocks}, ACTIONS) == []          # SDK принимает блоки и действия


async def test_plan_question_without_files_has_no_links(tmp_path):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Без файлов", "waiting_user", plan=False)
    job, _ = open_question(manager, job, PLAN_Q, "plan_approval")
    assert [b for b in job_blocks(manager, job) if b["type"] == "link"] == []


async def test_draft_question_shows_preview_video_and_two_buttons(tmp_path):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Черновик", "waiting_user", plan=True)
    preview = job.renders / "preview.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"0" * 10)
    job.add_artifact(preview, "preview")
    manager.store.save(job)
    job, _ = open_question(manager, job, DRAFT_Q, "review")
    blocks = job_blocks(manager, job)
    video = next(b for b in blocks if b["type"] == "video")
    assert video["artifact"] == {"job": job.job_id, "name": "preview.mp4"}
    (group,) = buttons_of(blocks)
    assert [i["action"] for i in group["items"]] == ["approve_draft", "rework_draft"]     # отклонять черновик нельзя
    assert [i["label"] for i in group["items"]] == ["Одобрить черновик", "Вернуть на доработку"]
    assert validate_panel_set({"blocks": blocks}, ACTIONS) == []


async def test_agent_approval_question_gets_yes_and_no_buttons(tmp_path):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Вопрос агента", "waiting_user", plan=False)
    question = {"question_id": "q-3", "kind": "approval", "question": "Брать второй источник?"}
    job, _ = open_question(manager, job, question, "story")
    blocks = job_blocks(manager, job)
    assert blocks[0]["title"] == "Ждёт решения: история"
    (group,) = buttons_of(blocks)
    assert [(i["action"], i["args"]["answer"]) for i in group["items"]] == [("answer_question", True),
                                                                          ("answer_question", False)]
    assert validate_panel_set({"blocks": blocks}, ACTIONS) == []


async def test_agent_choice_question_gets_one_button_per_option(tmp_path):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Выбор агента", "waiting_user", plan=False)
    question = {"question_id": "q-4", "kind": "choice", "question": "Какой кадр взять?", "options": ["первый", "средний"]}
    job, _ = open_question(manager, job, question, "build")
    (group,) = buttons_of(job_blocks(manager, job))
    assert [i["args"]["answer"] for i in group["items"]] == ["первый", "средний"]
    assert {i["action"] for i in group["items"]} == {"answer_question"}


async def test_text_question_has_no_buttons_only_the_hint(tmp_path):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Замечание", "waiting_user", plan=False)
    question = {"question_id": "q-2", "kind": "text", "question": "Что изменить в плане?"}
    job, _ = open_question(manager, job, question, "plan_approval")
    blocks = job_blocks(manager, job)
    assert buttons_of(blocks) == []
    assert "Замечание ждёт ответа в обзоре или в чате" in blocks[0]["text"]


@pytest.mark.parametrize("action, answer, question", [
    ("approve_plan", APPROVE, PLAN_Q),
    ("rework_plan", REDO_PLAN, PLAN_Q),
    ("reject_plan", REDO_STORY, PLAN_Q),
    ("approve_draft", READY, DRAFT_Q),
    ("rework_draft", EDITS, DRAFT_Q),
])
async def test_each_stage_button_delivers_its_answer_to_the_task(tmp_path, action, answer, question):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Кнопка", "waiting_user", plan=True)
    job, fut = open_question(manager, job, question, "plan_approval" if question is PLAN_Q else "review")
    result = await manager.panel_decide(action, {"job_id": job.job_id, "question_id": "q-1"})
    assert result["ok"] is True, result
    assert await asyncio.wait_for(fut, 2) == answer


async def test_agent_answers_deliver_the_chosen_value(tmp_path):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Ответ агента", "waiting_user", plan=False)
    yes = {"question_id": "q-5", "kind": "approval", "question": "Да?"}
    job, fut = open_question(manager, job, yes, "story")
    assert (await manager.panel_decide("answer_question", {"job_id": job.job_id, "question_id": "q-5",
                                                           "answer": False}))["ok"] is True
    assert await asyncio.wait_for(fut, 2) is False
    choice = {"question_id": "q-6", "kind": "choice", "question": "Какой?", "options": ["первый", "средний"]}
    job, fut2 = open_question(manager, job, choice, "build")
    assert (await manager.panel_decide("answer_question", {"job_id": job.job_id, "question_id": "q-6",
                                                           "answer": "средний"}))["ok"] is True
    assert await asyncio.wait_for(fut2, 2) == "средний"


async def test_stale_or_mismatched_clicks_are_refused_and_nothing_is_answered(tmp_path):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Стенд", "waiting_user", plan=True)
    job, fut = open_question(manager, job, PLAN_Q, "plan_approval")
    jid = job.job_id
    # старый вопрос (другой question_id)
    assert (await manager.panel_decide("approve_plan", {"job_id": jid, "question_id": "q-0"}))["ok"] is False
    # кнопка черновика на вопросе про план
    assert (await manager.panel_decide("approve_draft", {"job_id": jid, "question_id": "q-1"}))["ok"] is False
    # ответ агента, которого нет в вариантах
    assert (await manager.panel_decide("answer_question", {"job_id": jid, "question_id": "q-1",
                                                           "answer": "нет такого"}))["ok"] is False
    assert not fut.done()
    # текстовый вопрос кнопками не закрывается
    text_job, text_fut = open_question(manager, make_job(manager, "Текст", "waiting_user"),
                                       {"question_id": "q-9", "kind": "text", "question": "Как назвать?"}, "story")
    refused = await manager.panel_decide("answer_question", {"job_id": text_job.job_id, "question_id": "q-9",
                                                             "answer": True})
    assert refused["ok"] is False and not text_fut.done()
    # задача уже не ждёт ответа
    job.data["state"] = "running"
    manager.store.save(job)
    assert (await manager.panel_decide("approve_plan", {"job_id": jid, "question_id": "q-1"}))["ok"] is False
    assert not fut.done()


async def test_rework_of_the_plan_asks_the_remark_and_the_remark_reaches_the_stage(tmp_path):
    """Сквозная проверка: «Вернуть на доработку» на плане отвечает конвейеру, конвейер спрашивает замечание текстом,
    замечание из обзора (manager.answer) попадает в задачу, стадия продолжается с ним."""
    client, manager = manager_for(tmp_path)
    manager.pipeline.core = StubCore(tmp_path, {})
    job = make_job(manager, "Сквозь конвейер", "waiting_user", plan=True)
    job = manager._find(job.job_id)
    job.data.update(state="running", stage="plan_approval")       # стадию ставит run_job
    stage = asyncio.create_task(manager.pipeline._stage_plan_approval(job))
    await _until(lambda: manager.pipeline.waiting_question(job.job_id) == "q-1")
    answered = await manager.panel_decide("rework_plan", {"job_id": job.job_id, "question_id": "q-1"})
    assert answered["ok"] is True, (answered, job.data.get("state"), job.data.get("question"))
    await _until(lambda: (job.data.get("question") or {}).get("question_id") == "q-2")   # вопрос сохранён в задаче
    assert job.data["question"]["kind"] == "text"
    blocks = manager.panel_blocks()
    assert any(b.get("title") == "Ждёт решения: история и план" and "Замечание" in b["text"] for b in blocks)
    assert buttons_of(ap.waiting_blocks(job, job.data["question"])) == []
    assert manager.answer(job.job_id, "q-2", "сделай короче") is None
    assert await asyncio.wait_for(stage, 5) == "plan"
    assert job.data["plan_notes"] == ["сделай короче"]


async def test_approve_plan_moves_the_task_on_to_voiceover(tmp_path):
    client, manager = manager_for(tmp_path)
    manager.pipeline.core = StubCore(tmp_path, {})
    job = make_job(manager, "Одобрен", "waiting_user", plan=True)
    job = manager._find(job.job_id)
    job.data.update(state="running", stage="plan_approval")       # стадию ставит run_job
    stage = asyncio.create_task(manager.pipeline._stage_plan_approval(job))
    await _until(lambda: manager.pipeline.waiting_question(job.job_id) == "q-1")
    assert (await manager.panel_decide("approve_plan", {"job_id": job.job_id, "question_id": "q-1"}))["ok"] is True
    assert await asyncio.wait_for(stage, 5) == "voiceover"


async def test_the_done_message_names_the_stage_and_the_next_step():
    assert "черновик возвращён" in ap.action_done_message("rework_draft", EDITS)
    assert "жду замечание" in ap.action_done_message("rework_plan", REDO_PLAN)
    assert ap.action_done_message("approve_draft", READY).startswith("черновик одобрен")


def test_panel_actions_in_code_match_the_manifest():
    assert set(ap.ACTIONS) <= ACTIONS
    assert "approve" not in ACTIONS                        # общая кнопка «Одобрить» убрана (TZ 15b)


async def _until(pred, timeout=5):
    async def loop():
        while not pred():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(loop(), timeout)


def test_question_text_is_not_sent_as_html_or_json_breaking_text(tmp_path):
    """Текст вопроса от агента это данные: блок text (экранируется дашбордом), длина ограничена."""
    class Job:
        job_id = "j-00000001"
        data = {"title": "Т", "stage": "story"}
        plan_dir = tmp_path
        def artifact(self, kind):
            return None
    q = {"question_id": "q-1", "kind": "text", "question": "<b>x</b> " * 500}
    (block,) = ap.waiting_blocks(Job(), q)[:1]
    assert len(block["text"]) <= ap.TEXT_MAX and json.dumps(block)
