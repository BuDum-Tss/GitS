"""Ядро и модуль голоса (ТЗ 12: VOICE-15, VOICE-16, VOICE-17, VOICE-21; ТЗ 13: PET-9): VoiceModuleSpeech против модуля-заглушки
на SDK, события voice.* в разговор и присутствие, речь без модуля голоса (TextOnlySpeech), эмоции в presence."""
import asyncio
import socket
import time

import pytest
from aiohttp import web

import core_service
import events_log
import fast_brain
import speech_out
from companion import Companion
from environments.registry import Registry
from gits_sdk import ModuleClient
from relationships import Relationships


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def voice_manifest(**extra) -> dict:
    base = {"id": "voice", "title": "Голос", "description": "Тестовый модуль голоса.", "version": "1.0.0",
            "capabilities": ["voice.output", "voice.input"], "subscribe": ["settings", "module"],
            "emits": [{"kind": k, "description": k, "default_urgency": 1} for k in
                      ("voice.utterance", "voice.speech", "voice.user_speaking", "voice.heard", "voice.named")]}
    base.update(extra)
    return base


class FakeModule:
    """Модуль голоса-заглушка на настоящем SDK: запоминает speak / speak.stop / tts.synthesize, события шлёт тест."""

    def __init__(self, url, token, manifest=None):
        self.client = ModuleClient(manifest or voice_manifest(), url=url, token=token)
        self.spoken: list[dict] = []
        self.stops: list[dict] = []
        self.synth: list[dict] = []

        @self.client.on_request("speak")
        async def speak(body):
            self.spoken.append(body)

        @self.client.on_request("speak.stop")
        async def stop(body):
            self.stops.append(body)

        @self.client.on_request("tts.synthesize")
        async def synthesize(body):
            self.synth.append(body)
            return {"path": "C:\\tmp\\tts_1.wav", "duration_ms": 900}

    async def speech(self, phase, uid, **data):
        await self.client.event("voice.speech", f"Реплика {uid}: {phase}", data={"phase": phase, "utterance_id": uid, **data})


class Wired:
    """Слой модулей на настоящем компаньоне с выбором голос/текст и настоящем сервере шины."""

    def __init__(self, tmp_path):
        mem = str(tmp_path / "memory")
        self.presence = speech_out.PresenceTracker()
        self.voice = speech_out.VoiceModuleSpeech(self.presence)
        self.text = speech_out.TextOnlySpeech(self.presence)
        self.speech = speech_out.SpeechSelector(self.voice, self.text)
        self.companion = Companion(mem, Relationships(mem), speech=self.speech, presence=self.presence)
        self.heard_calls: list[tuple] = []
        self.voiced: list[tuple] = []

        async def on_voice(text, speaker):
            self.voiced.append((text, speaker))
        self.companion.on_voice = on_voice
        self.port = free_port()
        self.layer = core_service.wire_modules(self.companion, Registry(), port=self.port, specs=[],
                                               battery=lambda: {"level": 1.0, "sleeping": False})
        self.url = f"ws://127.0.0.1:{self.port}/module/ws"

    async def __aenter__(self):
        app = web.Application()
        self.layer.hub.attach(app)
        self.runner = web.AppRunner(app, shutdown_timeout=0.5)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        await self.layer.hub.start()
        return self

    async def __aexit__(self, *exc):
        await self.layer.attention.settle()
        await self.layer.stop()
        await self.runner.cleanup()

    async def module(self) -> FakeModule:
        mod = FakeModule(self.url, self.layer.hub.token)
        mod.task = asyncio.create_task(mod.client.run())
        await self.wait(lambda: self.layer.hub.is_connected("voice"))
        await mod.client.wait_connected(5)
        return mod

    async def stop_module(self, mod: FakeModule):
        await mod.client.stop()
        await asyncio.wait_for(mod.task, 5)

    async def wait(self, predicate, timeout=5.0):
        async def waiter():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(waiter(), timeout)


# ── VoiceModuleSpeech против модуля-заглушки (VOICE-15, VOICE-17, VOICE-21) ──────────────────────

async def test_speak_goes_to_the_module_and_speaking_follows_its_events(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await w.speech.speak("u-00000001", "Первое предложение. Второе предложение.", "angry", 50)
        await w.wait(lambda: mod.spoken)
        assert mod.spoken == [{"utterance_id": "u-00000001", "text": "Первое предложение. Второе предложение.",
                               "emotion": "angry", "replace": False}]
        assert w.speech.is_speaking() and not w.presence.state["speaking"]          # ждём начала звука
        assert w.presence.state["emotion"] != "angry"                               # стикер меняется вместе со звуком
        shown = []
        w.speech.when_audible("u-00000001", lambda: shown.append("text"))           # синхронизатор: ждёт начала звука
        assert shown == []
        await mod.speech("started", "u-00000001", seq=0, segment="Первое предложение.")
        await w.wait(lambda: w.presence.state["speaking"])
        assert shown == ["text"] and w.presence.state["emotion"] == "angry"
        assert (w.presence.state["utterance_id"], w.presence.state["segment"]) == ("u-00000001", "Первое предложение.")
        assert w.speech.current_segment() == "Первое предложение."
        await mod.speech("segment", "u-00000001", seq=1, segment="Второе предложение.")
        await w.wait(lambda: w.presence.state["segment"] == "Второе предложение.")
        await mod.speech("ended", "u-00000001", completed=True, spoken_upto=2)
        await w.wait(lambda: not w.presence.state["speaking"])
        assert w.presence.state["utterance_id"] is None and w.presence.state["segment"] == ""
        assert not w.speech.is_speaking() and w.speech.current_segment() == ""
        await w.stop_module(mod)


async def test_stop_asks_the_module_to_finish_the_sentence_and_second_speak_is_queued(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await w.speech.speak("u-00000001", "Длинная реплика. Ещё.", None, 50)
        await mod.speech("started", "u-00000001", seq=0, segment="Длинная реплика.")
        await w.wait(lambda: w.presence.state["speaking"])
        await w.speech.stop("u-00000009")                                          # чужая реплика: модулю ничего
        await w.speech.stop("u-00000001")
        await w.wait(lambda: mod.stops)
        assert mod.stops == [{"utterance_id": "u-00000001", "at": "segment_end"}]
        await w.speech.speak("u-00000002", "Срочное.", None, 90)                   # после stop внимание ставит следующую в очередь
        await w.wait(lambda: len(mod.spoken) == 2)
        assert mod.spoken[1]["replace"] is False
        await mod.speech("ended", "u-00000001", completed=False, spoken_upto=1, reason="stop")
        await w.wait(lambda: not w.presence.state["speaking"] or w.presence.state["utterance_id"] != "u-00000001")
        assert w.speech.is_speaking()                                              # вторая реплика ещё ждёт своей очереди
        await w.speech.stop(None)
        await w.wait(lambda: len(mod.stops) == 2)
        assert mod.stops[-1] == {"utterance_id": None, "at": "segment_end"}
        await w.stop_module(mod)


async def test_speak_over_an_unstopped_utterance_asks_to_replace(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await w.speech.speak("u-00000001", "Звучит.", None, 50)
        await w.speech.speak("u-00000002", "Перебивает.", None, 50)
        await w.wait(lambda: len(mod.spoken) == 2)
        assert [m["replace"] for m in mod.spoken] == [False, True]
        await w.stop_module(mod)


async def test_event_order_ended_of_old_before_started_of_new_keeps_speaking(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await w.speech.speak("u-00000001", "Старая.", None, 50)
        await w.speech.speak("u-00000002", "Новая.", None, 50)
        await mod.speech("started", "u-00000001", seq=0, segment="Старая.")
        await mod.speech("ended", "u-00000001", completed=False, spoken_upto=0, reason="replaced")
        await w.wait(lambda: w.speech.is_speaking() and not w.presence.state["speaking"])
        await mod.speech("started", "u-00000002", seq=0, segment="Новая.")
        await w.wait(lambda: w.presence.state["utterance_id"] == "u-00000002")
        assert w.speech.is_speaking() and w.speech.current_segment() == "Новая."
        await w.stop_module(mod)


async def test_utterance_that_never_starts_is_dropped_after_the_timeout(tmp_path):
    async with Wired(tmp_path) as w:
        now = [100.0]
        w.voice._clock = lambda: now[0]
        mod = await w.module()
        await w.speech.speak("u-00000001", "Не зазвучит.", None, 50)
        assert w.speech.is_speaking()
        now[0] += speech_out.VoiceModuleSpeech.START_TIMEOUT_S + 1
        assert not w.speech.is_speaking()
        await w.stop_module(mod)


async def test_module_gone_or_disabled_resets_speech_and_falls_back_to_text(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await w.speech.speak("u-00000001", "Звучит.", None, 50)
        await mod.speech("started", "u-00000001", seq=0, segment="Звучит.")
        await w.wait(lambda: w.presence.state["speaking"])
        await w.stop_module(mod)
        await w.wait(lambda: not w.layer.hub.is_connected("voice"))
        assert not w.speech.is_speaking()                                         # модуль пропал: речь оборвалась вместе с ним
        assert w.presence.state["speaking"] is False
        await w.speech.speak("u-00000002", "Теперь только текст.", None, 50)
        assert w.presence.state["segment"] == "Теперь только текст." and w.presence.state["speaking"]


async def test_disabled_voice_module_does_not_get_speak_and_text_is_used(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        w.layer.modules._settings_source = lambda: {"modules": {"voice": {"enabled": False}}}
        w.layer.modules.invalidate()
        await w.speech.speak("u-00000001", "Модуль выключен.", None, 50)
        await asyncio.sleep(0.1)
        assert mod.spoken == [] and not w.speech.is_speaking()
        assert w.presence.state["segment"] == "Модуль выключен."
        await w.stop_module(mod)


# ── без модуля голоса: только текст (VOICE-15, PET-4) ────────────────────────────────────────────

async def test_without_a_voice_module_speech_is_text_only(tmp_path):
    async with Wired(tmp_path) as w:
        assert not w.voice.available()
        await w.speech.speak("u-00000001", "Весь текст реплики целиком. Две фразы.", "happy", 50)
        assert not w.speech.is_speaking() and w.speech.current_segment() == ""    # очереди речи ждать нечего
        st = w.presence.state
        assert st["speaking"] and st["segment"] == "Весь текст реплики целиком. Две фразы." and st["emotion"] == "happy"
        await w.speech.stop("u-00000001")
        assert not w.presence.state["speaking"] and w.presence.state["segment"] == ""


async def test_text_only_speaking_ends_by_estimate(monkeypatch):
    monkeypatch.setattr(speech_out, "SECONDS_PER_CHAR", 0.0)
    monkeypatch.setattr(speech_out, "ESTIMATE_PAD_S", 0.05)
    presence = speech_out.PresenceTracker()
    sp = speech_out.TextOnlySpeech(presence)
    await sp.speak("u-1", "коротко", None, 50)
    assert presence.state["speaking"]
    await asyncio.sleep(0.2)
    assert presence.state["speaking"] is False and sp.is_speaking() is False
    await sp.speak("u-2", "первая", None, 50)
    await sp.speak("u-3", "вторая", None, 50)                                   # новая реплика заменяет прежнюю
    await sp.stop("u-2")                                                         # чужая: ничего
    assert presence.state["utterance_id"] == "u-3"


async def test_companion_default_speech_is_text_only(tmp_path):
    c = Companion(str(tmp_path / "memory"), rel=None)
    assert isinstance(c.speech, speech_out.TextOnlySpeech)
    await c.say("Привет")
    assert c.presence.state["segment"] == "Привет" and c.is_speaking() is False


# ── события voice.* (VOICE-6, VOICE-8, VOICE-14, VOICE-17) ───────────────────────────────────────

async def test_voice_utterance_goes_to_on_voice_with_the_speaker_and_skips_attention(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        speaker = {"id": "person-ab12cd34", "name": "Аня", "new": False, "owner": True, "has_owner": True}
        await mod.client.event("voice.utterance", "включи свет", data={"speaker": speaker}, addressed_to_me=True,
                               sender={"id": "voice:person-ab12cd34", "name": "Аня", "is_owner": True})
        await w.wait(lambda: w.voiced)
        assert w.voiced == [("включи свет", speaker)]
        await w.layer.attention.settle()
        assert w.layer.attention.last_decision is None and w.layer.attention.speech.items == []      # внимание не трогали
        events_log.flush()
        assert not [e for e in events_log.tail(50, {"module_event"}) if e["data"].get("event_kind") == "voice.utterance"]
        await w.stop_module(mod)


async def test_voice_utterance_without_speaker_data_falls_back_to_the_sender(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await mod.client.event("voice.utterance", "привет", sender={"id": "voice:person-1", "is_owner": False})
        await w.wait(lambda: w.voiced)
        text, speaker = w.voiced[0]
        assert text == "привет" and speaker["id"] == "person-1" and speaker["owner"] is False
        await w.stop_module(mod)


async def test_user_speaking_updates_presence_and_last_user_ts(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await mod.client.event("voice.user_speaking", "Человек начал говорить", data={"speaking": True})
        await w.wait(lambda: w.presence.state["user_speaking"])
        assert w.presence.state["last_user_ts"] == 0.0
        before = time.time()
        await mod.client.event("voice.user_speaking", "Человек закончил говорить", data={"speaking": False})
        await w.wait(lambda: not w.presence.state["user_speaking"])
        assert w.presence.state["last_user_ts"] >= before                        # конец речи обновляет last_user_ts
        events_log.flush()
        assert not [e for e in events_log.tail(50, {"module_event"}) if e["data"].get("event_kind") == "voice.user_speaking"]
        await w.stop_module(mod)


async def test_presence_is_broadcast_to_subscribers_on_voice_events(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        pet_manifest = {"id": "pet", "title": "Питомец", "description": "Тест.", "version": "1.0.0",
                        "capabilities": ["presence.view"], "subscribe": ["presence"]}
        pet = ModuleClient(pet_manifest, url=w.url, token=w.layer.hub.token)
        seen: list[dict] = []

        @pet.on_topic("presence")
        async def on_presence(data):
            seen.append(data)
        task = asyncio.create_task(pet.run())
        try:
            await pet.wait_connected(5)
            await w.speech.speak("u-00000001", "Фраза.", None, 50)
            await mod.speech("started", "u-00000001", seq=0, segment="Фраза.")
            await w.wait(lambda: any(p["speaking"] and p["segment"] == "Фраза." for p in seen))
            assert {"emotion_ts", "last_user_ts", "last_say", "speaking"} <= set(seen[-1])       # поля для rimworld и питомца
        finally:
            await pet.stop()
            await asyncio.wait_for(task, 5)
        await w.stop_module(mod)


async def test_heard_event_is_written_as_activity_and_still_goes_through_attention(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await mod.client.event("voice.heard", "В колонках: играет джаз", data={"summary": "играет джаз"}, urgency_hint=1,
                               addressed_to_me=False, memorable=False)
        await w.wait(lambda: w.companion.recent_heard())
        assert "играет джаз" in w.companion.recent_heard()
        await w.wait(lambda: w.companion._mem.recent_activity(1))
        last = w.companion._mem.recent_activity(1)[-1]
        assert (last["app"], last["category"], last["summary"]) == ("звук", "audio", "играет джаз")
        await w.layer.attention.settle()
        assert w.layer.attention.last_decision is not None                       # дальше событие идёт обычным путём внимания
        events_log.flush()
        assert [e for e in events_log.tail(50, {"module_event"}) if e["data"].get("event_kind") == "voice.heard"]
        await w.stop_module(mod)


async def test_named_voice_creates_the_person_or_the_owner_fact(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await mod.client.event("voice.named", "назначено имя", data={"voice_id": "person-1", "name": "Вася", "is_owner": False})
        await w.wait(lambda: w.companion._rel.get("voice:person-1"))
        assert w.companion._rel.get("voice:person-1")["source"] == "voice"
        await mod.client.event("voice.named", "назначено имя", data={"voice_id": "person-2", "name": "Аня", "is_owner": True})
        await w.wait(lambda: any("Имя: Аня" in f["text"] for f in w.companion._mem.facts()))
        await w.layer.attention.settle()
        events_log.flush()
        assert [e for e in events_log.tail(50, {"module_event"}) if e["data"].get("event_kind") == "voice.named"]   # не тихий вид
        await w.stop_module(mod)


async def test_bad_voice_event_data_does_not_break_the_core(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await mod.speech("started", "", seq=0, segment="x")
        await mod.speech("странная-фаза", "u-1")
        await mod.client.event("voice.named", "без данных", data={})
        await mod.client.event("voice.user_speaking", "пусто")
        await asyncio.sleep(0.1)
        assert not w.presence.state["speaking"]
        await w.stop_module(mod)


# ── эмоции в presence (PET-9) ──────────────────────────────────────────────────────────────────

async def test_every_published_emotion_reaches_presence_with_its_time(tmp_path, monkeypatch):
    async with Wired(tmp_path) as w:
        monkeypatch.setattr(fast_brain, "_EMOTION_FILE", None)
        before = time.time()
        fast_brain.publish("chat", {"emotion": "thinking", "action": "none"})
        st = w.presence.state
        assert st["emotion"] == "thinking" and st["emotion_ts"] >= before
        first = st["emotion_ts"]
        await asyncio.sleep(0.01)
        fast_brain.publish("say", {"emotion": "thinking", "action": "none"})      # та же эмоция, новое время: питомцу нужно знать
        assert w.presence.state["emotion_ts"] > first
    fast_brain.publish("x", {"emotion": "angry", "action": "none"})              # слой остановлен: подписка снята
    assert w.presence.state["emotion"] == "thinking"


def test_presence_has_the_emotion_time_field():
    assert speech_out.PresenceTracker.DEFAULTS["emotion_ts"] == 0.0
    p = speech_out.PresenceTracker()
    assert p.update(emotion="sad", emotion_ts=5.0) and p.state["emotion_ts"] == 5.0


async def test_real_voice_process_started_by_the_supervisor_speaks_end_to_end(tmp_path, monkeypatch):
    """Настоящий процесс modules/voice (GITS_VOICE_FAKE=1: звука нет), запущенный супервизором из настоящего modules.yaml:
    речь ядра -> speak -> события voice.speech -> presence -> конец; выключение модуля возвращает речь в текст."""
    import psutil

    import settings
    from supervisor import load_specs
    monkeypatch.setenv("GITS_VOICE_FAKE", "1")
    monkeypatch.setenv("GITS_VOICE_FAKE_SEGMENT_MS", "150")
    monkeypatch.setenv("GITS_VOICE_TMP", str(tmp_path / "vtmp"))
    specs = [s for s in load_specs() if s.id == "voice"]                       # только голос: остальные модули не запускаем
    assert len(specs) == 1 and specs[0].autostart
    mem = str(tmp_path / "memory")
    presence = speech_out.PresenceTracker()
    voice = speech_out.VoiceModuleSpeech(presence)
    selector = speech_out.SpeechSelector(voice, speech_out.TextOnlySpeech(presence))
    companion = Companion(mem, Relationships(mem), speech=selector, presence=presence)
    port = free_port()
    layer = core_service.wire_modules(companion, Registry(), port=port, specs=specs,
                                      battery=lambda: {"level": 1.0, "sleeping": False})
    app = web.Application()
    layer.hub.attach(app)
    runner = web.AppRunner(app, shutdown_timeout=0.5)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    await layer.start()
    pid = None

    async def wait(predicate, timeout=30.0):
        async def waiter():
            while not predicate():
                await asyncio.sleep(0.02)
        await asyncio.wait_for(waiter(), timeout)
    try:
        await wait(lambda: layer.hub.is_connected("voice"))
        pid = layer.modules.get("voice").pid
        assert voice.available() and not selector.is_speaking()
        await selector.speak("u-0000aaaa", "Первое предложение. Второе предложение.", "happy", 50)
        assert selector.is_speaking()
        await wait(lambda: presence.state["speaking"] and presence.state["segment"] == "Первое предложение. Второе предложение.")
        await wait(lambda: not presence.state["speaking"] and not selector.is_speaking())
        # «замолчи»: остановка на границе предложения
        await selector.speak("u-0000bbbb", "Раз. Два. Три. Четыре.", None, 50)
        await wait(lambda: presence.state["speaking"])
        await selector.stop("u-0000bbbb")
        await wait(lambda: not selector.is_speaking())
        # core.tts идёт модулю голоса
        value = await layer.hub.connection("voice").request("tts.synthesize", {"text": "Проверка", "format": "wav"}, timeout=20)
        assert value["duration_ms"] > 0
        # выключение модуля: процесс завершается, речь только текстом
        settings.update(modules={"voice": {"enabled": False}})
        layer.modules.invalidate()
        await layer.supervisor.apply("voice")
        await wait(lambda: not psutil.pid_exists(pid), 15)
        await wait(lambda: not layer.hub.is_connected("voice"))
        await selector.speak("u-0000cccc", "Теперь только текст.", None, 50)
        assert not selector.is_speaking() and presence.state["segment"] == "Теперь только текст."
    finally:
        await layer.stop()
        await runner.cleanup()
        if pid and psutil.pid_exists(pid):
            psutil.Process(pid).kill()


def test_core_process_has_no_audio_code_or_dependencies():
    """Приёмка ТЗ 12 п. 1: в процессе ядра нет импортов звука, а файлы звука переехали в modules/voice."""
    import ast
    from pathlib import Path
    agent = Path(__file__).resolve().parent.parent
    forbidden = {"tts_engine", "mic_listener", "hearing", "voice_id", "stt_client", "opus_encoder", "sounddevice",
                 "soundcard", "faster_whisper", "edge_tts", "ormsgpack", "webrtcvad", "desktop_pet", "quick_input"}
    offenders = []
    for path in agent.rglob("*.py"):
        rel = path.relative_to(agent).parts
        if rel[0] in ("memory", "models", "resources", "logs", ".venv", "tests"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                [node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.module else []
            offenders += [f"{path.name}: {n}" for n in names if n in forbidden]
    assert offenders == []
    for name in ("tts_engine.py", "mic_listener.py", "hearing.py", "voice_id.py", "stt_client.py", "opus_encoder.py"):
        assert not (agent / name).exists(), name
    req = (agent / "requirements.txt").read_text(encoding="utf-8").lower()
    for package in ("edge-tts", "soundcard", "faster-whisper", "ormsgpack"):
        assert package not in req, package                                         # ставятся из modules/voice/requirements.txt


@pytest.mark.parametrize("kind", ["voice.speech", "voice.user_speaking", "voice.utterance"])
async def test_frequent_voice_kinds_are_silent(tmp_path, kind):
    async with Wired(tmp_path) as w:
        assert kind in w.layer.attention.silent_kinds and kind in w.layer.attention.kind_handlers


async def test_when_audible_fires_on_end_and_on_reset_so_text_is_never_lost(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await w.speech.speak("u-00000002", "Тишина.", None, 50)
        await w.wait(lambda: mod.spoken)
        fired = []
        w.speech.when_audible("u-00000002", lambda: fired.append("ended"))
        await mod.speech("ended", "u-00000002", completed=False, spoken_upto=0, reason="error")      # звука не было
        await w.wait(lambda: fired)
        assert fired == ["ended"]
        w.speech.when_audible("u-неизвестная", lambda: fired.append("unknown"))                      # не звучит: сразу
        assert fired == ["ended", "unknown"]
        await w.stop_module(mod)
