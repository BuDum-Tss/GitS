"""Воспроизведение аудиофайла (ТЗ 12, VOICE-1).

`PowerShellPlayer` играет WAV/MP3 через PresentationCore в скрытом PowerShell, как прежняя озвучка питомца, и ждёт реальной
длительности трека: сегменты звучат по очереди, а не перекрываются. Отмена задачи `play` убивает процесс воспроизведения
(обрыв «сразу», `speak.stop at: now`, перебивание). Громкость задаётся на каждый сегмент.
"""
import asyncio
import subprocess
import sys


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


class PowerShellPlayer:
    """Реальное воспроизведение (Windows)."""

    async def play(self, path: str, volume: float) -> None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", build_script(path, volume),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=flags)
        try:
            await proc.wait()
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise
