"""Память-граф: факты о людях и мире в графе Graphiti (FalkorDB), всё локально.

Схема (проверена пробой, см. docs/MEMORY_GRAPH.md):
  1. ИЗВЛЕЧЕНИЕ наше: одна простая задача для локальной Gemma («выпиши устойчивые факты») со строгой схемой ответа.
     Полный конвейер Graphiti на малой модели терял половину фактов, а так проходят все.
  2. ХРАНЕНИЕ Graphiti: add_triplet сливает дубли сущностей, закрывает устаревшие факты (переехала в Казань →
     «живёт в Москве» закрыт датой), считает эмбеддинги (EmbeddingGemma в LM Studio) и ищет по смыслу и графу.
  3. ПОИСК: устаревшее по умолчанию не показываем; слабые совпадения отсекаются порогом по косинусу.
Работает в фоне: реплики кладутся в очередь (файл memory/graph_queue.json, переживает перезапуск), разбираются, когда
она не занята разговором. Если FalkorDB или модели недоступны, бот просто живёт без графа (очередь копится).
Телеметрия Graphiti выключена.
"""
import asyncio
import contextvars
import json
import logging
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
os.environ.setdefault("SEMAPHORE_LIMIT", "2")           # локальной модели хватает двух параллельных запросов

import character
import character_memory
import events_log
import graph_scope
import settings
from envcompat import env

logger = logging.getLogger("graph_memory")

LM_URL = env("GITS_LM_URL", "http://127.0.0.1:1234/v1")
LLM_MODEL = env("GITS_GRAPH_LLM", "google/gemma-4-e4b")
EMB_MODEL = env("GITS_GRAPH_EMBEDDER", "text-embedding-google_embeddinggemma-300m-qat")
FALKOR_HOST = env("GITS_GRAPH_HOST", "127.0.0.1")
FALKOR_PORT = int(env("GITS_GRAPH_PORT", "6380"))
MIN_SCORE = 0.5                   # косинус «запрос ↔ факт»: ниже считаем несвязанным (релевантное ~0.75, мусор ~0.3)
MIN_CHARS = 12                    # короче реплику на факты не разбираем
MAX_QUEUE = 300
CONTEXT_FACTS = 3                 # фактов графа в контексте ответа (ТЗ 04, KB-13)
MAX_ATTEMPTS = 3

_emb_mode = contextvars.ContextVar("emb_mode", default="document")   # query | document: у EmbeddingGemma разные приставки

# ── извлечение фактов ──────────────────────────────────────────────────────────

_SCHEMA = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "object", "properties": {
    "subject": {"type": "string"}, "relation": {"type": "string"}, "object": {"type": "string"},
    "fact": {"type": "string"}}, "required": ["subject", "relation", "object", "fact"]}}}, "required": ["facts"]}

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
    "верни пустой список.")


@dataclass
class Fact:
    subject: str
    relation: str
    object: str
    fact: str


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


# ── Graphiti ───────────────────────────────────────────────────────────────────

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


def _cos(a, b) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb) if na and nb else 0.0


class GraphitiBackend:
    """Тонкая обёртка над Graphiti. Тяжёлые импорты ленивые: без graphiti-core бот просто не использует граф."""

    def __init__(self):
        self._g: dict[str, object] = {}     # один клиент Graphiti на базу; база = id персонажа (ТЗ 05, CHR-9)
        self._emb = None

    def _build(self, database: str):
        import httpx
        from openai import AsyncOpenAI
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.client import CrossEncoderClient
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.embedder.client import EmbedderClient, EmbedderConfig
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
        from types import SimpleNamespace

        def prep(t):
            return f"task: search result | query: {t}" if _emb_mode.get() == "query" else f"title: none | text: {t}"

        class LMEmbedder(EmbedderClient):
            def __init__(self):
                self.config = EmbedderConfig(embedding_dim=768)
                self._http = httpx.AsyncClient(timeout=120)

            async def embed(self, texts):
                r = await self._http.post(f"{LM_URL}/embeddings", json={"model": EMB_MODEL, "input": [prep(t) for t in texts]})
                r.raise_for_status()
                return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]

            async def create(self, input_data):
                texts = [input_data] if isinstance(input_data, str) else [str(x) for x in input_data]
                return (await self.embed(texts))[0]

            async def create_batch(self, input_data_list):
                return await self.embed([str(t) for t in input_data_list])

        class EmbedReranker(CrossEncoderClient):
            def __init__(self, emb):
                self._emb = emb

            async def rank(self, query, passages):
                tok = _emb_mode.set("query")
                try:
                    q = (await self._emb.embed([query]))[0]
                finally:
                    _emb_mode.reset(tok)
                vs = await self._emb.embed(passages)
                return sorted(((p, _cos(q, v)) for p, v in zip(passages, vs, strict=False)), key=lambda x: -x[1])

        class NoThink:        # «размышления» Gemma выключаем: иначе каждый вызов в разы дольше
            def __init__(self, real):
                self._real = real
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

            async def _create(self, **kw):
                kw.setdefault("extra_body", {})["reasoning_effort"] = "none"
                return await self._real.chat.completions.create(**kw)

        if self._emb is None:
            self._emb = LMEmbedder()
        llm = OpenAIGenericClient(
            config=LLMConfig(api_key="lm-studio", model=LLM_MODEL, small_model=LLM_MODEL, base_url=LM_URL, temperature=0.1),
            client=NoThink(AsyncOpenAI(api_key="lm-studio", base_url=LM_URL, timeout=600)),
            max_tokens=4096, structured_output_mode="json_schema")
        return Graphiti(graph_driver=FalkorDriver(host=FALKOR_HOST, port=FALKOR_PORT, database=database),
                        llm_client=llm, embedder=self._emb, cross_encoder=EmbedReranker(self._emb))

    async def _client(self, database: str = character.DEFAULT_ID):
        if database not in self._g:
            g = await asyncio.to_thread(self._build, database)
            await g.build_indices_and_constraints()
            self._g[database] = g
        return self._g[database]

    async def healthy(self) -> tuple[bool, str]:
        try:
            from falkordb import FalkorDB
            await asyncio.to_thread(lambda: FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).connection.ping())
        except Exception:
            return False, f"база графа не отвечает ({FALKOR_HOST}:{FALKOR_PORT}): docker compose up -d graph"
        ok = await asyncio.to_thread(lambda: ensure_model_loaded(EMB_MODEL) and ensure_model_loaded(LLM_MODEL))
        return (True, "работает") if ok else (False, "в LM Studio нет моделей для памяти (Gemma и EmbeddingGemma)")

    async def add_fact(self, f: Fact, when: datetime, group: str = character.DEFAULT_ID) -> str:
        """Записать факт в группу; возвращает uuid связи (нужен, чтобы пометить воспоминание персонажа)."""
        from graphiti_core.edges import EntityEdge
        from graphiti_core.nodes import EntityNode
        g = await self._client(character.database_of(group))
        s, o = EntityNode(name=f.subject, group_id=group, labels=["Entity"]), EntityNode(name=f.object, group_id=group, labels=["Entity"])
        e = EntityEdge(group_id=group, source_node_uuid=s.uuid, target_node_uuid=o.uuid, created_at=datetime.now(timezone.utc),
                       name=f.relation, fact=f.fact, valid_at=when)
        await g.add_triplet(s, e, o)
        return e.uuid

    async def groups_with_entities(self, group: str, names: list[str]) -> list[str]:
        """Области (группы той же базы), где есть узел с одним из имён (без регистра): для поиска в чужой области (KB-13)."""
        from falkordb import FalkorDB

        def run() -> list[str]:
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            rows = gr.query("MATCH (n:Entity) WHERE toLower(n.name) IN $names RETURN DISTINCT n.group_id",
                            {"names": [n.lower() for n in names]}).result_set
            return [r[0] for r in rows]
        return await asyncio.to_thread(run)

    async def search(self, query: str, limit: int, group: str = character.DEFAULT_ID, groups: list[str] | None = None) -> list[dict]:
        g = await self._client(character.database_of(group))
        tok = _emb_mode.set("query")
        try:
            edges = await g.search(query, group_ids=groups or [group], num_results=limit)
        finally:
            _emb_mode.reset(tok)
        return [{"uuid": e.uuid, "fact": e.fact, "valid_at": e.valid_at, "invalid_at": e.invalid_at} for e in edges]


    async def upsert_fact(self, f: Fact, when: datetime, group: str = character.DEFAULT_ID):
        """Записать факт БЕЗ участия модели (данные игры достоверны): узел находится по имени, прежний факт того же
        отношения у субъекта удаляется, новый пишется с эмбеддингом. Быстро: только эмбеддинги."""
        from graphiti_core.edges import EntityEdge
        from graphiti_core.nodes import EntityNode
        from falkordb import FalkorDB
        g = await self._client(character.database_of(group))

        def lookup():
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            uuids = {}
            for name in (f.subject, f.object):
                rows = gr.query("MATCH (n:Entity {name: $n, group_id: $g}) RETURN n.uuid", {"n": name, "g": group}).result_set
                uuids[name] = rows[0][0] if rows else None
            gr.query("MATCH (a:Entity {name: $n, group_id: $g})-[r:RELATES_TO {name: $rel}]->() WHERE r.canon IS NULL OR r.canon = false DELETE r",
                     {"n": f.subject, "g": group, "rel": f.relation})
            return uuids
        uuids = await asyncio.to_thread(lookup)
        nodes = []
        for name in (f.subject, f.object):
            node = EntityNode(name=name, group_id=group, labels=["Entity"])
            if uuids[name]:
                node.uuid = uuids[name]
            await node.generate_name_embedding(g.embedder)
            await node.save(g.driver)
            nodes.append(node)
        edge = EntityEdge(group_id=group, source_node_uuid=nodes[0].uuid, target_node_uuid=nodes[1].uuid,
                          created_at=datetime.now(timezone.utc), name=f.relation, fact=f.fact, valid_at=when)
        await edge.generate_embedding(g.embedder)
        await edge.save(g.driver)

    async def retire(self, subject: str, relations: tuple, group: str = character.DEFAULT_ID):
        """Закрыть датой факты субъекта с указанными отношениями (колонист выбыл): в поиске они уже не покажутся."""
        def work():
            from falkordb import FalkorDB
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            gr.query("MATCH (a:Entity {name: $n, group_id: $g})-[r:RELATES_TO]->() WHERE r.name IN $rels AND r.invalid_at IS NULL AND (r.canon IS NULL OR r.canon = false) "
                     "SET r.invalid_at = $now", {"n": subject, "g": group, "rels": list(relations),
                                                 "now": datetime.now(timezone.utc).isoformat()})
        await asyncio.to_thread(work)

    async def purge_retired(self, days: float = 3.0, group: str = character.DEFAULT_ID):
        """Удалить давно закрытые факты о выбывших колонистах и узлы без связей (старше часа)."""
        def work():
            from falkordb import FalkorDB
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            iso = lambda sec: datetime.fromtimestamp(time.time() - sec, timezone.utc).isoformat()
            gr.query("MATCH (a:Entity {group_id: $g})-[r:RELATES_TO]->() WHERE r.name IN $rels AND r.invalid_at IS NOT NULL "
                     "AND r.invalid_at < $cut AND (r.canon IS NULL OR r.canon = false) DELETE r", {"g": group, "rels": list(COLONIST_RELATIONS), "cut": iso(days * 86400)})
            gr.query("MATCH (n:Entity {group_id: $g}) WHERE NOT (n)--() AND n.created_at < $cut DELETE n",
                     {"g": group, "cut": iso(3600)})
        await asyncio.to_thread(work)

    async def scores(self, query: str, facts: list[str]) -> list[float]:
        await self._client()
        tok = _emb_mode.set("query")
        try:
            q = (await self._emb.embed([query]))[0]
        finally:
            _emb_mode.reset(tok)
        vs = await self._emb.embed(facts)
        return [_cos(q, v) for v in vs]

    async def delete(self, uuid: str, group: str = character.DEFAULT_ID):
        from graphiti_core.edges import EntityEdge
        g = await self._client(character.database_of(group))
        edge = await EntityEdge.get_by_uuid(g.driver, uuid)
        await edge.delete(g.driver)

    async def snapshot(self, limit: int = 300, group: str = character.DEFAULT_ID) -> dict:
        def work():
            from falkordb import FalkorDB
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            nodes = [r[0] for r in gr.query("MATCH (n:Entity {group_id: $g}) RETURN n.name LIMIT $l", {"l": limit, "g": group}).result_set]
            edges = [{"from": r[0], "relation": r[1], "to": r[2], "fact": r[3], "uuid": r[4],
                      "valid_at": str(r[5])[:10] if r[5] else None, "invalid_at": str(r[6])[:10] if r[6] else None}
                     for r in gr.query("MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE a.group_id = $g RETURN a.name, r.name, b.name, r.fact, "
                                       "r.uuid, r.valid_at, r.invalid_at LIMIT $l", {"l": limit, "g": group}).result_set]
            return {"entities": nodes, "edges": edges}
        return await asyncio.to_thread(work)


    # ── память персонажа: канон, загрузка, воспоминания о себе (ТЗ 05, CHR-16…CHR-19) ────────────────────────────

    async def has_canon(self, group: str, subject: str, relation: str) -> bool:
        """Есть ли у субъекта действующий канонический факт с этим отношением (защита канона, CHR-17)."""
        def work():
            from falkordb import FalkorDB
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            rows = gr.query("MATCH (a:Entity {name: $n, group_id: $g})-[r:RELATES_TO {name: $rel}]->() "
                            "WHERE r.canon = true AND r.invalid_at IS NULL RETURN count(r)",
                            {"n": subject, "g": group, "rel": relation}).result_set
            return bool(rows and rows[0][0])
        return await asyncio.to_thread(work)

    async def seed_rows(self, group: str) -> dict[str, tuple[str, str]]:
        """Загруженные воспоминания персонажа: mem_id -> (uuid связи, текст факта)."""
        def work():
            from falkordb import FalkorDB
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            rows = gr.query("MATCH (a:Entity {group_id: $g})-[r:RELATES_TO]->() WHERE r.source = 'seed' "
                            "RETURN r.mem_id, r.uuid, r.fact", {"g": group}).result_set
            return {r[0]: (r[1], r[2]) for r in rows if r[0]}
        return await asyncio.to_thread(work)

    async def mark_memory(self, group: str, uuid: str, mem_id: str, mtype: str, about: str, weight: float, source: str):
        """Пометить связь как воспоминание персонажа: канон (seed) или жизнь (life), тип, субъект и вес."""
        def work():
            from falkordb import FalkorDB
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            gr.query("MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.mem_id = $m, r.canon = $canon, r.source = $src, "
                     "r.mtype = $t, r.about = $about, r.weight = $w",
                     {"u": uuid, "m": mem_id, "canon": source == "seed", "src": source, "t": mtype, "about": about,
                      "w": float(weight)})
        await asyncio.to_thread(work)

    async def delete_memory(self, group: str, mem_id: str):
        """Удалить воспоминание персонажа по id (запись удалена из YAML)."""
        def work():
            from falkordb import FalkorDB
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            gr.query("MATCH (a:Entity {group_id: $g})-[r:RELATES_TO {mem_id: $m}]->() DELETE r", {"g": group, "m": mem_id})
        await asyncio.to_thread(work)

    async def self_memories(self, group: str, subject: str) -> list[dict]:
        """Воспоминания о себе: связи персонажа-субъекта, у которых есть тип (ТЗ 05, CHR-19)."""
        def work():
            from falkordb import FalkorDB
            gr = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT).select_graph(character.database_of(group))
            rows = gr.query("MATCH (a:Entity {name: $n, group_id: $g})-[r:RELATES_TO]->() WHERE r.mtype IS NOT NULL "
                            "AND r.invalid_at IS NULL RETURN r.mem_id, r.uuid, r.fact, r.mtype, r.weight, r.source",
                            {"n": subject, "g": group}).result_set
            return [{"mem_id": r[0], "uuid": r[1], "fact": r[2], "mtype": r[3], "weight": float(r[4] or 0), "source": r[5]}
                    for r in rows]
        return await asyncio.to_thread(work)


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
        self.backend = backend or GraphitiBackend()
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

    def enqueue(self, speaker: str, text: str, ts: float | None = None, source: str | None = None):
        """Положить реплику в очередь на разбор. Короткое и пустое не берём. `source` - id эпизода (ТЗ 27): факт знает, откуда он."""
        text = text.strip()
        if len(text) < MIN_CHARS:
            return
        self._queue.append({"speaker": speaker or "Пользователь", "text": text[:1500], "ts": ts or time.time(), "tries": 0,
                            "character": self._character, **({"source": str(source)[:60]} if source else {})})
        self._queue = self._queue[-MAX_QUEUE:]
        self._save()
        self._wake.set()

    def pending(self) -> int:
        return len(self._queue)

    def status_text(self) -> str:
        if not settings.load().get("graph_memory", True):
            return "Память-граф: выключена в настройках."
        return f"Память-граф: {self._status}" + (f" (в очереди {len(self._queue)})" if self._queue else "") + "."

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
                await self.backend.add_fact(f, when, fact_group)
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

    def migrate_from_store(self, store, owner: str):
        """Один раз перенести в очередь факты о пользователе из старой памяти (memory_store): дальше их разберёт Gemma."""
        if store.meta("graph_migrated"):
            return 0
        n = 0
        for item in store.kind("user"):
            self.enqueue(owner or "Пользователь", f"{item['text']}", item.get("ts"))
            n += 1
        store.set_meta("graph_migrated", True)
        return n

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
        if self.scopes_of is None or not settings.load().get("knowledge", {}).get("scopes", True):
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
            self._self_replies += 1
            picked, nudged = character_memory.pick(cands, scores, n=n, min_score=MIN_SCORE,
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
        facts = await self.search(query, limit=CONTEXT_FACTS)
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
