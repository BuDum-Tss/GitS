"""Путь к claude.exe для Claude Agent SDK (SDK может искать сам, но так надёжнее)."""
import logging

logger = logging.getLogger("claude_cli")


def _find_claude_exe() -> str | None:
    import glob
    patterns = [
        r"C:\Users\*\AppData\Local\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\*\claude.exe",
        r"C:\Users\*\AppData\Local\Programs\Claude\claude.exe",
        r"C:\Program Files\Claude\claude.exe",
        r"C:\Users\*\AppData\Local\uv\cache\archive-v0\*\claude_agent_sdk\_bundled\claude.exe",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=False))
        if matches:
            return matches[-1]
    return None


_CLI_PATH = _find_claude_exe()
if not _CLI_PATH:
    logger.warning("claude.exe не найден: SDK будет искать сам")
