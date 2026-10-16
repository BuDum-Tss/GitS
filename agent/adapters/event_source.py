"""EventSource поверх журнала событий `events_log` (ТЗ 27): чтение строк с номерами для эпизодов."""
import events_log


class JournalSource:
    def after(self, after_id: int = 0, limit: int = 500, kinds: set | None = None) -> list[dict]:
        return events_log.after(after_id, limit, kinds)

    def last_id(self) -> int:
        return events_log.last_id()

    def delete_ids(self, ids: list[int]) -> int:
        return events_log.delete_ids(ids)
