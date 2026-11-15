"""Голос Discord: вход и выход, участники, приём звука по участникам, воспроизведение, режимы, объявление, одиночество, автовход,
инструменты и разрешение, отсутствие зависимостей. discord.py, расширение приёма и davey подменены (dc_rig), движок голоса либо
заглушка (StubEngine), либо настоящий сервер движка из ядра (сквозной тест). Панель: test_dc_voice_panel.py."""
import asyncio
import struct
from types import SimpleNamespace as NS

import discord_voice as dv
import pytest
from dc_rig import OWNER_ID, FakeVC, Rig, StubEngine, VoiceChannel

from gits_sdk import ToolError
from voice_rx import FrameDecoder, UnknownUser, build_sink, check_dependencies


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(dv, "TICK_S", 0.05)
    monkeypatch.setattr(dv, "ENGINE_WAIT_S", 0.2)


OWNER = {"owner": str(OWNER_ID)}


def vworld(rig):
    g, _ = rig.server(1, "Клуб", (("общий", 100),))
    g.me = rig.me
    voice = rig.dc.register(g.add(VoiceChannel(500, "Войс")))
    rig.me.guild = g
    return g, voice


def person(rig, uid, name, **kw):
    u = rig.dc.user(uid, name, **kw)
    u.guild = rig.dc.guilds[0]
    return u


def sess_of(rig):
    return rig.voice.sessions[1]


async def until(cond, timeout=3.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout)


def level(v):
    return b"v%d" % v


def pcm16(ms, value=3000):
    return [struct.pack("<320h", *([value] * 320)) for _ in range(ms // 20)]


async def test_join_announces_registers_speakers_and_leave_cleans_up(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
        g, voice = vworld(rig)
        owner, masha, bot2 = person(rig, OWNER_ID, "boss"), person(rig, 5, "masha"), person(rig, 6, "otherbot", bot=True)
        voice.members = [owner, masha, bot2, rig.me]
        out = await rig.core.call_tool("voice_join", {"channel": "Клуб / Войс"})
        assert "зашла в голосовой канал «Клуб / Войс»" in out and "слушает и может отвечать" in out
        [eng] = rig.engines
        assert eng.kw["session"] == "discord:1:500" and eng.kw["adapter"] == "discord"
        assert (eng.kw["echo_free"], eng.kw["can_barge_in"], eng.kw["speakers_labelled"], eng.kw["direct"]) == (True, True, True, False)
        assert eng.started and set(eng.joined) == {f"discord:{OWNER_ID}", "discord:5"}      # бот и она сама не слушаются
        assert eng.joined[f"discord:{OWNER_ID}"]["is_owner"] is True and eng.joined["discord:5"]["is_owner"] is False
        [note] = voice.sent
        assert "Я слушаю этот канал" in note["content"] and note["allowed_mentions"] == "no-mentions"
        assert sess_of(rig).announce.startswith("отправлено")
        assert rig.voice.known.has("500") and rig.voice.known.has("клуб / войс")
        voice.members.append(person(rig, 8, "petya"))                                     # вошёл позже
        sess_of(rig).wake.set()
        await until(lambda: "discord:8" in eng.joined)
        voice.members.remove(masha)
        sess_of(rig).wake.set()
        await until(lambda: "discord:5" in eng.left)
        assert await rig.core.call_tool("voice_leave", {}) == "вышла из голосового канала «Клуб / Войс»"
        vc = voice.vcs[0]
        assert vc.disconnected and vc.stopped_listening and eng.stopped and rig.voice.sessions == {}
        assert await rig.core.call_tool("voice_leave", {}) == "она не в голосовом канале"


async def test_join_defaults_to_channel_where_owner_sits_and_errors_when_nowhere(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
        g, voice = vworld(rig)
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("voice_join", {})
        assert e.value.code == "bad_request" and "владельца нет ни в одном голосовом канале" in str(e.value)
        voice.members = [person(rig, OWNER_ID, "boss")]
        assert "«Клуб / Войс»" in await rig.core.call_tool("voice_join", {})
        for name in ("#войс", "войс", "500", "channel:500"):
            assert rig.voice.find_channel(name) is voice
        with pytest.raises(ValueError):
            rig.voice.find_channel("нет такого")
        with pytest.raises(ValueError):
            rig.voice.find_channel("100")                                                  # текстовый канал


async def test_ambiguous_voice_name_asks_for_precision(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        g2 = rig.dc.guild(2, "Другой")
        g2.me = rig.me
        rig.dc.register(g2.add(VoiceChannel(501, "Войс")))
        with pytest.raises(ValueError, match="нескольким"):
            rig.voice.find_channel("Войс")
        assert rig.voice.find_channel("Другой / Войс").id == 501


async def test_rx_each_speaker_separately_dave_decrypted_and_16k(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        a, b = person(rig, 5, "masha"), person(rig, 6, "petya")
        voice.members = [a, b]
        await rig.voice.join(voice)
        vc, eng = voice.vcs[0], rig.engines[0]
        for _ in range(10):                                                              # 200 мс каждого, разной громкости
            vc.feed(a, level(1000))
            vc.feed(b, level(-2000))
        await until(lambda: len(eng.audio.get("discord:5", b"")) >= 10 * 640 and len(eng.audio.get("discord:6", b"")) >= 10 * 640)
        sa = struct.unpack(f"<{len(eng.audio['discord:5']) // 2}h", bytes(eng.audio["discord:5"]))
        sb = struct.unpack(f"<{len(eng.audio['discord:6']) // 2}h", bytes(eng.audio["discord:6"]))
        assert abs(sum(sa[400:2000]) / 1600 - 1000) < 30 and abs(sum(sb[400:2000]) / 1600 + 2000) < 60     # потоки не смешаны
        assert {c[0] for c in vc.dave.calls} == {5, 6} and sess_of(rig).counters["decoded"] == 20


async def test_rx_user_by_ssrc_unknown_user_bots_and_errors_are_counted_not_fatal(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        a, bot2 = person(rig, 5, "masha"), person(rig, 6, "bot", bot=True)
        voice.members = [a, bot2]
        await rig.voice.join(voice)
        vc, eng, s = voice.vcs[0], rig.engines[0], sess_of(rig)
        vc.ssrc[111] = 5
        vc.feed(None, level(500), ssrc=111)                                               # SSRC -> участник
        vc.feed(None, level(500), ssrc=222)                                               # неизвестный SSRC
        vc.feed(bot2, level(500))                                                         # бот не слушается
        vc.feed(a, b"bad")                                                                # ошибка декодера
        vc.feed(a, b"")                                                                   # пустой кадр игнорируется
        vc.dave.ready = False                                                             # смена эпохи DAVE: сессия не готова
        vc.feed(a, level(500))
        vc.dave.ready = True
        vc.feed(a, level(500))                                                            # и приём продолжается
        await until(lambda: len(eng.audio.get("discord:5", b"")) >= 2 * 640)
        assert s.decode_errors == {"UnknownUser": 1, "ValueError": 1, "DaveSessionNotReady": 1}
        assert "discord:6" not in eng.audio and s.counters["decoded"] == 2


async def test_silence_is_filled_in_pauses_so_the_engine_can_close_a_phrase(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        a = person(rig, 5, "masha")
        voice.members = [a]
        await rig.voice.join(voice)
        vc, eng = voice.vcs[0], rig.engines[0]
        for _ in range(5):
            vc.feed(a, level(8000))
        await asyncio.sleep(0.6)                                                          # Discord замолчал: пакетов нет
        data = bytes(eng.audio["discord:5"])
        assert len(data) > 5 * 640 + 15 * 640                                             # добавлено >= 300 мс тишины
        assert set(data[-640 * 10:]) == {0}


async def test_decoder_is_per_user_and_resets_after_repeated_failures():
    made = []

    class Dec:
        def __init__(self):
            made.append(self)

        def decode(self, frame, fec=False):
            raise ValueError("x")

    fd = FrameDecoder(None, decoder_factory=Dec)
    for _ in range(FrameDecoder.RESET_AFTER):
        with pytest.raises(ValueError):
            fd.decode(1, b"x")
    with pytest.raises(ValueError):
        fd.decode(1, b"x")
    with pytest.raises(ValueError):
        fd.decode(2, b"x")
    assert len(made) == 3                                                                 # пересоздан у 1-го, свой у 2-го
    with pytest.raises(UnknownUser):
        fd.decode("ssrc:5", b"x")


def test_sink_resolves_ssrc_and_swallows_handler_errors():
    got = []

    def on_frame(uid, opus):
        got.append((uid, opus))
        raise RuntimeError("boom")

    vc = FakeVC(None)
    vc.ssrc[7] = 99
    sink = build_sink(on_frame, lambda: vc, object)
    sink.write(None, NS(opus=b"x", packet=NS(ssrc=7)))
    sink.write(NS(id=5), NS(opus=b"y", packet=NS(ssrc=1)))
    sink.write(None, NS(opus=b"z", packet=NS(ssrc=8)))
    sink.write(None, NS(opus=b"", packet=NS(ssrc=8)))
    assert sink.wants_opus() is True and got == [(99, b"x"), (5, b"y"), ("ssrc:8", b"z")]


async def test_play_frames_to_discord_and_report_played_ms(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        voice.members = [person(rig, 5, "masha")]
        await rig.voice.join(voice)
        vc, eng = voice.vcs[0], rig.engines[0]
        task = asyncio.create_task(eng.play(pcm16(200)))
        await until(lambda: vc.source is not None)
        await asyncio.sleep(0.05)
        frames = vc.read_frames()
        vc.finish()
        spoken, reason = await asyncio.wait_for(task, 3)
        assert all(len(f) == dv.FRAME48_BYTES for f in frames) and 9 <= len(frames) <= 12      # 200 мс = 10 кадров по 20 мс
        assert abs(spoken - 200) <= 40 and reason is None
        mid = struct.unpack("<%dh" % (dv.FRAME48_BYTES // 2), frames[5])
        assert abs(mid[100] - 3000) < 200 and mid[100] == mid[101]                              # моно развёрнуто в стерео
        assert sess_of(rig).started_playing is False and sess_of(rig).pipe is None


async def test_stop_aborts_playback_at_once_and_engine_leave_exits(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        voice.members = [person(rig, 5, "masha")]
        await rig.voice.join(voice)
        vc, eng = voice.vcs[0], rig.engines[0]
        from gits_sdk.voice import PlayStream
        stream = PlayStream("u9", 2, "длинно", 5000)
        for c in pcm16(400):
            stream._put(c)                                                                   # поток не закрыт: реплика «идёт»
        task = asyncio.create_task(eng.kw["on_play"](stream, "u9"))
        await until(lambda: vc.source is not None)
        eng.kw["on_stop"]("u9", "barge_in")
        stream.drain()
        spoken = await asyncio.wait_for(task, 3)
        assert vc.stops >= 1 and vc.source is None and spoken <= 100
        assert sess_of(rig).counters["stops"] == 1
        await eng.kw["on_leave"]()
        await until(lambda: rig.voice.sessions == {})
        assert vc.disconnected and "по просьбе движка" in rig.voice.last_note


def test_pipe_underrun_gives_silence_and_ends_when_finished():
    pipe = dv.PcmPipe()
    assert pipe.read() == bytes(dv.FRAME48_BYTES) and pipe.underruns == 1 and pipe.frames == 0
    pipe.feed(b"\x01\x00" * 1920 + b"\x02\x00" * 100)
    assert pipe.read()[:2] == b"\x01\x00" and pipe.frames == 1
    pipe.finish()
    tail = pipe.read()
    assert len(tail) == dv.FRAME48_BYTES and tail[:2] == b"\x02\x00" and tail[-2:] == b"\x00\x00"
    assert pipe.read() == b"" and pipe.played_ms == 40
    pipe.abort()
    pipe.feed(b"zz")
    assert pipe.buffered_ms == 0 and pipe.read() == b""


async def test_read_mode_listens_but_play_is_refused_with_error_ack(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"default_chat_mode": "read"}) as rig:
        g, voice = vworld(rig)
        a = person(rig, 5, "masha")
        voice.members = [a]
        out = await rig.voice.join(voice)
        assert "режим «чтение»" in out
        vc, eng = voice.vcs[0], rig.engines[0]
        vc.feed(a, level(1000))
        await until(lambda: eng.audio.get("discord:5"))                                    # слушает
        spoken, reason = await eng.play(pcm16(100))
        assert (spoken, reason) == (0, "error") and vc.source is None and sess_of(rig).counters["refused_plays"] == 1
        rig.bot.modes.set("channel", 500, "reply")                                          # владелец включил ответы: действует сразу
        sess_of(rig).wake.set()
        await until(lambda: sess_of(rig).can_speak())
        task = asyncio.create_task(eng.play(pcm16(60)))
        await until(lambda: vc.source is not None)
        vc.read_frames()
        vc.finish()
        assert (await asyncio.wait_for(task, 3))[1] is None


async def test_ignore_mode_refuses_agent_but_owner_may_enter_deaf_and_mute(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
        g, voice = vworld(rig)
        a = person(rig, 5, "masha")
        voice.members = [a, person(rig, OWNER_ID, "boss")]
        rig.bot.modes.set("guild", 1, "ignore")
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("voice_join", {"channel": "500"})
        assert e.value.code == "denied" and "игнорируется" in str(e.value) and rig.engines == []
        out = await rig.core.panel_action("voice_connect", {"id": "500"})
        assert out["ok"] is True
        s, eng, vc = sess_of(rig), rig.engines[0], voice.vcs[0]
        assert s.listening is False and eng.joined == {} and voice.sent == []                # не слушает, не объявляет
        vc.feed(a, level(1000))
        await asyncio.sleep(0.1)
        assert eng.audio == {} and (await eng.play(pcm16(60)))[1] == "error"
        rig.bot.modes.set("channel", 500, "reply")                                            # режим сменили: заработала
        s.wake.set()
        await until(lambda: s.listening and eng.joined)


async def test_mode_changed_to_ignore_makes_agent_session_leave(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        voice.members = [person(rig, 5, "masha")]
        await rig.voice.join(voice)
        rig.bot.modes.set("channel", 500, "ignore")
        sess_of(rig).wake.set()
        await until(lambda: rig.voice.sessions == {})
        assert "игнор" in rig.voice.last_error


async def test_announce_fallbacks_text_setting_and_skipped_note(tmp_path, monkeypatch):
    ms = {"voice_announce_text": "  Слушаю   вас. "}
    async with Rig(tmp_path, monkeypatch, module_settings=ms) as rig:
        g, voice = vworld(rig)
        voice.members = [person(rig, 5, "masha")]
        voice.perms.send_messages = False                                                    # в чате голосового писать нельзя
        text = g.channels[0]
        g.system_channel = text
        await rig.voice.join(voice)
        assert voice.sent == [] and text.sent[0]["content"] == "Слушаю вас." and sess_of(rig).announce == "отправлено в «#общий»"
        await rig.voice.leave()
        text.perms.send_messages = False
        await rig.voice.join(voice)
        assert sess_of(rig).announce.startswith("пропущено: некуда писать")
    async with Rig(tmp_path / "b", monkeypatch, module_settings={"voice_announce": False}) as rig:
        g, voice = vworld(rig)
        await rig.voice.join(voice)
        assert voice.sent == [] and sess_of(rig).announce == "выключено настройкой"


async def test_alone_timeout_leaves_and_company_resets_it(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"voice_alone_timeout_s": 0.3}) as rig:
        g, voice = vworld(rig)
        voice.members = [person(rig, 5, "masha")]
        await rig.voice.join(voice)
        await asyncio.sleep(0.5)
        assert 1 in rig.voice.sessions                                                       # человек есть: сидит
        voice.members = []
        await until(lambda: rig.voice.sessions == {} and voice.vcs[0].disconnected and rig.voice.last_note, 3)
        assert "никого нет" in rig.voice.last_note
    async with Rig(tmp_path / "z", monkeypatch, module_settings={"voice_alone_timeout_s": 0}) as rig:     # 0: не выходить
        g, voice = vworld(rig)
        await rig.voice.join(voice)
        await asyncio.sleep(0.4)
        assert 1 in rig.voice.sessions


async def test_autojoin_on_ready_and_when_a_person_enters_with_retry_pause(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"voice_autojoin": "Клуб / Войс"}) as rig:
        g, voice = vworld(rig)
        a = person(rig, 5, "masha")
        rig.voice.on_ready()                                                                 # в канале пусто: не заходит
        await asyncio.sleep(0.1)
        assert rig.voice.sessions == {}
        voice.members = [a]
        await rig.voice.on_voice_state(a, NS(channel=None), NS(channel=voice))
        await until(lambda: 1 in rig.voice.sessions)
        await rig.voice.leave()
        voice.connect_error = type("Forbidden", (Exception,), {})("нет прав")
        await rig.voice.on_voice_state(a, NS(channel=None), NS(channel=voice))
        await until(lambda: "автовход" in rig.voice.last_error)
        voice.connect_error = None
        await rig.voice.on_voice_state(a, NS(channel=None), NS(channel=voice))               # пауза после неудачи: молчит
        await asyncio.sleep(0.1)
        assert rig.voice.sessions == {}
        rig.voice._autojoin_retry_at = 0
        rig.voice.on_ready()                                                                 # а при старте бота заходит
        await until(lambda: 1 in rig.voice.sessions)
    async with Rig(tmp_path / "x", monkeypatch, module_settings={"voice_autojoin": "нет такого"}) as rig:
        vworld(rig)
        rig.voice.on_ready()
        await until(lambda: "автовход" in rig.voice.last_error)


async def test_kicked_moved_and_lost_client_end_the_session(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        other = rig.dc.register(g.add(VoiceChannel(501, "Второй")))
        voice.members = [person(rig, 5, "masha")]
        await rig.voice.join(voice)
        await rig.voice.on_voice_state(rig.me, NS(channel=voice), NS(channel=None))
        assert rig.voice.sessions == {} and "отключили" in rig.voice.last_error
        await rig.voice.join(voice)
        await rig.voice.on_voice_state(rig.me, NS(channel=voice), NS(channel=other))
        assert rig.voice.sessions == {} and "переместили" in rig.voice.last_error
        await rig.voice.join(voice)
        await rig.bot.voice.on_client_lost()
        assert rig.voice.sessions == {} and "Discord потеряно" in rig.voice.last_error


async def test_dropped_connection_is_reestablished_without_losing_engine_session(tmp_path, monkeypatch):
    monkeypatch.setattr(dv, "RECONNECT_GRACE_S", 0.1)
    monkeypatch.setattr(dv, "RECONNECT_DELAYS", (0.05, 0.05))
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        a = person(rig, 5, "masha")
        voice.members = [a]
        await rig.voice.join(voice)
        first = voice.vcs[0]
        first.connected = False
        await until(lambda: len(voice.vcs) == 2, 4)
        second = voice.vcs[1]
        await until(lambda: sess_of(rig).state == "connected" and second.sink is not None)
        assert first.disconnected and len(rig.engines) == 1 and not rig.engines[0].stopped
        second.feed(a, level(700))                                                           # приём идёт через новое соединение
        await until(lambda: rig.engines[0].audio.get("discord:5"))
        assert sess_of(rig).counters["reconnects"] == 1
        second.connected = False                                                             # и снова, но теперь канал недоступен
        voice.connect_error = RuntimeError("нет сети")
        await until(lambda: rig.voice.sessions == {}, 6)
        assert "не удалось переподключиться" in rig.voice.last_error


async def test_discord_errors_become_understandable_tool_errors(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
        g, voice = vworld(rig)
        voice.members = [person(rig, OWNER_ID, "boss")]
        voice.perms.connect = False
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("voice_join", {})
        assert e.value.code == "denied" and "Connect" in str(e.value)
        voice.perms.connect = True
        voice.perms.speak = False
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("voice_join", {})
        assert "Speak" in str(e.value)
        voice.perms.speak = True
        voice.user_limit = 1
        with pytest.raises(ToolError, match="заполнен"):
            await rig.core.call_tool("voice_join", {})
        voice.user_limit = 0
        voice.connect_error = type("Forbidden", (Exception,), {"status": 403})("Missing Permissions")
        with pytest.raises(ToolError, match="Connect/Speak") as e:
            await rig.core.call_tool("voice_join", {})
        assert e.value.code == "denied" and rig.engines[-1].stopped                          # связь с движком не осталась висеть
        voice.connect_error = type("ConnectionClosed", (Exception,), {"code": 4017})("x")
        with pytest.raises(ToolError, match="DAVE"):
            await rig.core.call_tool("voice_join", {})
        assert rig.voice.sessions == {}


async def test_engine_off_blocks_join_with_clear_message(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
        g, voice = vworld(rig)
        voice.members = [person(rig, OWNER_ID, "boss")]

        def off(**kw):
            e = StubEngine(**kw)
            e.ok = False
            rig.engines.append(e)
            return e

        rig.voice.client_factory = off
        with pytest.raises(ToolError, match="движок голоса ядра недоступен"):
            await rig.core.call_tool("voice_join", {})
        assert voice.vcs == [] and rig.engines[-1].stopped


async def test_missing_nacl_or_davey_refuses_with_names(tmp_path, monkeypatch):
    def importer(name):
        if name in ("nacl", "davey"):
            raise ImportError(name)
        return object()

    assert len(check_dependencies(importer)) == 2
    async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
        g, voice = vworld(rig)
        voice.members = [person(rig, OWNER_ID, "boss")]
        rig.voice.importer = importer
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("voice_join", {})
        assert "PyNaCl" in str(e.value) and "davey" in str(e.value) and rig.engines == [] and voice.vcs == []
        assert any("davey" in p for p in rig.voice.status()["problems"])


async def test_danger_check_asks_only_for_unknown_channel_without_owner(tmp_path, monkeypatch):
    from manifest import MANIFEST
    async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
        g, voice = vworld(rig)
        check = rig.core.check_tool
        assert "ещё не была" in await check("voice_join", {"channel": "Клуб / Войс"})
        assert await check("voice_join", {"channel": "нет такого"}) is None                  # ошибку объяснит сам инструмент
        voice.members = [person(rig, OWNER_ID, "boss")]
        assert await check("voice_join", {"channel": "Клуб / Войс"}) is None                 # владелец сидит сам
        assert await check("voice_join", {}) is None
        voice.members = []
        rig.voice.known.add("channel:500", "Войс")                                           # уже заходила
        assert await check("voice_join", {"channel": "500"}) is None
        t = {x["name"]: x for x in MANIFEST["tools"]}
        assert t["voice_join"]["danger_check"] is True and t["voice_join"]["risk"] == t["message"]["risk"]
        assert not t["voice_leave"].get("danger_check") and all(len(x["description"]) <= 300 for x in t.values())
        with pytest.raises(ToolError):
            await rig.core.call_tool("voice_join", {"channel": 5})


async def test_leave_by_name_and_two_sessions(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        g2 = rig.dc.guild(2, "Другой")
        g2.me = rig.me
        v2 = rig.dc.register(g2.add(VoiceChannel(600, "Тихий")))
        await rig.voice.join(voice)
        await rig.voice.join(v2)
        assert len(rig.voice.sessions) == 2
        with pytest.raises(dv.VoiceError, match="нескольких"):
            await rig.voice.leave()
        assert "Другой / Тихий" in await rig.voice.leave("Другой")
        assert list(rig.voice.sessions) == [1]
        with pytest.raises(dv.VoiceError):
            await rig.voice.leave("нет")


async def test_moving_to_another_channel_of_the_same_guild_replaces_the_session(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, voice = vworld(rig)
        other = rig.dc.register(g.add(VoiceChannel(501, "Второй")))
        await rig.voice.join(voice)
        assert "уже в голосовом канале" in await rig.voice.join(voice)
        await rig.voice.join(other)
        assert sess_of(rig).channel is other and voice.vcs[0].disconnected and rig.engines[0].stopped and len(rig.engines) == 2


async def test_bot_requests_voice_states_intent(monkeypatch):
    import discord
    seen = {"events": []}

    class Intents:
        @classmethod
        def none(cls):
            return cls()

    class Client:
        def __init__(self, **kw):
            seen.update(kw)

        def event(self, fn):
            seen["events"].append(fn.__name__)
            return fn

    monkeypatch.setattr(discord, "Intents", Intents, raising=False)
    monkeypatch.setattr(discord, "Client", Client, raising=False)
    from discord_bot import DiscordBot
    bot = DiscordBot.__new__(DiscordBot)
    bot._me, bot.voice = None, None
    DiscordBot._make_client(bot)
    assert seen["intents"].voice_states is True and "on_voice_state_update" in seen["events"]


async def test_end_to_end_with_real_voice_engine_listen_utterance_and_speak(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from gits_sdk.voice import protocol as V
    from services.voice_engine.config import EngineConfig
    from services.voice_engine.engine import VoiceEngine
    from services.voice_engine.server import VoiceServer
    from services.voice_engine.stt import FakeStt
    from services.voice_engine.tts import FakeTts

    events, speech = [], []

    async def emit(event):
        events.append(event)

    engine = VoiceEngine(stt=FakeStt(default="Глэдос, слышишь меня"), tts=FakeTts(ms_per_char=10), config=EngineConfig(), emit_event=emit,
                         journal=lambda *a: None, on_speech=speech.append, names=lambda: ("Глэдос",))
    app = web.Application()
    VoiceServer(engine, lambda: "tok", hello_timeout_s=1.0).attach(app)
    server = TestServer(app)
    await server.start_server()
    ended = lambda: [s for s in speech if s["phase"] == "ended"]        # noqa: E731
    try:
        async with Rig(tmp_path, monkeypatch, module_settings=OWNER) as rig:
            rig.voice.client_factory = None                                                  # настоящий VoiceAdapterClient
            rig.voice.engine_url, rig.voice.engine_token = f"ws://127.0.0.1:{server.port}{V.WS_PATH}", "tok"
            g, voice = vworld(rig)
            boss = person(rig, OWNER_ID, "boss")
            voice.members = [boss]
            await rig.voice.join(voice)
            vc = voice.vcs[0]
            sid = "discord:1:500"
            await until(lambda: engine.session(sid) is not None and f"discord:{OWNER_ID}" in engine.session(sid).speakers)
            for _ in range(60):                                                              # 1.2 с «речи» в реальном времени
                vc.feed(boss, level(9000))
                await asyncio.sleep(0.02)
            await until(lambda: events, 8)                                                   # тишину после речи добавил адаптер
            ev = events[0]
            assert ev["data"]["phrase"] == "Глэдос, слышишь меня" and ev["sender"]["is_owner"] is True and ev["data"]["session"] == sid
            # самодостаточный текст: ник говорящего, пометка владельца и название канала (из hello.title адаптера)
            assert ev["text"] == "boss (владелец) в голосовом канале «Клуб / Войс»: «Глэдос, слышишь меня»"
            engine.speak(sid, "привет")
            await until(lambda: vc.source is not None)
            await asyncio.sleep(0.1)
            frames = vc.read_frames()
            vc.finish()
            assert len(frames) >= 3 and all(len(f) == dv.FRAME48_BYTES for f in frames)
            await until(lambda: ended())
            assert ended()[0]["completed"] is True
            rig.bot.modes.set("channel", 500, "read")                                         # режим «чтение»: voice.say не звучит
            sess_of(rig).wake.set()
            await until(lambda: not sess_of(rig).can_speak())
            engine.speak(sid, "ещё")
            await until(lambda: len(ended()) == 2)
            assert ended()[1]["completed"] is False and vc.source is None
            await rig.voice.leave()
            await until(lambda: engine.session(sid) is None)
    finally:
        await engine.aclose()
        await server.close()
