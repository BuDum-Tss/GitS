"""Расположение файлов модуля и папок задач (ТЗ 20, MON-1, раздел 6). Всё, что зависит от места репозитория, берётся отсюда,
чтобы тесты подставляли временные папки."""
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from gits_sdk import resources

ROOT = Path(__file__).resolve().parents[1]

# имена файлов шрифтов из регламента (ТЗ 21 §2): без любого из них монтаж не начинается (MON-24)
REQUIRED_FONTS = ("IBMPlexSansCondensed-SemiBold.ttf", "IBMPlexSansCondensed-Bold.ttf", "IBMPlexSans-Medium.ttf",
                  "IBMPlexMono-Regular.ttf")


_CHARACTER_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")


@dataclass(frozen=True)
class Layout:
    root: Path = ROOT
    jobs: Path = field(default_factory=lambda: Path(os.environ.get("MONTAGE_JOBS_DIR") or ROOT / "jobs"))
    characters_root: Path | None = None      # аренда (GITS_CHARACTERS_ROOT): папки персонажей, ресурсы берутся из <root>/<персонаж>/montage
    owner: str = ""                          # персонаж-хозяин аренды

    def for_owner(self, character: str, characters_root: Path | str) -> "Layout":
        """Раскладка на время аренды: ресурсы хозяина из <root>/<персонаж>/montage, задачи в своей подпапке; других персонажей
        модуль не видит. Id персонажа проверяется (он приходит в теле acquire)."""
        if not _CHARACTER_ID.fullmatch(character or ""):
            raise ValueError("неверный id персонажа")
        return replace(self, characters_root=Path(characters_root), owner=character, jobs=self.jobs / character)

    @property
    def template(self) -> Path:
        return self.root / "template"

    @property
    def character(self) -> Path:
        """Ресурсы монтажа персонажа: resources/<Имя>/montage (шрифты, звуки, пиктограммы, регламент стиля)."""
        if self.characters_root is not None and self.owner:
            return self.characters_root / self.owner / "montage"
        return resources.module_dir("montage")

    @property
    def assets(self) -> Path:
        return self.character / "assets"

    @property
    def style(self) -> Path:
        """Регламент стиля персонажа (для ГЛаДОС: Aperture Science)."""
        return self.character / "APERTURE_STYLE.md"

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    @property
    def skills(self) -> Path:
        return self.root / ".claude" / "skills"

    @property
    def references(self) -> Path:
        return self.root / "references"

    @property
    def prompts(self) -> Path:
        return self.root / "agent" / "prompts"

    @property
    def verdicts(self) -> Path:
        """Обучающий журнал решений человека (MON-QA-5): вне папок задач, переживает уборку."""
        return self.jobs / "qa_verdicts.jsonl"

    def missing_fonts(self) -> list[str]:
        fonts = self.assets / "fonts"
        return [name for name in REQUIRED_FONTS if not (fonts / name).is_file()]


DEFAULT = Layout()
