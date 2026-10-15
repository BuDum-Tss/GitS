"""Страховка архитектуры (ТЗ 29, ARC-1). Разбирает код через `ast`, без запуска модулей.

1. Домен чист: файлы `agent/domain/*.py` не импортируют ядро, хранилища, сеть и `asyncio`, а также `app` и `adapters`.
   Пока папки `agent/domain` нет, проверять нечего, тест проходит.
2. Размеры классов агента печатаются (без падения): строки и число методов крупнейших классов `agent/*.py`.
   Классы выше 400 строк или 40 методов помечаются как предупреждение.
"""
import ast
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
