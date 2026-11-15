"""Воспроизведение реплики из нескольких предложений: настоящий движок голоса (деление по предложениям, разрыв при опоздании синтеза),
настоящий VoiceAdapterClient и плеер, повторяющий discord.player.AudioPlayer._do_run / PCMAudio.read (сверено по исходникам discord.py
2.7.1 в образе ядра): чтение кадра каждые 20 мс в отдельном потоке, пустой ответ read() завершает плеер и зовёт after, `VoiceRecvClient.stop()`
останавливает И воспроизведение, И приём."""
import asyncio
import threading
import time

import discord_voice as dv
import pytest
from dc_rig import Rig, VoiceChannel

FRAME = dv.FRAME48_BYTES


class RealtimePlayer(threading.Thread):
    """Копия цикла AudioPlayer._do_run: read() раз в 20 мс по часам, пустой ответ или stop() завершают, потом after(err)."""

    def __init__(self, source, after):
        super().__init__(daemon=True)
        self.source, self.after = source, after
        self.end = threading.Event()
        self.real, self.silent, self.log = 0, 0, []

    def run(self):
        start, loops = time.perf_counter(), 0
        try:
            while not self.end.is_set():
                data = self.source.read()          # PCMAudio.read: len != FRAME_SIZE -> b''
                if len(data) != FRAME:
                    data = b""
                if not data:
                    self.end.set()
                    break
                self.log.append(data)
                loops += 1
                time.sleep(max(0, 0.02 + (start + 0.02 * loops - time.perf_counter())))
        finally:
            if self.after is not None:
                self.after(None)

    def stop(self):
        self.end.set()


class RealtimeVC:
    """Голосовой клиент как VoiceRecvClient: play() запускает поток-плеер, stop() останавливает и воспроизведение, и приём."""

    def __init__(self, channel):
        self.channel, self.connected, self.sink = channel, True, None
        self._player = None
        self.listening = False
        self.players = []
        self._connection = type("C", (), {"dave_session": None, "dave_protocol_version": 0})()

    def is_connected(self):
        return self.connected

    def listen(self, sink):
        self.sink, self.listening = sink, True

    def stop_listening(self):
        self.listening = False

    def play(self, source, after=None):
        if self.is_playing():
            raise RuntimeError("Already playing audio.")
        self._player = RealtimePlayer(source, after)
        self.players.append(self._player)
        self._player.start()

    def is_playing(self):
        return self._player is not None and not self._player.end.is_set()

    def stop_playing(self):
        if self._player:
            self._player.stop()
            self._player = None

    def stop(self):
        self.stop_playing()
        self.stop_listening()

    async def disconnect(self, force=False):
        self.connected = False

    def _get_id_from_ssrc(self, ssrc):
        return None


class SlowTts:
    """Синтез: тон `ms_per_char` мс на символ; предложения с меткой «поздно» готовятся `late_s` секунд."""

    def __init__(self, late_s=0.0, ms_per_char=50):
        self.late_s, self.ms = late_s, ms_per_char

    async def synthesize(self, text, *, voice=None, emotion=None):
        if "поздно" in text:
            await asyncio.sleep(self.late_s)
        return b"\x10\x00" * (len(text) * self.ms * 32 // 2)

    async def aclose(self):
        pass


async def run_reply(tmp_path, monkeypatch, text, late_s):
    """Реплика через настоящий движок и адаптер; возвращает (плеер, сыграно по ack, событие ended, строки журнала адаптера)."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from gits_sdk.voice import protocol as V
    from services.voice_engine.config import EngineConfig
    from services.voice_engine.engine import VoiceEngine
    from services.voice_engine.server import VoiceServer
    from services.voice_engine.stt import FakeStt

    speech = []
    engine = VoiceEngine(stt=FakeStt(default="х"), tts=SlowTts(late_s), config=EngineConfig(), emit_event=lambda e: asyncio.sleep(0),
                         journal=lambda *a: None, on_speech=speech.append, names=lambda: ("Глэдос",))
    app = web.Application()
    VoiceServer(engine, lambda: "tok", hello_timeout_s=1.0).attach(app)
    server = TestServer(app)
    await server.start_server()
    try:
        async with Rig(tmp_path, monkeypatch) as rig:
            rig.voice.client_factory = None
            rig.voice.engine_url, rig.voice.engine_token = f"ws://127.0.0.1:{server.port}{V.WS_PATH}", "tok"
            rig.voice.source_factory = lambda pipe: type("Src", (), {"read": lambda self: pipe.read()})()
            g, _ = rig.server(1, "Клуб", (("общий", 100),))
            g.me = rig.me
            voice = rig.dc.register(g.add(VoiceChannel(500, "Войс")))
            rig.me.guild = g
            voice.members = [rig.dc.user(5, "masha")]

            async def connect(cls=None, timeout=30, **kw):
                vc = RealtimeVC(voice)
                voice.vcs.append(vc)
                return vc
            voice.connect = connect
            await rig.voice.join(voice)
            vc = voice.vcs[0]
            sid = "discord:1:500"
            for _ in range(200):
                if engine.session(sid) is not None:
                    break
                await asyncio.sleep(0.02)
            engine.speak(sid, text)
            for _ in range(600):
                if any(s["phase"] == "ended" for s in speech):
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
            ended = [s for s in speech if s["phase"] == "ended"]
            return vc, ended, dict(rig.voice.sessions[1].counters), rig.voice.sessions[1].listening and vc.listening
    finally:
        await engine.aclose()
        await server.close()


S1 = "Первое предложение вполне длинное."            # 34 символа = 1.7 с при 50 мс на символ
S2 = "Второе предложение пришло поздно тоже."


@pytest.mark.parametrize("late_s", [0.0, 0.4, 1.5])
async def test_two_sentence_reply_is_played_to_the_end_even_when_second_is_late(tmp_path, monkeypatch, late_s):
    # синтез второго предложения стартует сразу вместе с первым: late_s отсчитывается от начала, а первое звучит 1.7 с,
    # поэтому опоздание на границе = late_s - 1.7 + запас; берём late_s так, чтобы оно было заметно больше длины первого
    delay = 1.7 + late_s if late_s else 0.0
    vc, ended, counters, still_listening = await run_reply(tmp_path, monkeypatch, f"{S1} {S2}", delay)
    [ev] = ended
    player = vc.players[0]
    real_ms = len([f for f in player.log if f.strip(b"\x00")]) * 20
    total_ms = (len(S1) + len(S2)) * 50
    assert ev["completed"] is True and ev.get("reason") is None
    assert abs(ev["spoken_ms"] - total_ms) <= 100, (ev["spoken_ms"], total_ms, counters)      # (в) ack = сумма настоящих кадров
    assert abs(real_ms - total_ms) <= 100, (real_ms, total_ms, counters)                       # (а) проиграны ВСЕ кадры обоих предложений
    assert still_listening, "после реплики приём звука не должен останавливаться"


REAL1 = "Это первое предложение примерно такой длины, как в журнале."   # 58 символов
REAL2 = "А это второе, и синтез для него пришёл с опозданием на четыреста миллисекунд, как было вживую."


async def test_log_like_reply_two_sentences_2550_plus_4160_ms(tmp_path, monkeypatch):
    s1, s2 = REAL1[:51].rstrip(",. ") + ".", REAL2[:83].rstrip(",. ") + " поздно."
    total = (len(s1) + len(s2)) * 50
    vc, ended, counters, _ = await run_reply(tmp_path, monkeypatch, f"{s1} {s2}", len(s1) * 0.05 + 0.425)
    [ev] = ended
    real_ms = len([f for f in vc.players[0].log if f.strip(b"\x00")]) * 20
    print("EV", ev, "total", total, "real", real_ms, "all", len(vc.players[0].log) * 20, counters)
    assert abs(real_ms - total) <= 100 and abs(ev["spoken_ms"] - total) <= 100


# ── сценарии на заглушке движка и «живом» плеере ────────────────────────────────────────────────

def chunks16(ms, value=3000):
    import struct
    return [struct.pack("<320h", *([value] * 320)) for _ in range(ms // 20)]


async def rt_session(rig):
    g, _ = rig.server(1, "Клуб", (("общий", 100),))
    g.me = rig.me
    rig.me.guild = g
    voice = rig.dc.register(g.add(VoiceChannel(500, "Войс")))
    voice.members = [rig.dc.user(5, "masha")]
    rig.voice.source_factory = lambda pipe: type("Src", (), {"read": lambda self: pipe.read()})()

    async def connect(cls=None, timeout=30, **kw):
        vc = RealtimeVC(voice)
        voice.vcs.append(vc)
        return vc
    voice.connect = connect
    await rig.voice.join(voice)
    return voice.vcs[0], rig.engines[0], rig.voice.sessions[1]


def real_frames(player):
    return len([f for f in player.log if f.strip(bytes([0]))])


async def paced(stream, chunks, step=0.02):
    for c in chunks:
        stream._put(c)
        await asyncio.sleep(step)


async def test_player_dying_early_is_restarted_and_the_whole_reply_is_played(tmp_path, monkeypatch):
    from gits_sdk.voice import PlayStream
    async with Rig(tmp_path, monkeypatch) as rig:
        vc, eng, sess = await rt_session(rig)
        stream = PlayStream("u1", 1, "текст", 2000)
        task = asyncio.create_task(eng.kw["on_play"](stream, "u1"))
        feeder = asyncio.create_task(paced(stream, chunks16(1000)))
        await asyncio.sleep(0.4)
        vc.players[0].end.set()                       # плеер умер посреди реплики (обрыв соединения, чужой stop)
        await feeder
        stream._close()
        spoken = await asyncio.wait_for(task, 5)
        assert sess.counters["player_restarts"] >= 1 and len(vc.players) >= 2
        assert abs(sum(real_frames(p) for p in vc.players) * 20 - 1000) <= 100 and abs(spoken - 1000) <= 100
        assert stream.stop_reason is None and vc.listening


async def test_stop_of_playback_does_not_stop_listening_and_reason_is_reported(tmp_path, monkeypatch):
    from gits_sdk.voice import PlayStream
    async with Rig(tmp_path, monkeypatch) as rig:
        vc, eng, sess = await rt_session(rig)
        stream = PlayStream("u1", 1, "текст", 3000)
        for c in chunks16(1000):
            stream._put(c)
        task = asyncio.create_task(eng.kw["on_play"](stream, "u1"))
        await asyncio.sleep(0.2)
        stream.stop_reason = "barge_in"               # как делает клиент SDK на play.stop
        eng.kw["on_stop"]("u1", "barge_in")
        stream.drain()
        spoken = await asyncio.wait_for(task, 3)
        assert vc.listening and sess.listening, "VoiceRecvClient.stop() останавливает и приём: нужен stop_playing()"
        assert spoken <= 400 and stream.stop_reason == "barge_in" and sess.counters["aborted_plays"] == 1


async def test_new_play_replacing_the_old_one_acks_the_old_with_a_reason_not_completed(tmp_path, monkeypatch):
    from gits_sdk.voice import PlayStream
    async with Rig(tmp_path, monkeypatch) as rig:
        vc, eng, sess = await rt_session(rig)
        old = PlayStream("old", 1, "старое", 3000)
        for c in chunks16(600):
            old._put(c)
        t_old = asyncio.create_task(eng.kw["on_play"](old, "old"))
        await asyncio.sleep(0.2)
        new = PlayStream("new", 2, "новое", 400)
        for c in chunks16(200):
            new._put(c)
        new._close()
        t_new = asyncio.create_task(eng.kw["on_play"](new, "new"))
        old._put(chunks16(20)[0])
        await asyncio.wait_for(t_new, 3)
        old._close()
        await asyncio.wait_for(t_old, 3)
        assert old.stop_reason == "replaced" and new.stop_reason is None


async def test_end_of_play_waits_for_the_real_remainder_not_buffer_at_the_moment_of_end(tmp_path, monkeypatch):
    """Движок отдал 2.6 с звука разом (быстрее реального времени) и сказал play.end: реплика должна доиграться вся."""
    from gits_sdk.voice import PlayStream
    monkeypatch.setattr(dv, "PLAY_TAIL_S", 0.5)
    async with Rig(tmp_path, monkeypatch) as rig:
        vc, eng, sess = await rt_session(rig)
        stream = PlayStream("u1", 1, "текст", 2600)
        for c in chunks16(2600):
            stream._put(c)
        stream._close()
        spoken = await asyncio.wait_for(eng.kw["on_play"](stream, "u1"), 8)
        assert abs(spoken - 2600) <= 100 and abs(real_frames(vc.players[0]) * 20 - 2600) <= 100
        assert sess.counters.get("play_timeouts", 0) == 0


async def test_pipe_returns_silence_not_end_on_underrun_and_one_info_line_per_reply(tmp_path, monkeypatch, caplog):
    import logging
    from gits_sdk.voice import PlayStream
    caplog.set_level(logging.INFO, logger="discord.voice")
    async with Rig(tmp_path, monkeypatch) as rig:
        vc, eng, sess = await rt_session(rig)
        stream = PlayStream("u7", 1, "текст", 1500)
        task = asyncio.create_task(eng.kw["on_play"](stream, "u7"))
        await paced(stream, chunks16(400))
        await asyncio.sleep(0.6)                      # разрыв: синтез предложения опоздал, кадров нет
        assert vc.players[0].is_alive(), "в разрыве плеер не должен заканчиваться"
        await paced(stream, chunks16(400))
        stream._close()
        spoken = await asyncio.wait_for(task, 5)
        lines = [r.getMessage() for r in caplog.records if "реплика u7" in r.getMessage()]
        assert len(lines) == 1 and "сыграно" in lines[0] and "недогрузов" in lines[0] and "итог completed" in lines[0]
        assert abs(spoken - 800) <= 100 and sess.counters["underruns"] >= 20
        from test_dc_voice_panel import panel, voice_text
        text = voice_text(await panel(rig))
        assert "недогрузов" in text and "оборвано" in text and "таймаутов" in text
