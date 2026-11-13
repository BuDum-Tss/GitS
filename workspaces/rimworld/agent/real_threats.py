"""Настоящие угрозы: враг, который бродит далеко от колонии, угрозой не считается (игрок его не видит, за туманом).

Мод отдаёт всех враждебных существ карты; здесь оставляем тех, кто рядом с колонистами или уже идёт в бой."""
NEAR_TILES = 45
_WANDER_JOBS = {"GotoWander", "Wait_Wander", "Wait", "Wander", "Wait_MaintainPosture"}


def _xz(p) -> tuple[int, int] | None:
    pos = (p or {}).get("position") or {}
    return (pos["x"], pos["z"]) if "x" in pos and "z" in pos else None


def filter_threats(state: dict) -> dict:
    """Возвращает состояние, где `threats` содержит только близких или активных врагов (список меняется на месте)."""
    threats = state.get("threats")
    if not threats:
        return state
    pawns = [c for c in (_xz(p) for p in (state.get("colony") or {}).get("pawns") or []) if c]
    if not pawns:
        return state

    def real(t: dict) -> bool:
        spot = _xz(t)
        if spot is None:
            return True
        near = min(max(abs(spot[0] - x), abs(spot[1] - z)) for x, z in pawns) <= NEAR_TILES
        return near or t.get("job") not in _WANDER_JOBS
    state["threats"] = [t for t in threats if real(t)]
    return state
