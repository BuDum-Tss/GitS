"""Справочник инструментов агента по режимам: `python agent/tools_doc.py` пишет docs/TOOLS.md (базовые, затем режимы)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import game_modes  # noqa: E402


def _first_lines() -> dict[str, str]:
    """Первая фраза описания каждого инструмента из rimworld_mcp.py."""
    src = (Path(__file__).parent / "rimworld_mcp.py").read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r'@mcp\.tool\(\)\s*\n(?:async )?def (\w+)\([^)]*\)[^:]*:\s*\n\s+"""(.*?)(?:\n\s*\n|""")', src, re.S):
        text = " ".join(m.group(2).split())
        out[m.group(1)] = text.split(". ")[0].rstrip(".")[:150]
    return out


def render() -> str:
    doc = _first_lines()
    lines = ["# Инструменты агента RimWorld по режимам", "",
             "Сгенерировано `python agent/tools_doc.py`. Каждый инструмент принадлежит одному режиму; базовые доступны в любом. "
             "Агент переходит между режимами вызовом `switch_mode`.", "", f"## Базовые ({len(game_modes.CORE)}), в любом режиме", ""]
    lines += [f"- `{t}`: {doc.get(t, '')}" for t in game_modes.CORE]
    for m in game_modes.MODES.values():
        lines += ["", f"## {m.title} (`{m.id}`), своих инструментов {len(m.tools)}", "", f"*{m.purpose}*", ""]
        lines += [f"- `{t}`: {doc.get(t, '')}" for t in m.tools]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "docs" / "TOOLS.md"
    out.write_text(render(), encoding="utf-8")
    print(f"записано {out}")
