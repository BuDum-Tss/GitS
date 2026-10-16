"""«Пол безопасности»: действия, о которых спрашивают ВСЕГДА, даже при включённом авто-разрешении.

Авто-разрешение превращает любую инъекцию (текст на странице, в файле, в выводе команды) в
выполненное действие, поэтому необратимое и всё, что касается секретов, остаётся под
ручным подтверждением. Функции возвращают причину (строку) или None, если действие обычное.

Это единственная копия: `agent/safety.py` реэкспортирует её (ТЗ 31, MC-4), модули импортируют `gits_sdk.safety`.
"""
import re
from pathlib import Path

_SECRETS = re.compile(
    r"secrets\.env|(^|[\\/\s])\.env\b|\.ssh|id_rsa|id_ed25519|\.aws|\.npmrc|\.git-credentials|"
    r"credentials|cookies|login data|wallet|keychain|\.kube|\.gnupg|\bpasswd\b|\bshadow\b", re.I)

_DESTRUCTIVE = [
    (r"\brm\s+(-\w*[rf]|--recursive|--force)", "рекурсивное или принудительное удаление"),
    (r"\b(del|erase)\b.*\s/[sqf]\b", "массовое удаление файлов"),
    (r"\b(rd|rmdir)\b.*\s/s", "удаление папки со всем содержимым"),
    (r"remove-item\b.*-recurse", "рекурсивное удаление"),
    (r"\bformat\s+[a-z]:|\bmkfs\b|\bdiskpart\b|\bdd\s+if=", "операция с диском"),
    (r"\bshutdown\b|\brestart-computer\b|\bstop-computer\b", "выключение или перезагрузка компьютера"),
    (r"git\s+(push\s+.*(--force|-f\b)|reset\s+--hard|clean\s+-\w*f)", "необратимая операция git"),
    (r"\breg(\.exe)?\s+(delete|add|import)\b|set-itemproperty\s+hk", "правка реестра"),
    (r"(curl|wget|iwr|irm|invoke-webrequest)\b.*\|\s*(sh|bash|iex|powershell|invoke-expression)",
     "скачать и сразу выполнить"),
    (r"\b(iex|invoke-expression)\b", "выполнение строки как кода"),
    (r"\bschtasks\b|\bsc\s+(create|config)\b|\bnet\s+user\b|\bnetsh\b|\bicacls\b|\btakeown\b",
     "системные настройки или права"),
    (r"\bpip3?\s+install\b|\bnpm\s+(i|install)\b|\bwinget\s+install\b|\bchoco\s+install\b",
     "установка программ из интернета"),
]


def _outside(path: str, root: Path) -> bool:
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return True
    return not (p == root or root in p.parents)


def danger_of(tool_name: str, tool_input: dict, project_root: Path) -> str | None:
    """Причина, по которой действие нужно подтверждать всегда, либо None."""
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        low = cmd.lower()
        for pattern, why in _DESTRUCTIVE:
            if re.search(pattern, low):
                return why
        if _SECRETS.search(cmd):
            return "команда касается файлов с секретами"
        return None
    if tool_name in ("Write", "Edit"):
        path = str(tool_input.get("file_path", ""))
        if _SECRETS.search(path):
            return "запись в файл с секретами"
        if _outside(path, project_root):
            return "запись вне папки проекта"
        return None
    if tool_name in ("Read", "Glob", "Grep"):
        target = " ".join(str(tool_input.get(k, "")) for k in ("file_path", "path", "pattern", "glob"))
        if _SECRETS.search(target):
            return "чтение файлов с секретами"
        return None
    if tool_name == "WebFetch":
        url = str(tool_input.get("url", ""))
        if len(url) > 200 or re.search(r"(token|key|secret|password|passwd)=", url, re.I):
            return "в адресе страницы могут уходить данные"
        return None
    return None
