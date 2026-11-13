"""Путь к claude.exe для Claude Agent SDK (ТЗ 31, MC-4: модули не импортируют `agent/claude_cli.py`).

`locate()` ищет Claude в обычных местах установки; `CLI_PATH` считается один раз при импорте. Ядро отдаёт тот же ответ
запросом `core.claude.locate`.
"""
from __future__ import annotations

import glob
import logging

log = logging.getLogger("gits_sdk.claude_path")

PATTERNS = (
    r"C:\Users\*\AppData\Local\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\*\claude.exe",
    r"C:\Users\*\AppData\Local\Programs\Claude\claude.exe",
    r"C:\Program Files\Claude\claude.exe",
    r"C:\Users\*\AppData\Local\uv\cache\archive-v0\*\claude_agent_sdk\_bundled\claude.exe",
)


def locate() -> str | None:
    """Путь к claude.exe или None (тогда SDK Claude ищет сам)."""
    for pattern in PATTERNS:
        matches = sorted(glob.glob(pattern, recursive=False))
        if matches:
            return matches[-1]
    return None


CLI_PATH = locate()
if not CLI_PATH:
    log.warning("claude.exe не найден: SDK будет искать сам")
