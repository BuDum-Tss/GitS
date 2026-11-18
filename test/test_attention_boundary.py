"""Граница сервиса внимания (`agent/services/attention/`): остальное ядро знает его только через `attention.Attention` и протокол
`contracts.EventAttention`, внутренних файлов папки не импортирует и обращается только к публичным методам."""
import ast
import inspect
from pathlib import Path

from interfaces import contracts
from services.attention import Attention

AGENT = Path(__file__).resolve().parents[1] / "agent"
SKIP_PARTS = {"tests", ".venv", "node_modules", "logs", "tools"}
OWN = ("services", "attention")                                           # папка сервиса: внутри неё можно что угодно
OWN_V2 = ("services", "attention_v2")                                     # внимание v2: свой пакет со своим интерфейсом (RUNTIME_SPEC)
ALLOWED_ROOT_NAMES = {"Attention", "Question", "BATCH_KIND", "event_of", "AttentionEventSink"}              # имена, которые остальное ядро берёт из пакета
ATT_NAMES = {"attention", "att"}


def _protocol_members() -> set[str]:
    return {n for n in dir(contracts.EventAttention) if not n.startswith("_")} | {"decider"}


def _production_files():
    for p in AGENT.rglob("*.py"):
        parts = p.relative_to(AGENT).parts
        if SKIP_PARTS & set(parts) or parts[:2] == OWN or parts[:2] == OWN_V2:
            continue
        yield p, ast.parse(p.read_text(encoding="utf-8"))


def test_attention_satisfies_its_protocol():
    missing = [n for n in _protocol_members() if n != "decider" and not hasattr(Attention, n)]
    assert not missing, f"в Attention нет методов протокола: {missing}"
    for name in _protocol_members() - {"decider"}:
        proto = inspect.signature(getattr(contracts.EventAttention, name))
        real = inspect.signature(getattr(Attention, name))
        assert [p for p in proto.parameters if p != "self"][:1] == [p for p in real.parameters if p != "self"][:1], name


def test_nobody_outside_imports_attention_internals():
    bad = []
    for p, tree in _production_files():
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                if n.module.startswith("services.attention."):
                    bad.append(f"{p.relative_to(AGENT)}: from {n.module}")
                elif n.module == "services.attention":
                    bad += [f"{p.relative_to(AGENT)}: {a.name}" for a in n.names if a.name not in ALLOWED_ROOT_NAMES]
            elif isinstance(n, ast.Import):
                bad += [f"{p.relative_to(AGENT)}: import {a.name}" for a in n.names if a.name.startswith("services.attention.")]
    assert not bad, f"внутренние файлы внимания импортируются снаружи: {bad}"


def test_outside_code_uses_only_public_attention_methods():
    public = _protocol_members()
    bad = []
    for p, tree in _production_files():
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute):
                v = n.value
                if (isinstance(v, ast.Name) and v.id in ATT_NAMES) or (isinstance(v, ast.Attribute) and v.attr == "attention"):
                    if n.attr not in public:
                        bad.append(f"{p.relative_to(AGENT)}: attention.{n.attr}")
    assert not bad, f"обращения к внутренностям внимания: {bad}"
