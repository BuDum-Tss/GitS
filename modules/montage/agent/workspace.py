"""Рабочая копия шаблона в `jobs/<id>/work/` (ТЗ 20, раздел 6).

Копируется только то, что нужно агенту и рендеру: `src/`, `scripts/`, `public/fonts`, `public/sfx`, конфигурация и
`qa_zones.json`; демо-материалов нет. `node_modules` не копируется: junction (на Windows) или символьная ссылка на
`template/node_modules`. Папка `.claude/skills` модуля копируется в `work/.claude/skills`, откуда её берёт агент
(`setting_sources=["project"]`). Исходники и слова расшифровки подключаются ссылками (жёсткая ссылка, а на другом диске
копия). Повторный вызов ничего не затирает в `src/`: правки агента (свои компоненты в `src/custom/`) сохраняются.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from layout import Layout

COPY_FILES = ("package.json", "package-lock.json", "tsconfig.json", "remotion.config.ts", "qa_zones.json", "plan.schema.json")
COPY_DIRS = ("src", "scripts")
PUBLIC_DIRS = ("fonts", "sfx")
MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".wav", ".mp3", ".m4a",
                  ".aac", ".flac", ".ogg"}


def link_or_copy(src: Path, dst: Path) -> None:
    """Жёсткая ссылка (не занимает место), при отказе копия."""
    if dst.exists():
        try:
            if dst.stat().st_size == src.stat().st_size:
                return
        except OSError:
            pass
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def make_dir_link(link: Path, target: Path) -> None:
    """Junction на Windows (не требует прав администратора), иначе символьная ссылка."""
    if link.exists() or link.is_symlink():
        return
    if sys.platform == "win32":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode != 0 or not link.exists():
            raise OSError(f"не удалось создать junction {link} -> {target}: {done.stderr.decode('oem', 'replace')[:200]}")
    else:
        os.symlink(target, link, target_is_directory=True)


def prepare_work(job_dir: Path, layout: Layout) -> Path:
    """Создать `work/` (если её нет) и подключить исходники. Возвращает путь папки."""
    work = job_dir / "work"
    tpl = layout.template
    work.mkdir(parents=True, exist_ok=True)
    if not (work / "src").is_dir():
        for name in COPY_DIRS:
            shutil.copytree(tpl / name, work / name, dirs_exist_ok=True)
    for name in COPY_FILES:
        if (tpl / name).is_file() and not (work / name).is_file():
            shutil.copy2(tpl / name, work / name)
    for sub in PUBLIC_DIRS:
        if (tpl / "public" / sub).is_dir() and not (work / "public" / sub).is_dir():
            shutil.copytree(tpl / "public" / sub, work / "public" / sub)
    for sub in ("media", "vo", "transcript"):
        (work / "public" / sub).mkdir(parents=True, exist_ok=True)
    skills = work / ".claude" / "skills"
    if layout.skills.is_dir() and not skills.is_dir():
        shutil.copytree(layout.skills, skills)
    make_dir_link(work / "node_modules", tpl / "node_modules")
    sync_media(job_dir)
    return work


def sync_media(job_dir: Path) -> list[str]:
    """Ссылки на исходники в `work/public/media/` (Remotion берёт только имя файла, ТЗ 20 §6) и слова расшифровки в
    `work/public/transcript/words.json`. Возвращает имена подключённых файлов."""
    work = job_dir / "work"
    linked = []
    sources = job_dir / "sources"
    if sources.is_dir():
        for f in sorted(sources.iterdir()):
            if f.is_file() and f.suffix.lower() in MEDIA_SUFFIXES:
                link_or_copy(f, work / "public" / "media" / f.name)
                linked.append(f.name)
    words = job_dir / "transcript" / "words.json"
    if words.is_file():
        target = work / "public" / "transcript" / "words.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(words, target)
    return linked


def custom_components(work: Path) -> set[str]:
    """Имена компонентов, зарегистрированных в `work/src/custom/index.ts` (ключи реестра `customComponents`)."""
    import re
    try:
        text = (work / "src" / "custom" / "index.ts").read_text(encoding="utf-8")
    except OSError:
        return set()
    m = re.search(r"customComponents[^=]*=\s*\{(.*?)\n\};", text, re.S)
    body = m.group(1) if m else ""
    names = set()
    for line in re.sub(r"//[^\n]*", "", body).replace("\n", ",").split(","):
        key = line.strip().split(":")[0].strip().strip("'\"")
        if key.isidentifier():
            names.add(key)
    return names
