"""Набор проверки качества справочника (ТЗ 04, KB-23, KB-21): recall@k и доля нерелевантных ответов.

    python -m tools.knowledge_eval                       # синтетический набор, детерминированный эмбеддер, без сети
    python -m tools.knowledge_eval --live --db memory/knowledge.db --questions мои.jsonl   # настоящий справочник и EmbeddingGemma

Вопрос набора: `{"q": "...", "expect_uri_contains": ["Bedroom"]}` - попал, если среди первых k кусков есть документ, чей `uri`
содержит одну из подстрок; `{"q": "...", "offtopic": true}` - вопрос не по теме, после порога ответ должен быть пустым.
Показатели: `recall_at_k` (цель >= 0.8) и `offtopic_rate` (доля непустых ответов на вопросы не по теме, цель <= 0.1).
Встроенный набор небольшой и синтетический (ТЗ просит 40 вопросов по загруженной вики RimWorld, её в репозитории нет): он проверяет
механику и пороги; на настоящей вики запускать с `--live --questions`. Подбор порогов: `--min-cos`, `--min-cos-bm25`."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "sdk")]

DATA = HERE / "knowledge_eval_data"
COLLECTION = "eval.wiki"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def uri_of(hit) -> str:
    return str(hit.meta.get("uri") or hit.key.rsplit("#", 1)[0])


async def evaluate(search, questions: list[dict], *, k: int = 5, collections: list[str] | None = None) -> dict:
    """search - HybridSearch. Возвращает показатели и списки промахов / ложных ответов."""
    on_topic = [q for q in questions if not q.get("offtopic")]
    off_topic = [q for q in questions if q.get("offtopic")]
    misses, noisy, hit_count = [], [], 0
    for q in on_topic:
        hits = await search.search(q["q"], collections=collections, k=k, rewrite=False)
        wanted = q.get("expect_uri_contains") or []
        if any(w in uri_of(h) for h in hits for w in wanted):
            hit_count += 1
        else:
            misses.append(q["q"])
    for q in off_topic:
        if await search.search(q["q"], collections=collections, k=k, rewrite=False):
            noisy.append(q["q"])
    return {"questions": len(on_topic), "recall_at_k": round(hit_count / len(on_topic), 3) if on_topic else None,
            "offtopic_questions": len(off_topic), "offtopic_rate": round(len(noisy) / len(off_topic), 3) if off_topic else None,
            "k": k, "misses": misses, "noisy": noisy}


async def build_synthetic(search, corpus: list[dict]) -> None:
    from knowledge_base import KnowledgeBase
    await KnowledgeBase(search).ingest(COLLECTION, corpus)


async def run(args) -> dict:
    from search import HybridIndex, HybridSearch, SearchConfig
    from search.embedder import HashEmbedder, LmStudioEmbedder
    questions = load_jsonl(Path(args.questions) if args.questions else DATA / "questions.jsonl")
    if args.live:
        search = HybridSearch(HybridIndex(args.db), LmStudioEmbedder(), None,
                              SearchConfig(min_cos=args.min_cos or 0.60, min_cos_bm25=args.min_cos_bm25 or 0.45))
        return await evaluate(search, questions, k=args.k)
    search = HybridSearch(HybridIndex(":memory:"), HashEmbedder(), None,
                          SearchConfig(min_cos=args.min_cos or 0.30, min_cos_bm25=args.min_cos_bm25 or 0.10))
    await build_synthetic(search, load_jsonl(DATA / "corpus.jsonl"))
    return await evaluate(search, questions, k=args.k)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="настоящий справочник (--db) и EmbeddingGemma в LM Studio")
    ap.add_argument("--db", default="memory/knowledge.db")
    ap.add_argument("--questions", help="свой файл вопросов .jsonl")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--min-cos", type=float, default=0.0)
    ap.add_argument("--min-cos-bm25", type=float, default=0.0)
    res = asyncio.run(run(ap.parse_args(argv)))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    ok = (res["recall_at_k"] or 0) >= 0.8 and (res["offtopic_rate"] or 0) <= 0.1
    print("цели KB-23 (recall >= 0.8, не по теме <= 0.1): " + ("выполнены" if ok else "НЕ выполнены"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
