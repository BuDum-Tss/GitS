"""Обработчики событий модуля голоса (ТЗ 12; вынесены из core_service в ТЗ 29, ARC-7, поведение прежнее)."""
import asyncio

import latency_trace

VOICE_SILENT_KINDS = {"voice.speech", "voice.user_speaking", "voice.utterance", "voice.trace"}      # частые и служебные: в module_event не пишутся


def make_voice_handlers(companion, attention, speech) -> dict:
    """События модуля голоса, которые ядро обрабатывает само, мимо оценки внимания (ТЗ 12): voice.utterance - реплика
    пользователя в разговор с каналом voice (VOICE-6); voice.speech и voice.user_speaking - присутствие (VOICE-17, VOICE-8);
    voice.named - голосу назначено имя, человек заводится в памяти и отношениях (раньше это делал инструмент name_voice)."""

    async def on_utterance(item) -> None:
        speaker = (item.data or {}).get("speaker")
        if not isinstance(speaker, dict):
            sender = item.sender or {}
            speaker = {"id": str(sender.get("id", "")).removeprefix("voice:") or None, "name": sender.get("name"),
                       "new": False, "owner": bool(sender.get("is_owner")), "has_owner": bool(sender.get("is_owner"))}
        if item.trace is not None:
            await companion.on_voice(item.text, speaker, trace=item.trace)
        else:
            await companion.on_voice(item.text, speaker)

    async def on_speech(item) -> None:
        if hasattr(speech, "on_event"):
            speech.on_event(item.data or {})

    async def on_user_speaking(item) -> None:
        attention.set_user_speaking(bool((item.data or {}).get("speaking")))

    async def on_trace(item) -> None:
        latency_trace.audio_arrived((item.data or {}).get("trace"))      # метки звука (ТЗ 24, TRC-1): запись трассы

    async def on_named(item) -> None:
        data = item.data or {}
        name, voice_id = str(data.get("name") or "").strip(), data.get("voice_id")
        if not name or not voice_id:
            return
        if data.get("is_owner"):
            writer = getattr(companion, "memory_writer", None)
            if writer is not None:
                await asyncio.to_thread(writer.add_fact, f"Имя: {name}", kind="user", importance=5)
        else:
            rel = getattr(companion, "_rel", None)
            if rel is not None:
                await asyncio.to_thread(rel.ensure_person, f"voice:{voice_id}", name, source="voice")

    return {"voice.utterance": on_utterance, "voice.speech": on_speech, "voice.user_speaking": on_user_speaking,
            "voice.named": on_named, "voice.trace": on_trace}


def make_heard_hook(companion):
    """VOICE-14: сводка услышанного в колонках дополнительно пишется в память занятий и в контекст разговора, а сам факт
    «В колонках: ...» идёт обычным путём внимания (voice.heard, срочность 1: чаще всего в сводку)."""
    async def on_heard(item) -> None:
        summary = str((item.data or {}).get("summary") or item.text.removeprefix("В колонках:")).strip()
        if not summary:
            return
        if hasattr(companion, "note_heard"):
            companion.note_heard(summary)
        mem = getattr(companion, "_mem", None)
        if mem is not None:
            await asyncio.to_thread(mem.add_activity, "звук", "audio", summary)
    return on_heard
