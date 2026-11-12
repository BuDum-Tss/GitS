"""Поддельный процесс быстрой модели pi на Python (тот же протокол, что у `services/agent-runtime/llm.mjs`), для `test_pi_llm.py`.

Вопрос вида `die` убивает процесс, `slow` отвечает долго, `fail` даёт `failed`, остальное: «эхо: <вопрос>» (с картинкой метка `+img`).
Запись о каждом `cancel` уходит в stderr строкой `cancel <id>`.
"""
import json
import os
import sys
import threading
import time


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def answer(m, cancelled):
    rid, user = m["id"], m["user"]
    if user == "die":
        os._exit(3)
    if user == "slow":
        for _ in range(100):
            if rid in cancelled:
                return
            time.sleep(0.05)
    if user == "fail":
        return send({"type": "failed", "id": rid, "error": "лимит исчерпан"})
    text = f"эхо: {user}" + (" +img" if m.get("image_b64") else "")
    if m.get("stream"):
        for word in text.split(" "):
            send({"type": "delta", "id": rid, "text": word + " "})
    send({"type": "done", "id": rid, "text": text if user != "empty" else "", "usage": {"input": 1, "output": 1}})


def main():
    cancelled = set()
    for line in sys.stdin:
        m = json.loads(line)
        kind = m.get("type")
        if kind == "init":
            send({"type": "ready"})
        elif kind == "complete":
            threading.Thread(target=answer, args=(m, cancelled), daemon=True).start()
        elif kind == "cancel":
            cancelled.add(m["id"])
            sys.stderr.write(f"cancel {m['id']}\n")
            sys.stderr.flush()
            send({"type": "failed", "id": m["id"], "error": "cancelled"})
        elif kind == "shutdown":
            return


if __name__ == "__main__":
    main()
