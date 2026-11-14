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

    def resolve_resource(self, raw: str) -> Path | None:
        """Путь к ресурсу персонажа по короткому имени: `design/assets/Intro/x.mp4`, `montage/design/...` или `resources/<Имя>/montage/...`
        (так их называет агент). Нет такого файла: None."""
        s = raw.replace("\\", "/").strip().lstrip("/")
        from gits_sdk.resources import resource_path
        candidate = resource_path(s.removeprefix("character/"), home=self.character.parent)
        if candidate is not None and candidate.exists():
            return candidate
        if s.startswith("character/"):
            return None
        parts = s.split("/")
        if parts[0] == "resources" and len(parts) > 2 and parts[2] == "montage":
            parts = parts[3:]
        elif parts[0] in ("resources", "montage"):               # resources/design/...: раздел files, корень = папка montage персонажа
            parts = parts[1:]
        if not parts or ".." in parts:
            return None
        p = resource_path("/".join(parts), home=self.character)
        return p if p is not None and p.exists() else None

    def list_resources(self, limit: int = 120) -> list[tuple[Path, int]]:
        """Файлы ресурсов персонажа для монтажа (папка montage без подготовленных превью и кода): путь и размер."""
        out: list[tuple[Path, int]] = []
        base = self.character
        if not base.is_dir():
            return out
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(base).parts
            if not p.is_file() or "out" in rel[:1] or "components" in rel or p.suffix.lower() in (".html", ".css", ".json"):
                continue
            out.append((p, p.stat().st_size))
            if len(out) >= limit:
                break
        return out

    def catalog_files(self) -> list[tuple[str, int]]:
        """Ресурсы и компоненты дизайна с путями для files и размером."""
        base = self.character
        out: list[tuple[str, int]] = []
        if not base.is_dir():
            return out
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(base).parts
            if not p.is_file() or rel[0] == "out" or any(part in {"node_modules", ".git", ".claude"} for part in rel):
                continue
            out.append(("resources/" + "/".join(rel), p.stat().st_size))
        return out

    def missing_fonts(self) -> list[str]:
        fonts = self.assets / "fonts"
        return [name for name in REQUIRED_FONTS if not (fonts / name).is_file()]


DEFAULT = Layout()
