#!/usr/bin/env python3
"""Быстрый тест: снять скриншот окна RimWorld и сохранить его."""
import sys
from pathlib import Path

# Убедимся что game_ui доступен
sys.path.insert(0, str(Path(__file__).parent))

try:
    import game_ui

    # Получить скриншот
    png_bytes = game_ui.screenshot_png()

    # Сохранить на диск
    output_path = Path(__file__).parent / "last_screenshot.png"
    with open(output_path, "wb") as f:
        f.write(png_bytes)

    print(f"✓ Скриншот сохранён: {output_path}")

except RuntimeError as e:
    print(f"✗ Ошибка: {e}")
    sys.exit(1)
except Exception as e:
    print(f"✗ Непредвиденная ошибка: {e}")
    sys.exit(1)
