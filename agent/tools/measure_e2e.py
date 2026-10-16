"""Сквозной замер реакции (ТЗ 24, TRC-7): p50 и p95 по этапам для чата и голоса.

Два режима:
  stub  (по умолчанию) - сценарий на заглушках: обращения идут через настоящее ядро (Companion, трасса, журнал), модель и
        речь подменены. Цифры показывают накладные расходы ядра и самой трассы, а не скорость модели или синтеза.
        Ничего внешнего не запускается: ни LM Studio, ни TTS, ни сеть, ни игра.
  live  - по просьбе владельца, на запущенном ядре: отправляет реплики в дашборд (POST /api/chat), ждёт новую трассу
        и читает GET /api/latency. Нужен работающий ядро на http://127.0.0.1:8766.

Запуск: python agent/tools/measure_e2e.py [--turns 20] [--mode stub|live] [--out путь.md]
Результат печатается и (с --out) пишется в markdown-таблицу. Файл docs/LATENCY_BASELINE.md заполняет владелец из режима live.
"""
import argparse
import asyncio
import copy
import json
import sys
import tempfile
import time
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
for _p in (AGENT, AGENT.parent / "sdk"):              # ядро и пакет gits_sdk (до установки пакета)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

STAGE_NAMES = {
    "mic_end": "конец фразы", "stt_done": "распознавание", "core_in": "приход в ядро", "classify_done": "классификатор",
    "route": "выбор маршрута", "retrieve_done": "память", "llm_first_token": "первый токен", "llm_done": "ответ модели",
    "first_sentence_ready": "первое предложение", "tts_first_audio": "синтез первого звука", "play_start": "начало звука",
}

PHRASES = ["привет, как дела", "расскажи, что ты умеешь", "какая сегодня погода", "запомни, что я люблю чай",
           "нет, не так, я имел в виду другое", "спасибо, хорошего дня"]


class _Speech:
    """Речь без модуля голоса (как FakeSpeech в тестах): говорит мгновенно, очередь внимания получает сыгранность."""

    def __init__(self):
        self.speaking = False

    async def speak(self, utterance_id, text, emotion, priority):
        return None

    async def stop(self, utterance_id=None):
        self.speaking = False

    def is_speaking(self):
        return self.speaking

    def current_segment(self):
        return ""


class _NoDecider:
    """Классификатора нет: вызовов сети не будет (ТЗ 03, как без сервиса decider)."""
    last_ms = None

    async def classify(self, text, questions, timeout=None):
        return None


class _Io:
    async def deliver(self, channel, text):
        return True

    async def from_module(self, *a, **k):
        return None


class _Router:
    verbs = ("найд", "включ")
    last_kind = "chat"

    def looks_like_action(self, text):
        return False

    async def handle(self, text, announce=True):
        return None


async def run_stub(turns: int, memory_dir: str) -> dict:
    """Сценарий на заглушках: `turns` реплик чата и `turns` реплик голоса через настоящее ядро.
    Глобалы, которые подменяются (модель, эмоции, классификатор, папка журнала), возвращаются на место."""
    import decide
    import events_log
    import fast_brain
    import latency_trace as lt
    import settings
    import speech_out
    from attention import Attention
    from companion import Companion
    from conversation_io import DASHBOARD, UserInput
    from relationships import Relationships

    saved = (fast_brain._ask, fast_brain.publish, fast_brain.react_chat, decide.decide, events_log._dir)
    settings.init(memory_dir)
    events_log.init(memory_dir)

    async def ask(system, user, timeout=30, max_tokens=40, image_b64=None, temperature=0.3, background=False):
        return "Хорошо, поняла."                      # модель заглушена: ответ мгновенный

    async def no_reaction(text, rel=None):
        return None

    async def no_classifier(*args, **kwargs):
        return None

    fast_brain._ask = ask
    fast_brain.publish = lambda *a, **k: None         # эмоции не пишем в файл и не рассылаем
    fast_brain.react_chat = no_reaction
    decide.decide = no_classifier
    try:
        presence = speech_out.PresenceTracker()
        companion = Companion(memory_dir, Relationships(memory_dir), speech=_Speech(), presence=presence)
        companion.io = _Io()
        companion.graph = None
        companion.router = _Router()
        cfg = copy.deepcopy(settings.DEFAULTS)
        att = Attention(companion, companion.io, settings_source=lambda: cfg, decider=_NoDecider(),
                        rules_file=Path(memory_dir) / "rules.json", idle_source=lambda: 0.0, autopump=False)
        companion.attention = att

        for i in range(turns):
            await companion.on_input(UserInput(PHRASES[i % len(PHRASES)], DASHBOARD))
            await att.speech.pump()
            await att.settle()
        for i in range(turns):
            # голос: метки конца фразы и распознавания, как их ставит модуль голоса; звук - метки, как после Speaker
            wire = lt.wire_new("voice", marks={"mic_end": lt.now_ms()})
            wire["marks"]["stt_done"] = lt.now_ms()
            t = lt.from_bus(wire, "voice.utterance")
            await companion.on_voice(PHRASES[i % len(PHRASES)], {"owner": True, "has_owner": True}, trace=t)
            lt.speak_sent(t)
            await att.speech.pump()
            await att.settle()
            now = lt.now_ms()
            lt.audio_arrived({"id": t.id, "marks": {"tts_first_audio": now, "play_start": now}})
        events_log.flush()
        rows = events_log.tail(limit=10000, kinds={"trace"})
        return {"mode": "stub", "turns_chat": turns, "turns_voice": turns, **lt.summarize(rows)}
    finally:
        fast_brain._ask, fast_brain.publish, fast_brain.react_chat, decide.decide, events_log._dir = saved
        events_log.flush()


class _SimLLM:
    """Модель с заданной скоростью (ТЗ 25): `sentences` предложений по `sentence_ms` мс. Целиком отдаёт ответ после последнего,
    потоком - каждое предложение сразу, как готово. Скорость условная: сравнивается путь ответа, а не LM Studio."""
    SENTENCES = ("Первое предложение ответа идёт сюда. ", "Второе предложение чуть длиннее первого. ",
                 "Третье предложение завершает мысль. ", "Четвёртое предложение уже лишнее. ")

    def __init__(self, sentence_ms: float, sentences: int = 3):
        self.sentence_ms, self.sentences = sentence_ms, sentences

    async def ask(self, system, user, *, timeout, max_tokens=40, temperature=0.3, background=False, image_b64=None, parallel=False):
        await asyncio.sleep(self.sentence_ms * self.sentences / 1000)
        return "".join(self.SENTENCES[:self.sentences])

    async def ask_stream(self, system, user, *, timeout, max_tokens=140, temperature=0.7):
        for sent in self.SENTENCES[:self.sentences]:
            await asyncio.sleep(self.sentence_ms / 1000)
            yield sent


class _VoiceLikeSpeech(_Speech):
    def __init__(self, streaming: bool):
        super().__init__()
        self.streaming = streaming

    async def speak(self, utterance_id, text, emotion, priority, *, queue=False):
        return None


async def run_compare(turns: int, memory_dir: str, sentence_ms: float = 250.0, sentences: int = 3) -> dict:
    """До и после (ТЗ 25, приёмка): реплики чата с одной и той же условной моделью, ответ целиком и потоком по предложениям.
    Возвращает по каждому режиму сводку трасс; главное число - `at.first_sentence_ready` (когда первое предложение ушло в речь)."""
    import decide
    import events_log
    import fast_brain
    import latency_trace as lt
    import settings
    import speech_out
    from attention import Attention
    from companion import Companion
    from conversation_io import DASHBOARD, UserInput
    from relationships import Relationships

    saved = (fast_brain.publish, fast_brain.react_chat, decide.decide, events_log._dir)
    settings.init(memory_dir)
    events_log.init(memory_dir)

    async def no_reaction(text, rel=None):
        return None

    async def no_classifier(*args, **kwargs):
        return None

    fast_brain.publish = lambda *a, **k: None
    fast_brain.react_chat = no_reaction
    decide.decide = no_classifier
    out: dict = {"sentence_ms": sentence_ms, "sentences": sentences}
    try:
        for mode in ("whole", "stream"):
            sub = Path(memory_dir) / mode
            sub.mkdir(parents=True, exist_ok=True)
            events_log.init(str(sub))
            companion = Companion(str(sub), Relationships(str(sub)), speech=_VoiceLikeSpeech(mode == "stream"),
                                  presence=speech_out.PresenceTracker(), llm=_SimLLM(sentence_ms, sentences))
            companion.io = _Io()
            companion.graph = None
            companion.router = _Router()
            cfg = copy.deepcopy(settings.DEFAULTS)
            att = Attention(companion, companion.io, settings_source=lambda cfg=cfg: cfg, decider=_NoDecider(),
                            rules_file=sub / "rules.json", idle_source=lambda: 0.0, autopump=False)
            companion.attention = att
            for i in range(turns):
                await companion.on_input(UserInput(PHRASES[i % len(PHRASES)], DASHBOARD))
                await att.speech.pump()
                await att.settle()
            events_log.flush()
            out[mode] = lt.summarize(events_log.tail(limit=10000, kinds={"trace"}))
        return out
    finally:
        fast_brain.publish, fast_brain.react_chat, decide.decide, events_log._dir = saved
        events_log.flush()


def compare_markdown(rep: dict) -> str:
    lines = [f"Условная модель: {rep['sentences']} предложения по {rep['sentence_ms']} мс. Метка `first_sentence_ready`: когда первое "
             "предложение ушло в речь (от прихода обращения в ядро).", "",
             "| режим | p50, мс | p95, мс | n |", "|---|---|---|---|"]
    for mode, title in (("whole", "ответ целиком (до ТЗ 25)"), ("stream", "поток по предложениям")):
        at = (rep.get(mode) or {}).get("at", {}).get("first_sentence_ready", {})
        lines.append(f"| {title} | {at.get('p50')} | {at.get('p95')} | {at.get('n')} |")
    return "\n".join(lines) + "\n"


async def run_live(turns: int, url: str, timeout_s: float = 90.0) -> dict:
    """Живое ядро: реплики в дашборд, ожидание новой трассы на каждую, итог из GET /api/latency."""
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(f"{url}/api/latency", params={"window": "86400"}) as r:
            before = (await r.json()).get("traces", 0)
        for i in range(turns):
            async with s.post(f"{url}/api/chat", json={"message": PHRASES[i % len(PHRASES)]}) as r:
                if r.status != 200:
                    raise RuntimeError(f"дашборд ответил {r.status}")
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                await asyncio.sleep(1.0)
                async with s.get(f"{url}/api/latency", params={"window": "86400"}) as r:
                    if (await r.json()).get("traces", 0) > before + i:
                        break
        async with s.get(f"{url}/api/latency", params={"window": "86400"}) as r:
            rep = await r.json()
    return {"mode": "live", "turns_chat": turns, **{k: rep.get(k) for k in ("traces", "by_source", "steps", "at", "end_to_sound")}}


def to_markdown(rep: dict) -> str:
    lines = [f"Режим: {rep.get('mode')}. Трасс: {rep.get('traces')}. Источники: {rep.get('by_source')}.", "",
             "| этап | что измеряет | p50, мс | p95, мс | n |", "|---|---|---|---|---|"]
    for stage, v in (rep.get("steps") or {}).items():
        lines.append(f"| {stage} | {STAGE_NAMES.get(stage, stage)} (шаг от предыдущего) | {v.get('p50')} | {v.get('p95')} | {v.get('n')} |")
    e = rep.get("end_to_sound") or {}
    lines.append(f"| до звука | от конца фразы до начала воспроизведения | {e.get('p50')} | {e.get('p95')} | {e.get('n')} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--turns", type=int, default=20)
    ap.add_argument("--mode", choices=("stub", "live"), default="stub")
    ap.add_argument("--url", default="http://127.0.0.1:8766")
    ap.add_argument("--out", default="")
    ap.add_argument("--compare", action="store_true", help="до и после ТЗ 25: ответ целиком и потоком на условной модели")
    ap.add_argument("--sentence-ms", type=float, default=250.0, help="для --compare: время одного предложения модели")
    args = ap.parse_args(argv)
    if args.compare:
        with tempfile.TemporaryDirectory() as tmp:
            rep = asyncio.run(run_compare(args.turns, tmp, args.sentence_ms))
        md = compare_markdown(rep)
        print(md)
        if args.out:
            Path(args.out).write_text(md, encoding="utf-8")
        return 0
    if args.mode == "stub":
        with tempfile.TemporaryDirectory() as tmp:
            rep = asyncio.run(run_stub(args.turns, tmp))
    else:
        rep = asyncio.run(run_live(args.turns, args.url))
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    md = to_markdown(rep)
    print(md)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
