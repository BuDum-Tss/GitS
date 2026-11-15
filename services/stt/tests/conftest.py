"""Тесты сервиса распознавания: пути (пакет stt и, для теста совместимости, agent/ с клиентом ядра), никаких весов и сети."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # services/stt
GITS = HERE.parent.parent
for p in (GITS / "agent", HERE):                       # свой пакет stt первым
    if str(p) in sys.path:
        sys.path.remove(str(p))
    sys.path.insert(0, str(p))
