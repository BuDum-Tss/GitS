"""Точка входа модуля. Запускает супервизор ядра (запись в agent/modules.yaml) или вручную: `python main.py`.

Ядро должно работать; токен клиент берёт из окружения или из файла ядра.
"""
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# свои файлы первыми; SDK до установки пакета gits-sdk. Внутренностей ядра (agent/) модуль не импортирует (ТЗ 31)
sys.path[:0] = [str(HERE), str(HERE.parent)]

from app import build_client  # noqa: E402
from manifest import build_manifest  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    build_client(build_manifest()).main()
