"""Воспроизводимый стенд сравнения вариантов поиска по памяти (ТЗ 04a, часть без внешних сервисов).

Не полный бейкофф: MemPalace, LightRAG и Graphiti здесь не запускаются. Стенд гоняет синтетическую обезличенную выборку
(`memory_bakeoff_data.py`: люди, предпочтения, события, истории, устаревшие факты, вопросы без ответа) через варианты
гибридного индекса `agent/search` и считает recall@5, долю верных ответов на устаревшие факты, долю пустых ответов там,
где ответа нет, и задержку p50/p95. Эмбеддер: `hash` (детерминированный, без сети; знает только совпадения слов) или
`lmstudio` (EmbeddingGemma, нужен запущенный LM Studio).

Запуск из папки agent:  python -m tools.memory_bakeoff [--embedder hash|lmstudio] [--json] [--md docs/MEMORY_BAKEOFF_RESULTS.md]
"""
import argparse
import asyncio
import json
import statistics
import sys
import time

from search.embedder import HashEmbedder, LmStudioEmbedder
from search.index import HybridIndex, SearchConfig
from tools.memory_bakeoff_data import CORPUS, QUESTIONS

DAY = 86400.0
T0 = 1_700_000_000.0
COLL = "memory"

# порог hash-эмбеддера подобран на этой выборке (перебор 0.22-0.40); для EmbeddingGemma действуют значения ТЗ 04
CONFIGS = {
    "hash": SearchConfig(min_cos=0.30, min_cos_bm25=0.10),
    "lmstudio": SearchConfig(),
}


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


async def _build(embedder, supersede: bool) -> HybridIndex:
    idx = HybridIndex(":memory:", clock=lambda: T0 + 31 * DAY)
    texts = [c[2] for c in CORPUS]
    vecs = await embedder.embed(texts, False) if embedder else [None] * len(texts)
    for c, v in zip(CORPUS, vecs, strict=True):
        idx.upsert(COLL, c[0], c[2], vec=v, ts=T0 + c[1] * DAY, supersedes=[c[3]] if supersede and len(c) > 3 else None)
    return idx


async def run_variant(name: str, embedder, cfg: SearchConfig, *, use_fts=True, use_vec=True, supersede=True) -> dict:
    idx = await _build(embedder if use_vec else None, supersede)
    recalls, stale_ok, none_empty, lat = [], [], [], []
    by_kind: dict[str, list[float]] = {}
    for q, kind, gold in QUESTIONS:
        t = time.perf_counter()
        qv = (await embedder.embed([q], True))[0] if (embedder and use_vec) else None
        hits = idx.search(q if use_fts else "", qv, collections=[COLL], k=5, cfg=cfg)
        lat.append((time.perf_counter() - t) * 1000)
        keys = [h.key for h in hits]
        if kind == "none":
            none_empty.append(1.0 if not hits else 0.0)
            continue
        r = sum(1 for g in gold if g in keys) / len(gold)
        recalls.append(r)
        by_kind.setdefault(kind, []).append(r)
        if kind == "stale":
            old = "old" + gold[0][3:]
            stale_ok.append(1.0 if keys and keys[0] == gold[0] and (old not in keys or keys.index(old) > 0) else 0.0)
    return {
        "variant": name,
        "recall_at_5": round(statistics.mean(recalls), 3),
        "recall_by_kind": {k: round(statistics.mean(v), 3) for k, v in by_kind.items()},
        "stale_correct": round(statistics.mean(stale_ok), 3),
        "empty_when_no_answer": round(statistics.mean(none_empty), 3),
        "latency_ms_p50": round(_pct(lat, 0.5), 2),
        "latency_ms_p95": round(_pct(lat, 0.95), 2),
    }


async def run_all(embedder_name: str = "hash") -> dict:
    emb = HashEmbedder() if embedder_name == "hash" else LmStudioEmbedder()
    cfg = CONFIGS[embedder_name]
    rows = [
        await run_variant("fts_only", emb, cfg, use_vec=False),
        await run_variant("vector_only", emb, cfg, use_fts=False),
        await run_variant("hybrid", emb, cfg),
        await run_variant("hybrid_no_supersede", emb, cfg, supersede=False),
    ]
    return {"embedder": embedder_name, "documents": len(CORPUS), "questions": len(QUESTIONS),
            "no_answer_questions": sum(1 for q in QUESTIONS if q[1] == "none"), "rows": rows}


def to_markdown(rep: dict) -> str:
    out = [f"Эмбеддер: `{rep['embedder']}`; записей {rep['documents']}, вопросов {rep['questions']} "
           f"(без ответа {rep['no_answer_questions']}).", "",
           "| Вариант | recall@5 | люди | предпочтения | события | истории | устаревшие верно | пусто без ответа | p50, мс | p95, мс |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        k = r["recall_by_kind"]
        out.append(f"| {r['variant']} | {r['recall_at_5']} | {k.get('person', '-')} | {k.get('preference', '-')} | "
                   f"{k.get('event', '-')} | {k.get('story', '-')} | {r['stale_correct']} | {r['empty_when_no_answer']} | "
                   f"{r['latency_ms_p50']} | {r['latency_ms_p95']} |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedder", choices=["hash", "lmstudio"], default="hash")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--md", help="записать таблицу в файл")
    args = ap.parse_args(argv)
    rep = asyncio.run(run_all(args.embedder))
    text = json.dumps(rep, ensure_ascii=False, indent=2) if args.json else to_markdown(rep)
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(to_markdown(rep) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
