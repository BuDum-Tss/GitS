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


EVENT_MAX = 1000          # всё событие целиком (предел текста внимания)
NAME_MAX = 40
TITLE_MAX = 60
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def clean_name(name: str, limit: int = NAME_MAX) -> str:
    """Ник или название из чужих рук в одну строку без кавычек: переводы строк и управляющие символы в пробел, «»\"' в апостроф."""
    text = _CONTROL.sub(" ", str(name))
    text = text.replace("«", "'").replace("»", "'").replace('"', "'")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def event_text(*, text: str, name: str, is_owner: bool, title: str) -> str:
    """Текст события для агента: кто сказал и где. «Маша в голосовом канале «Игры»: «текст»», у владельца «Маша (владелец) ...»."""
    who = clean_name(name) or "кто-то"
    where = f"в голосовом канале «{clean_name(title, TITLE_MAX)}»" if clean_name(title, TITLE_MAX) else "в голосовом канале"
    head = f"{who}{' (владелец)' if is_owner else ''} {where}: «"
    room = max(80, EVENT_MAX - len(head) - 1)
    return f"{head}{text[:room]}»"


# Реплики-поддакивания («угу», «отлично», «хороший план»): человек слушает и кивает, ответа не ждёт. Все слова фразы из этого
# набора и слов не больше BACKCHANNEL_MAX_WORDS. «Да» и «нет» сюда не входят: это могут быть ответы на её вопрос.
BACKCHANNEL_MAX_WORDS = 6
BACKCHANNEL_KIND = "voicechat.backchannel"
_ACK_WORDS = frozenset((
    "угу ага ок окей окэй ладно хорошо отлично класс круто супер здорово понятно ясно ясненько ну вот так точно верно именно "
    "согласен согласна интересно прикольно норм нормально молодец неплохо замечательно прекрасно чудесно отличный хороший "
    "крутой классный отличная хорошая крутая классная идея план мысль решение это такой такая такое очень вполне "
    "ммм мм эм хм хмм ох ух").split())


def is_backchannel(text: str) -> bool:
    """Фраза из одних слов-поддакиваний (не больше `BACKCHANNEL_MAX_WORDS`): без вопроса и без обращения по имени."""
    words = re.findall(r"[а-яёa-z]+", (text or "").lower().replace("ё", "е"))
    if not words or len(words) > BACKCHANNEL_MAX_WORDS or "?" in (text or ""):
        return False
    return all(w in _ACK_WORDS_NORM for w in words)


_ACK_WORDS_NORM = frozenset(w.replace("ё", "е") for w in _ACK_WORDS)


def v2_fields(*, session: str, speaker_id: str, adapter: str, addressed: bool, reason: str, text: str = "") -> dict:
    """Добавка для внимания v2 (`from_legacy` читает ключ `v2`, старое внимание его не видит): приоритет по причине обращения
    (`name`/`owner`/`one_to_one` это USER, `continuation` это IMPORTANT, не обращённая фраза это BACKGROUND), ключ слияния
    `<сессия>:<говорящий>`, инструмент ответа по адаптеру (`discord.say` для Discord, иначе `voice.say`)."""
    fields = {"priority": V2_PRIORITY.get(reason, "USER") if addressed else "BACKGROUND",
              "group_key": f"{session}:{speaker_id}", "reply_tool": V2_SAY_TOOL.get(adapter, "voice.say")}
    if addressed and reason != "name" and is_backchannel(text):
        # поддакивание не должно доливаться в идущий ход и будить агента: отдельный пассивный вид, фон
        fields.update({"priority": "BACKGROUND", "kind": BACKCHANNEL_KIND})
    return fields


def utterance_event(*, text: str, speaker: dict, session: str, adapter: str, duration_ms: int, language: str,
                    addressed: bool, reason: str, interrupted: bool = False,
                    urgency_hint: int | None = None, title: str = "") -> dict:
    """Тело события для `Attention.submit(PEER_ID, event)`: поля те же, что у событий модулей (ТЗ 01, раздел 7.1)."""
    body = event_text(text=text, name=speaker.get("name") or speaker["id"], is_owner=bool(speaker.get("is_owner")), title=title)
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
        "v2": v2_fields(session=session, speaker_id=speaker["id"], adapter=adapter, addressed=addressed, reason=reason, text=text),
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
