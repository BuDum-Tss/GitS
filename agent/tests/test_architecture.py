"""Страховка архитектуры (ТЗ 29, ARC-1). Разбирает код через `ast`, без запуска модулей.

1. Домен чист: файлы `agent/domain/*.py` не импортируют ядро, хранилища, сеть и `asyncio`, а также `app` и `adapters`.
   Пока папки `agent/domain` нет, проверять нечего, тест проходит.
2. Размеры классов агента печатаются (без падения): строки и число методов крупнейших классов `agent/*.py`.
   Классы выше 400 строк или 40 методов помечаются как предупреждение.
"""
import ast
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
DOMAIN = AGENT / "domain"

FORBIDDEN = {
    "settings", "events_log", "fast_brain", "character", "companion", "attention", "focus", "router", "approvals",
    "module_hub", "aiohttp", "httpx", "sqlite3", "asyncio", "app", "adapters",
}
WARN_LINES = 400
WARN_METHODS = 40


def _imported_names(tree: ast.AST) -> list[tuple[int, str]]:
    """(номер строки, полное имя импорта) для всех import и from-import в дереве."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append((node.lineno, ("." * node.level) + node.module))
            if node.level:                                  # from .. import app
                out += [(node.lineno, ("." * node.level) + alias.name) for alias in node.names]
    return out


def _is_forbidden(name: str) -> bool:
    top = name.lstrip(".").split(".")[0]
    return top in FORBIDDEN


def test_domain_imports_nothing_impure():
    if not DOMAIN.is_dir():
        return                                              # R1 создаёт папку; до этого проверять нечего
    problems = []
    for path in sorted(DOMAIN.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, name in _imported_names(tree):
            if _is_forbidden(name):
                problems.append(f"{path.name}:{lineno} импортирует {name}")
    assert not problems, "домен не должен зависеть от ввода-вывода и ядра:\n" + "\n".join(problems)


def test_contracts_import_only_the_standard_library():
    """ТЗ 29, ARC-3 (шаг R2): `agent/contracts.py` это интерфейсы. Импортирует только стандартную библиотеку
    (typing, dataclasses и т. п.) и ничего из ядра, хранилищ, сети и адаптеров."""
    path = AGENT / "contracts.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems = []
    for lineno, name in _imported_names(tree):
        top = name.lstrip(".").split(".")[0]
        if name.startswith(".") or top not in sys.stdlib_module_names or _is_forbidden(name):
            problems.append(f"contracts.py:{lineno} импортирует {name}")
    assert not problems, "контракты не должны тянуть ядро и ввод-вывод:\n" + "\n".join(problems)


def test_class_sizes_report():
    """Только отчёт: печатает крупнейшие классы и предупреждает о превышении порогов, не падает."""
    rows = []
    for path in sorted(AGENT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                lines = (node.end_lineno or node.lineno) - node.lineno + 1
                methods = sum(1 for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)))
                rows.append((lines, methods, path.name, node.name))
    print("\nКрупнейшие классы агента (строки, методы, файл, класс):")
    for lines, methods, fname, cname in sorted(rows, reverse=True)[:12]:
        flag = "  <-- предупреждение ARC-1" if lines > WARN_LINES or methods > WARN_METHODS else ""
        print(f"  {lines:5d} строк  {methods:3d} методов  {fname}:{cname}{flag}")


# ── ТЗ 31, MC-4: модули не импортируют внутренности ядра ────────────────────────────────────────────────────────────────

MODULES = AGENT.parent / "modules"
# Исключения: "модуль/файл.py:имя" -> дата удаления (ГГГГ-ММ-ДД). Пока пусто: все модули переведены на SDK.
MODULE_IMPORT_EXCEPTIONS: dict[str, str] = {}


def _core_names() -> set[str]:
    """Имена верхнего уровня ядра, которыми модуль мог бы воспользоваться через sys.path: файлы и пакеты `agent/`."""
    names = {p.stem for p in AGENT.glob("*.py")}
    names |= {p.name for p in AGENT.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    return names


def _module_imports_of_core() -> list[str]:
    core = _core_names()
    found = []
    for mod in sorted(p for p in MODULES.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        own = {p.stem for p in mod.rglob("*.py")} | {p.name for p in mod.rglob("*") if p.is_dir()}
        for path in sorted(mod.rglob("*.py")):
            rel = path.relative_to(mod)
            if "tests" in rel.parts or "node_modules" in rel.parts or ".venv" in rel.parts or path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for lineno, name in _imported_names(tree):
                top = name.lstrip(".").split(".")[0]
                if not name.startswith(".") and top in core and top not in own:
                    found.append(f"{mod.name}/{rel.as_posix()}:{lineno} импортирует {name}")
    return found


def test_modules_do_not_import_core_internals():
    """Код модулей (`modules/*`, кроме тестов) импортирует стандартную библиотеку, сторонние пакеты, `gits_sdk` и свои файлы;
    файлы ядра (`events_log`, `fast_brain`, `safety`, `claude_cli`, `latency_trace`...) берутся только через SDK."""
    problems = []
    for line in _module_imports_of_core():
        key = line.split(" импортирует ")[0].rsplit(":", 1)[0] + ":" + line.rsplit(" ", 1)[-1].split(".")[0]
        if key not in MODULE_IMPORT_EXCEPTIONS:
            problems.append(line)
    assert not problems, "модули не должны импортировать внутренности ядра (используйте gits_sdk):\n" + "\n".join(problems)


def test_import_exceptions_have_removal_dates():
    import re
    for key, date in MODULE_IMPORT_EXCEPTIONS.items():
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date), f"исключение {key}: нужна дата удаления ГГГГ-ММ-ДД"
