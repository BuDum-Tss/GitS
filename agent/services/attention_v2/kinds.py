"""Виды событий окружений (RUNTIME_SPEC 3.3): потолок приоритета, срок жизни, перевод старых событий шины в `Event`."""
import time
from dataclasses import dataclass

from .event import MAX_TEXT, NORMAL, USER, Event, Reply, Sender, new_event_id, parse_priority

# сроки по умолчанию, секунды (раздел 3.3)
TTL_VOICE = 90.0
TTL_CHAT = 900.0
TTL_GAME = 60.0
TTL_TIMER = 600.0
TTL_SPOKEN = 120.0
TTL_DEFAULT = 600.0                       # вид, про который ничего не известно

_GAME_WORKSPACES = {"rimworld", "minecraft"}
_URGENCY_TO_PRIORITY = [0, 10, 30, 50, 70, 100]        # urgency_hint 0..5 старой шины


def default_ttl(kind: str, workspace: str = "") -> float:
    """Срок по виду, если манифест его не задал: голос 90 с, чат 15 мин, игра 60 с, таймер 10 мин, итог речи 2 мин."""
    tail = kind.split(".", 1)[-1]
    parts = set(tail.split("."))
    if parts & {"voice", "utterance"}:
        return TTL_VOICE
    if "spoken" in parts:
        return TTL_SPOKEN
    if "timer" in parts:
        return TTL_TIMER
    if parts & {"message", "dm", "group", "text", "chat"}:
        return TTL_CHAT
    if workspace in _GAME_WORKSPACES or kind.split(".", 1)[0] in _GAME_WORKSPACES:
        return TTL_GAME
    return TTL_DEFAULT


@dataclass(frozen=True)
class KindSpec:
    kind: str
    default_priority: float = NORMAL
    max_priority: float = USER
    ttl_s: float = TTL_DEFAULT
    coalesce: bool = False                # события с `group_key` этого вида можно сливать (4.5)
    reply_tool: str | None = None
    critical: bool = False                # не режется потолком `FOCUS_CAP` вне фокуса (4.3)
    passive: bool = False                 # фон: ждёт в очереди и уходит в пачку вместе с другими, но сам ход не запускает (ни по `ON_TURN_S`)
    legacy: bool = False                  # вид выведен из события старой шины, не объявлен манифестом

    @classmethod
    def from_dict(cls, d: dict) -> "KindSpec":
        kind = d.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("вид события: нужна непустая строка kind")
        mx = parse_priority(d.get("max_priority", USER))
        df = parse_priority(d.get("default_priority", NORMAL))
        ttl = d.get("ttl_s", default_ttl(kind))
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
            raise ValueError(f"{kind}: ttl_s должно быть положительным числом")
        return cls(kind=kind, default_priority=min(df, mx), max_priority=mx, ttl_s=float(ttl),
                   coalesce=bool(d.get("coalesce", False)), reply_tool=d.get("reply_tool"),
                   critical=bool(d.get("critical", False)), passive=bool(d.get("passive", False)))


@dataclass
class NormalizeResult:
    accepted: bool
    reason: str | None = None             # почему отброшено (для журнала)
    clamped_from: float | None = None     # приоритет до зажима потолком, если зажали
    spec: KindSpec | None = None


class KindRegistry:
    """Виды всех окружений. Окружение объявляет их в манифесте (`event_kinds`); неизвестный вид отбрасывается."""

    def __init__(self):
        self._specs: dict[str, KindSpec] = {}
        self._by_workspace: dict[str, set[str]] = {}

    def register(self, spec: KindSpec, workspace: str | None = None) -> None:
        self._specs[spec.kind] = spec
        ws = workspace or spec.kind.split(".", 1)[0]
        self._by_workspace.setdefault(ws, set()).add(spec.kind)

    def register_manifest(self, workspace: str, manifest) -> list[str]:
        """Объявленные манифестом виды: `{"event_kinds": [{kind, default_priority, max_priority, ttl_s, coalesce, reply_tool,
        critical}, ...]}` (или сам список). Значения приоритета числом или именем уровня. Прежние виды окружения заменяются."""
        items = manifest.get("event_kinds", []) if isinstance(manifest, dict) else manifest
        specs = [KindSpec.from_dict(d) for d in (items or [])]
        self.unregister_workspace(workspace)
        for s in specs:
            self.register(s, workspace)
        return [s.kind for s in specs]

    def unregister_workspace(self, workspace: str) -> None:
        for k in self._by_workspace.pop(workspace, set()):
            self._specs.pop(k, None)

    def ensure_legacy(self, workspace: str, kind: str) -> KindSpec:
        """Вид старого события шины без объявления: принимается видом по умолчанию (потолок USER, срок по имени вида)."""
        spec = self._specs.get(kind)
        if spec is None:
            spec = KindSpec(kind=kind, default_priority=NORMAL, max_priority=USER, ttl_s=default_ttl(kind, workspace),
                            coalesce=True, legacy=True)
            self.register(spec, workspace)
        return spec

    def get(self, kind: str) -> KindSpec | None:
        return self._specs.get(kind)

    def __contains__(self, kind: str) -> bool:
        return kind in self._specs

    def kinds(self, workspace: str | None = None) -> list[str]:
        if workspace is None:
            return sorted(self._specs)
        return sorted(self._by_workspace.get(workspace, set()))

    def normalize(self, event: Event) -> NormalizeResult:
        """Привести событие к правилам вида: приоритет по умолчанию и не выше потолка, срок `ts + ttl_s`, текст до 1000 символов.
        Неизвестный вид: `accepted=False` с причиной (вызывающий пишет её в журнал)."""
        spec = self._specs.get(event.kind)
        if spec is None:
            return NormalizeResult(False, reason=f"неизвестный вид {event.kind!r}")
        clamped = None
        if event.priority is None:
            event.priority = spec.default_priority
        event.priority = max(0, event.priority)
        if event.priority > spec.max_priority:
            clamped, event.priority = event.priority, spec.max_priority
        if event.expires_at is None:
            event.expires_at = event.ts + spec.ttl_s
        if len(event.text) > MAX_TEXT:
            event.text = event.text[:MAX_TEXT - 1] + "…"
        if event.reply is None and spec.reply_tool:
            event.reply = Reply(tool=spec.reply_tool)
        return NormalizeResult(True, clamped_from=clamped, spec=spec)


def from_legacy(peer_id: str, event_dict: dict, registry: KindRegistry, *, now: float | None = None,
                event_id: str | None = None) -> Event:
    """Событие старой шины (`kind`, `text`, `urgency_hint` 0-5, `addressed_to_me`, `sender`, `reply_channel`, `dedupe_key`,
    `expires_in_s`, `data`) в `Event`. Срочность 0..5 это приоритет [0, 10, 30, 50, 70, 100]; обращение к ней поднимает до USER,
    но только если отправитель не бот. Ключ `v2` (`priority`, `group_key`, `reply_tool`) от отправителя перекрывает всё это. Необъявленный вид принимается видом по умолчанию (не отбрасывается)."""
    d = event_dict
    ts = d.get("ts") if isinstance(d.get("ts"), (int, float)) else (now if now is not None else time.time())
    kind = d.get("kind") or f"{peer_id}.event"
    if kind not in registry:
        registry.ensure_legacy(peer_id, kind)
    sender = Sender.from_obj(d.get("sender"))

    priority = None                       # None: priority вида по умолчанию
    hint = d.get("urgency_hint")
    if isinstance(hint, (int, float)) and not isinstance(hint, bool):
        priority = _URGENCY_TO_PRIORITY[max(0, min(5, int(hint)))]
    if d.get("addressed_to_me") is True and not (sender and sender.is_bot):
        priority = USER

    expires_at = None
    exp = d.get("expires_in_s")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > 0:
        expires_at = ts + exp

    group_key = d.get("dedupe_key") or None
    v2 = d.get("v2") if isinstance(d.get("v2"), dict) else {}          # добавка отправителя для v2: приоритет, ключ слияния, инструмент ответа
    try:
        explicit = parse_priority(v2.get("priority"))
    except ValueError:
        explicit = None
    if explicit is not None:
        priority = explicit
    data_key = (d.get("data") or {}).get("group_key") if isinstance(d.get("data"), dict) else None   # ключ слияния от окружения шины (поля group_key в протоколе нет)
    group_key = v2.get("group_key") or data_key or group_key

    reply = None
    rc = d.get("reply_channel")
    if isinstance(rc, dict) and rc.get("module"):
        reply = Reply(tool=str(rc.get("tool") or f"{rc['module']}.message"), target=rc.get("target"))
    elif isinstance(rc, str) and rc:
        reply = Reply(tool=rc)
    if v2.get("reply_tool"):
        reply = Reply(tool=str(v2["reply_tool"]), target=reply.target if reply is not None else None)

    return Event(id=event_id or new_event_id(), ts=ts, workspace=peer_id, kind=kind, sender=sender,
                 text=str(d.get("text", "") or ""), data=dict(d.get("data") or {}), priority=priority,
                 expires_at=expires_at, reply=reply, group_key=group_key)
