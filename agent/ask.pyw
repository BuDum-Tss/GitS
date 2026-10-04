"""Отправить сообщение ассистенту из командной строки или из PowerToys Run.

Из строки PowerToys Run (плагин «Оболочка», префикс >):
    > pythonw "C:/MyProjects/rimworld-agent/agent/ask.pyw" привет, что нового?
Сообщение уходит так же, как из чата в дашборде; ответ придёт облачком питомца,
в чат и голосом. Расширение .pyw нужно, чтобы не мигало окно консоли.
"""
import sys

import httpx

text = " ".join(sys.argv[1:]).strip()
if text:
    try:
        httpx.post("http://127.0.0.1:8766/api/chat", json={"message": text}, timeout=5)
    except Exception:
        pass   # ядро не запущено: сообщению некуда идти
