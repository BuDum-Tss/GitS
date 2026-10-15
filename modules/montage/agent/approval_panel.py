"""Одобрение по этапам в пространстве «Монтаж» (ТЗ 15b): у задачи в ожидании ответа подпись этапа, файлы этапа и кнопки
именно этого этапа. Ответ уходит в конвейер тем же путём, что и ответ из обзора (`Pipeline.deliver_answer`): конвейер,
режимы приёмки и формулировки вопросов не меняются, здесь только панель и выбор ответа.

Текст замечания панель принять не может (в блоках нет поля ввода): «Вернуть на доработку» отвечает выбором варианта, а
конвейер сам спрашивает замечание текстом (в обзоре или в чате), как и раньше.
"""
from __future__ import annotations

import os
from urllib.parse import quote

from pipeline import APPROVE, EDITS, READY, REDO_PLAN, REDO_STORY

PLAN_OPTIONS = (APPROVE, REDO_PLAN, REDO_STORY)       # вопрос plan_approval: утвердить историю и план / переделать
DRAFT_OPTIONS = (READY, EDITS)                         # вопрос review: готово / правки
MAX_OPTION_BUTTONS = 6
TEXT_MAX = 900

# кнопка этапа -> (ответ конвейера, варианты вопроса, которым кнопка отвечает)
STAGE_ACTIONS = {
    "approve_plan": (APPROVE, PLAN_OPTIONS),
    "rework_plan": (REDO_PLAN, PLAN_OPTIONS),
    "reject_plan": (REDO_STORY, PLAN_OPTIONS),
    "approve_draft": (READY, DRAFT_OPTIONS),
    "rework_draft": (EDITS, DRAFT_OPTIONS),
}
# вопросы агента (approval: да / нет; choice: по вариантам); ответ проверяется по самому вопросу
ANSWER_ACTION = "answer_question"
ACTIONS = tuple(STAGE_ACTIONS) + (ANSWER_ACTION,)

STAGE_LABELS = {
    "intake": "исходники", "clarify": "уточнение", "transcribe": "расшифровка", "story": "история", "plan": "план",
    "plan_approval": "история и план", "voiceover": "закадровый голос", "build": "сборка", "preview": "черновик",
    "review": "черновик", "final": "финал",
}
REWORK_HINT = ("«Вернуть на доработку» и «Отклонить» текста не принимают: после выбора конвейер спросит замечание "
               "текстом в обзоре или в чате.")
DONE_MESSAGES = {
    "approve_plan": "история и план одобрены, монтаж идёт дальше",
    "rework_plan": "план возвращён на доработку, жду замечание: напишите его в обзоре или в чате",
    "reject_plan": "история отклонена, переделываю историю, жду замечание: напишите его в обзоре или в чате",
    "approve_draft": "черновик одобрен, готовлю финал",
    "rework_draft": "черновик возвращён на доработку, жду замечание: напишите его в обзоре или в чате",
}


def stage_name(stage) -> str:
    return STAGE_LABELS.get(str(stage or ""), str(stage or "этап"))


def core_base() -> str:
    """Адрес HTTP ядра (раздача файлов `/api/artifact`): тот же хост и порт, что и у WebSocket модуля."""
    ws = os.getenv("GLADOS_CORE_URL") or "ws://127.0.0.1:8766/module/ws"
    return "http://" + ws.split("://", 1)[-1].split("/", 1)[0]


def artifact_url(job_id: str, name: str) -> str:
    return f"{core_base()}/api/artifact?module=montage&job={quote(job_id)}&name={quote(name)}"


def stage_label(question: dict, stage) -> str:
    """Подпись этапа: по варианту вопроса (история и план, черновик), иначе по стадии конвейера."""
    options = set(str(o) for o in (question.get("options") or []))
    if question.get("kind") == "choice" and options == set(PLAN_OPTIONS):
        return "история и план"
    if question.get("kind") == "choice" and options == set(DRAFT_OPTIONS):
        return "черновик"
    return stage_name(stage)


def waiting_blocks(job, question: dict) -> list[dict]:
    """Блоки задачи, которая ждёт ответа: подпись этапа, файлы, видео черновика и кнопки этого этапа.
    `job` - задача (job_id, data, plan_dir, artifact()), `question` - её текущий вопрос с question_id."""
    jid = job.job_id
    qid = str(question.get("question_id") or "")
    kind = question.get("kind")
    options = [str(o) for o in (question.get("options") or [])]
    label = stage_label(question, job.data.get("stage"))
    args = {"job_id": jid, "question_id": qid}
    title = str(job.data.get("title") or jid)
    question_text = str(question.get("question") or "")
    if kind == "text":
        text = f"{title}. Замечание ждёт ответа в обзоре или в чате: {question_text}"
    else:
        text = f"{title}. {question_text}"
    blocks: list[dict] = [{"type": "text", "title": f"Ждёт решения: {label}", "text": text[:TEXT_MAX]}]
    if label == "история и план":
        for name, caption in (("story.md", "История: story.md"), ("plan.md", "План: plan.md")):
            if (job.plan_dir / name).is_file():
                blocks.append({"type": "link", "label": caption, "url": artifact_url(jid, name)})
    if label == "черновик":
        preview = job.artifact("preview")
        if preview and os.path.isfile(preview):
            blocks.append({"type": "video", "title": "Черновик",
                           "artifact": {"job": jid, "name": os.path.basename(preview)}})
    items: list[dict] = []
    if kind == "text":
        return blocks
    if label == "история и план" and set(options) == set(PLAN_OPTIONS):
        items = [{"label": "Одобрить историю и план", "action": "approve_plan", "style": "primary"},
                 {"label": "Вернуть на доработку", "action": "rework_plan"},
                 {"label": "Отклонить (переделать историю)", "action": "reject_plan", "style": "danger"}]
        blocks[0]["text"] = (blocks[0]["text"] + " " + REWORK_HINT)[:TEXT_MAX]
    elif label == "черновик" and set(options) == set(DRAFT_OPTIONS):
        items = [{"label": "Одобрить черновик", "action": "approve_draft", "style": "primary"},
                 {"label": "Вернуть на доработку", "action": "rework_draft"}]
        blocks[0]["text"] = (blocks[0]["text"] + " " + REWORK_HINT)[:TEXT_MAX]
    elif kind == "approval":
        items = [{"label": "Одобрить", "action": ANSWER_ACTION, "style": "primary", "args": {**args, "answer": True}},
                 {"label": "Отклонить", "action": ANSWER_ACTION, "style": "danger", "args": {**args, "answer": False}}]
    elif kind == "choice":
        items = [{"label": opt[:40], "action": ANSWER_ACTION, "args": {**args, "answer": opt}}
                 for opt in options[:MAX_OPTION_BUTTONS]]
        return blocks + ([{"type": "buttons", "items": items}] if items else [])
    if not items:
        return blocks
    # кнопки этапа: у каждой id действия и qid, по которым менеджер проверит, что вопрос ещё открыт
    for item in items:
        if item["action"] != ANSWER_ACTION:
            item["args"] = dict(args)
    return blocks + [{"type": "buttons", "items": items}]


def action_done_message(action: str, answer) -> str:
    return DONE_MESSAGES.get(action) or ("ответ принят" if answer is None else f"ответ принят: {answer}")
