"""SDK модулей ГЛаДОС (ТЗ 01): `from gits_sdk import ModuleClient, ToolError`.

protocol.py: общие типы и проверки (его же импортирует ядро), client.py: клиент модуля, testing.py: FakeCore для тестов.
"""
from . import protocol
from .client import ModuleClient
from .protocol import PROTOCOL, ManifestError, ProtocolError, ToolError, validate_manifest

__version__ = "0.1.0"

__all__ = ["ModuleClient", "ToolError", "ProtocolError", "ManifestError", "PROTOCOL", "validate_manifest",
           "protocol", "__version__"]
