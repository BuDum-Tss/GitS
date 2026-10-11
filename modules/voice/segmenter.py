"""Разбивка реплики на предложения для озвучки по одному (VOICE-3).

Перенесено из `desktop_pet.py` без изменений: сегменты озвучиваются по очереди, облачко питомца показывает звучащее.
"""
import re


def split_sentences(text: str) -> list[str]:
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p for p in parts if p]
