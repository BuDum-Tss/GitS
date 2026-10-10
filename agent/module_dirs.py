"""Где лежит репозиторий модуля RimWorld (ТЗ 11, RW-2): переменная `RIMWORLD_MODULE_DIR` для `modules.yaml` ({RIMWORLD_MODULE_DIR}).

Порядок поиска (репозиторий переименовывается из `rimworld-agent` в `rimworld-module`, поэтому работают оба имени папки):
  1. `RIMWORLD_MODULE_DIR` (например, в `agent/secrets.env`);
  2. `RIMWORLD_AGENT_DIR`: прежнее имя, читается как запасное одну версию, с предупреждением в журнал;
  3. соседняя с GitS папка `rimworld-module`, если она есть;
  4. соседняя папка `rimworld-agent` (ещё не переименованная).
Ничего не найдено: переменная остаётся пустой, а супервизор пометит модуль «не настроен» (CORE-12)."""
import logging
import os
from pathlib import Path

logger = logging.getLogger("module_dirs")

GITS_ROOT = Path(__file__).resolve().parent.parent
NEW_VAR, OLD_VAR = "RIMWORLD_MODULE_DIR", "RIMWORLD_AGENT_DIR"
SIBLING_NAMES = ("rimworld-module", "rimworld-agent")


def resolve_rimworld_dir(environ=None, root: Path = GITS_ROOT) -> tuple[str | None, str | None]:
    """(путь, предупреждение) по порядку выше. Путь из переменной не проверяется на существование: его задал человек."""
    environ = os.environ if environ is None else environ
    if environ.get(NEW_VAR):
        return environ[NEW_VAR], None
    if environ.get(OLD_VAR):
        return environ[OLD_VAR], f"переменная {OLD_VAR} устарела: переименуй в {NEW_VAR} (в agent/secrets.env)"
    for name in SIBLING_NAMES:
        candidate = root.parent / name
        if candidate.is_dir():
            return str(candidate), None
    return None, None


def apply(environ=None, root: Path = GITS_ROOT) -> str | None:
    """Записать найденный путь в `RIMWORLD_MODULE_DIR` окружения процесса ядра (его подставляет супервизор)."""
    environ = os.environ if environ is None else environ
    path, warning = resolve_rimworld_dir(environ, root)
    if warning:
        logger.warning(warning)
    if path:
        environ[NEW_VAR] = path
    return path
