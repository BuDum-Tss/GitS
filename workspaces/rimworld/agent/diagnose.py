#!/usr/bin/env python3
"""Диагностика: проверить статус RimWorld и MCP сервера."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import httpx

    # Проверка MCP сервера
    print("=" * 60)
    print("🔍 ДИАГНОСТИКА: Статус RimWorld Agent Bridge")
    print("=" * 60)

    BASE = "http://localhost:8765"

    # Проверка ping
    try:
        r = httpx.get(f"{BASE}/ping", timeout=2)
        if r.status_code == 200:
            print("\n✓ MCP Сервер ЗАПУЩЕН и отвечает на ping")

            # Получить состояние
            r_state = httpx.get(f"{BASE}/state", timeout=5)
            state = r_state.json()
            print("\n📊 Состояние игры:")
            print(f"   Status: {state.get('status')}")
            print(f"   Tick: {state.get('tick', 'N/A')}")

            if state.get('status') not in ("no_game", "no_map", "initializing"):
                print("\n✓ ИГРА АКТИВНА И ГОТОВА К УПРАВЛЕНИЮ")
            else:
                print(f"\n⚠ Игра не в активном состоянии: {state.get('status')}")
        else:
            print(f"\n✗ MCP Сервер вернул ошибку: {r.status_code}")
    except Exception as e:
        print(f"\n✗ MCP Сервер НЕ отвечает: {e}")
        print(f"   Адрес: {BASE}")

    # Проверка файла game_ui
    game_ui_path = Path(__file__).parent / "game_ui.py"
    if game_ui_path.exists():
        print(f"\n✓ game_ui.py найден: {game_ui_path}")
    else:
        print("\n✗ game_ui.py НЕ найден")

except Exception as e:
    print(f"✗ Ошибка при диагностике: {e}")
