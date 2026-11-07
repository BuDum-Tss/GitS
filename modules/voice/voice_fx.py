"""Фильтр под голос персонажа: обработка готового WAV цепочкой ffmpeg после синтеза (любой движок: ESpeech, Fish, edge-tts).

Пресет задаёт настройка `tts_fx` (off | light | glados | heavy). Обработка блокирующая: звать через `asyncio.to_thread` (сам
синтез так и вызывается). Любая ошибка ffmpeg оставляет исходный файл: голос не пропадает из-за фильтра."""
import logging
import os
import subprocess
import tempfile

from opus_encoder import find_ffmpeg

logger = logging.getLogger("voice_fx")

# цепочки подбираются на слух: `python modules/voice/tools/fx_sample.py` складывает образцы всех пресетов в одну папку
PRESETS: dict[str, str] = {
    # лёгкий холодок: чуть выше тон, короткое металлическое эхо
    "light": "highpass=f=100,rubberband=pitch=1.03,aecho=0.8:0.7:7:0.18,alimiter=limit=0.9",
    # машинный голос: тон выше, тонкий корпус, хорус и короткое эхо как «металл», лёгкий цифровой шум, подъём около 3 кГц
    "glados": ("highpass=f=140,lowpass=f=8500,rubberband=pitch=1.05,"
               "chorus=0.7:0.9:35|50:0.3|0.25:0.3|0.4:1.5|2,aecho=0.8:0.75:5|11:0.3|0.2,"
               "acrusher=bits=13:mode=log:aa=1:mix=0.18,equalizer=f=2800:t=q:w=1:g=3,alimiter=limit=0.9"),
    # сильнее: ниже разрядность, заметный хорус и фазер
    "heavy": ("highpass=f=170,lowpass=f=7500,rubberband=pitch=1.07,"
              "chorus=0.6:0.9:25|40|60:0.4|0.3|0.25:0.25|0.35|0.4:1.2|1.8|2.5,aphaser=type=t:speed=0.4:decay=0.4,"
              "aecho=0.8:0.8:4|9|14:0.35|0.25|0.15,acrusher=bits=10:mode=log:aa=1:mix=0.3,"
              "equalizer=f=3000:t=q:w=1:g=4,alimiter=limit=0.9"),
}
OFF = "off"
_preset = "glados"


def set_preset(name) -> None:
    """Пресет из настройки `tts_fx`: неизвестное имя и мусор значат «выключено»."""
    global _preset
    name = str(name or OFF).strip().lower()
    _preset = name if name in PRESETS else OFF


def preset() -> str:
    return _preset


def apply(path: str, name: str | None = None) -> str:
    """Обработать WAV на месте и вернуть путь (тот же файл). Выключено или ошибка: файл не тронут."""
    name = (name or _preset)
    chain = PRESETS.get(name)
    if not chain or not path or not os.path.exists(path):
        return path
    out = None
    try:
        fd, out = tempfile.mkstemp(suffix=os.path.splitext(path)[1] or ".wav", prefix="gits_fx_", dir=os.path.dirname(path))
        os.close(fd)
        subprocess.run([find_ffmpeg(), "-y", "-loglevel", "error", "-i", path, "-af", chain, "-ar", "44100", "-ac", "1", out],
                       check=True, capture_output=True, timeout=30)
        os.replace(out, path)
        out = None
    except Exception as e:
        err = getattr(e, "stderr", b"") or b""
        logger.warning(f"Фильтр голоса «{name}» не применён: {type(e).__name__}: {err.decode(errors='replace')[-200:] or e}")
    finally:
        if out and os.path.exists(out):
            os.remove(out)
    return path
