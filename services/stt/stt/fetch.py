"""Разовая загрузка весов в кэш Hugging Face: `python -m stt.fetch [gigaam] [whisper] [--check]`.

Сам сервис ничего не скачивает (HF_HUB_OFFLINE=1, кэш смонтирован только для чтения). Эта команда нужна, только если готового кэша
Hugging Face на хосте нет: она кладёт веса в каталог HF_HOME (в контейнере это том stt_models, см. README). Без аргументов качает
обе модели (GigaAM и запасной Whisper). `--check` ничего не качает: показывает, какие файлы уже есть в кэше (код возврата 1, если
нужных нет).
"""
import argparse
import os
import sys

if "--check" not in sys.argv:
    os.environ["HF_HUB_OFFLINE"] = "0"             # здесь сеть как раз нужна
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from .config import SttConfig  # noqa: E402
from .engine import gigaam_encoder_name  # noqa: E402


def gigaam_files(model: str) -> list[str]:
    """Файлы GigaAM, которые читает onnx-asr (int8, RNN-T или CTC): конфигурация, три части сети и словарь."""
    prefix = model.removeprefix("gigaam-").replace("-", "_")
    if prefix.endswith("ctc"):
        return ["config.json", f"{prefix}.int8.onnx", f"{prefix}_vocab.txt"]
    return ["config.json", gigaam_encoder_name(model), f"{prefix}_decoder.int8.onnx", f"{prefix}_joint.int8.onnx", f"{prefix}_vocab.txt"]


WHISPER_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def whisper_repo(model: str) -> str:
    return model if "/" in model else f"Systran/faster-whisper-{model}"


def check(repo: str, files: list[str]) -> bool:
    from huggingface_hub import try_to_load_from_cache
    ok = True
    for name in files:
        found = isinstance(try_to_load_from_cache(repo, name), str)
        print(f"  {'есть ' if found else 'НЕТ  '} {repo}: {name}")
        ok = ok and found
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stt.fetch", description=__doc__.split("\n")[0])
    ap.add_argument("what", nargs="*", choices=["gigaam", "whisper"], default=[], help="что качать (по умолчанию обе модели)")
    ap.add_argument("--check", action="store_true", help="только проверить кэш, ничего не качать")
    args = ap.parse_args(argv)
    what = set(args.what) or {"gigaam", "whisper"}
    cfg = SttConfig.from_env()
    print(f"Кэш Hugging Face: {os.environ.get('HF_HOME', '(по умолчанию ~/.cache/huggingface)')}")
    status = True
    if "gigaam" in what:
        files = gigaam_files(cfg.gigaam_model)
        if args.check:
            status = check(cfg.gigaam_repo, files) and status
        else:
            from huggingface_hub import snapshot_download
            print(f"Качаю {cfg.gigaam_repo}: {', '.join(files)}")
            snapshot_download(cfg.gigaam_repo, allow_patterns=files)
    if "whisper" in what:
        repo = whisper_repo(cfg.whisper_model)
        if args.check:
            status = check(repo, ["config.json", "model.bin", "tokenizer.json"]) and status
        else:
            from huggingface_hub import snapshot_download
            print(f"Качаю {repo}")
            snapshot_download(repo, allow_patterns=WHISPER_PATTERNS)
    if not args.check:
        print("Готово.")
    return 0 if status else 1


if __name__ == "__main__":
    sys.exit(main())
