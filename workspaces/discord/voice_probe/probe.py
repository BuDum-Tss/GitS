"""Проба голоса Discord: вход в канал, приём расшифрованного (DAVE) звука каждого участника, воспроизведение.

Запуск вручную в контейнере (см. docs/DISCORD_VOICE_PROBE.md):
    python /app/workspaces/discord/voice_probe/probe.py --guild ID --channel ID --seconds 60 [--save]

Токен берётся из DISCORD_BOT_TOKEN и нигде не печатается. По умолчанию звук нигде не сохраняется;
флаг --save пишет по 5 секунд звука каждого участника в WAV 16 кГц моно (для прослушивания).
Все тяжёлые импорты (discord, nacl, davey) ленивые: --help и тесты работают без них и без сети.

Схема приёма (расшифровка DAVE, декодер Opus, приёмник для discord-ext-voice-recv) вынесена в общий модуль
workspaces/discord/voice_rx.py: его же использует адаптер голосового канала (discord_voice.py).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import os
import struct
import sys
import threading
import time
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# общая часть приёма (расшифровка DAVE, декодер, приёмник) лежит рядом, в workspaces/discord: её же использует адаптер
_HERE = str(Path(__file__).resolve().parent.parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from voice_rx import (  # noqa: E402,F401  (часть имён переэкспортируется для тестов и совместимости)
    OPUS_SILENCE, REQUIRED, FrameDecoder, build_sink, check_dependencies, dave_info, describe_connect_error)

# --- форматы звука -------------------------------------------------------------------------------
DISCORD_RATE = 48000  # Opus в Discord: 48 кГц, стерео
OUT_RATE = 16000  # для распознавания речи и WAV: 16 кГц, моно
DECIM = DISCORD_RATE // OUT_RATE  # 3
SAVE_SECONDS = 5


# --- чистые функции (проверяются без сети) -------------------------------------------------------
def pcm48_stereo_to_16k_mono(pcm: bytes) -> bytes:
    """PCM s16le 48 кГц стерео -> s16le 16 кГц моно (среднее каналов, среднее по трём отсчётам)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 4])  # целое число стерео-пар
    if sys.byteorder == "big":
        samples.byteswap()
    mono = [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples), 2)]
    out = array("h")
    for i in range(0, len(mono) - DECIM + 1, DECIM):
        out.append(sum(mono[i : i + DECIM]) // DECIM)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def rms_s16(pcm: bytes) -> float:
    """Среднеквадратичный уровень PCM s16le (0..32767); для пустого буфера 0."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n)


def make_tone_pcm(freq: float, seconds: float, amplitude: float = 0.25) -> bytes:
    """Синус как PCM s16le 48 кГц стерео (формат, который ждёт discord.PCMAudio)."""
    count = int(DISCORD_RATE * seconds)
    fade = int(DISCORD_RATE * 0.01)  # 10 мс плавного входа/выхода, чтобы не щёлкало
    out = array("h")
    for i in range(count):
        env = min(1.0, i / fade, (count - i) / fade) if fade else 1.0
        v = int(32767 * amplitude * env * math.sin(2 * math.pi * freq * i / DISCORD_RATE))
        out.append(v)
        out.append(v)
    return out.tobytes()


def make_phrase_pcm() -> bytes:
    """«Фраза» из синусов: три коротких ноты с паузами."""
    gap = bytes(int(DISCORD_RATE * 0.08) * 4)
    return gap.join(make_tone_pcm(f, 0.25) for f in (523.0, 659.0, 784.0))


@dataclass
class UserStats:
    """Счётчики по одному участнику."""

    packets: int = 0  # принято пакетов с кадром Opus (после транспортной расшифровки)
    decoded: int = 0  # успешно расшифровано DAVE и декодировано в PCM
    failed: int = 0  # ошибок расшифровки/декодирования
    rms_sum: float = 0.0
    rms_n: int = 0
    errors: dict = field(default_factory=dict)  # тип ошибки -> сколько раз

    def add_error(self, name: str) -> None:
        self.failed += 1
        self.errors[name] = self.errors.get(name, 0) + 1

    def add_frame_level(self, level: float) -> None:
        self.decoded += 1
        self.rms_sum += level
        self.rms_n += 1

    @property
    def avg_rms(self) -> float:
        return self.rms_sum / self.rms_n if self.rms_n else 0.0

    def snapshot(self) -> "UserStats":
        return UserStats(self.packets, self.decoded, self.failed, self.rms_sum, self.rms_n, dict(self.errors))


def format_dave(info: dict) -> str:
    """Одна строка статуса DAVE: версия протокола, готовность, статус сессии, последняя ошибка."""
    ver = info.get("protocol_version", "?")
    ready = info.get("ready")
    ready_s = "да" if ready else ("нет" if ready is not None else "?")
    parts = [f"DAVE: версия протокола={ver}", f"ready={ready_s}"]
    if info.get("status"):
        parts.append(f"статус={info['status']}")
    if info.get("error"):
        parts.append(f"последняя ошибка={info['error']}")
    return ", ".join(parts)


def format_report(stats: dict, dave: dict, elapsed: float) -> str:
    """Отчёт раз в секунду: строка DAVE и по строке на участника (id, пакеты, кадры, RMS, ошибки)."""
    lines = [f"[{elapsed:5.1f}с] {format_dave(dave)}"]
    if not stats:
        lines.append("   участников со звуком пока нет")
    for uid in sorted(stats, key=str):
        s = stats[uid]
        errs = ""
        if s.errors:
            errs = " ошибки: " + ", ".join(f"{k}x{v}" for k, v in sorted(s.errors.items()))
        lines.append(
            f"   {uid}: пакетов={s.packets} расшифровано={s.decoded} сбоев={s.failed} rms={s.avg_rms:.0f}{errs}"
        )
    return "\n".join(lines)


def verdict(stats: dict) -> str:
    """Итог пробы по критерию успеха: звук каждого участника идёт отдельно и расшифровывается."""
    if not stats:
        return "ИТОГ: звук никого не принят (никто не говорил или приём не работает)."
    bad = [u for u, s in stats.items() if s.packets and s.decoded == 0]
    good = [u for u, s in stats.items() if s.decoded > 0]
    if good and not bad:
        return f"ИТОГ: УСПЕХ, расшифровано и декодировано участников по отдельности: {len(good)}."
    if good:
        return f"ИТОГ: ЧАСТИЧНО, работает у {len(good)}, не работает у {len(bad)}: {', '.join(map(str, bad))}."
    return "ИТОГ: ПРОВАЛ, пакеты приходят, но ни один кадр не расшифрован/декодирован (см. ошибки выше)."


# --- ядро пробы (нужен discord) ------------------------------------------------------------------
class Probe:
    """Собирает статистику приёма. handle_opus() вызывается из потока приёма, поэтому под замком."""

    def __init__(self, save: bool, save_dir: str):
        self.stats: dict = {}
        self.lock = threading.Lock()
        self.save = save
        self.save_dir = save_dir
        self.save_buf: dict = {}
        self.saved: set = set()
        self.last_error: Optional[str] = None
        self.vc = None  # голосовой клиент, заполняется после входа
        self.decoder = FrameDecoder()

    def dave_info(self) -> dict:
        return dave_info(self.vc, self.last_error)

    def handle_opus(self, uid, opus: bytes) -> None:
        """Один кадр Opus от участника: DAVE-расшифровка, декодирование, счётчики."""
        with self.lock:
            st = self.stats.setdefault(uid, UserStats())
            st.packets += 1
        self.decoder.vc = self.vc
        try:
            pcm = self.decoder.decode(uid, opus)
        except Exception as e:  # статистика важнее падения: считаем и идём дальше
            msg = f"{type(e).__name__}: {str(e)[:80]}"
            with self.lock:
                self.stats[uid].add_error(type(e).__name__)
                self.last_error = msg
            return
        mono = pcm48_stereo_to_16k_mono(pcm)
        with self.lock:
            self.stats[uid].add_frame_level(rms_s16(mono))
        if self.save and uid not in self.saved:
            buf = self.save_buf.setdefault(uid, bytearray())
            buf += mono
            if len(buf) >= OUT_RATE * 2 * SAVE_SECONDS:
                self.saved.add(uid)
                self._write_wav(uid, bytes(buf))
                del self.save_buf[uid]

    def _write_wav(self, uid, data: bytes) -> None:
        path = os.path.join(self.save_dir, f"voice_probe_{uid}_{int(time.time())}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUT_RATE)
            w.writeframes(data)
        print(f"   сохранён WAV участника {uid}: {path}", flush=True)


async def run(args) -> int:
    import discord
    from discord.ext import voice_recv

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("ОШИБКА: не задан DISCORD_BOT_TOKEN в окружении.", file=sys.stderr)
        return 2
    if not discord.opus.is_loaded() and not discord.opus._load_default():
        print("ОШИБКА: нет libopus в образе (apt install libopus0).", file=sys.stderr)
        return 3

    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True  # приём голоса требует voice_states; привилегированные intents не нужны
    client = discord.Client(intents=intents)
    probe = Probe(args.save, args.save_dir)
    ready = asyncio.Event()

    @client.event
    async def on_ready():
        ready.set()

    login = asyncio.create_task(client.start(token))
    try:
        done, _ = await asyncio.wait(
            {login, asyncio.create_task(ready.wait())}, timeout=40, return_when=asyncio.FIRST_COMPLETED
        )
        if login in done:
            login.result()  # поднимет LoginFailure и т. п.
        if not ready.is_set():
            print("ОШИБКА: бот не подключился к Discord за 40 с.", file=sys.stderr)
            return 4
        channel = client.get_channel(args.channel)
        if channel is None or getattr(channel, "guild", None) is None or channel.guild.id != args.guild:
            print("ОШИБКА: канал не найден на этом сервере (проверьте ID, бот должен быть на сервере).", file=sys.stderr)
            return 5
        perms = channel.permissions_for(channel.guild.me)
        missing = [n for n in ("view_channel", "connect", "speak", "use_voice_activation") if not getattr(perms, n)]
        if missing:
            print(
                f"ОШИБКА: у бота нет прав в канале: {', '.join(missing)} "
                "(нужны View Channel, Connect, Speak, Use Voice Activity).",
                file=sys.stderr,
            )
            return 6
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=30)
        probe.vc = vc
        print(f"Вошёл в канал «{channel.name}». Говорите в канале; проба идёт {args.seconds} с.", flush=True)
        print(format_dave(probe.dave_info()), flush=True)
        vc.listen(build_sink(probe.handle_opus, lambda: probe.vc))

        async def play_test():
            await asyncio.sleep(2)
            for label, pcm in (("тон 1 с", make_tone_pcm(440.0, 1.0)), ("фраза", make_phrase_pcm())):
                print(f"   играю: {label}", flush=True)
                vc.play(discord.PCMAudio(io.BytesIO(pcm)))
                while vc.is_playing():
                    await asyncio.sleep(0.1)
                await asyncio.sleep(0.5)

        player = asyncio.create_task(play_test())
        t0 = time.monotonic()
        while time.monotonic() - t0 < args.seconds:
            await asyncio.sleep(1)
            with probe.lock:
                snap = {u: s.snapshot() for u, s in probe.stats.items()}
            print(format_report(snap, probe.dave_info(), time.monotonic() - t0), flush=True)
        player.cancel()
        vc.stop()
        await vc.disconnect()
        print(verdict(probe.stats))
        return 0
    except Exception as e:
        print("ОШИБКА: " + describe_connect_error(e), file=sys.stderr)
        return 1
    finally:
        await client.close()


# --- запуск --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe.py",
        description="Проба голоса Discord: вход, приём расшифрованного (DAVE) звука участников, воспроизведение. "
        "Токен читается из DISCORD_BOT_TOKEN.",
    )
    p.add_argument("--guild", type=int, required=True, help="ID сервера")
    p.add_argument("--channel", type=int, required=True, help="ID голосового канала")
    p.add_argument("--seconds", type=int, default=60, help="длительность пробы, секунд (по умолчанию 60)")
    p.add_argument(
        "--save",
        action="store_true",
        help="сохранить по 5 с звука каждого участника в WAV 16 кГц моно (по умолчанию звук не сохраняется)",
    )
    p.add_argument("--save-dir", default="/tmp", help="куда класть WAV при --save (по умолчанию /tmp)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.seconds <= 0:
        print("ОШИБКА: --seconds должно быть больше 0.", file=sys.stderr)
        return 2
    problems = check_dependencies()
    if problems:
        for m in problems:
            print("ОШИБКА: " + m, file=sys.stderr)
        return 3
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
