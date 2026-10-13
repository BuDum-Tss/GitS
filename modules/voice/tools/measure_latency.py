"""Замеры задержки голоса (ТЗ 12, VOICE-19). Запускает человек: нужен прогретый Fish Speech (docker compose up fish-speech)
и, для третьего замера, WhisperX (docker compose up whisperx). Колонки и микрофон не используются: воспроизведение
заменено ожиданием длительности файла.

Три замера, по N повторов (по умолчанию 20), медиана и p95 в миллисекундах:
  first    от вызова озвучки до начала воспроизведения первого предложения;
  gap      пауза между предложениями: от конца одного до начала следующего (3 предложения подряд);
  utter    от конца фразы пользователя (последний кадр в VAD) до готовой реплики: в режиме module это приход события
           `voice.utterance` в FakeCore, в режиме legacy - вызов `on_voice` (в ядре до переноса).

Режимы:
  --mode module   текущая ветка (после переноса): Speaker и MicListener модуля, события через настоящий ModuleClient.
  --mode legacy   ветка master (до переноса): `tts_engine.speak_sequence` и `mic_listener.MicListener` из каталога агента.
                  Укажи --agent-dir каталога agent/ из master (например git worktree: `git worktree add ..\\GitS-master master`).

Запуск (из корня репозитория, тем же Python, что у ядра):
  python modules/voice/tools/measure_latency.py --mode module --json after.json
  python modules/voice/tools/measure_latency.py --mode legacy --agent-dir C:\\MyProjects\\GitS-master\\agent --json before.json
Сравнение: ухудшение любого замера больше чем на 100 мс нужно разобрать до сдачи.
"""
import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
GITS = HERE.parent.parent
TEXT = "Первое предложение для замера. Второе предложение идёт следом. Третье предложение заканчивает реплику."
FRAME = 480


def wav_seconds(path: str) -> float:
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 1.0                                   # mp3 от запасного голоса: длительность неизвестна, берём секунду


def stats(values: list[float]) -> dict:
    values = sorted(values)
    if not values:
        return {"n": 0}
    p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
    return {"n": len(values), "median_ms": round(statistics.median(values), 1), "p95_ms": round(p95, 1),
            "min_ms": round(values[0], 1), "max_ms": round(values[-1], 1)}


def pcm_frames(audio: str) -> list[bytes]:
    import numpy as np
    with wave.open(audio, "rb") as w:
        rate, ch = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, ch)[:, 0].astype(np.float32)
    if rate != 16000:                                # простая передискретизация до 16 кГц (для замера хватает)
        n = int(len(data) * 16000 / rate)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)
    pcm = data.astype(np.int16).tobytes()
    return [pcm[i:i + FRAME * 2] for i in range(0, len(pcm) - FRAME * 2, FRAME * 2)]


# ── режим module ─────────────────────────────────────────────────────────────

async def measure_module(n: int, audio: str | None, skip_utter: bool) -> dict:
    sys.path[:0] = [str(HERE), str(GITS / "agent"), str(GITS / "sdk")]
    import tts_engine
    from gits_sdk import ModuleClient
    from gits_sdk.testing import FakeCore
    from app import VoiceModule
    from manifest import build_manifest

    class SilentPlayer:
        def __init__(self):
            self.starts: list[float] = []
            self.ends: list[float] = []

        async def play(self, path, volume):
            self.starts.append(time.perf_counter())
            await asyncio.sleep(wav_seconds(path))
            self.ends.append(time.perf_counter())

    tmp = Path(tempfile.gettempdir()) / "glados_voice_measure"
    out: dict = {}
    async with FakeCore(settings={"voice_muted": False, "volume": 100, "mic": False, "hearing": False,
                                  "dnd": False, "quiet_until": 0.0}) as core:
        client = ModuleClient(build_manifest(), url=core.url, token=core.token)
        player = SilentPlayer()
        mod = VoiceModule(client, str(tmp), fake=False, player=player, tmp_dir=tmp)
        task = asyncio.create_task(client.run())
        await core.wait_connected()
        await client.wait_connected(5)
        mod.outbox.start()
        mod.speaker.start()
        await asyncio.to_thread(tts_engine.synth_file, "Прогрев.", tmp)          # прогрев Fish Speech
        first, gaps = [], []
        for i in range(n):
            player.starts.clear()
            player.ends.clear()
            t0 = time.perf_counter()
            await mod.speaker.speak(f"u-m{i:04d}", TEXT)
            while len(player.ends) < 3:
                await asyncio.sleep(0.005)
            first.append((player.starts[0] - t0) * 1000)
            gaps += [(player.starts[k + 1] - player.ends[k]) * 1000 for k in range(2)]
            await asyncio.sleep(0.3)
        out["first"], out["gap"] = stats(first), stats(gaps)
        out["first_note"] = "от speak до запуска воспроизведения (плюс запуск PowerShell ~0,3-0,6 с при настоящем звуке)"
        if audio and not skip_utter:
            from mic_listener import MicListener
            from stt_client import STTClient
            from voice_id import VoiceIdentity
            frames = pcm_frames(audio)
            vad = {"speech": True}
            lis = MicListener(STTClient(), VoiceIdentity(str(tmp)), mod._on_voice, lambda: False,
                              vad=lambda f: vad["speech"], clock=time.time)
            lis.prepare()
            samples = []
            for i in range(n):
                before = len([e for e in core.events if e["kind"] == "voice.utterance"])
                vad["speech"] = True
                for f in frames[:120]:
                    await lis.handle_frame(f)
                vad["speech"] = False
                t_end = None
                for _ in range(20):
                    t_end = t_end or time.perf_counter()
                    await lis.handle_frame(b"\0" * (FRAME * 2))
                # t_end - момент, когда тишины набралось на конец фразы: считаем от последнего речевого кадра
                limit = time.perf_counter() + 30
                while len([e for e in core.events if e["kind"] == "voice.utterance"]) <= before and time.perf_counter() < limit:
                    await asyncio.sleep(0.005)
                samples.append((time.perf_counter() - t_end) * 1000)
            out["utter"] = stats(samples)
            out["utter_note"] = "от первого кадра тишины после фразы до события voice.utterance в FakeCore (включая 0,5 с VAD)"
        await mod.speaker.aclose()
        await mod.outbox.stop()
        await client.stop()
        await asyncio.wait_for(task, 5)
    return out


# ── режим legacy ─────────────────────────────────────────────────────────────

async def measure_legacy(n: int, audio: str | None, agent_dir: str, skip_utter: bool) -> dict:
    sys.path.insert(0, agent_dir)
    import tts_engine
    starts: list[float] = []
    ends: list[float] = []

    def fake_play(path):
        starts.append(time.perf_counter())
        time.sleep(wav_seconds(path))
        ends.append(time.perf_counter())
    tts_engine._play_file = fake_play
    tts_engine.start()
    await asyncio.to_thread(tts_engine.synth_file, "Прогрев.")
    first, gaps = [], []
    segments = [s for s in TEXT.split(". ") if s]
    segments = [s if s.endswith(".") else s + "." for s in segments]
    for i in range(n):
        starts.clear()
        ends.clear()
        t0 = time.perf_counter()
        tts_engine.speak_sequence(segments)
        while len(ends) < 3:
            await asyncio.sleep(0.005)
        first.append((starts[0] - t0) * 1000)
        gaps += [(starts[k + 1] - ends[k]) * 1000 for k in range(2)]
        await asyncio.sleep(0.3)
    out = {"first": stats(first), "gap": stats(gaps),
           "first_note": "от speak_sequence до запуска воспроизведения (в питомце к этому добавлялся опрос /api/state раз в 2 с)"}
    if audio and not skip_utter:
        import mic_listener as ml
        from stt_client import STTClient
        from voice_id import VoiceIdentity
        frames = pcm_frames(audio)
        got: list[float] = []

        async def on_voice(text, info):
            got.append(time.perf_counter())
        vad = {"speech": True}
        lis = ml.MicListener(STTClient(), VoiceIdentity(str(Path(agent_dir) / "_measure_mem")), on_voice, lambda: False)
        seg = ml.UtteranceSegmenter(lambda f: vad["speech"])
        samples = []
        for i in range(n):
            vad["speech"] = True
            for f in frames[:120]:
                seg.feed(f, False)
            vad["speech"] = False
            done = None
            t_end = None
            for _ in range(20):
                t_end = t_end or time.perf_counter()
                done = seg.feed(b"\0" * (FRAME * 2), False) or done
            if done is None:
                continue
            await lis.handle_utterance(done[0])
            if got:
                samples.append((got.pop() - t_end) * 1000)
        out["utter"] = stats(samples)
        out["utter_note"] = "от первого кадра тишины после фразы до вызова on_voice"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("module", "legacy"), required=True)
    ap.add_argument("-n", type=int, default=20, help="повторов на замер (по умолчанию 20)")
    ap.add_argument("--agent-dir", help="legacy: каталог agent/ ветки master")
    ap.add_argument("--audio", default=str(HERE / "resources" / "tts" / "references" / "GlaDos.wav"),
                    help="WAV 16 кГц моно с речью для замера utter (нужен WhisperX); пропустить: --skip-utter")
    ap.add_argument("--skip-utter", action="store_true", help="не мерить распознавание (WhisperX не запущен)")
    ap.add_argument("--json", help="записать результат в файл")
    args = ap.parse_args()
    if args.mode == "legacy" and not args.agent_dir:
        raise SystemExit("для --mode legacy нужен --agent-dir")
    audio = args.audio if Path(args.audio).exists() else None
    if args.mode == "module":
        result = asyncio.run(measure_module(args.n, audio, args.skip_utter))
    else:
        result = asyncio.run(measure_legacy(args.n, audio, args.agent_dir, args.skip_utter))
    result["mode"], result["n"] = args.mode, args.n
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
