"""Approver поверх разрешений и вопросов пользователю: `approvals`."""
import approvals


class ApprovalsModule:
    def has_pending(self) -> bool:
        return approvals.has_pending()

    def formulating(self) -> bool:
        """Идёт формулировка вопроса (контекстная метка `approvals.announcing`)."""
        return bool(approvals.announcing.get())

    async def request(self, text: str, announce, danger: str | None = None) -> bool:
        return await approvals.request(text, announce, danger=danger)

    async def ask(self, kind: str, question: str, options, timeout_s: float, source: str, *,
                  announce=None, context: dict | None = None):
        return await approvals.ask(kind, question, options, timeout_s, source, announce=announce, context=context)

    async def try_answer(self, text: str) -> bool:
        return await approvals.try_answer(text)
