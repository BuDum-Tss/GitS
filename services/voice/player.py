"""Воспроизведение аудиофайла (ТЗ 12, VOICE-1).

`PowerShellPlayer` играет WAV/MP3 через PresentationCore в скрытом PowerShell, как прежняя озвучка питомца, и ждёт реальной
длительности трека: сегменты звучат по очереди, а не перекрываются. Отмена задачи `play` убивает процесс воспроизведения
(обрыв «сразу», `speak.stop at: now`, перебивание). Громкость задаётся на каждый сегмент.
"""
import asyncio
import shutil
import subprocess
import sys

from opus_encoder import find_ffmpeg

# Заголовок окна ffplay: по нему OBS ловит звук только голоса («Захват звука приложения»: GladosVoice:SDL_app:ffplay.exe)
FFPLAY_TITLE = "GladosVoice"


def build_script(path: str, volume: float) -> str:
    uri = "file:///" + path.replace("\\", "/").replace("'", "''")
    return (
        "Add-Type -AssemblyName PresentationCore;"
        f"$mp=New-Object system.windows.media.mediaplayer;"
        f"$mp.Open([System.Uri]::new('{uri}'));"
        f"$mp.Volume={max(0.0, min(1.0, volume)):.2f};"
        "$mp.Play();"
        "for ($i=0; $i -lt 100 -and -not $mp.NaturalDuration.HasTimeSpan; $i++) { Start-Sleep -Milliseconds 100 };"
        "if ($mp.NaturalDuration.HasTimeSpan) {"
        "  Start-Sleep -Milliseconds ([int]$mp.NaturalDuration.TimeSpan.TotalMilliseconds + 400)"
        "} else {"
        "  Start-Sleep -Milliseconds 15000"
        "};"
        "$mp.Close()"
    )


def find_ffplay() -> str | None:
    """ffplay рядом с ffmpeg (в PATH или в папке ffmpeg): плеер голоса для отдельного захвата звука в OBS; нет: None."""
    found = shutil.which("ffplay")
    if found:
        return found
    try:
        from pathlib import Path
        candidate = Path(find_ffmpeg()).with_name("ffplay.exe")
        return str(candidate) if candidate.exists() else None
    except Exception:
        return None


def ffplay_args(exe: str, path: str, volume: float) -> list[str]:
    """Аргументы ffplay: окно 16x16 за краем экрана без рамки, чтобы OBS видел процесс, а зритель на ПК окно не видел."""
    return [exe, "-hide_banner", "-loglevel", "quiet", "-autoexit", "-window_title", FFPLAY_TITLE, "-x", "16", "-y", "16",
            "-noborder", "-left", "-3000", "-top", "-3000", "-volume", str(int(round(max(0.0, min(1.0, volume)) * 100))), path]


class _ProcPlayer:
    """Общая часть: запустить процесс воспроизведения и ждать конца; отмена убивает процесс."""

    def command(self, path: str, volume: float) -> list[str]:
        raise NotImplementedError

    async def play(self, path: str, volume: float) -> None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = await asyncio.create_subprocess_exec(*self.command(path, volume), stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL, creationflags=flags)
        try:
            await proc.wait()
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise


class FfplayPlayer(_ProcPlayer):
    """Воспроизведение через ffplay: звук голоса можно захватить в OBS отдельно от остального звука ПК."""

    def __init__(self, exe: str):
        self.exe = exe

    def command(self, path: str, volume: float) -> list[str]:
        return ffplay_args(self.exe, path, volume)


def default_player():
    """ffplay, если он есть (отдельный звук для OBS), иначе PowerShell."""
    exe = find_ffplay()
    return FfplayPlayer(exe) if exe else PowerShellPlayer()


class PowerShellPlayer(_ProcPlayer):
    """Реальное воспроизведение (Windows)."""

    def command(self, path: str, volume: float) -> list[str]:
        return ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", build_script(path, volume)]
