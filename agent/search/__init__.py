"""Общий механизм гибридного поиска по коллекциям (ТЗ 04): FTS5 + векторы + фильтры + вес свежести."""
from search.index import Hit, HybridIndex, SearchConfig
from search.service import HybridSearch

__all__ = ["Hit", "HybridIndex", "HybridSearch", "SearchConfig"]
