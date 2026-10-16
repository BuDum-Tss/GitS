"""Сборка справочника (ТЗ 04): `knowledge.db` в общей папке памяти + опыт из сервиса уроков текущего персонажа."""
import settings
from knowledge_base import KnowledgeBase
from search import HybridIndex, HybridSearch, SearchConfig
from search.embedder import LmStudioEmbedder

FILE = "knowledge.db"


def build(lessons, episodes, *, embedder=None, path=None) -> KnowledgeBase:
    index = HybridIndex(path or settings.memory_dir() / FILE)
    search = HybridSearch(index, embedder if embedder is not None else LmStudioEmbedder(), None, SearchConfig())
    return KnowledgeBase(search, experience=lambda: (lessons.service(), episodes.parts().search))
