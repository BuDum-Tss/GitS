"""Путь к claude.exe для Claude Agent SDK (SDK может искать сам, но так надёжнее).

Поиск живёт в SDK (`gits_sdk.claude_path`, ТЗ 31 MC-4): им же пользуются модули.
"""
from gits_sdk.claude_path import CLI_PATH as _CLI_PATH  # noqa: F401
from gits_sdk.claude_path import locate as _find_claude_exe  # noqa: F401
