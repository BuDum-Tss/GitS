"""MONTAGE_OUT_DIR, рабочие папки и пути модуля в контейнере (CHARACTER_HOME), поиск программ в PATH. Docker не нужен."""
import importlib
import os
import shutil
import stat

import manifest
import media
from layout import Layout


def test_output_dir_default_without_env(monkeypatch):
    monkeypatch.delenv("MONTAGE_OUT_DIR", raising=False)
    assert manifest.default_output_dir() == manifest.WINDOWS_OUTPUT_DIR
    assert manifest.resolve_output_dir(None) == os.path.expandvars(manifest.WINDOWS_OUTPUT_DIR)
    assert manifest.resolve_output_dir("D:/clips") == "D:/clips"


def test_output_dir_from_env(monkeypatch):
    monkeypatch.setenv("MONTAGE_OUT_DIR", "/data/montage/out")
    assert manifest.default_output_dir() == "/data/montage/out"
    assert manifest.resolve_output_dir(None) == "/data/montage/out"
    assert manifest.resolve_output_dir(manifest.WINDOWS_OUTPUT_DIR) == "/data/montage/out"   # устаревшее значение по умолчанию
    assert manifest.resolve_output_dir("/mnt/custom") == "/mnt/custom"                       # явная настройка главнее


def test_serve_dirs_use_env_out_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MONTAGE_OUT_DIR", str(tmp_path / "out"))
    dirs = manifest.serve_dirs(tmp_path / "jobs")
    assert dirs == [os.path.abspath(tmp_path / "jobs"), os.path.abspath(tmp_path / "out")]


def test_manifest_default_follows_env_at_import(monkeypatch):
    monkeypatch.setenv("MONTAGE_OUT_DIR", "/data/montage/out")
    try:
        fresh = importlib.reload(manifest)
        assert fresh.MANIFEST["settings_schema"]["output_dir"]["default"] == "/data/montage/out"
    finally:
        monkeypatch.delenv("MONTAGE_OUT_DIR")
        importlib.reload(manifest)


def test_layout_paths_in_container(monkeypatch, tmp_path):
    home = tmp_path / "data"
    (home / "core").mkdir(parents=True)
    (home / "core" / "character.yaml").write_text("id: glados\n", encoding="utf-8")
    monkeypatch.setenv("CHARACTER_HOME", str(home))
    monkeypatch.setenv("MONTAGE_JOBS_DIR", str(tmp_path / "work" / "jobs"))
    layout = Layout()
    assert layout.character == home / "montage"
    assert layout.assets == home / "montage" / "assets"
    assert layout.style == home / "montage" / "APERTURE_STYLE.md"
    assert layout.jobs == tmp_path / "work" / "jobs"                      # рабочая папка в томе, не рядом с кодом


def test_tool_lookup_uses_path(monkeypatch, tmp_path):
    """В контейнере ffmpeg, ffprobe и npx лежат в PATH: поиск не ходит по путям Windows."""
    for name in ("ffmpeg", "ffprobe", "npx"):
        f = tmp_path / name
        f.write_text("#!/bin/sh\n", encoding="utf-8")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: str(tmp_path / n) if (tmp_path / n).exists() else None)
    assert media.find_ffmpeg() == str(tmp_path / "ffmpeg")
    assert media.find_ffprobe() == str(tmp_path / "ffprobe")
    assert media.find_npx() == str(tmp_path / "npx")


def test_kill_processes_under_is_noop_outside_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(media.sys, "platform", "linux")
    assert media.kill_processes_under(tmp_path) == 0
