"""Выгрузка из журнала событий для оценки классификатора обращений (ТЗ 03, ATT-5).

Из `memory/events.db` берутся реплики пользователя (вид `user`) и итоги маршрутизатора (вид `plan`):
  - `выполнено: <текст>` с feasible=true       -> action  (просьба выполнена);
  - `не могу: <текст>` с feasible=false        -> cannot  (просьба понята, инструмента нет);
  - иначе (плана не было или планировщик решил, что это разговор) -> chat.
Реплики пишутся в JSONL (`ts`, `text`, `plan`). С флагом `--eval` каждая реплика отправляется классификатору (сервис decider
должен работать), и считается точность `intent` против разметки маршрутизатора: нужен ли маршрутизатор (action и cannot) или
хватает разговора (chat). Намерения `action` и `attention_rule` считаются «нужен маршрутизатор», остальные «разговор».

    python agent/tools/export_decide_eval.py --out decide_eval.jsonl [--days 14] [--eval] [--memory-dir memory]
"""
import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events_log  # noqa: E402

PLAN_WINDOW_S = 180.0                 # итог маршрутизатора ищется не позже чем через столько секунд после реплики
ROUTER_INTENTS = ("action", "attention_rule")
_DONE, _CANNOT = "выполнено: ", "не могу: "


def build_dataset(events: list[dict], window_s: float = PLAN_WINDOW_S) -> list[dict]:
    """Реплики пользователя с разметкой по итогу маршрутизатора. events - как events_log.tail (старые сверху)."""
    plans = [e for e in events if e.get("kind") == "plan"]
    rows = []
    for ev in events:
        if ev.get("kind") != "user" or not ev.get("text", "").strip():
            continue
        text, label = ev["text"].strip(), "chat"
        for p in plans:
            if not (0 <= p["ts"] - ev["ts"] <= window_s):
                continue
            ptext = p.get("text", "")
            feasible = (p.get("data") or {}).get("feasible")
            if ptext.startswith(_DONE) and ptext[len(_DONE):].strip() == text and feasible is True:
                label = "action"
                break
            if ptext.startswith(_CANNOT) and ptext[len(_CANNOT):].strip() == text and feasible is False:
                label = "cannot"
                break
        rows.append({"ts": ev["ts"], "text": text, "plan": label})
    return rows


def needs_router(plan: str) -> bool:
    return plan in ("action", "cannot")


async def evaluate(rows: list[dict], classify: Callable[[str], Awaitable[str | None]]) -> dict:
    """Точность intent против разметки маршрутизатора. classify(text) -> намерение или None (классификатор не уверен/нет)."""
    confusion: Counter = Counter()
    known = correct = 0
    for row in rows:
        intent = await classify(row["text"])
        row["intent"] = intent
        if intent is None:
            confusion[(row["plan"], "нет ответа")] += 1
            continue
        known += 1
        predicted = intent in ROUTER_INTENTS
        correct += predicted == needs_router(row["plan"])
        confusion[(row["plan"], intent)] += 1
    return {"total": len(rows), "classified": known, "correct": correct,
            "accuracy": correct / known if known else None, "coverage": known / len(rows) if rows else None,
            "confusion": {f"{plan} -> {intent}": n for (plan, intent), n in confusion.most_common()}}


async def _classify_with_service(text: str) -> str | None:
    import decide
    cls = decide.parse_input(await decide.Decider().classify(text, decide.INPUT_QUESTIONS, timeout=5.0), 0.5)
    return cls.intent if cls else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--memory-dir", default="./memory")
    parser.add_argument("--out", default="decide_eval.jsonl")
    parser.add_argument("--days", type=float, default=14.0)
    parser.add_argument("--eval", action="store_true", help="прогнать реплики через классификатор и посчитать точность intent")
    args = parser.parse_args(argv)
    events_log.init(args.memory_dir)
    events = events_log.tail(100_000, {"user", "plan"}, since=time.time() - args.days * 86400)
    rows = build_dataset(events)
    result = asyncio.run(evaluate(rows, _classify_with_service)) if args.eval else None
    with open(args.out, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Выгружено реплик: {len(rows)} -> {args.out} ({dict(Counter(r['plan'] for r in rows))})")
    if result:
        acc = f"{result['accuracy'] * 100:.1f}%" if result["accuracy"] is not None else "нет данных (сервис недоступен?)"
        print(f"Классификатор ответил по {result['classified']} из {result['total']}; точность «нужен ли маршрутизатор»: {acc}")
        for pair, n in result["confusion"].items():
            print(f"  {pair}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
