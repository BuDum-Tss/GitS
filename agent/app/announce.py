"""Объявления о долгих запусках (ТЗ 29, ARC-4; ТЗ 03, AN-1-AN-3): мимо формулы важности, каждое должно прозвучать."""
import secrets

from domain import dedupe
from speech_queue import ANNOUNCE_P

from .attention_core import ANNOUNCE_DEFAULT_URGENCY, TEXT_LOG, TEXT_MAX, AttentionContext, Item
from .decision_log import DecisionLog
from .digest import Digest
from .items import ItemStore
from .speech_coordinator import SpeechCoordinator


def is_announce_event(kind: str, body: dict) -> bool:
    """Событие - объявление о запуске: вид `<модуль>.launch` или явная пометка `announce: true`."""
    return body.get("announce") is True or kind.endswith(".launch")


class Announcer:
    def __init__(self, ctx: AttentionContext, store: ItemStore, digest: Digest, speech: SpeechCoordinator,
                 decisions: DecisionLog):
        self.ctx, self.store, self.digest, self.speech, self.decisions = ctx, store, digest, speech, decisions
        self.recent: dict[tuple[str, str], dict] = {}   # (модуль, текст) -> {item, cand, short} недавних объявлений (AN-1)

    async def submit(self, module_id: str, text: str, *, data: dict | None = None) -> None:
        """Объявление о запуске от самого ядра (инструмент модуля с `announce: true`, AN-3): тот же путь, что у `.launch`."""
        text = str(text).strip()
        if not text:
            return
        now = self.ctx.clock()
        item = Item(id="a-" + secrets.token_hex(4), module=module_id, kind=f"{module_id}.launch", text=text[:TEXT_MAX],
                    data=dict(data or {}), sender=None, reply_channel=None, addressed_to_me=False,
                    urgency=ANNOUNCE_DEFAULT_URGENCY, needs_reply=False, memorable=False, created=now, expires=None,
                    dedupe_key=None, urgency_hint=ANNOUNCE_DEFAULT_URGENCY, updated=now)
        self.store.register(item)
        await self.announce(item)

    async def announce(self, item: Item, *, mode: str = "fact", emotion: str | None = None, ttl: float | None = None) -> None:
        """Формула важности, правила и пауза между репликами не применяются. Пользователь просил не отвлекать - объявление
        звучит коротко; пользователь отошёл - уходит в сводку (и в журнал). Одинаковые объявления одного источника в
        пределах 20 с склеиваются: остаётся одно, в факте счётчик."""
        ctx = self.ctx
        now = ctx.clock()
        cfg = ctx.settings()
        att = ctx.att(cfg)
        key = dedupe.announce_key(item.module, item.text)
        self.recent = {k: v for k, v in self.recent.items() if now - v["item"].created <= dedupe.ANNOUNCE_MERGE_S}
        old = self.recent.get(key)
        if old is not None:
            self._merge(old, now)
            return
        quiet = ctx.quiet(cfg)
        away = ctx.idle() > att["away_after_s"]
        short = quiet and mode == "fact"                              # готовую реплику модуля сократить нельзя
        item.urgency = item.urgency if item.urgency is not None else ANNOUNCE_DEFAULT_URGENCY
        item.priority, item.decision = float(ANNOUNCE_P), "ANNOUNCE"
        entry = {"item": item, "cand": None, "short": short, "mode": mode}
        self.recent[key] = entry
        if away:
            route = "digest"                                          # сказать некому: сводка, не Telegram
            self.digest.add(item)
        else:
            route = "short" if short else "speech"
            fact = self.fact(item, short) if mode == "fact" else item.text
            entry["cand"] = self.speech.speech.new(source=f"announce:{item.id}", mode=mode, text=fact, emotion=emotion,
                                                   priority=ANNOUNCE_P, ttl=ttl, must=True, item_id=item.id)
            self.speech.kick()
        self.decisions.write(item, att, score={"w_user": 0.3 if quiet else 1.0, "priority": float(ANNOUNCE_P), "away": away},
                             rule="ANNOUNCE", route=route)

    def _merge(self, entry: dict, now: float) -> None:
        old: Item = entry["item"]
        old.count += 1
        old.updated = now
        cand = entry["cand"]
        if cand is not None and cand in self.speech.speech.items and entry["mode"] == "fact":
            cand.text = self.fact(old, entry["short"])                  # ещё не звучало: счётчик в факте
        self.digest.update_entry(old, text=False)
        self.ctx.log.log_nowait("attention", old.text[:TEXT_LOG], item=old.id, module=old.module, event_kind=old.kind,
                                merged=True, count=old.count, decision="ANNOUNCE")

    def fact(self, item: Item, short: bool) -> str:
        """Факт для реплики: что запускается и зачем (формулирует модель); пометка «коротко» при «не отвлекать»."""
        parts = [f"Объявление о запуске (модуль «{self.ctx.module_title(item.module)}»): {item.text}",
                 "Пользователь должен об этом узнать."]
        if item.count > 1:
            parts.append(f"(одно и то же объявление пришло {item.count} раз подряд, скажи один раз)")
        if short:
            parts.append("Коротко: пользователь просил не отвлекать, одна короткая фраза.")
        return " ".join(parts)
