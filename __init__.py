"""ctxfind V01: Контекстно-зависимый поиск по кодовой базе.

API:
  - Scanner: обход ФС
  - ProjectIndexer: построение графа
  - QueryEngine: поиск
  - QueryOptions: конфигурация поиска
  - Renderer: вывод
  - ContextTree: результат
"""

__version__ = "0.1.0"

from core.scanner import Scanner, FileEntry
from core.indexer import ProjectIndexer
from core.query import QueryEngine, QueryOptions
from context.renderer import Renderer
from context.chunk import ContextTree, TreeNode, ContextAssembler

__all__ = [
    "Scanner",
    "FileEntry",
    "ProjectIndexer",
    "QueryEngine",
    "QueryOptions",
    "Renderer",
    "ContextTree",
    "TreeNode",
    "ContextAssembler",
]
