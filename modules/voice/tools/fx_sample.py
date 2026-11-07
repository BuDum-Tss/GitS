"""Образцы пресетов фильтра голоса для подбора на слух: python modules/voice/tools/fx_sample.py [файл.wav] [папка].

Без аргументов берёт референс голоса (`espeech_ref.wav` персонажа по умолчанию) и пишет `<папка>/<пресет>.wav` для каждого пресета
и `dry.wav` без обработки. Синтез и сервисы не нужны."""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

import voice_fx  # noqa: E402

DEFAULT_SRC = HERE.parents[1] / "resources" / "GlaDOS" / "voice" / "espeech_ref.wav"


def main(argv: list[str]) -> int:
    src = Path(argv[0]) if argv else DEFAULT_SRC
    out = Path(argv[1]) if len(argv) > 1 else Path.cwd() / "fx_samples"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, out / "dry.wav")
    for name in voice_fx.PRESETS:
        dst = out / f"{name}.wav"
        shutil.copy(src, dst)
        voice_fx.apply(str(dst), name)
        print(name, dst.stat().st_size, "байт")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
