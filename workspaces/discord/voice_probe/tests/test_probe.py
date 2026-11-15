"""Тесты пробы голоса без сети и без discord.py."""

import importlib.util
import math
import struct
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "probe.py"
_spec = importlib.util.spec_from_file_location("voice_probe_probe", _PATH)
probe = importlib.util.module_from_spec(_spec)
sys.modules["voice_probe_probe"] = probe
_spec.loader.exec_module(probe)


def _stereo(samples):
    """Список (L, R) -> байты s16le."""
    return b"".join(struct.pack("<hh", left, right) for left, right in samples)


def test_args_defaults_and_required():
    a = probe.build_parser().parse_args(["--guild", "1", "--channel", "2"])
    assert (a.guild, a.channel, a.seconds, a.save, a.save_dir) == (1, 2, 60, False, "/tmp")
    b = probe.build_parser().parse_args(["--guild", "1", "--channel", "2", "--seconds", "5", "--save", "--save-dir", "/x"])
    assert (b.seconds, b.save, b.save_dir) == (5, True, "/x")
    with pytest.raises(SystemExit):
        probe.build_parser().parse_args(["--guild", "1"])


def test_help_runs_without_discord(capsys):
    with pytest.raises(SystemExit) as e:
        probe.main(["--help"])
    assert e.value.code == 0
    assert "DISCORD_BOT_TOKEN" in capsys.readouterr().out


def test_main_rejects_bad_seconds(capsys):
    assert probe.main(["--guild", "1", "--channel", "2", "--seconds", "0"]) == 2
    assert "--seconds" in capsys.readouterr().err


def test_check_dependencies_reports_missing():
    def importer(name):
        if name in ("nacl", "davey"):
            raise ImportError(name)
        return object()

    msgs = probe.check_dependencies(importer)
    assert len(msgs) == 2
    assert any("PyNaCl" in m for m in msgs)
    assert any("davey" in m for m in msgs)
    assert probe.check_dependencies(lambda n: object()) == []


def test_main_exits_3_when_package_missing(monkeypatch, capsys):
    monkeypatch.setattr(probe, "check_dependencies", lambda: ["нет davey: тест"])
    assert probe.main(["--guild", "1", "--channel", "2"]) == 3
    assert "нет davey" in capsys.readouterr().err


def test_describe_connect_error():
    class LoginFailure(Exception):
        pass

    class ConnectionClosed(Exception):
        code = 4014

    assert "токен" in probe.describe_connect_error(LoginFailure("x"))
    assert "Connect" in probe.describe_connect_error(ConnectionClosed())
    assert "ValueError" in probe.describe_connect_error(ValueError("boom"))


def test_downmix_length_and_values():
    # 960 стерео-пар (20 мс) -> 320 отсчётов 16 кГц моно
    pcm = _stereo([(3000, 1000)] * 960)
    out = probe.pcm48_stereo_to_16k_mono(pcm)
    assert len(out) == 320 * 2
    assert set(struct.unpack("<320h", out)) == {2000}


def test_downmix_averages_three_samples_and_ignores_partial_tail():
    pcm = _stereo([(0, 0), (300, 300), (600, 600)]) + b"\x01\x02"  # хвост не кратен паре
    assert struct.unpack("<h", probe.pcm48_stereo_to_16k_mono(pcm)) == (300,)
    assert probe.pcm48_stereo_to_16k_mono(b"") == b""


def test_rms():
    assert probe.rms_s16(b"") == 0.0
    assert probe.rms_s16(struct.pack("<4h", 100, -100, 100, -100)) == pytest.approx(100.0)
    tone = probe.make_tone_pcm(440.0, 0.5, amplitude=0.5)
    mono = probe.pcm48_stereo_to_16k_mono(tone)
    # RMS синуса = амплитуда / sqrt(2); децимация усреднением слегка срезает уровень
    assert probe.rms_s16(mono) == pytest.approx(32767 * 0.5 / math.sqrt(2), rel=0.1)


def test_tone_format():
    pcm = probe.make_tone_pcm(440.0, 1.0)
    assert len(pcm) == 48000 * 2 * 2
    assert len(probe.make_phrase_pcm()) % 4 == 0


def test_report_format():
    s1 = probe.UserStats(packets=50, decoded=48, failed=2)
    s1.rms_sum, s1.rms_n = 4800.0, 48
    s1.errors = {"DaveSessionNotReady": 2}
    s2 = probe.UserStats(packets=10)
    text = probe.format_report({222: s2, 111: s1}, {"protocol_version": 1, "ready": True, "status": "active"}, 3.0)
    assert "DAVE: версия протокола=1, ready=да, статус=active" in text
    assert "111: пакетов=50 расшифровано=48 сбоев=2 rms=100 ошибки: DaveSessionNotReadyx2" in text
    assert text.index("111:") < text.index("222:")
    assert "участников со звуком пока нет" in probe.format_report({}, {}, 1.0)


def test_verdict():
    ok = probe.UserStats(packets=5, decoded=5)
    bad = probe.UserStats(packets=5, decoded=0, failed=5)
    assert "УСПЕХ" in probe.verdict({1: ok})
    assert "ЧАСТИЧНО" in probe.verdict({1: ok, 2: bad})
    assert "ПРОВАЛ" in probe.verdict({2: bad})
    assert "никого" in probe.verdict({})


def test_snapshot_is_independent():
    s = probe.UserStats()
    s.add_error("X")
    snap = s.snapshot()
    s.add_error("X")
    assert snap.errors == {"X": 1} and s.errors == {"X": 2}
