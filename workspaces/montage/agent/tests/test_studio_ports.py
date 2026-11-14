"""Порты Remotion Studio: в контейнере из опубликованного диапазона, иначе любой свободный."""
import socket

import pytest
import studio


def test_without_the_variable_any_free_port(monkeypatch):
    monkeypatch.delenv(studio.PORTS_ENV, raising=False)
    assert studio.free_port() > 0


def test_port_comes_from_the_range_and_skips_busy_ones(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        base = probe.getsockname()[1]
    monkeypatch.setenv(studio.PORTS_ENV, f"{base}-{base + 2}")
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", base))
        assert studio.free_port() == base + 1                       # первый занят: берётся следующий
        with socket.socket() as busy2:
            busy2.bind(("0.0.0.0", base + 1))
            with socket.socket() as busy3:
                busy3.bind(("0.0.0.0", base + 2))
                with pytest.raises(studio.StudioError):
                    studio.free_port()


def test_broken_range_falls_back_to_any_port(monkeypatch):
    monkeypatch.setenv(studio.PORTS_ENV, "не-порт")
    assert studio.free_port() > 0
