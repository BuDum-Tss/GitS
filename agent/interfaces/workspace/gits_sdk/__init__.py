"""SDK участников шины ГЛаДОС (ТЗ 01): `from gits_sdk import WorkspaceClient, ServiceClient, ToolError`.

protocol.py: общие типы и проверки (его же импортирует ядро), client.py: клиенты (`WorkspaceClient`, `ServiceClient`, база `PeerClient`), control.py: интерфейс аренды workspace (сервер и `LeaseClient`),
testing.py: FakeCore для тестов, workspace_tools.py: песочница путей и инструменты окружения (`Sandbox`, `WorkspaceTools`).
"""
from . import protocol
from .client import PeerClient, ServiceClient, WorkspaceClient
from .protocol import (PROTOCOL, ManifestError, ProtocolError, ToolError, WorkspaceArea, WorkspaceCommand, WorkspaceSkill,
                       WorkspaceSpec, parse_workspace_spec, validate_manifest)
from .workspace_tools import Sandbox, WorkspaceTools, action_record

__version__ = "0.1.0"

__all__ = ["PeerClient", "WorkspaceClient", "ServiceClient", "ToolError", "ProtocolError", "ManifestError", "PROTOCOL", "validate_manifest",
           "Sandbox", "WorkspaceTools", "action_record", "WorkspaceSpec", "WorkspaceArea", "WorkspaceCommand", "WorkspaceSkill", "parse_workspace_spec",
           "protocol", "__version__"]
