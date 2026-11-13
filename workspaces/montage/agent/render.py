"""Remotion: проверка типов, проверка плана схемой шаблона, кадры и рендер (ТЗ 20, стадии build, preview, final).

Команды из `docs/PIPELINE.md`; запускаются в `jobs/<id>/work/`. Интерфейс `Renderer` позволяет тестам подставить заглушку.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import media

NODE_FLAGS = ["--disable-warning=MODULE_TYPELESS_PACKAGE_JSON"]
RENDER_TIMEOUT_S = 3 * 60 * 60
STILL_TIMEOUT_S = 300
_PROGRESS = re.compile(r"Rendered\s+(\d+)/(\d+)|\((\d+)/(\d+)\)|(\d+)/(\d+)")


class RenderError(RuntimeError):
    """Сбой рендера или проверки (текст для журнала и для агента)."""


class Renderer(Protocol):
    async def tsc(self, work: Path) -> list[str]: ...
    async def validate_plan(self, work: Path, plan: Path) -> list[str]: ...
    async def render(self, work: Path, props: Path, out: Path, *, scale: float | None, crf: int,
                     on_progress: Callable[[float], None] | None = None) -> None: ...
    async def still(self, work: Path, props: Path, frame: int, out: Path) -> None: ...


def _clip(text: str, n: int = 1500) -> str:
    text = text.strip()
    return text[-n:] if len(text) > n else text


class RemotionRenderer:
    def __init__(self):
        self.npx = media.find_npx()
        import shutil
        self.node = shutil.which("node") or "node"

    async def tsc(self, work: Path) -> list[str]:
        """`npx tsc --noEmit`: список ошибок типов (пустой: чисто)."""
        res = await media.run_cmd([self.npx, "tsc", "--noEmit"], cwd=work, timeout=600)
        if res.ok:
            return []
        lines = [ln for ln in (res.out + "\n" + res.err).splitlines() if "error TS" in ln]
        return lines[:30] or [_clip(res.out + res.err)]

    async def validate_plan(self, work: Path, plan: Path) -> list[str]:
        """Та же zod-схема, что в `calculateMetadata`: `scripts/validate-plan.mjs` (проверка формы плана)."""
        res = await media.run_cmd([self.node, *NODE_FLAGS, "scripts/validate-plan.mjs", str(plan)], cwd=work, timeout=120)
        try:
            doc = json.loads(res.out)
        except ValueError:
            return [] if res.ok else [_clip(res.out + res.err)]
        out: list[str] = []
        for issues in doc.values():
            if issues == "ok":
                continue
            out += [f"{i.get('path') or 'plan'}: {i.get('message')}" for i in issues]
        return out

    async def render(self, work: Path, props: Path, out: Path, *, scale: float | None, crf: int,
                     on_progress: Callable[[float], None] | None = None) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        args = [self.npx, "remotion", "render", "src/index.ts", "Main", str(out), f"--props={props}", f"--crf={crf}",
                "--codec=h264"]
        if scale is not None:
            args.append(f"--scale={scale}")

        def on_line(line: str) -> None:
            m = re.search(r"Rendered\s+(\d+)/(\d+)", line) or re.search(r"\((\d+)/(\d+)\)", line)
            if m and on_progress and int(m.group(2)):
                on_progress(min(1.0, int(m.group(1)) / int(m.group(2))))

        res = await media.run_cmd(args, cwd=work, on_line=on_line, timeout=RENDER_TIMEOUT_S)
        if not res.ok or not out.is_file():
            raise RenderError("рендер Remotion не удался: " + _clip(res.err or res.out))

    async def still(self, work: Path, props: Path, frame: int, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        res = await media.run_cmd([self.npx, "remotion", "still", "src/index.ts", "Main", str(out), f"--props={props}",
                                   f"--frame={frame}"], cwd=work, timeout=STILL_TIMEOUT_S)
        if not res.ok or not out.is_file():
            raise RenderError("кадр Remotion не получился: " + _clip(res.err or res.out))
