"""EventLog поверх журнала событий (SQLite `memory/events.db`): `events_log`."""
import events_log


class SqliteEventLog:
    def log(self, kind: str, text: str = "", **data) -> None:
        events_log.log(kind, text, **data)

    def log_nowait(self, kind: str, text: str = "", **data) -> None:
        events_log.log_nowait(kind, text, **data)
