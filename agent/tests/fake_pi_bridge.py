"""Поддельный мост среды pi для тестов `adapters/runtime_pi.py`: говорит по протоколу `services/agent-runtime` (строки JSON),
поведение задаёт текст запроса. Без сети, Node и моделей.

Запросы: `hello` (текст и конец), `tool` (вызов find_tools, ждёт tool_result), `slowtool` (вызов и ожидание), `wait` (ждёт
steer или abort), `system` (отвечает текущей системной подсказкой), `die` (процесс умирает), `big` (очень длинная строка).
"""
import json
import os
import sys


def out(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    system, session_file, run, waiting = "", None, None, None
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        kind = msg.get("type")
        if kind == "init":
            system, session_file = msg.get("system", ""), msg.get("session_file")
            auth = msg.get("auth_file") or ""
            if msg.get("provider") == "openai" and not os.path.exists(auth):
                out({"type": "error", "fatal": True, "message": "нет входа: выполни npx @earendil-works/pi-ai login openai"})
                return
            restored = 0
            if session_file and os.path.exists(session_file):
                restored = int(open(session_file, encoding="utf-8").read() or 0)
            sys.stderr.write("fake bridge ready\n")
            sys.stderr.flush()
            out({"type": "ready", "restored": restored})
        elif kind == "prompt":
            if run is not None:
                out({"type": "error", "id": msg.get("id"), "message": "busy", "fatal": False})
                continue
            run, text = msg["id"], msg.get("text", "")
            if text == "hello":
                out({"type": "text", "id": run, "text": "Привет"})
                end(run, session_file)
                run = None
            elif text in ("tool", "slowtool"):
                waiting = "c1"
                out({"type": "tool_call", "id": run, "call_id": "c1", "name": "find_tools", "args": {"query": text}})
            elif text == "wait":
                waiting = "steer"
                out({"type": "text", "id": run, "text": "работаю"})
            elif text == "system":
                out({"type": "text", "id": run, "text": "system=" + system})
                end(run, session_file)
                run = None
            elif text == "big":
                out({"type": "text", "id": run, "text": "x" * 300000})
                end(run, session_file)
                run = None
            elif text == "die":
                os._exit(3)
        elif kind == "tool_result" and run is not None and waiting == "c1":
            out({"type": "text", "id": run, "text": f"ok={msg.get('ok')} {msg.get('text')}"})
            end(run, session_file)
            run, waiting = None, None
        elif kind == "steer":
            if run is None:
                out({"type": "error", "message": "idle", "fatal": False})
            elif waiting == "steer":
                out({"type": "text", "id": run, "text": "подмешано: " + msg.get("text", "")})
                end(run, session_file)
                run, waiting = None, None
        elif kind == "abort":
            if run is not None:
                out({"type": "end", "id": run, "status": "aborted", "error": None,
                     "usage": {"requests": 1, "input": 0, "output": 0, "cache_read": 0}})
                run, waiting = None, None
        elif kind == "set_system":
            system = msg.get("system", "")
        elif kind == "reset":
            if session_file and os.path.exists(session_file):
                os.remove(session_file)
            out({"type": "reset_done"})
        elif kind == "shutdown":
            return


def end(run, session_file):
    if session_file:
        n = 0
        if os.path.exists(session_file):
            n = int(open(session_file, encoding="utf-8").read() or 0)
        open(session_file, "w", encoding="utf-8").write(str(n + 1))
    out({"type": "end", "id": run, "status": "ok", "error": None,
         "usage": {"requests": 1, "input": 5, "output": 7, "cache_read": 0}})


if __name__ == "__main__":
    main()
