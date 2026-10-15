"""Расположение файлов модуля и папок задач (ТЗ 20, MON-1, раздел 6). Всё, что зависит от места репозитория, берётся отсюда,
чтобы тесты подставляли временные папки."""
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# имена файлов шрифтов из регламента (ТЗ 21 §2): без любого из них монтаж не начинается (MON-24)
REQUIRED_FONTS = ("IBMPlexSansCondensed-SemiBold.ttf", "IBMPlexSansCondensed-Bold.ttf", "IBMPlexSans-Medium.ttf",
                  "IBMPlexMono-Regular.ttf")


@dataclass(frozen=True)
class Layout:
    root: Path = ROOT
    jobs: Path = field(default_factory=lambda: Path(os.environ.get("MONTAGE_JOBS_DIR") or ROOT / "jobs"))

    @property
    def template(self) -> Path:
        return self.root / "template"

    @property
    def assets(self) -> Path:
        return self.root / "assets"

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
