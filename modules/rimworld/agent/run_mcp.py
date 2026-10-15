#!/usr/bin/env python3
"""Запустить MCP сервер rimworld (из этой же папки, где бы ни лежал репозиторий)."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Запустить MCP сервер
result = subprocess.run([sys.executable, str(HERE / "rimworld_mcp.py")], cwd=str(HERE))
sys.exit(result.returncode)
