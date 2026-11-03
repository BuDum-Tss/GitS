"""LanguageModel поверх быстрой модели (Claude без инструментов): `fast_brain._ask`; потоком: `fast_brain._ask_stream`."""
import fast_brain


class ClaudeLLM:
    async def ask(self, system: str, user: str, *, timeout: float, max_tokens: int = 40, temperature: float = 0.3,
                  background: bool = False, image_b64: str | None = None, parallel: bool = False) -> str:
        return await fast_brain._ask(system, user, timeout=timeout, max_tokens=max_tokens, image_b64=image_b64,
                                     temperature=temperature, background=background)

    def ask_stream(self, system: str, user: str, *, timeout: float, max_tokens: int = 140, temperature: float = 0.7):
        """Ответ потоком кусков текста (асинхронный генератор)."""
        return fast_brain._ask_stream(system, user, timeout=timeout, max_tokens=max_tokens, temperature=temperature)
