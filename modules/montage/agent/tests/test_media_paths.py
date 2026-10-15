"""Поиск ffmpeg и ffprobe (MON-2): на машине разработчика они есть."""
import os

from media import find_ffmpeg, find_ffprobe


def test_ffmpeg_and_ffprobe_found():
    assert os.path.isfile(find_ffmpeg())
    assert os.path.isfile(find_ffprobe())
