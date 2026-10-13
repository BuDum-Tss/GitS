"""Сервис модели решений (Jev-подобной) на GLiNER2.5-multi-Decide: ответы на набор вопросов о тексте за один проход.

Общий сервис GitS (ТЗ 03, ATT-1): им пользуются ядро (менеджер внимания, agent/decide.py) и rimworld-module. Запускается
в своём окружении (`services/decider/.venv`, Python 3.12 + torch CPU), чтобы не мешать Gemma на видеокарте; супервизор ядра
запускает его сам (запись `decider` в agent/modules.yaml, kind: service):

    services\\decider\\.venv\\Scripts\\python.exe services\\decider\\decider_server.py            (порт 8768, процессор)

Протокол (близок к Kev/Jev):
    POST /v1/systemone  {"state": "...", "questions": {"id": {"type": "choice", "options": [...], "instructions": "..."}}}
    ->  {"answers": {"id": {"type": "choice", "answer": "<вариант>", "confidence": 0.31, "probabilities": {"<вариант>": 0.31}}},
         "ms": 280}
    GET  /health

Типы вопросов: choice (options: список или словарь {вариант: описание}), score (options: упорядоченные уровни, answer это уровень),
noul (да/нет, answer "yes"/"no"). Модель отдаёт выбранный вариант и уверенность; вероятности остальных вариантов она не
сообщает, поэтому в `probabilities` только выбранный. Веса: fastino/GLiNER2.5-multi-Decide (Apache-2.0), кэш services/decider/hf (HF_HOME)."""
import asyncio
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ.setdefault("HF_HOME", str(Path(__file__).parent / "hf"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")        # веса уже в кэше: без обращений к сети при каждом запуске

from aiohttp import web  # noqa: E402
from envcompat import env  # noqa: E402

MODEL_ID = env("GITS_DECIDER_MODEL", "fastino/GLiNER2.5-multi-Decide")
PORT = int(env("GITS_DECIDER_PORT", "8768"))
MAX_STATE_CHARS = 6000

_model = None
_lock = asyncio.Lock()


def load():
    global _model
    from gliner2 import AutoExtractor
    t = time.time()
    _model = AutoExtractor.from_pretrained(MODEL_ID)
    print(f"[decider] модель {MODEL_ID} загружена за {time.time() - t:.1f} с", flush=True)
    _model.classify_text("warmup", {"x": ["a", "b"]})          # первый вызов дольше остальных


def _labels(q: dict) -> dict[str, str]:
    """{текст метки для модели: вариант ответа}. Описание варианта (criteria {вариант: описание}) становится текстом метки:
    зашифрованное имя вроде draft_colonists модель понимает хуже, чем фразу «enemies attack, call colonists to arms»."""
    opts = q.get("options") if q.get("options") is not None else q.get("criteria")
    if q.get("type") == "noul":
        return {"yes": "yes", "no": "no"}
    if isinstance(opts, dict):
        out = {}
        for k, desc in opts.items():
            text = str(desc) if desc else str(k).replace("_", " ")
            out[text if text not in out else f"{text} ({k})"] = str(k)
        return out
    return {str(o).replace("_", " "): str(o) for o in (opts or [])}


def decide_sync(state: str, questions: dict) -> dict:
    tasks, kinds, back = {}, {}, {}
    for qid, q in questions.items():
        labels = _labels(q) if isinstance(q, dict) else {}
        if len(labels) >= 2:
            tasks[str(qid)] = list(labels)
            kinds[str(qid)] = q.get("type", "choice")
            back[str(qid)] = labels
    if not tasks:
        return {}
    raw = _model.classify_text(state[:MAX_STATE_CHARS], tasks, include_confidence=True)
    answers = {}
    for qid, res in raw.items():
        text, conf = (res.get("label"), float(res.get("confidence") or 0.0)) if isinstance(res, dict) else (res, 0.0)
        label = back.get(qid, {}).get(text, text)
        answers[qid] = {"type": kinds.get(qid, "choice"), "answer": label, "confidence": round(conf, 4),
                        "probabilities": {label: round(conf, 4)}}
        if kinds.get(qid) == "noul":
            answers[qid]["noul"] = round(conf if label == "yes" else 1.0 - conf, 4)
    return answers


async def handle_decide(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    state, questions = str(body.get("state") or ""), body.get("questions")
    if not state or not isinstance(questions, dict):
        return web.json_response({"error": "нужны state и questions"}, status=400)
    t0 = time.perf_counter()
    async with _lock:                      # модель одна, вызовы по очереди
        answers = await asyncio.to_thread(decide_sync, state, questions)
    return web.json_response({"answers": answers, "ms": round((time.perf_counter() - t0) * 1000)})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": _model is not None, "model": MODEL_ID})


def main():
    load()
    app = web.Application()
    app.router.add_post("/v1/systemone", handle_decide)
    app.router.add_get("/health", handle_health)
    print(f"[decider] слушаю http://127.0.0.1:{PORT}", flush=True)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    sys.exit(main())
