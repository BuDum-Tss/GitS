"""Память-граф: факты о людях и мире в SQLite (`graph.db`), всё локально, без Docker.

Схема (замена Graphiti и FalkorDB, см. docs/MEMORY_GRAPH.md):
  1. ИЗВЛЕЧЕНИЕ наше: одна простая задача для локальной Gemma («выпиши устойчивые факты») со строгой схемой ответа.
  2. ХРАНЕНИЕ `fact_store.FactStore`: таблица связей «субъект - отношение - объект» с интервалами действия; точный повтор не
     дублируется, однозначное отношение (живёт_в, работает) закрывает прежний факт датой (переехала в Казань - «живёт в Москве» закрыт).
  3. ПОИСК общий `search.HybridIndex` (слова FTS5 + векторы EmbeddingGemma из LM Studio) в том же файле; устаревшее по умолчанию
     не показываем, слабые совпадения отсекаются порогом по косинусу.
Работает в фоне: реплики кладутся в очередь (файл memory/graph_queue.json, переживает перезапуск), разбираются, когда
она не занята разговором. Если модели недоступны, бот просто живёт без памяти-графа (очередь копится).
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import character
import character_memory
import events_log
import graph_scope
import settings
import stems as stemlib
from envcompat import env
from fact_store import FactStore, iso
from search.embedder import LmStudioEmbedder
from search.index import HybridIndex, SearchConfig, cosine
from search.service import HybridSearch

logger = logging.getLogger("graph_memory")

LM_URL = env("GITS_LM_URL", "http://127.0.0.1:1234/v1")
LLM_MODEL = env("GITS_GRAPH_LLM", "google/gemma-4-e4b")
EMB_MODEL = env("GITS_GRAPH_EMBEDDER", "text-embedding-google_embeddinggemma-300m-qat")
MIN_SCORE = 0.5                   # косинус «запрос ↔ факт»: ниже считаем несвязанным (релевантное ~0.75, мусор ~0.3)
MIN_CHARS = 12                    # короче реплику на факты не разбираем
MAX_QUEUE = 300
CONTEXT_FACTS = 3                 # фактов графа в контексте ответа (ТЗ 04, KB-13)
MAX_ATTEMPTS = 3
MIN_IMPORTANCE_PROFILE = 4        # факт о владельце и о самом персонаже идёт в память, только если она сама сочла его важным (4-5)
MIN_IMPORTANCE_OTHER = 3          # факты о других людях и мире мягче: связи и события жизни (3-5), мелочь (1-2) не пишется
SAME_FACT_OVERLAP = 0.6              # доля общих основ слов, с которой два факта профиля считаются одним
PROFILE_FACTS = 8                 # фактов профиля владельца в контексте ответа (всегда, если говорит он)

# ── извлечение фактов ──────────────────────────────────────────────────────────

_SCHEMA = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "object", "properties": {
    "subject": {"type": "string"}, "relation": {"type": "string"}, "object": {"type": "string"},
    "fact": {"type": "string"}, "importance": {"type": "integer"}},
    "required": ["subject", "relation", "object", "fact", "importance"]}}}, "required": ["facts"]}

_SYSTEM = (
    "Ты извлекаешь долговременные факты о людях из реплики. Подписанный говорящий — это человек, о котором идёт речь, "
    "когда он говорит «я», «мне», «моя»: подставляй его имя. Выписывай только устойчивые факты (имя, место жительства, "
    "работа, близкие люди, питомцы, увлечения, планы, события жизни), не мелочь и не просьбы к ассистенту. "
    "subject и object — короткие названия (человек, место, предмет), relation — короткий глагол или предикат на "
    "русском в виде одного слова или слов через подчёркивание (живёт_в, работает, подруга, смотрит, владеет), "
    "fact — одна законченная фраза на русском: кто, что, где. Если в реплике упомянут другой человек по имени, выпиши "
    "и факт о нём («Вася подарила Ане кота» → Вася подарила кота Ане; Вася — подруга Ани). Объект работы это "
    "должность или занятие, а не способ («работает дизайнером удалённо», а не «работает удалённо»). "
    "Если реплика говорит, что что-то изменилось (переехала, "
    "сменила работу), в fact так и напиши («Аня переехала в Казань и теперь живёт в Казани»). Если фактов нет, "
    "верни пустой список. Не записывай то, что верно только ближайшие минуты, то, что пересчитывается за секунду, и "
    "оценки человека, которые ему было бы неприятно прочитать: если факт стоит записи, запиши факт без оценки. "
    "importance (1-5) это важность факта для долгой памяти: 5 - имя, семья, здоровье, главные цели, события жизни; 4 - "
    "работа, учёба, место жительства, близкие, устойчивые предпочтения; 3 - увлечения и знакомые; 1-2 - мелочь, "
    "настроение, сиюминутное. Лучше пропустить факт, чем записать мелочь.")


@dataclass
class Fact:
    subject: str
    relation: str
    object: str
    fact: str
    importance: int = 3


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip(" .,;:")


def parse_facts(raw: str) -> list[Fact]:
    """Ответ модели → факты; всё неполное или слишком длинное отбрасывается."""
    try:
        items = json.loads(raw)["facts"]
    except Exception:
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        try:
            f = Fact(_clean(it["subject"]), _clean(it["relation"]).upper().replace(" ", "_"), _clean(it["object"]),
                     _clean(it["fact"]))
        except (KeyError, TypeError):
            continue
        if f.subject and f.relation and f.object and f.fact and max(map(len, (f.subject, f.object))) <= 60 \
                and len(f.fact) <= 220 and f.subject.lower() != f.object.lower():
            try:
                f.importance = max(1, min(5, int(it.get("importance", 3))))
            except (TypeError, ValueError):
                f.importance = 3
            out.append(f)
    return out[:8]


class FactExtractor:
    def __init__(self, post=None):
        self._post = post or self._http_post

    @staticmethod
    async def _http_post(payload: dict) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{LM_URL}/chat/completions", json=payload)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def extract(self, speaker: str, text: str) -> list[Fact]:
        payload = {"model": LLM_MODEL, "temperature": 0.1, "max_tokens": 700, "reasoning_effort": "none",
                   "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": f"{speaker}: {text}"}],
                   "response_format": {"type": "json_schema", "json_schema": {"name": "facts", "schema": _SCHEMA}}}
        return parse_facts(await self._post(payload))


# ── хранилище: SQLite ──────────────────────────────────────────────────────────

def _lms_path() -> str:
    return os.path.expandvars(r"%USERPROFILE%\.lmstudio\bin\lms.exe")


def ensure_model_loaded(model: str) -> bool:
    """LM Studio при подгрузке «по запросу» отменяет загрузку второй модели: загружаем явно через его CLI."""
    import httpx
    try:
        data = httpx.get(f"{LM_URL.removesuffix('/v1')}/api/v0/models", timeout=5).json()["data"]
    except Exception:
        return False
    st = next((m.get("state") for m in data if m["id"] == model), None)
    if st == "loaded":
        return True
    if st is None or not os.path.exists(_lms_path()):
        return False
    try:
        subprocess.run([_lms_path(), "load", model, "--ttl", "86400", "-y"], capture_output=True, timeout=180,
                       creationflags=0x08000000)
        return True
    except Exception:
        return False


class SqliteBackend:
    """Факты в одном файле SQLite (`graph.db`): структура в `FactStore`, поиск по словам и смыслу в `HybridIndex` (ТЗ 04).
    Группа = коллекция индекса. Эмбеддинги EmbeddingGemma из LM Studio; без них поиск идёт по словам."""

    def __init__(self, path: Path | str, embedder=None):
        self.path = Path(path)
        self.store = FactStore(self.path)
        self.embedder = embedder or LmStudioEmbedder(query_timeout=5.0)
        self.search_service = HybridSearch(HybridIndex(self.path), self.embedder,
                                           cfg=SearchConfig(min_cos=MIN_SCORE, min_cos_bm25=0.4))

    async def healthy(self) -> tuple[bool, str]:
        ok = await asyncio.to_thread(lambda: ensure_model_loaded(EMB_MODEL) and ensure_model_loaded(LLM_MODEL))
        return (True, "работает") if ok else (False, "в LM Studio нет моделей для памяти (Gemma и EmbeddingGemma)")

    async def add_fact(self, f: Fact, when: datetime, group: str = character.DEFAULT_ID, *, canon: bool = False,
                       source: str = "extract", profile: str | None = None) -> str:
        """Записать факт в группу; возвращает uuid связи (нужен, чтобы пометить воспоминание персонажа)."""
        now = time.time()
        uuid, new = await asyncio.to_thread(self.store.add, f.subject, f.relation, f.object, f.fact, when.timestamp(), group,
                                            now=now, canon=canon, source=source, importance=f.importance, profile=profile)
        if new:
            await self.search_service.add(group, uuid, f.fact, ts=when.timestamp())
        return uuid

    def remember_sync(self, f: Fact, when: float, group: str, *, profile: str | None, source: str) -> bool:
        """Запись без модели и без ожидания: строка в таблицу и в индекс по словам (вектор не нужен: профиль читается
        напрямую, а не поиском по смыслу). True, если факт новый."""
        uuid, new = self.store.add(f.subject, f.relation, f.object, f.fact, when, group, now=time.time(),
                                   source=source, importance=f.importance, profile=profile)
        if new:
            self.search_service.index.upsert(group, uuid, f.fact, ts=when)
        return new

    def profile_facts(self, group: str, profile: str, limit: int) -> list[dict]:
        return self.store.profile_facts(group, profile, limit)

    def forget_words(self, group: str, keys: list[str], need: int) -> int:
        ids = self.store.forget_by_words(group, keys, need)
        for uuid in ids:
            self.search_service.index.delete(group, uuid)
        return len(ids)

    async def upsert_fact(self, f: Fact, when: datetime, group: str = character.DEFAULT_ID):
        """Записать факт БЕЗ участия модели (данные игры достоверны): прежний факт того же отношения у субъекта
        удаляется, новый пишется с эмбеддингом."""
        gone = await asyncio.to_thread(self.store.replace_relation, f.subject, f.relation, group)
        for uuid in gone:
            await self.search_service.delete(group, uuid)
        await self.add_fact(f, when, group)

    async def groups_with_entities(self, group: str, names: list[str]) -> list[str]:
        """Области того же персонажа, где есть сущность с одним из имён (без регистра): для поиска в чужой области (KB-13)."""
        return await asyncio.to_thread(self.store.groups_with_entities, group, names)

    async def search(self, query: str, limit: int, group: str = character.DEFAULT_ID, groups: list[str] | None = None) -> list[dict]:
        """Факты по запросу (устаревшие тоже, с `invalid_at`); `score` это косинус, None - нашлось только по словам."""
        hits = await self.search_service.search(query, collections=groups or [group], k=limit, rewrite=False,
                                                include_stale=True)
        rows = await asyncio.to_thread(self.store.get, [h.key for h in hits])
        return [{"uuid": h.key, "fact": rows[h.key]["fact"], "valid_at": iso(rows[h.key]["valid_at"]),
                 "invalid_at": iso(rows[h.key]["invalid_at"]), "score": h.cos} for h in hits if h.key in rows]

    async def retire(self, subject: str, relations: tuple, group: str = character.DEFAULT_ID):
        """Закрыть датой факты субъекта с указанными отношениями (колонист выбыл): в поиске они уже не покажутся."""
        await asyncio.to_thread(self.store.retire, subject, relations, group, time.time())

    async def purge_retired(self, days: float = 3.0, group: str = character.DEFAULT_ID):
        """Удалить давно закрытые факты о выбывших колонистах."""
        gone = await asyncio.to_thread(self.store.purge, COLONIST_RELATIONS, group, time.time() - days * 86400)
        for uuid in gone:
            await self.search_service.delete(group, uuid)

    async def scores(self, query: str, facts: list[str]) -> list[float]:
        if not facts:
            return []
        q = (await self.embedder.embed([query], True))[0]
        return [cosine(q, v) for v in await self.embedder.embed(facts, False)]

    async def delete(self, uuid: str, group: str = character.DEFAULT_ID):
        await asyncio.to_thread(self.store.delete, uuid)
        await self.search_service.delete(group, uuid)

    async def snapshot(self, limit: int = 500, group: str = character.DEFAULT_ID) -> dict:
        return await asyncio.to_thread(self.store.snapshot, group, limit)

    async def stats(self, group: str = character.DEFAULT_ID) -> dict:
        """Сводка для дашборда: факты по состояниям, областям и источникам, размер файла и число проиндексированных кусков."""
        def work() -> dict:
            out = self.store.stats(group)
            out["db_bytes"] = sum(p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal")) if p.exists())
            out["indexed"] = self.search_service.index.count()
            return out
        return await asyncio.to_thread(work)

    # ── память персонажа: канон, загрузка, воспоминания о себе (ТЗ 05, CHR-16…CHR-19) ────────────────────────────

    async def has_canon(self, group: str, subject: str, relation: str) -> bool:
        """Есть ли у субъекта действующий канонический факт с этим отношением (защита канона, CHR-17)."""
        return await asyncio.to_thread(self.store.has_canon, group, subject, relation)

    async def seed_rows(self, group: str) -> dict[str, tuple[str, str]]:
        """Загруженные воспоминания персонажа: mem_id -> (uuid связи, текст факта)."""
        return await asyncio.to_thread(self.store.seed_rows, group)

    async def mark_memory(self, group: str, uuid: str, mem_id: str, mtype: str, about: str, weight: float, source: str):
        """Пометить связь как воспоминание персонажа: канон (seed) или жизнь (life), тип, субъект и вес."""
        await asyncio.to_thread(self.store.mark, uuid, mem_id, mtype, about, weight, source)

    async def delete_memory(self, group: str, mem_id: str):
        """Удалить воспоминание персонажа по id (запись удалена из YAML)."""
        for uuid in await asyncio.to_thread(self.store.delete_memory, group, mem_id):
            await self.search_service.delete(group, uuid)

    async def self_memories(self, group: str, subject: str) -> list[dict]:
        """Воспоминания о себе: связи персонажа-субъекта, у которых есть тип (ТЗ 05, CHR-19)."""
        return await asyncio.to_thread(self.store.self_memories, group, subject)


# ── колонисты из игры: перезапись по данным состояния ────────────────────────────

_SKILLS_RU = {"Shooting": "Стрельба", "Melee": "Ближний бой", "Construction": "Строительство", "Mining": "Добыча",
              "Cooking": "Готовка", "Plants": "Растениеводство", "Animals": "Животные", "Crafting": "Ремесло",
              "Artistic": "Искусство", "Medicine": "Медицина", "Social": "Общение", "Intellectual": "Интеллект"}
COLONIST_RELATIONS = ("КОЛОНИСТ", "НАВЫКИ", "ЧЕРТЫ", "СОСТОЯНИЕ")
_SYNC_EVERY = 300                       # сек: не чаще раза в пять минут, если состав колонии не менялся


def colonist_facts(pawn: dict, colony: str) -> list[Fact]:
    """Факты о колонисте строго по данным игры (без участия модели). Каждое отношение одно на колониста."""
    n = pawn.get("name") or "?"
    out = [Fact(n, "КОЛОНИСТ", colony,
                f"{n} — колонист поселения «{colony}» в RimWorld, {pawn.get('age', '?')} лет")]
    skills = sorted(((v, k) for k, v in (pawn.get("skills") or {}).items() if v and v > 0), reverse=True)[:3]
    if skills:
        out.append(Fact(n, "НАВЫКИ", f"навыки {n}",
                        f"Лучшие навыки {n}: " + ", ".join(f"{_SKILLS_RU.get(k, k)} {v}" for v, k in skills)))
    if pawn.get("traits"):
        out.append(Fact(n, "ЧЕРТЫ", f"черты {n}", f"Черты {n}: " + ", ".join(pawn["traits"])))
    bits = [f"здоровье {round((pawn.get('health_pct') or 0) * 100)}%", f"настроение {round((pawn.get('mood_pct') or 0) * 100)}%"]
    if pawn.get("is_downed"):
        bits.append("лежит раненым")
    if pawn.get("bleeding"):
        bits.append("кровоточит")
    if pawn.get("is_drafted"):
        bits.append("в бою")
    if pawn.get("current_job"):
        bits.append(f"занят: {pawn['current_job']}")
    out.append(Fact(n, "СОСТОЯНИЕ", f"состояние {n}", f"Сейчас {n}: " + ", ".join(bits)))
    return out


def colony_facts(colony: str, names: list[str]) -> list[Fact]:
    """Факты о самом поселении: кто им управляет и кто в нём живёт (колонисты связаны с ним узлом-поселением)."""
    me = character.active().name
    return [Fact(me, "УПРАВЛЯЕТ", colony, f"{me} управляет поселением «{colony}» в RimWorld"),
            Fact(colony, "СОСТАВ", f"состав {colony}", f"В поселении «{colony}» колонистов: {len(names)} ({', '.join(names)})")]


# ── граф памяти бота ────────────────────────────────────────────────────────────

class GraphMemory:
    def __init__(self, memory_dir: str, backend=None, extractor=None, busy=None):
        self._file = Path(memory_dir) / "graph_queue.json"
        self.backend = backend or SqliteBackend(Path(memory_dir) / "graph.db")
        self.extractor = extractor or FactExtractor()
        self._busy = busy or (lambda: False)         # () -> True, пока идёт разговор: тогда не занимаем модель
        # области (ТЗ 04, KB-12/13): scopes_of() - области активного профиля, scope_of(текст) - область нового факта; без них все в global
        self.scopes_of = None
        self.scope_of = None
        self._queue: list[dict] = self._load()
        self._wake = asyncio.Event()
        self._status = "ещё не проверялась"
        self._health_at = 0.0
        self._healthy = False
        self._colony_names: list[str] = []
        self._last_sync = 0.0
        self._last_purge = 0.0
        self._character = character.DEFAULT_ID     # персонаж, под которым пишутся новые факты (ТЗ 05, CHR-9)
        self._self_replies = 0                     # реплик с последнего напоминания о себе (CHR-19)

    # персонаж
    def set_character(self, char_id: str) -> None:
        """Переключение персонажа (ТЗ 05, CHR-7): новые факты уходят в его группу; очередь общая, метка на каждой записи."""
        self._character = char_id

    def group(self) -> str:
        return character.group_id(self._character)

    def _names(self, char_id: str) -> set[str]:
        """Имена персонажа в нижнем регистре: имя и алиасы (для защиты «я», CHR-18)."""
        try:
            ch = character.load(char_id)
        except character.CharacterError:
            return {char_id.lower()}
        return {n.lower() for n in (ch.name, *ch.aliases)}

    # очередь
    def _load(self) -> list[dict]:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self):
        try:
            self._file.write_text(json.dumps(self._queue[-MAX_QUEUE:], ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def enqueue(self, speaker: str, text: str, ts: float | None = None, source: str | None = None, owner: bool = False):
        """Положить реплику в очередь на разбор. Короткое и пустое не берём. `source` - id эпизода (ТЗ 27): факт знает, откуда он."""
        text = text.strip()
        if len(text) < MIN_CHARS:
            return
        self._queue.append({"speaker": speaker or "Пользователь", "text": text[:1500], "ts": ts or time.time(), "tries": 0,
                            "character": self._character, **({"source": str(source)[:60]} if source else {}),
                            **({"owner": True} if owner else {})})
        self._queue = self._queue[-MAX_QUEUE:]
        self._save()
        self._wake.set()

    def pending(self) -> int:
        return len(self._queue)

    def status_text(self) -> str:
        if not settings.load().get("graph_memory", True):
            return "Факты: запись выключена в настройках."
        return f"Факты: {self._status}" + (f" (в очереди {len(self._queue)})" if self._queue else "") + "."

    # фоновый разбор
    async def _ready(self) -> bool:
        if time.time() - self._health_at > 60:
            self._healthy, self._status = await self.backend.healthy()
            self._health_at = time.time()
        return self._healthy

    async def step(self) -> bool:
        """Разобрать одну реплику из очереди. True, если что-то сделано."""
        if not self._queue or not settings.load().get("graph_memory", True) or self._busy():
            return False
        if not await self._ready():
            return False
        item = self._queue[0]
        try:
            char_id = item.get("character") or character.DEFAULT_ID
            group = character.group_id(char_id)
            facts = await self.extractor.extract(item["speaker"], item["text"])
            when = datetime.fromtimestamp(item["ts"], timezone.utc)
            names = self._names(char_id)
            fact_group = group
            if facts and self.scope_of is not None:       # KB-12: факт по теме профиля уходит в его область, при сомнении global
                try:
                    scope = item.get("scope") or await self.scope_of(item["text"])
                except Exception:
                    scope = graph_scope.GLOBAL
                if scope and scope != graph_scope.GLOBAL:
                    fact_group = character.group_id(char_id, scope)
            written = []
            for f in facts:
                if f.subject.lower() in names:            # CHR-18: реплика собеседника не пишет в «я» персонажа
                    await self._about_character(f, group)
                    continue
                about_owner = bool(item.get("owner")) and f.subject.lower() == str(item["speaker"]).lower()
                if f.importance < (MIN_IMPORTANCE_PROFILE if about_owner else MIN_IMPORTANCE_OTHER):
                    continue                                   # мелочь в память не идёт
                await self.backend.add_fact(f, when, fact_group, **({"profile": "owner"} if about_owner else {}))
                written.append(f)
            facts = written
            self._queue.pop(0)
            self._save()
            if facts:
                src = f" [эпизод {item['source']}]" if item.get("source") else ""
                events_log.log("memory", f"граф: +{len(facts)} фактов{src}: " + "; ".join(f.fact for f in facts)[:300],
                               **({"episode": item["source"]} if item.get("source") else {}))
            return True
        except Exception as e:
            item["tries"] += 1
            logger.warning(f"Граф: {e}")
            if item["tries"] >= MAX_ATTEMPTS:
                self._queue.pop(0)
                events_log.log("error", f"граф: реплика пропущена после {MAX_ATTEMPTS} сбоев: {str(e)[:120]}")
            self._save()
            self._health_at = 0.0            # перепроверить доступность
            return False

    async def _about_character(self, f: Fact, group: str) -> None:
        """Факт о персонаже из реплики собеседника не применяется (CHR-18). Если у него есть канон с тем же отношением,
        это противоречие: пишем событие memory_conflict и не трогаем канон (CHR-17)."""
        try:
            conflict = await self.backend.has_canon(group, f.subject, f.relation)
        except Exception as e:
            logger.warning(f"Граф, канон: {e}")
            conflict = False
        if conflict:
            events_log.log("memory_conflict", f"канон сохранён, факт не применён: {f.fact}", relation=f.relation)

    async def run(self):
        while True:
            try:
                done = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Граф: {e}")
                done = False
            if done:
                continue
            if self._queue:
                await asyncio.sleep(20)
            else:
                await self._ready()          # статус (база, модели) обновляется и без очереди
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 300)
                except asyncio.TimeoutError:
                    pass


    # колонисты: данные игры перезаписываются, выбывшие помечаются устаревшими
    async def sync_colonists(self, pawns: list[dict], colony: str, force: bool = False) -> int:
        """Привести граф в соответствие с игрой: у действующих колонистов факты ЗАМЕНЯЮТСЯ новыми (если изменились),
        у пропавших факты закрываются датой. Возвращает число записанных фактов."""
        pawns = [p for p in pawns if p.get("name") and not p.get("is_prisoner")]
        names = sorted(p["name"] for p in pawns)
        if not pawns or not settings.load().get("graph_memory", True):
            return 0
        if not force and names == self._colony_names and time.time() - self._last_sync < _SYNC_EVERY:
            return 0
        if not await self._ready():
            return 0
        written = 0
        try:
            sums = self._load_sums()
            batch = [f for p in pawns for f in colonist_facts(p, colony)] + colony_facts(colony, names)
            for f in batch:
                key = f"{f.subject}|{f.relation}"
                if sums.get(key) == f.fact:
                    continue
                await self.backend.upsert_fact(f, datetime.now(timezone.utc), self.group())
                sums[key] = f.fact
                written += 1
            gone = sorted({k.split("|")[0] for k in sums if k.endswith("|КОЛОНИСТ")} - set(names))
            for who in gone:
                await self.backend.retire(who, COLONIST_RELATIONS, self.group())
                for rel in COLONIST_RELATIONS:
                    sums.pop(f"{who}|{rel}", None)
            self._save_sums(sums)
            self._colony_names, self._last_sync = names, time.time()
            purge = getattr(self.backend, "purge_retired", None)
            if purge and time.time() - self._last_purge > 3600:
                self._last_purge = time.time()
                await purge(group=self.group())
            if written or gone:
                events_log.log("memory", f"граф: колонисты обновлены ({written} фактов), выбыли: {', '.join(gone) or 'нет'}")
        except Exception as e:
            logger.warning(f"Граф, колонисты: {e}")
        return written

    def _load_sums(self) -> dict:
        try:
            return json.loads((self._file.parent / "graph_colonists.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_sums(self, sums: dict):
        try:
            (self._file.parent / "graph_colonists.json").write_text(json.dumps(sums, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ── профили: владелец и она сама (запись и чтение без модели) ──────────────────────────────

    def remember(self, text: str, importance: int = 4, subject: str = "") -> bool:
        """Запомнить факт в профиль владельца (от третьего лица; сюда же его пожелания к ней и договорённости).
        Записывается сразу, без очереди и модели. Мелочь (важность ниже порога профиля) не пишется. True, если запись новая."""
        text = re.sub(r"\s+", " ", str(text)).strip()[:300]
        if len(text) < 6 or importance < MIN_IMPORTANCE_PROFILE:
            return False
        new_stems = stemlib.stems(text)
        for old in self.profile_facts("owner", 200):       # то же самое уже записано (имя владельца могло смениться, слова другие)
            old_stems = stemlib.stems(old["text"])
            if old["text"].lower() == text.lower() or (
                    new_stems and old_stems and len(new_stems & old_stems) / min(len(new_stems), len(old_stems)) >= SAME_FACT_OVERLAP):
                return False
        f = Fact((subject or "Пользователь").strip() or "Пользователь", "ЗАМЕТКА", text[:60], text, int(importance))
        try:
            return self.backend.remember_sync(f, time.time(), self.group(), profile="owner", source="remember")
        except Exception as e:
            logger.warning(f"Граф, запись профиля: {e}")
            return False

    def profile_facts(self, profile: str = "owner", limit: int = 20) -> list[dict]:
        """Факты профиля владельца без поиска и моделей: [{uuid, text, importance, ts}]."""
        try:
            return self.backend.profile_facts(self.group(), profile, limit)
        except Exception as e:
            logger.warning(f"Граф, профиль: {e}")
            return []

    def profile_block(self) -> str:
        """Блок контекста: главное о владельце. Всегда в ответе владельцу, вне темы разговора."""
        owner = self.profile_facts("owner", PROFILE_FACTS)
        if not owner:
            return ""
        return "Главное, что ты помнишь о пользователе (включая его пожелания к тебе):\n" + "\n".join(f"  - {f['text']}" for f in owner)

    def forget_matching(self, text: str, strict: bool = False) -> int:
        """Забыть факты (кроме канона), где есть значимые слова фразы: «забудь что я люблю чай» находит «Любит чай»."""
        if strict:
            keys, need = [text.lower().strip()], 1
        else:
            keys = [w[:3] if len(w) > 3 else w for w in stemlib.words(text)]
            if not keys:
                return 0
            need = max(1, -(-len(keys) * 6 // 10))
        try:
            return self.backend.forget_words(self.group(), keys, need)
        except Exception as e:
            logger.warning(f"Граф, забывание: {e}")
            return 0

    # чтение
    async def search(self, query: str, limit: int = 6, include_old: bool = False) -> list[dict]:
        """Факты по смыслу запроса: без устаревших (если не просили историю) и без слабых совпадений."""
        if not query.strip() or not await self._ready():
            return []
        try:
            groups = await self.search_groups(query)
            if len(groups) > 1:                       # KB-13: global + области профиля (+ чужая по названной сущности)
                found = await self.backend.search(query, limit * 2, self.group(), groups=groups)
            else:
                found = await self.backend.search(query, limit * 2, self.group())
            if not include_old:
                found = [f for f in found if not f.get("invalid_at")]
            if not found:
                return []
            if all("score" in f for f in found):          # бэкенд уже отсёк слабое по косинусу (None - нашлось по словам)
                keep = [dict(f, score=round(f["score"] or 0.0, 3)) for f in found]
            else:
                scores = await self.backend.scores(query, [f["fact"] for f in found])
                keep = [dict(f, score=round(s, 3)) for f, s in zip(found, scores, strict=False) if s >= MIN_SCORE]
            return sorted(keep, key=lambda f: -f["score"])[:limit]
        except Exception as e:
            logger.warning(f"Граф, поиск: {e}")
            return []

    async def search_groups(self, query: str) -> list[str]:
        """Группы поиска (KB-13): личная память + области активного профиля; чужая область добавляется, только если в реплике
        названа сущность, которая в графе есть лишь в ней (точное имя узла без регистра)."""
        base = self.group()
        groups = [base]
        if self.scopes_of is None:
            return groups
        try:
            own = [character.group_id(self._character, s) for s in self.scopes_of() if s and s != graph_scope.GLOBAL]
            groups += [g for g in own if g not in groups]
            finder = getattr(self.backend, "groups_with_entities", None)
            if finder is not None:
                names = graph_scope.entity_candidates(query)
                if names:
                    foreign = await finder(base, names)
                    if foreign and not set(foreign) & set(groups):
                        groups += [g for g in foreign if g not in groups][:graph_scope.MAX_FOREIGN]
        except Exception as e:
            logger.info(f"Граф, области поиска: {e}")
        return groups

    async def self_memories(self, query: str, n: int = character_memory.N_PICK) -> list[dict]:
        """Воспоминания персонажа о себе, подходящие к реплике (ТЗ 05, CHR-19). Пусто, если ничего не прошло порог."""
        if not query.strip() or not await self._ready():
            return []
        try:
            cands = await self.backend.self_memories(self.group(), self._subject())
            if not cands:
                return []
            scores = await self.backend.scores(query, [c["fact"] for c in cands])
            q_stems = stemlib.stems(query)       # общие слова с репликой поднимают оценку: «прошлая жизнь» ↔ «была человеком»
            scores = [s + character_memory.WORD_BONUS * min(2, len(q_stems & stemlib.stems(c["fact"])))
                      for s, c in zip(scores, cands, strict=False)]
            self._self_replies += 1
            picked, nudged = character_memory.pick(cands, scores, n=n, min_score=character_memory.SELF_MIN_SCORE,
                                                   nudge=self._self_replies >= character_memory.NUDGE_EVERY)
            if nudged:
                self._self_replies = 0
            return picked
        except Exception as e:
            logger.warning(f"Граф, воспоминания о себе: {e}")
            return []

    def _subject(self) -> str:
        try:
            return character.load(self._character).name
        except character.CharacterError:
            return self._character

    async def context_block(self, query: str) -> str:
        """Факты по теме. Факты профиля владельца сюда не входят: владельцу они уже показаны блоком профиля (без повторов), а чужому
        голосу (`[говорит …]`, `[незнакомый голос]`) профиль владельца не показываем вовсе."""
        from app.memory_capture import SPEAKER_TAG
        shown = {f["uuid"] for f in self.profile_facts("owner", 500 if SPEAKER_TAG.match(query) else PROFILE_FACTS)}
        found = await self.search(query, limit=CONTEXT_FACTS + len(shown))
        facts = [f for f in found if f.get("uuid") not in shown][:CONTEXT_FACTS]
        if not facts:
            return ""
        return "Что ты знаешь по теме (из памяти-графа, это проверенные факты):\n" + "\n".join(f"- {f['fact']}" for f in facts)

    async def forget(self, uuid: str) -> bool:
        try:
            await self.backend.delete(uuid, self.group())
            return True
        except Exception as e:
            logger.warning(f"Граф, удаление: {e}")
            return False

    async def snapshot(self) -> dict:
        try:
            return await self.backend.snapshot(group=self.group())
        except Exception as e:
            return {"entities": [], "edges": [], "error": str(e)[:200]}

    async def stats(self) -> dict:
        """Состояние памяти-графа для дашборда: включена ли, что с моделями, очередь и сводка по фактам."""
        out = {"enabled": bool(settings.load().get("graph_memory", True)), "healthy": self._healthy, "status": self._status,
               "queue": len(self._queue), "group": self.group(), "min_score": MIN_SCORE, "context_facts": CONTEXT_FACTS}
        fn = getattr(self.backend, "stats", None)
        if fn is not None:
            try:
                out.update(await fn(self.group()))
            except Exception as e:
                out["error"] = str(e)[:200]
        return out

    async def explain(self, query: str) -> dict:
        """Проверка поиска для дашборда: какие факты находятся по запросу (с косинусом) и какой блок уйдёт в промпт."""
        hits = await self.search(query, limit=8, include_old=True)
        return {"query": query, "hits": hits, "context": await self.context_block(query), "min_score": MIN_SCORE,
                "context_facts": CONTEXT_FACTS}
