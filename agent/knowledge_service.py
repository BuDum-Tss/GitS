"""Сборка справочника (ТЗ 04): `knowledge.db` в общей папке памяти + опыт из сервиса уроков текущего персонажа.

Настройки поиска берутся из раздела `knowledge` на каждом вызове (KB-21), видимость по умолчанию - из активного профиля (KB-7),
перевод запроса (KB-6) - локальной моделью (`LanguageModel`). Справка в ответе разговора (KB-11) подключается к `ContextBuilder`."""
import settings
from knowledge_base import KnowledgeBase
from search import HybridIndex, HybridSearch, SearchConfig
from search.embedder import LmStudioEmbedder

FILE = "knowledge.db"


def build(lessons, episodes, *, embedder=None, path=None, companion=None, profiles=None, llm=None,
          settings_source=None) -> KnowledgeBase:
    index = HybridIndex(path or settings.memory_dir() / FILE)
    search = HybridSearch(index, embedder if embedder is not None else LmStudioEmbedder(), None, SearchConfig())

    def visible():
        p = profiles.current() if profiles is not None else None
        return (list(p.knowledge), list(p.memory_scopes)) if p is not None else ([], [])

    kb = KnowledgeBase(search, experience=lambda: (lessons.service(), episodes.parts().search),
                       settings=settings_source or settings.load,
                       llm=llm if llm is not None else getattr(companion, "_llm", None), visible=visible)
    if companion is not None:
        companion.knowledge = kb
        companion.context_builder.knowledge_of = kb.chat_block
        companion.context_builder.knowledge_on = kb.chat_enabled
    return kb
