"""Состояние колонии из игры (RimWorld, событие `rimworld.colonists`): world state, а не память.

Данные игры достоверны и устаревают за минуты, поэтому они не воспоминания: они лежат в одном JSON-файле (`colony_state.json` в папке
памяти), целиком перезаписываются при каждом событии, выбывшие колонисты просто исчезают из него. Раньше это были факты графа; теперь
по теме реплики (имя колониста, слова про колонию или поселение) отдаётся блок строк, который адаптер памяти подмешивает к блоку
«по теме» отдельной строкой «Состояние игры»."""
import json
import logging
from pathlib import Path

from services.conversation import stems as stemlib

logger = logging.getLogger("colony_state")

MAX_LINES = 8
_SKILLS_RU = {"Shooting": "Стрельба", "Melee": "Ближний бой", "Construction": "Строительство", "Mining": "Добыча",
              "Cooking": "Готовка", "Plants": "Растениеводство", "Animals": "Животные", "Crafting": "Ремесло",
              "Artistic": "Искусство", "Medicine": "Медицина", "Social": "Общение", "Intellectual": "Интеллект"}
_COLONY_PREFIXES = ("колон", "посел")


def pawn_lines(pawn: dict, colony: str) -> list[str]:
    """Строки о колонисте строго по данным игры."""
    n = pawn.get("name") or "?"
    out = [f"{n} — колонист поселения «{colony}» в RimWorld, {pawn.get('age', '?')} лет"]
    skills = sorted(((v, k) for k, v in (pawn.get("skills") or {}).items() if v and v > 0), reverse=True)[:3]
    if skills:
        out.append(f"Лучшие навыки {n}: " + ", ".join(f"{_SKILLS_RU.get(k, k)} {v}" for v, k in skills))
    if pawn.get("traits"):
        out.append(f"Черты {n}: " + ", ".join(pawn["traits"]))
    bits = [f"здоровье {round((pawn.get('health_pct') or 0) * 100)}%", f"настроение {round((pawn.get('mood_pct') or 0) * 100)}%"]
    if pawn.get("is_downed"):
        bits.append("лежит раненым")
    if pawn.get("bleeding"):
        bits.append("кровоточит")
    if pawn.get("is_drafted"):
        bits.append("в бою")
    if pawn.get("current_job"):
        bits.append(f"занят: {pawn['current_job']}")
    out.append(f"Сейчас {n}: " + ", ".join(bits))
    return out


class ColonyState:
    def __init__(self, path: Path | str):
        self._file = Path(path)
        self.colony = ""
        self.pawns: dict[str, list[str]] = {}
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self.colony, self.pawns = str(data.get("colony") or ""), dict(data.get("pawns") or {})
        except (OSError, ValueError, AttributeError):
            pass

    def sync(self, pawns: list[dict], colony: str) -> bool:
        """Привести состояние к данным игры. Пустой список значит «игры нет», а не «все ушли»: состояние не трогается.
        True, если что-то изменилось."""
        pawns = [p for p in pawns if p.get("name") and not p.get("is_prisoner")]
        if not pawns:
            return False
        state = {p["name"]: pawn_lines(p, colony) for p in pawns}
        if state == self.pawns and colony == self.colony:
            return False
        self.colony, self.pawns = colony, state
        try:
            self._file.write_text(json.dumps({"colony": colony, "pawns": state}, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning(f"Состояние колонии не записано: {e}")
        return True

    def block(self, query: str) -> str:
        """Состояние игры по теме реплики: строки названных колонистов; про колонию в целом - состав. Пусто, если реплика не об игре."""
        if not self.pawns or not query.strip():
            return ""
        q = stemlib.stems(query)
        lines = [ln for name, rows in self.pawns.items() if stemlib.stems(name) & q for ln in rows]
        if self.colony and (stemlib.stems(self.colony) & q or any(w.startswith(_COLONY_PREFIXES) for w in q)):
            lines.insert(0, f"В поселении «{self.colony}» колонистов: {len(self.pawns)} ({', '.join(self.pawns)})")
        if not lines:
            return ""
        return "Состояние игры (данные модуля, а не воспоминания):\n" + "\n".join(f"- {ln}" for ln in lines[:MAX_LINES])
