#!/usr/bin/env python3
"""
Простая навигация по меню RimWorld для попадания в активную игру.
Использует game_ui напрямую.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import game_ui

def navigate_to_game():
    """Попадаем в активную игру, ища Quick Start, затем Continue, затем Load."""
    print("🔍 Делаю скриншот главного меню...")

    # Первый скриншот
    png_bytes = game_ui.screenshot_png()
    output_path = Path(__file__).parent / "screenshot_menu.png"
    with open(output_path, "wb") as f:
        f.write(png_bytes)

    print(f"✓ Скриншот сохранён: {output_path}")
    print("  Анализирую экран...")

    # Здесь должен быть анализ скриншота
    # На данный момент просто даём информацию
    print("""
    Для анализа скриншота нужно:
    1. Проверить есть ли кнопка "Quick Start (Dev)"
    2. Если нет - искать кнопку "Continue"
    3. Если нет - открыть Load и выбрать свежий сейв

    Файл скриншота сохранён и готов к анализу.
    """)

    return output_path

if __name__ == "__main__":
    try:
        result = navigate_to_game()
        print(f"✓ Скриншот готов: {result}")
    except Exception as e:
        print(f"✗ Ошибка: {e}")
        sys.exit(1)
