"""LanguageModel поверх локальной модели (LM Studio): `fast_brain._ask`."""
import fast_brain


class LMStudioLLM:
    async def ask(self, system: str, user: str, *, timeout: float, max_tokens: int = 40, temperature: float = 0.3,
                  background: bool = False, image_b64: str | None = None) -> str:
        return await fast_brain._ask(system, user, timeout=timeout, max_tokens=max_tokens, image_b64=image_b64,
                                     temperature=temperature, background=background)
