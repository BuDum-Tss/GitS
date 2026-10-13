"""Пакет-алиас `glados_module` (ТЗ 05, CHR-3): импорт выдаёт DeprecationWarning, объекты те же, что в `gits_sdk`."""
import importlib
import sys
import warnings


def _forget_alias():
    for name in [k for k in sys.modules if k == "glados_module" or k.startswith("glados_module.")]:
        del sys.modules[name]


def test_glados_module_alias_warns_and_shares_objects():
    _forget_alias()
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        old = importlib.import_module("glados_module")
        old_protocol = importlib.import_module("glados_module.protocol")
    assert any(issubclass(w.category, DeprecationWarning) and "gits_sdk" in str(w.message) for w in seen)
    import gits_sdk
    assert old.ModuleClient is gits_sdk.ModuleClient
    assert old.ToolError is gits_sdk.ToolError
    assert old_protocol is gits_sdk.protocol
    assert old.__all__ == gits_sdk.__all__


def test_new_package_does_not_warn():
    _forget_alias()
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        importlib.import_module("gits_sdk")
    assert not [w for w in seen if issubclass(w.category, DeprecationWarning) and "glados" in str(w.message)]
