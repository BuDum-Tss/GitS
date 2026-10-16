"""LanguageModel поверх локальной модели (LM Studio): `fast_brain._ask`; StreamingLanguageModel: `fast_brain._ask_stream`."""
import fast_brain


class LMStudioLLM:
    async def ask(self, system: str, user: str, *, timeout: float, max_tokens: int = 40, temperature: float = 0.3,
                  background: bool = False, image_b64: str | None = None, parallel: bool = False) -> str:
        extra = {"parallel": True} if parallel else {}        # мгновенный уровень (ТЗ 25): мимо блокировки модели
        return await fast_brain._ask(system, user, timeout=timeout, max_tokens=max_tokens, image_b64=image_b64,
                                     temperature=temperature, background=background, **extra)

    def ask_stream(self, system: str, user: str, *, timeout: float, max_tokens: int = 140, temperature: float = 0.7):
        """Ответ потоком кусков текста (асинхронный генератор, ТЗ 25, FAST-4)."""
        return fast_brain._ask_stream(system, user, timeout=timeout, max_tokens=max_tokens, temperature=temperature)
