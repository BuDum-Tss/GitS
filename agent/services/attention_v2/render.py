"""Представление агенту (RUNTIME_SPEC раздел 6): строка события, пачка, блок «прервано». Чистые функции без состояния."""
import re
import time

from .event import MAX_TEXT, USER, Event
from .runtime import ActionRecord

NAME_MAX = 40                              # ник и название канала в строке
TOOL_MAX = 60
SUMMARY_MAX = 80
VOICE_SAY_TOOLS = ("voice.say", "discord.say")      # инструменты ответа голосовых событий (текст хода сам уходит в канал)


def is_text_reply(e: Event) -> bool:
    """Ответ на событие идёт обычным текстом хода (ядро направляет его само): голосовая фраза, а также обращение человека (приоритет USER)
    из чата модуля с адресом ответа (`Reply.target`: Telegram, Discord): текст уходит в тот же чат (`deliver`), не голосом."""
    r = e.reply
    if r is None or not r.tool:
        return False
    return r.tool in VOICE_SAY_TOOLS or bool(r.target and e.prio >= USER)


_QUOTES = '"\'`«»“”„‟‘’‚‹›'
_SPACES = re.compile(r"\s+")


def clean_name(value, limit: int = NAME_MAX) -> str:
    """Ник или название для строки события: без переводов строк, управляющих символов и кавычек, длина ограничена."""
    s = "" if value is None else str(value)
    s = "".join(" " if (ch.isspace() or ord(ch) < 32) else ch for ch in s)
    s = s.translate({ord(q): None for q in _QUOTES})
    s = _SPACES.sub(" ", s).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def one_line(text, limit: int = MAX_TEXT) -> str:
    """Текст события в одну строку (переводы строк это пробелы), не длиннее `limit`."""
    s = _SPACES.sub(" ", "" if text is None else str(text)).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def fmt_hms(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def fmt_hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _short_kind(e: Event) -> str:
    kind = e.kind[len(e.workspace) + 1:] if e.kind.startswith(e.workspace + ".") else e.kind
    return f"{clean_name(e.workspace, NAME_MAX)}/{clean_name(kind, NAME_MAX * 2)}"


def sender_label(e: Event) -> str:
    s = e.sender
    if s is None:
        return ""
    name = clean_name(s.name) or clean_name(s.id)
    if not name:
        return ""
    if s.is_owner:
        name += " (владелец)"
    if s.is_bot:
        name += " (бот)"
    return name


def render_event(e: Event) -> str:
    """`[ЧЧ:ММ:СС] workspace/kind Отправитель: текст {ответ: tool}`; без отправителя текст идёт сразу за видом."""
    who = sender_label(e)
    head = f"[{fmt_hms(e.ts)}] {_short_kind(e)}"
    body = f"{who}: {one_line(e.text)}" if who else one_line(e.text)
    line = f"{head} {body}".rstrip()
    if e.reply is not None and e.reply.tool:
        answer = "текстом" if is_text_reply(e) else clean_name(e.reply.tool, TOOL_MAX)   # голос: текст хода звучит сам
        line += f" {{ответ: {answer}}}"
    return f"⟲ {line}" if e.interrupted else line


def sort_for_agent(events: list[Event]) -> list[Event]:
    """Порядок строк: по убыванию приоритета, при равенстве старые первыми."""
    return sorted(events, key=lambda e: (-e.prio, e.ts))


def render_interrupt_note(events: list[Event], actions: list[ActionRecord], started_at: float | None = None) -> str:
    """Блок «прервано» (6.3): какие события шли, какие действия с эффектом завершены (с кратким результатом), какие начаты и не
    завершены. Решать, продолжать ли прерванное, агент будет сам."""
    done = [a for a in actions if a.effect and a.status == "done"]
    unfinished = [a for a in actions if a.status == "started"]
    failed = [a for a in actions if a.status == "failed"]

    def fmt(a: ActionRecord) -> str:
        s = clean_name(a.tool, TOOL_MAX)
        return f"{s} «{one_line(a.summary, SUMMARY_MAX)}»" if a.summary else s

    parts = []
    when = f" начался в {fmt_hms(started_at)}" if started_at is not None else ""
    head = f"ход по событиям ниже{when}"
    if done:
        parts.append("успели: " + ", ".join(fmt(a) for a in done))
    if unfinished:
        parts.append("не успели: " + ", ".join(clean_name(a.tool, TOOL_MAX) for a in unfinished))
    if failed:
        parts.append("сбой: " + ", ".join(clean_name(a.tool, TOOL_MAX) for a in failed))
    if not parts:
        parts.append("действий не было")
    lines = ["(прервано: " + head + "; " + "; ".join(parts) + ")"]
    lines += ["  " + render_event(e) for e in sort_for_agent(events)]
    return "\n".join(lines)


def render_batch(events: list[Event], digest_line: str = "", interrupt_note: str = "") -> str:
    """Пачка для хода (6.2): сводка пропущенного, блок «прервано», строки событий по убыванию приоритета (⟲ у вернувшихся)."""
    lines = []
    if digest_line:
        lines.append(digest_line)
    if interrupt_note:
        lines.append(interrupt_note)
    lines += [render_event(e) for e in sort_for_agent(events)]
    return "\n".join(lines)
