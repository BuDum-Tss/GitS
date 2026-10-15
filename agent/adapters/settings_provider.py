"""SettingsProvider поверх настроек ядра: `settings`."""
import settings


class SettingsModule:
    def load(self) -> dict:
        return settings.load()

    def update(self, **fields) -> dict:
        return settings.update(**fields)

    def quiet_active(self, cfg: dict | None = None) -> bool:
        return settings.quiet_active(cfg)
