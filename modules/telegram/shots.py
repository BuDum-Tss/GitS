"""Скриншоты для черновика поста (ТЗ 10, TG-6): общая папка MEMORY_DIR/screenshots/.

Файлы пишет ядро (до ТЗ 14) или модуль desktop (после). Модуль их только читает и ничего в папке не создаёт.
"""
import os
from pathlib import Path


class Shots:
    def __init__(self, memory_dir: str):
        self.dir = Path(memory_dir) / "screenshots"

    def resolve(self, name: str) -> Path | None:
        """Имя файла -> путь внутри папки снимков (только имя, без путей)."""
        p = self.dir / os.path.basename(name)
        return p if p.is_file() else None

    def last(self) -> Path | None:
        files = sorted(self.dir.glob("shot_*.*"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None
