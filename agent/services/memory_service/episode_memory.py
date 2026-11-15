"""Память персонажа поверх эпизодов: запись заметок, забывание, блоки контекста ответа (единая память вместо графа фактов).

Блоки контекста:
  «Твои воспоминания (это о тебе)»  канон из memories.yaml, подходящий к реплике: до трёх, по порогу `SELF_MIN_SCORE` с прибавкой за
                                    общие слова; раз в `NUDGE_EVERY` реплик, если ничего не подошло, напоминание о привычке или мнении
                                    (CHR-19). Смысл берётся из векторов, уже лежащих в индексе; нет эмбеддера или у записи ещё нет
                                    вектора - оценка по общим словам (запрос «помнишь Кэролайн?» находит запись с этим именем без нейросети);
  «Что ты помнишь по теме»          остальные эпизоды (жизнь и заметки) тем же гибридным поиском: свежесть и яркость идут в вес.
Записи о владельце отдельной категории не имеют: это обычные эпизоды (заметки и жизнь), находятся поиском по теме.

Запись «запомни» (`remember`) это заметка `kind=note` текстом; индекс по словам готов сразу, вектор добавит сон, когда эмбеддер доступен.
Забывание (`forget_matching`, `forget`) трогает эпизоды и заметки, но никогда не канон (CHR-17)."""
import asyncio
import hashlib
import logging
import re
import time

from services.character import character_memory
from services.conversation import stems as stemlib
from services.memory_service import episodes as dom
from services.memory_service.search.index import cosine

logger = logging.getLogger("episode_memory")

TOPIC_LIMIT = 4                  # эпизодов «по теме» в контексте ответа
MIN_NOTE_IMPORTANCE = 3          # важность ниже: мелочь, в память не пишется
EMBED_TIMEOUT_S = 3.0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


class EpisodeMemory:
    def __init__(self, *, store, recall, search, clock=time.time):
        """store - `EpisodeStore`, recall - `EpisodeRecall`, search - `HybridSearch` (индекс и эмбеддер той же базы)."""
        self.store, self.recall, self.search, self.clock = store, recall, search, clock
        self._self_replies = 0                       # реплик с последнего напоминания о себе (CHR-19)

    # ── запись и забывание ───────────────────────────────────────────────────

    def remember(self, text: str, importance: int = 4) -> bool:
        """Запомнить заметку (то, что она или владелец пожелали сохранить; договорённости, поправки). Записывается сразу, без
        очереди и модели. Мелочь (важность ниже порога) и повтор уже записанного не пишутся. Яркость заметки это важность / 5.
        True, если запись новая."""
        text = _norm(text)[:600]
        if len(text) < 6 or int(importance) < MIN_NOTE_IMPORTANCE:
            return False
        ep_id = "n-" + hashlib.sha1(text.lower().encode("utf-8")).hexdigest()[:12]
        salience = max(1, min(5, int(importance))) / 5
        try:
            if not self.store.add_note(ep_id, text, salience=salience, ts=self.clock()):
                return False
            self.search.index.upsert("episodes", ep_id, dom.index_text("", text), ts=self.clock(),
                                     meta={"episode": ep_id, "kind": dom.NOTE, "salience": salience})
            return True
        except Exception as e:
            logger.warning(f"Запись заметки: {e}")
            return False

    def forget(self, episode_id: str) -> bool:
        """Забыть эпизод или заметку по id; канон не забывается."""
        try:
            if not self.store.delete(episode_id):
                return False
            self.search.index.delete("episodes", episode_id)
            return True
        except Exception as e:
            logger.warning(f"Забывание: {e}")
            return False

    def forget_matching(self, text: str, strict: bool = False) -> int:
        """Забыть эпизоды и заметки (кроме канона), где есть значимые слова фразы: «забудь что я люблю чай» находит «Любит чай».
        `strict`: подстрока фразы целиком."""
        if strict:
            keys, need = [text.lower().strip()], 1
        else:
            keys = [w[:3] if len(w) > 3 else w for w in stemlib.words(text)]
            if not keys:
                return 0
            need = max(1, -(-len(keys) * 6 // 10))
        try:
            ids = [r["id"] for r in self.store.forgettable()
                   if sum(k in f"{r['title']} {r['text']}".lower() for k in keys) >= need]
        except Exception as e:
            logger.warning(f"Забывание: {e}")
            return 0
        return sum(self.forget(i) for i in ids)

    # ── поиск ────────────────────────────────────────────────────────────────

    async def find(self, query: str, limit: int = 6) -> list[dict]:
        """Все виды эпизодов по теме запроса (для инструмента recall и пиров): [{id, kind, text, score}]. Сбой поиска даёт пустой список."""
        try:
            return await self.recall.find(query, k=limit)
        except Exception as e:
            logger.warning(f"Поиск эпизодов: {e}")
            return []

    # ── блоки контекста ──────────────────────────────────────────────────────

    async def canon_scores(self, query: str, cands: list[dict]) -> list[float]:
        """Оценки канона к реплике: косинус по векторам индекса плюс прибавка за общие слова, а где вектора нет (эмбеддер выключен или
        запись ещё не проиндексирована) - оценка по общим словам."""
        q_stems = stemlib.stems(query)
        stems = [stemlib.stems(c["text"]) for c in cands]
        by_words = character_memory.word_scores(q_stems, stems)
        embedder = getattr(self.search, "embedder", None)
        if embedder is None:
            return by_words
        try:
            qvec = (await asyncio.wait_for(embedder.embed([query], True), EMBED_TIMEOUT_S))[0]
            vecs = await asyncio.to_thread(self.search.index.vectors, "episodes", [c["id"] for c in cands])
        except Exception as e:                       # LM Studio выключен или не ответил: канон не пропадает, подбор по словам
            logger.info(f"Воспоминания о себе без эмбеддера: {e}")
            return by_words
        out = []
        for c, fs, w in zip(cands, stems, by_words, strict=True):
            vec = vecs.get(c["id"])
            out.append(cosine(qvec, vec) + character_memory.WORD_BONUS * min(2, len(q_stems & fs)) if vec is not None else w)
        return out

    async def canon_block(self, query: str, n: int = character_memory.N_PICK) -> str:
        """«Твои воспоминания (это о тебе)»: канон, подходящий к реплике. Пусто, если ничего не прошло порог."""
        if not query.strip():
            return ""
        try:
            cands = await asyncio.to_thread(self.store.of_kind, dom.CANON)
            if not cands:
                return ""
            scores = await self.canon_scores(query, cands)
            self._self_replies += 1
            picked, nudged = character_memory.pick(cands, scores, n=n, min_score=character_memory.SELF_MIN_SCORE,
                                                   nudge=self._self_replies >= character_memory.NUDGE_EVERY)
            if nudged:
                self._self_replies = 0
            return character_memory.block(picked)
        except Exception as e:
            logger.warning(f"Воспоминания о себе: {e}")
            return ""

    async def topic_block(self, query: str, limit: int = TOPIC_LIMIT) -> str:
        """«Что ты помнишь по теме»: эпизоды жизни и заметки по запросу (гибридный поиск, свежесть и яркость в весе)."""
        if not query.strip():
            return ""
        hits = await self.recall.find(query, k=limit, kinds=(dom.LIFE, dom.NOTE), diary=False)
        if not hits:
            return ""
        rows = await asyncio.to_thread(self.store.get_many, [h["id"] for h in hits])
        lines = []
        for h in hits:
            ep = rows.get(h["id"])
            if ep is not None and ep["text"]:
                when = f"{h['when']}: " if ep["kind"] == dom.LIFE else ""
                lines.append(f"- {when}{ep['text']}")
        return "Что ты помнишь по теме (это твои записи, а не слова из разговора):\n" + "\n".join(lines) if lines else ""

    # ── состояние ────────────────────────────────────────────────────────────

    def status_text(self) -> str:
        try:
            kinds = self.store.counts()["kinds"]
        except Exception as e:
            return f"Память: не читается ({str(e)[:60]})."
        return (f"Память: эпизодов {kinds.get(dom.LIFE, 0)}, заметок {kinds.get(dom.NOTE, 0)}, "
                f"канон персонажа {kinds.get(dom.CANON, 0)}.")
