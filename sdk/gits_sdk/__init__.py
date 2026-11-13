"""SDK участников шины ГЛаДОС (ТЗ 01): `from gits_sdk import WorkspaceClient, ServiceClient, ToolError`.

protocol.py: общие типы и проверки (его же импортирует ядро), client.py: клиенты (`WorkspaceClient`, `ServiceClient`; прежнее
`ModuleClient` оставлено алиасом, уберёт фаза 4), control.py: интерфейс аренды workspace (сервер и `LeaseClient`),
testing.py: FakeCore для тестов.
"""
from . import protocol
from .client import ModuleClient, PeerClient, ServiceClient, WorkspaceClient
from .protocol import PROTOCOL, ManifestError, ProtocolError, ToolError, validate_manifest

__version__ = "0.1.0"

__all__ = ["PeerClient", "WorkspaceClient", "ServiceClient", "ModuleClient", "ToolError", "ProtocolError", "ManifestError", "PROTOCOL", "validate_manifest",
           "protocol", "__version__"]
