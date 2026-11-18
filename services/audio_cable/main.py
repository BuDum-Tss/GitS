"""Точка входа адаптера «виртуальные кабели»: `python main.py --input CABLE-A --output CABLE-B` из папки сервиса.

Токен шины (GITS_TOKEN) и адрес движка (GITS_VOICE_URL или GITS_CORE_URL) берутся из окружения; секреты читаются из .env
(путь можно заменить переменной GITS_SECRETS_FILE). Токен нигде не печатается. `--list` печатает устройства и выходит.
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SDK до установки пакета gits-sdk; свои файлы первыми. Внутренностей ядра (agent/) адаптер не импортирует
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "agent" / "interfaces" / "workspace")]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(os.environ.get("GITS_SECRETS_FILE") or HERE.parent.parent / ".env"))   # секреты не в git; файла нет: не страшно

from cable_adapter import CableAdapter  # noqa: E402
from cable_io import DeviceError, LoopError, SoundcardIn, SoundcardOut, find_device, list_devices, same_cable  # noqa: E402

log = logging.getLogger("gits.audio_cable")
STATUS_EVERY_S = 60


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Голосовой адаптер «виртуальные кабели» для движка голоса ГЛаДОС")
    p.add_argument("--list", action="store_true", help="показать устройства и выйти")
    p.add_argument("--input", default=os.environ.get("GITS_CABLE_IN", ""),
                   help="подстрока имени устройства входа: звук собеседников (например «CABLE-A Output»); GITS_CABLE_IN")
    p.add_argument("--output", default=os.environ.get("GITS_CABLE_OUT", ""),
                   help="подстрока имени устройства выхода: её голос (например «CABLE-B Input»); GITS_CABLE_OUT")
    p.add_argument("--session", default="cable", help="id сессии в движке (по умолчанию cable)")
    p.add_argument("--name", default="Собеседник", help="имя говорящего в событиях")
    p.add_argument("--owner", action="store_true", help="говорящий считается владельцем")
    p.add_argument("--title", default="", help="название канала для событий (необязательно)")
    return p.parse_args(argv)


def print_devices() -> None:
    devices = list_devices()
    print("Выходы (колонки; сюда играем её голос, например CABLE-B Input):")
    for n in devices["speaker"]:
        print(f"  {n}")
    print("Входы (микрофоны; отсюда слушаем собеседников, например CABLE-A Output):")
    for n in devices["microphone"]:
        print(f"  {n}")


def build_adapter(args: argparse.Namespace) -> CableAdapter:
    """Находит устройства, отказывает при петле. Ошибка: `DeviceError` с понятным текстом."""
    if not args.input or not args.output:
        raise DeviceError("задай --input и --output (или GITS_CABLE_IN и GITS_CABLE_OUT); список устройств: --list")
    mic = find_device("microphone", args.input)
    spk = find_device("speaker", args.output)
    if same_cable(mic.name, spk.name):
        raise LoopError(f"вход «{mic.name}» и выход «{spk.name}» это один кабель: она услышит себя")
    return CableAdapter(SoundcardIn(mic), SoundcardOut(spk), session=args.session, name=args.name, is_owner=args.owner,
                        title=args.title)


async def amain(args: argparse.Namespace) -> int:
    try:
        adapter = build_adapter(args)
        await adapter.start()
    except DeviceError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    log.info("кабель: вход «%s», выход «%s», сессия %s", adapter.audio_in.device_name, adapter.audio_out.device_name, args.session)
    try:
        while True:
            await asyncio.sleep(STATUS_EVERY_S)
            log.info("кабель: %s", adapter.status())
    finally:
        await adapter.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    args = parse_args()
    if args.list:
        try:
            print_devices()
        except DeviceError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(2)
        return
    try:
        code = asyncio.run(amain(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
