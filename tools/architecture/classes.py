"""Диаграммы классов ядра, построенные из кода (аналог диаграмм IntelliJ для Python): `python -m tools.architecture.classes`.

Ничего не рисуется вручную: `pyreverse` (из pylint) разбирает исходники `agent/` и отдаёт классы, поля, методы, наследование
и связи «класс владеет классом». Результат:
  docs/architecture/classes/<пакет>.mmd и .puml   по одному файлу на пакет ядра (PlantUML открывается в IntelliJ);
  docs/architecture/classes/overview.mmd           все классы ядра без полей и методов, только связи;
  docs/architecture/classes/packages.mmd           зависимости модулей между собой;
  docs/architecture/index.html                     страница с выбором пакета (Mermaid, нужен доступ к cdn.jsdelivr.net).
Нужен `pip install pylint` (в dev-requirements.txt).
"""
from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent"
OUT = ROOT / "docs" / "architecture"
CLASSES = OUT / "classes"
CODE = "from pylint import run_pyreverse; run_pyreverse()"


def packages() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for p in sorted((AGENT / "services").iterdir()):
        if p.is_dir() and (p / "__init__.py").exists() and p.name != "__pycache__":
            found[f"services.{p.name}"] = p
    for name in ("kernel", "environments", "dashboard", "host"):
        if (AGENT / name).is_dir():
            found[name] = AGENT / name
    found["interfaces.contracts"] = AGENT / "interfaces" / "contracts.py"
    return found


def pyreverse(targets: list[Path], name: str, fmt: str, *, only_names: bool = False) -> None:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(AGENT), str(AGENT / "interfaces" / "workspace")]))
    cmd = [sys.executable, "-c", CODE, "-o", fmt, "-p", name, "-d", str(CLASSES), "--filter-mode", "PUB_ONLY",
           *(["-k"] if only_names else []), *map(str, targets)]
    r = subprocess.run(cmd, cwd=AGENT, env=env, capture_output=True, text=True)
    if r.returncode not in (0, 1):                 # 1: замечания анализа, файлы всё равно записаны
        raise RuntimeError(f"pyreverse {name}: {r.stderr[-400:] or r.stdout[-400:]}")


def build() -> list[str]:
    CLASSES.mkdir(parents=True, exist_ok=True)
    for old in CLASSES.glob("*"):
        old.unlink()
    pk = packages()
    for name, path in pk.items():
        for fmt in ("mmd", "puml"):
            pyreverse([path], name, fmt)
    core = [p for p in pk.values()]
    for fmt in ("mmd", "puml"):
        pyreverse(core, "overview", fmt, only_names=True)
    for f in CLASSES.glob("classes_*"):            # classes_<имя>.mmd -> <имя>.mmd; packages_ оставляем под своим именем
        f.rename(CLASSES / f.name.removeprefix("classes_"))
    for f in CLASSES.glob("packages_overview.*"):
        f.rename(CLASSES / f.name.replace("packages_overview", "packages"))
    for f in list(CLASSES.glob("packages_*")):
        f.unlink()
    names = sorted(p.stem for p in CLASSES.glob("*.mmd") if p.stem not in ("packages",))
    names = ["overview"] + [n for n in names if n != "overview"]
    return names


def page(names: list[str]) -> str:
    data = {n: (CLASSES / f"{n}.mmd").read_text(encoding="utf-8") for n in names}
    data["packages"] = (CLASSES / "packages.mmd").read_text(encoding="utf-8") if (CLASSES / "packages.mmd").exists() else ""
    import json
    opts = "".join(f'<option value="{html.escape(n)}">{html.escape(n)}</option>' for n in [*names, "packages"])
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><title>Классы ядра GitS</title>
<style>body{{font:14px system-ui;margin:16px}}select{{font:inherit;padding:4px;margin-bottom:12px}}#g{{overflow:auto}}</style>
<h3>Диаграмма классов ядра (из кода, <code>python -m tools.architecture.classes</code>)</h3>
<select id="s">{opts}</select> <a id="src" href="#"></a><div id="g"></div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script>
<script>
const D={json.dumps(data, ensure_ascii=False)};
mermaid.initialize({{startOnLoad:false,maxTextSize:900000,securityLevel:'loose'}});
async function show(n){{const {{svg}}=await mermaid.render('m'+Date.now(),D[n]);document.getElementById('g').innerHTML=svg;
document.getElementById('src').textContent='classes/'+n+'.puml';document.getElementById('src').href='classes/'+n+'.puml'}}
const s=document.getElementById('s');s.onchange=()=>show(s.value);show(s.value);
</script></html>"""


if __name__ == "__main__":
    names = build()
    (OUT / "index.html").write_text(page(names), encoding="utf-8")
    print("записано:", len(names), "пакетов,", OUT / "index.html")
