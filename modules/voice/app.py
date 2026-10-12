"""Модуль голоса (ТЗ 12): склейка клиента модуля, очереди реплик, микрофона и слуха.

Ядро решает, ЧТО сказать, и шлёт `speak` / `speak.stop` / `tts.synthesize`; модуль решает, КАК это прозвучит, и событиями
сообщает ядру, что сказал пользователь (`voice.utterance`), когда говорит она сама (`voice.speech`), когда говорит человек
(`voice.user_speaking`) и что слышно в колонках (`voice.heard`). Все события уходят через очередь `Outbox`, чтобы порядок
сохранялся (начало и конец реплики не перепутаются).
"""
import asyncio
import json
import logging
import secrets
import time
import wave
from collections import deque
from pathlib import Path

from fakes import FakePlayer, fake_synth_file
from glados_module import ModuleClient, ToolError
from hearing import Hearing
from mic_listener import MicListener
from player import PowerShellPlayer
from speaker import Speaker
from stt_client import STTClient
from voice_id import VoiceIdentity

import common
import events_log
import tts_engine

log = logging.getLogger("voice")

TEXT_MAX = 5000                 # предел текста для tts.synthesize (как у прежнего core.tts)
SWEEP_EVERY_S = 3600            # VOICE-5: чистка временной папки раз в час
STATE_EVERY_S = 60              # как часто сверять состояние для дашборда
PANEL_EVERY_S = 2               # пространство «Голос» в дашборде (TZ 15): проверка изменений, отправка не чаще раза в секунду
SYNTH_SAMPLES = 5               # сколько последних замеров синтеза держим для пространства
OUTBOX_MAX = 500
SELF_SAMPLE_TEXT = "Проверка голоса. Меня зовут ГЛаДОС. Это тест."


class Outbox:
    """Очередь исходящих событий: уходят по одному, в порядке постановки."""

    def __init__(self, client: ModuleClient):
        self._client = client
        self._q: asyncio.Queue = asyncio.Queue(maxsize=OUTBOX_MAX)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def put(self, kind: str, text: str, **fields) -> None:
        try:
            self._q.put_nowait((kind, text, fields))
        except asyncio.QueueFull:
            log.warning(f"очередь событий переполнена, «{kind}» отброшено")

    async def drain(self, timeout: float = 5.0) -> None:
        """Дождаться, пока всё поставленное уйдёт (тесты, остановка)."""
        await asyncio.wait_for(self._q.join(), timeout)

    async def _run(self) -> None:
        while True:
            kind, text, fields = await self._q.get()
            try:
                await self._client.event(kind, text, **fields)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"событие {kind} не отправлено: {type(e).__name__}: {e}")
            finally:
                self._q.task_done()


def _wav_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as w:
        return round(w.getnframes() / float(w.getframerate()) * 1000)


class VoiceModule:
    def __init__(self, client: ModuleClient, memory_dir: str, *, fake: bool | None = None, player=None, stt=None,
                 tmp_dir: Path | None = None, synth=None):
        """fake (или GITS_VOICE_FAKE=1): синтез пишет тишину, воспроизведение только ждёт, микрофон и слух не запускаются.
        player / stt / synth(текст, папка) -> путь подменяют части в тестах."""
        self.client = client
        self.fake = common.fake_mode() if fake is None else fake
        self.tmp = Path(tmp_dir) if tmp_dir is not None else common.temp_dir()
        self._synth_blocking = synth or (fake_synth_file if self.fake else tts_engine.synth_file)
        self.voices = VoiceIdentity(memory_dir)
        self.stt = stt or STTClient(on_change=self._stt_changed)
        self.outbox = Outbox(client)
        self.player = player or (FakePlayer() if self.fake else PowerShellPlayer())
        self.hearing = Hearing(self._effective, on_gist=self._on_gist)
        self.speaker = Speaker(synth=self._synth, player=self.player, emit=self._speech_event,
                               volume=lambda: max(0, min(100, int(self.client.settings.get("volume", 100)))) / 100.0,
                               muted=lambda: bool(self.client.settings.get("voice_muted")),
                               on_segment=self.hearing.note_own_speech)
        self.mic = MicListener(
            self.stt, self.voices, self._on_voice, lambda: self.speaker.audible, self._own_voice_sample,
            on_speaking=self._on_user_speaking, settings_get=self._effective,
            echo_tail=lambda: max(0, int(self.client.module_settings.get("echo_tail_ms", common.ECHO_TAIL_DEFAULT_MS))) / 1000.0,
            # перебивание и узнавание голосов нужны отпечатки голоса: только с сервером WhisperX (VOICE-10)
            barge_enabled=lambda: bool(self.client.module_settings.get("barge_in")) and self.stt.diarized,
            on_barge_in=self._on_barge_in, diarized=lambda: self.stt.diarized)
        self._tasks: list[asyncio.Task] = []
        self._state_tasks: set[asyncio.Task] = set()
        self._last_state: tuple | None = None
        self._synth_ms: deque = deque(maxlen=SYNTH_SAMPLES)     # замеры синтеза, мс (пространство «Голос», TZ 15)
        self._panel_key: str | None = None
        self._register()

    # ── настройки ────────────────────────────────────────────────────────────

    def _effective(self) -> dict:
        """Общие настройки ядра; выключенный модуль (`enabled: false`) не слушает ни микрофон, ни колонки."""
        cfg = dict(self.client.settings)
        if not self.client.enabled:
            cfg["mic"] = cfg["hearing"] = False
        return cfg

    # ── запуск и остановка ───────────────────────────────────────────────────

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.outbox.start()
        self.speaker.start()
        self._tasks.append(loop.create_task(self._sweep_loop()))
        self._tasks.append(loop.create_task(self._state_loop()))
        self._tasks.append(loop.create_task(self._panel_loop()))
        if not self.fake:
            self._tasks.append(loop.create_task(self.stt.watch()))     # STT: проверка WhisperX при старте и раз в минуту
            self._tasks.append(loop.create_task(self.mic.run()))
            self._tasks.append(loop.create_task(self.hearing.run()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            await asyncio.wait_for(self.outbox.drain(), 2)           # хвост событий (ended при остановке) успевает уйти
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        await self.speaker.aclose()
        await self.outbox.stop()
        await self.stt.aclose()

    async def _sweep_loop(self) -> None:
        while True:
            removed = await asyncio.to_thread(common.sweep_dir, self.tmp)
            if removed:
                log.info(f"временная папка: удалено старых файлов {removed}")
            await asyncio.sleep(SWEEP_EVERY_S)

    async def _state_loop(self) -> None:
        while True:
            await self.report_state()
            await asyncio.sleep(STATE_EVERY_S)

    async def report_state(self, force: bool = False) -> None:
        cfg = self._effective()
        data = {"mic": bool(cfg.get("mic")), "hearing": bool(cfg.get("hearing")), "muted": bool(cfg.get("voice_muted")),
                "tts": tts_engine.backend(), "fake": self.fake, "stt": self.stt.label(), "stt_mode": self.stt.mode}
        key = tuple(sorted(data.items()))
        if not force and key == self._last_state:
            return
        self._last_state = key
        summary = (f"микрофон: {'вкл' if data['mic'] else 'выкл'}, слух: {'вкл' if data['hearing'] else 'выкл'}, "
                   f"голос: {'заглушён' if data['muted'] else 'вкл'}, синтез: {data['tts']}, {data['stt']}")
        await self.client.state(summary, data)

    def _stt_changed(self, status: dict) -> None:
        """Режим распознавания сменился (WhisperX ↔ локальный CPU, или модель открылась / не открылась): одна строка в журнал
        и новое состояние модуля. Вызывается из STTClient внутри асинхронного кода."""
        if status.get("mode") == "local":
            if status.get("model_state") == "загружена":
                line = "распознавание: WhisperX не отвечает, работаю локально (CPU), без узнавания голосов"
            else:
                line = (f"распознавание: WhisperX не отвечает, локальная модель {status.get('model')} не загружена "
                        "(скачивание только с разрешения владельца): речь пока не распознаётся")
        elif status.get("mode") == "server":
            line = "распознавание: WhisperX отвечает, работаю через него (с узнаванием голосов)"
        else:
            line = f"распознавание: {status.get('label')}"
        events_log.log_nowait("hearing", line)
        try:
            task = asyncio.get_running_loop().create_task(self.report_state())
        except RuntimeError:
            return
        self._state_tasks.add(task)                    # ссылка до конца работы: задача не уйдёт сборщику раньше времени
        task.add_done_callback(self._state_tasks.discard)

    # ── синтез и события ─────────────────────────────────────────────────────

    async def _synth(self, text: str) -> str | None:
        t0 = time.monotonic()
        out = await asyncio.to_thread(self._synth_blocking, text, self.tmp)
        self._synth_ms.append(round((time.monotonic() - t0) * 1000))
        return out

    # ── пространство «Голос» в дашборде (TZ 15, TAB-9) ───────────────────────

    def panel_blocks(self) -> list[dict]:
        """Микрофон, слух, заглушка, синтез, говорит ли она сейчас, последние замеры синтеза. Без устройств: их ядро не
        знает (настройки устройств модуль пока не ведёт)."""
        cfg = self._effective()
        speaking = self.speaker.is_speaking()
        mic, hearing, muted = bool(cfg.get("mic")), bool(cfg.get("hearing")), bool(cfg.get("voice_muted"))
        samples = list(self._synth_ms)
        blocks: list[dict] = [
            {"type": "stats", "title": "Сейчас", "items": [
                {"label": "Микрофон", "value": "вкл" if mic else "выкл"},
                {"label": "Слух", "value": "вкл" if hearing else "выкл"},
                {"label": "Голос", "value": "заглушён" if muted else "вкл"},
                {"label": "Синтез", "value": str(tts_engine.backend())[:40]},
                {"label": "Говорит", "value": "да" if speaking else "нет"},
                {"label": "Слышит", "value": "да" if mic and not speaking else "нет"},
                {"label": "Распознавание", "value": self.stt.short()},
                {"label": "Синтез, последний замер, мс", "value": samples[-1] if samples else "нет"}]},
            {"type": "list", "title": "Замеры синтеза, мс", "items": [{"text": f"{ms} мс"} for ms in reversed(samples)]}
            if samples else {"type": "text", "title": "Замеры синтеза, мс", "text": "Замеров пока нет."},
            {"type": "text", "title": "Распознавание речи", "text": self.stt.label()},
        ]
        if self.fake:
            blocks.append({"type": "text", "text": "Тестовый режим: звук не идёт, микрофон и слух не запущены."})
        return blocks

    async def _panel_loop(self) -> None:
        while True:
            try:
                blocks = self.panel_blocks()                  # дёшево: только чтение атрибутов
                key = json.dumps(blocks, ensure_ascii=False, sort_keys=True)
                if key != self._panel_key and await self.client.panel.set(blocks):
                    self._panel_key = key
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("пространство голоса не обновлено: %s", e)
            await asyncio.sleep(PANEL_EVERY_S)

    def _speech_event(self, data: dict) -> None:
        self.outbox.put("voice.speech", f"Реплика {data.get('utterance_id')}: {data.get('phase')}", data=data)

    def _on_user_speaking(self, speaking: bool) -> None:
        self.outbox.put("voice.user_speaking", "Человек начал говорить" if speaking else "Человек закончил говорить",
                        data={"speaking": bool(speaking)})

    async def _on_voice(self, text: str, info: dict) -> None:
        """VOICE-6: распознанная реплика уходит событием, а не вызовом разговора."""
        sender = {"id": "voice:" + str(info.get("id") or "unknown"), "is_owner": bool(info.get("owner"))}
        if info.get("name"):
            sender["name"] = str(info["name"])
        self.outbox.put("voice.utterance", text, data={"speaker": info}, sender=sender, addressed_to_me=True)

    async def _on_gist(self, summary: str) -> None:
        """VOICE-14: сводка услышанного в колонках."""
        self.outbox.put("voice.heard", f"В колонках: {summary}", data={"summary": summary}, memorable=False)

    async def _on_barge_in(self) -> None:
        await self.speaker.stop(None, "now", reason="barge_in")

    async def _own_voice_sample(self) -> bytes:
        """VOICE-9: образец её голоса для записи в voice_id: синтез внутри модуля и перевод в PCM 16 кГц."""
        if self.fake:
            return b""
        import opus_encoder
        path = await asyncio.to_thread(tts_engine.synth_file, SELF_SAMPLE_TEXT, self.tmp)
        if not path:
            return b""
        try:
            return await opus_encoder.audio_file_to_pcm16k(path)
        finally:
            await asyncio.to_thread(self._unlink, path)

    @staticmethod
    def _unlink(path) -> None:
        try:
            Path(path).unlink()
        except OSError:
            pass

    async def synthesize(self, body: dict) -> dict:
        """VOICE-5: запрос ядра `tts.synthesize` → файл в %TEMP%\\glados_voice\\ (wav, либо ogg через opus_encoder)."""
        text, fmt = body.get("text"), body.get("format") or "wav"
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
            raise ToolError(f"text: непустая строка до {TEXT_MAX} символов", code="bad_request")
        if fmt not in ("wav", "ogg"):
            raise ToolError("format: wav или ogg", code="bad_request")
        src = await self._synth(text)                                  # блокирующий синтез уходит в поток
        if not src:
            raise ToolError("синтез речи не удался", code="unavailable")
        await asyncio.to_thread(self.tmp.mkdir, parents=True, exist_ok=True)
        name = "tts_" + secrets.token_hex(6)
        wav = self.tmp / (name + ".wav")
        try:
            if str(src).lower().endswith(".wav"):
                await asyncio.to_thread(Path(src).replace, wav)
                src = None
            else:
                import opus_encoder
                await opus_encoder.audio_file_to_wav_file(str(src), str(wav))
            duration = await asyncio.to_thread(_wav_ms, wav)
            if fmt == "wav":
                return {"path": str(wav), "duration_ms": duration}
            import opus_encoder
            data = await opus_encoder.audio_file_to_ogg_opus(str(wav))
            ogg = self.tmp / (name + ".ogg")
            await asyncio.to_thread(ogg.write_bytes, data)
            await asyncio.to_thread(self._unlink, wav)
            return {"path": str(ogg), "duration_ms": duration}
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"не удалось подготовить звук: {e}", code="internal") from e
        finally:
            if src:
                await asyncio.to_thread(self._unlink, src)

    # ── обработчики ядра ─────────────────────────────────────────────────────

    def _register(self) -> None:
        c = self.client

        @c.on_request("speak")
        async def speak(body):
            uid, text = body.get("utterance_id"), body.get("text")
            if not isinstance(uid, str) or not uid or not isinstance(text, str):
                log.warning("speak: нужны utterance_id и text, сообщение отброшено")
                return None
            if not c.enabled:
                return None
            await self.speaker.speak(uid, text, body.get("emotion"), bool(body.get("replace")))
            return None

        @c.on_request("speak.stop")
        async def speak_stop(body):
            uid = body.get("utterance_id")
            at = body.get("at") if body.get("at") in ("now", "segment_end") else "segment_end"
            await self.speaker.stop(uid if isinstance(uid, str) else None, at)
            return None

        @c.on_request("tts.synthesize")
        async def tts_synthesize(body):
            return await self.synthesize(body)

        @c.on_topic("settings")
        async def on_settings(data):
            if c.settings.get("voice_muted"):
                self.speaker.mute_now()
            await self.report_state()

        @c.on_topic("module")
        async def on_module(data):
            if not c.enabled:
                await self.speaker.stop(None, "now")
            await self.report_state()

        @c.on_reconnect
        async def reconnected():
            await self.report_state(force=True)

        @c.on_shutdown
        async def shutting_down(reason):
            await self.speaker.stop(None, "now")

        @c.tool("recent_audio")
        async def recent_audio(args):
            if not self._effective().get("hearing"):
                raise ToolError("слух выключен, системный звук не слушается", code="unavailable")
            text = self.hearing.recent_text(300)
            if not text:
                raise ToolError("за последние минуты речи в звуке не было", code="unavailable")
            return text

        @c.tool("name_voice")
        async def name_voice(args):
            last = self.mic.last_info
            if not last or not last.get("id") or str(last["id"]).startswith("SPEAKER"):
                raise ToolError("голос не узнан: микрофон выключен, сервер WhisperX не запущен или голоса пока не было",
                                code="unavailable")
            name, voice_id = str(args.get("name") or "").strip(), str(last["id"])
            if not name:
                raise ToolError("имя не названо", code="bad_request")
            if not await asyncio.to_thread(self.voices.set_name, voice_id, name):
                raise ToolError("такого голоса нет в памяти", code="unavailable")
            owner = bool(args.get("is_owner"))
            if owner:
                await asyncio.to_thread(self.voices.set_owner, voice_id)
            # человека в памяти и отношениях заводит ядро (модуль ни памяти, ни отношений не ведёт)
            self.outbox.put("voice.named", f"Голосу {voice_id} назначено имя {name}",
                            data={"voice_id": voice_id, "name": name, "is_owner": owner})
            if owner:
                return f"запомнила: этот голос принадлежит тебе, тебя зовут {name}"
            return f"запомнила: этот голос принадлежит человеку по имени {name}"
