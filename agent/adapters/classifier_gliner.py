"""Classifier поверх классификатора внимания (`decide.Decider`, сервис GLiNER на :8768)."""
import decide


class GlinerClassifier:
    def __init__(self, decider: decide.Decider | None = None):
        self._decider = decider if decider is not None else decide.Decider()

    @property
    def last_ms(self) -> float | None:
        return self._decider.last_ms

    async def classify(self, text: str, questions: dict, *, timeout: float) -> dict | None:
        return await self._decider.classify(text, questions, timeout=timeout)
