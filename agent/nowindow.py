"""Не открывать окна терминалов у дочерних процессов (Windows).

Ядро и питомец запускаются без консоли (pythonw). Когда такой процесс порождает консольную
программу (claude.exe для помощника и игровых агентов, PowerShell для озвучки), Windows создаёт
для неё новое окно, а в Windows 11 это ещё и окно Windows Terminal. CREATE_NO_WINDOW по умолчанию
добавляется ко всем запускам процессов; GUI-программы (игра) от флага не страдают.
"""
import subprocess
import sys

if sys.platform == "win32" and not getattr(subprocess.Popen, "_nowindow_patched", False):
    _CREATE_NEW_CONSOLE, _DETACHED, _NO_WINDOW = 0x00000010, 0x00000008, 0x08000000
    _orig_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) or 0
        if not flags & (_CREATE_NEW_CONSOLE | _DETACHED | _NO_WINDOW):
            kwargs["creationflags"] = flags | _NO_WINDOW
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init
    subprocess.Popen._nowindow_patched = True
