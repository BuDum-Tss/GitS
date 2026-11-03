"""Быстрый запуск мира RimWorld для разработки: python modules/rimworld/tools/quick_world.py [--restart] [--timeout 300].

Запускает игру (если не запущена; --restart закрывает и запускает заново, чтобы подхватить новый мод), ждёт главное меню,
жмёт «Быстрый запуск (Dev)» и ждёт, пока мод ответит состоянием колонии. Выход 0, когда колония готова."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "agent"), os.path.join(HERE, "..", "..", "..", "sdk")]
EXE = r"C:\GOG Games\RimWorld\RimWorldWin64.exe"
URL = "http://localhost:8765/command"


def in_colony() -> bool:
    req = urllib.request.Request(URL, data=b'{"type":"ui_state"}', headers={"Content-Type": "application/json"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=5))
        return d.get("status") == "ok" and '"pawns"' in d.get("result", "")
    except Exception:
        return False


def running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq RimWorldWin64.exe"], capture_output=True, text=True).stdout
    return "RimWorldWin64" in out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    if args.restart and running():
        subprocess.run(["taskkill", "/F", "/IM", "RimWorldWin64.exe"], capture_output=True)
        time.sleep(3)
    if in_colony():
        print("колония уже идёт")
        return 0
    if not running():
        subprocess.Popen([EXE])
        print("игра запущена")
    import game_watchdog
    end = time.time() + args.timeout
    while time.time() < end:
        if in_colony():
            print("колония готова")
            return 0
        try:
            if game_watchdog.press_quick_start():
                print("нажат «Быстрый запуск (Dev)»")
                time.sleep(25)            # генерация карты
        except Exception as e:
            print("ждём меню:", e)
        time.sleep(5)
    print("не дождались колонии")
    return 1


if __name__ == "__main__":
    sys.exit(main())
