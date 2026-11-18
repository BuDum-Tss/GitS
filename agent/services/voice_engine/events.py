"""События движка голоса для ядра: что сказал участник канала (событие внимания) и что она сама сказала (журнал).

Принцип «сообщение это событие»: фраза участника голосового канала это событие `voicechat.utterance` менеджера внимания, а не
реплика диалога. Отвечает на неё агент инструментом `voice.say`; её собственные озвученные реплики попадают в память как исходящие
события (`module_out`, вид `voicechat.spoken`), тем же путём, каким туда попадают её сообщения в чаты.
"""
import re
from typing import Iterable

PEER_ID = "voicechat"                       # от чьего имени событие входит в конвейер внимания (участником шины это не является)
KIND_UTTERANCE = "voicechat.utterance"
KIND_SPOKEN = "voicechat.spoken"
UTTERANCE_TTL_S = 90.0                      # устаревшую фразу отвечать поздно: событие живёт полторы минуты
EVENT_TEXT_MAX = 600
SPOKEN_TTL_S = 120.0                        # итог речи для v2: событие-фон, живёт две минуты

# внимание v2 (RUNTIME_SPEC 9): приоритет по причине обращения, у фона BACKGROUND; имена уровней разбирает `from_legacy`
V2_PRIORITY = {"name": "USER", "owner": "USER", "one_to_one": "USER", "continuation": "IMPORTANT"}
V2_SAY_TOOL = {"discord": "discord.say"}    # явная дорожка речи по адаптеру; остальные адаптеры отвечают через `voice.say`

_WORD_END = r"\w{0,2}"                      # падежное окончание имени: до двух букв (имя, имени, имя+окончание); само имя и алиасы приходят из карточки персонажа (`wiring`)


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def name_patterns(names: Iterable[str]) -> list[re.Pattern]:
    """Шаблоны имени и алиасов персонажа: целое слово с возможным коротким окончанием."""
    out = []
    for name in {_norm(n).strip() for n in names if n and n.strip()}:
        if len(name) < 2:
            continue
        out.append(re.compile(rf"(?<!\w){re.escape(name)}{_WORD_END}(?!\w)"))
    return out


def is_addressed(text: str, patterns: list[re.Pattern]) -> bool:
    """Обратились ли к ней по имени или алиасу."""
    low = _norm(text)
    return any(p.search(low) for p in patterns)


TAIL_PHRASES = 6                         # сколько недавних фраз попадает в хвост «до этого в канале»
TAIL_WINDOW_S = 60.0
TAIL_CHARS = 600
TAIL_PHRASE_CHARS = 150
EVENT_MAX = 1000          # всё событие целиком (предел текста внимания)
NAME_MAX = 40
TITLE_MAX = 60
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def clean_name(name: str, limit: int = NAME_MAX) -> str:
    """Ник или название из чужих рук в одну строку без кавычек: переводы строк и управляющие символы в пробел, «»\"' в апостроф."""
    text = _CONTROL.sub(" ", str(name))
    text = text.replace("«", "'").replace("»", "'").replace('"', "'")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def event_text(*, text: str, name: str, is_owner: bool, title: str, context: str = "") -> str:
    """Текст события для агента: кто сказал и где. «Маша в голосовом канале «Игры»: «текст»», у владельца «Маша (владелец) ...»."""
    who = clean_name(name) or "кто-то"
    where = f"в голосовом канале «{clean_name(title, TITLE_MAX)}»" if clean_name(title, TITLE_MAX) else "в голосовом канале"
    head = f"{who}{' (владелец)' if is_owner else ''} {where}: «"
    tail = f"\n({context})" if context else ""
    room = max(80, EVENT_MAX - len(head) - len(tail) - 1)
    return f"{head}{text[:room]}»{tail}"


def recent_tail(phrases, now: float) -> str:
    """Хвост «до этого в канале: Имя: текст; ты: текст»: последние фразы за минуту, старые первыми, не больше ~600 символов.
    Свои реплики подписаны «ты». Текущую фразу вызывающий в `phrases` не включает."""
    picked, used = [], 0
    for ph in reversed([p for p in phrases if now - p.ts <= TAIL_WINDOW_S][-TAIL_PHRASES:]):
        item = f"{'ты' if ph.own else (clean_name(ph.name) or 'кто-то')}: {ph.text[:TAIL_PHRASE_CHARS]}"
        if used + len(item) + 2 > TAIL_CHARS:
            break
        picked.append(item)
        used += len(item) + 2
    return "до этого в канале: " + "; ".join(reversed(picked)) if picked else ""


def v2_fields(*, session: str, speaker_id: str, adapter: str, addressed: bool, reason: str) -> dict:
    """Добавка для внимания v2 (`from_legacy` читает ключ `v2`, старое внимание его не видит): приоритет по причине обращения
    (`name`/`owner`/`one_to_one` это USER, `continuation` это IMPORTANT, не обращённая фраза это BACKGROUND), ключ слияния
    `<сессия>:<говорящий>`, инструмент ответа по адаптеру (`discord.say` для Discord, иначе `voice.say`)."""
    return {"priority": V2_PRIORITY.get(reason, "USER") if addressed else "BACKGROUND",
            "group_key": f"{session}:{speaker_id}", "reply_tool": V2_SAY_TOOL.get(adapter, "voice.say")}


def utterance_event(*, text: str, speaker: dict, session: str, adapter: str, duration_ms: int, language: str,
                    addressed: bool, reason: str, interrupted: bool = False, context: str = "",
                    urgency_hint: int | None = None, title: str = "") -> dict:
    """Тело события для `Attention.submit(PEER_ID, event)`: поля те же, что у событий модулей (ТЗ 01, раздел 7.1)."""
    body = event_text(text=text, name=speaker.get("name") or speaker["id"], is_owner=bool(speaker.get("is_owner")), title=title,
                      context=context)
    event = {
        "kind": KIND_UTTERANCE,
        "text": body,
        "sender": {"id": speaker["id"], "name": speaker.get("name") or speaker["id"], "is_owner": bool(speaker.get("is_owner"))},
        "addressed_to_me": addressed,
        "reply_channel": {"module": "voice", "target": session, "tool": "voice.say", "target_arg": "session"},
        "expires_in_s": UTTERANCE_TTL_S,
        "data": {
            "session": session, "adapter": adapter, "duration_ms": duration_ms, "language": language,
            "speaker": speaker, "needs_reply": addressed, "addressed_reason": reason,
            "interrupted_me": interrupted, "title": title, "phrase": text[:EVENT_TEXT_MAX],
        },
        "v2": v2_fields(session=session, speaker_id=speaker["id"], adapter=adapter, addressed=addressed, reason=reason),
    }
    if urgency_hint is not None and not addressed:
        event["urgency_hint"] = urgency_hint
    return event


def spoken_event(*, session: str, adapter: str, uid: str, spoken_text: str, completed: bool, reason: str = "",
                 spoken_ms: int = 0) -> dict:
    """Итог её речи для внимания v2 (RUNTIME_SPEC 7.1): фон (BACKGROUND, срок 2 мин), агента не будит. Текст это то, что реально прозвучало."""
    clipped = spoken_text[:EVENT_TEXT_MAX]
    tail = "" if completed else f" (оборвано{': ' + reason if reason else ''})"
    return {
        "kind": KIND_SPOKEN, "text": f"ты сказала в голосовом канале: «{clipped}»{tail}", "urgency_hint": 1, "expires_in_s": SPOKEN_TTL_S,
        "data": {"session": session, "adapter": adapter, "utterance_id": uid, "completed": completed, "reason": reason,
                 "spoken_ms": spoken_ms},
        "v2": {"priority": "BACKGROUND"},
    }
