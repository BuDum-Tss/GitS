"""Журнал её действий и решений: что именно она поставила, пометила, приказала и почему.

Без него она видит чертежи на карте, но не помнит, что поставила их сама (и сочиняет «кто-то решил»). Пишет MCP-сервер
(отдельный процесс) в memory/actions.jsonl; читают контроллер, комментатор и разговор. Подряд идущие одинаковые действия
сворачиваются в одну строку.
"""
import json
import time
from pathlib import Path

FILE = Path(__file__).parent / "memory" / "actions.jsonl"
KEEP = 300
# команды, которые что-то меняют в игре (запросы и служебные в журнал не идут)
MUTATING = {"build", "place_line", "cancel_construction", "deconstruct", "designate_mine", "designate_chop",
            "designate_smooth", "create_stockpile", "create_grow_zone", "delete_zone", "set_zone_home", "add_bill",
            "cancel_bill", "order_tame", "order_slaughter", "research", "forbid", "unforbid", "equip", "wear",
            "set_work_priority", "set_schedule", "force_work", "draft", "undraft", "capture", "rescue", "tend"}


# рутина, которая не стоит упоминания в речи и забивает журнал (расписание, приоритеты, разрешение предметов)
NOISE = {"set_schedule", "set_work_priority", "unforbid", "forbid", "set_manual_priorities", "force_work", "draft", "undraft"}


def _describe(cmd: dict) -> str:
    t = cmd.get("type", "?")
    what = cmd.get("def_name") or cmd.get("recipe") or cmd.get("project") or ""
    mat = f" из {cmd['material']}" if cmd.get("material") else ""
    if t == "build":
        return f"чертёж {what}{mat} на ({cmd.get('x')},{cmd.get('z')})"
    if t == "place_line":
        return f"линия {what}{mat} ({cmd.get('x1')},{cmd.get('z1')})→({cmd.get('x2')},{cmd.get('z2')})"
    if t == "add_bill":
        return f"заказ {what} x{cmd.get('count', 1)} на верстак {cmd.get('workbench_id')}"
    if t in ("cancel_construction", "deconstruct", "designate_mine", "designate_chop", "designate_smooth"):
        pos = f" ({cmd['x']},{cmd['z']})" if "x" in cmd else ""
        return f"{t}{pos}"
    return t + (f" {what}" if what else "")


def record(cmd: dict, result: str):
    """Записать выполненное (или не удавшееся) действие."""
    if cmd.get("type") not in MUTATING:
        return
    try:
        ok = not str(result).startswith(("ERROR", "WARNING"))
        entry = {"ts": time.time(), "type": cmd["type"], "text": _describe(cmd), "ok": ok}
        if not ok:
            entry["error"] = str(result)[:140]
        FILE.parent.mkdir(parents=True, exist_ok=True)
        with FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def note(decision: str, why: str = ""):
    """Решение с причиной (не команда игре): «ставлю склад у входа, потому что…»."""
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        with FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "type": "decision", "text": decision[:200], "why": why[:200],
                                "ok": True}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load(limit: int = KEEP) -> list[dict]:
    try:
        lines = FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def recent_text(minutes: int = 90, limit: int = 14, entries: list[dict] | None = None, speech: bool = False) -> str:
    """Строки для подсказки: что она делала недавно. Подряд идущие одинаковые типы сворачиваются."""
    now = time.time()
    items = [e for e in (entries if entries is not None else _load()) if now - e["ts"] <= minutes * 60
             and not (speech and e["type"] in NOISE)]
    rows: list[list] = []
    for e in items:
        label = e["text"] + ("" if e.get("ok", True) else f" (НЕ УДАЛОСЬ: {e.get('error', '')})")
        if e["type"] == "decision" and e.get("why"):
            label += f", потому что {e['why']}"
        if rows and rows[-1][0] == e["type"] and e["type"] not in ("decision",) and rows[-1][3] == e.get("ok", True):
            rows[-1][2] += 1
            rows[-1][4] = label
        else:
            rows.append([e["type"], e["ts"], 1, e.get("ok", True), label])
    lines = []
    for typ, ts, count, ok, label in rows[-limit:]:
        when = time.strftime("%H:%M", time.localtime(ts))
        lines.append(f"- {when} {label}" + (f" (и ещё {count - 1} таких подряд, последнее выше)" if count > 1 else ""))
    return "\n".join(lines)


def block(minutes: int = 90, limit: int = 14, speech: bool = False) -> str:
    text = recent_text(minutes, limit, speech=speech)
    if not text:
        return ""
    return ("=== ТВОИ ПОСЛЕДНИЕ ДЕЙСТВИЯ И РЕШЕНИЯ (это сделала ты сама; чертежи и пометки на карте, скорее всего, твои, "
            "а не чьи-то чужие) ===\n" + text + "\n\n")
