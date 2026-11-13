"""Разбор JSON из ответа модели: общая чистая функция размышления, эпизодов и дневника."""
import json


def parse_step(raw: str) -> dict | None:
    """Первый JSON-объект из ответа модели (она может обрамить его пояснением)."""
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
