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

_WORD_END = r"\w{0,2}"                      # падежное окончание имени: «Глэдос», «Глэдоса», «Глэдосу»


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


def utterance_event(*, text: str, speaker: dict, session: str, adapter: str, duration_ms: int, language: str,
                    addressed: bool, reason: str, interrupted: bool = False) -> dict:
    """Тело события для `Attention.submit(PEER_ID, event)`: поля те же, что у событий модулей (ТЗ 01, раздел 7.1)."""
    return {
        "kind": KIND_UTTERANCE,
        "text": text[:EVENT_TEXT_MAX],
        "sender": {"id": speaker["id"], "name": speaker.get("name") or speaker["id"], "is_owner": bool(speaker.get("is_owner"))},
        "addressed_to_me": addressed,
        "reply_channel": {"module": "voice", "target": session, "tool": "voice.say", "target_arg": "session"},
        "expires_in_s": UTTERANCE_TTL_S,
        "data": {
            "session": session, "adapter": adapter, "duration_ms": duration_ms, "language": language,
            "speaker": speaker, "needs_reply": addressed, "addressed_reason": reason,
            "interrupted_me": interrupted,
        },
    }
