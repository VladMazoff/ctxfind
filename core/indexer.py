from __future__ import annotations

"""Оркестратор сканирования: парсеры -> граф -> символы.

Контракты:
  - ProjectIndexer.build_graph(file_entries) -> InMemoryGraph
  - Мерджит ParseResult в граф, строит кросс-файловые связи.
  - V01: интеграция с LinkerEngine для CSS<->HTML<->JS связей.
  - Все парсеры зарегистрированы: python, javascript, html, css.
"""

import concurrent.futures
import os
from typing import Dict, List, Optional, Set

from config import (
    ALL_EXTENSIONS,
    DEFAULT_IGNORE_PATTERNS,
    MIN_SYMBOL_LENGTH_FOR_BRIDGE,
    ParseError,
)
from core.graph import EdgeDTO, GraphError, InMemoryGraph
from core.linker import LinkerEngine
from core.scanner import FileEntry, Scanner
from parsers.base import BaseParser, NodeDTO, ParseResult, SymbolDTO
from parsers.py_parser import PythonParser
from parsers.js_parser import JavaScriptParser
from parsers.html_parser import HTMLParserAdapter
from parsers.css_parser import CSSParser


# V01: Реестр парсеров -- все языки
PARSER_REGISTRY: Dict[str, type] = {
    "python": PythonParser,
    "javascript": JavaScriptParser,
    "html": HTMLParserAdapter,
    "css": CSSParser,
}


def get_parser(lang: str) -> Optional[BaseParser]:
    """Возвращает экземпляр парсера для языка или None."""
    parser_cls = PARSER_REGISTRY.get(lang)
    if parser_cls:
        return parser_cls()
    return None


class ProjectIndexer:
    """Оркестратор индексации проекта.

    Контракт:
        build_graph(file_entries) -> InMemoryGraph

    V01 изменения:
      - LinkerEngine интегрирован для единого индекса кросс-языковых связей.
      - Все парсеры зарегистрированы.
      - semantic_role из NodeDTO переносится в граф.
      - Убран внутренний Scanner -- принимает готовый список FileEntry.
    """

    def __init__(
        self,
        max_workers: Optional[int] = None,
        use_threads: bool = False,
    ):
        self.max_workers = max_workers
        self.use_threads = use_threads
        self.linker = LinkerEngine()

    def build_graph(
        self,
        file_entries: List[FileEntry],
    ) -> InMemoryGraph:
        """Строит InMemoryGraph из отсканированных файлов.

        Pipeline:
            1. Парсеры обрабатывают файлы.
            2. Результаты мерджатся в InMemoryGraph.
            3. LinkerEngine индексирует файлы для кросс-языковых связей.
            4. Строятся кросс-файловые связи.

        Args:
            file_entries: список FileEntry от Scanner.

        Returns:
            InMemoryGraph с узлами, рёбрами и индексами.
        """
        if not file_entries:
            return InMemoryGraph()

        # 1. Парсинг
        parse_results = self._parse_files(file_entries)

        # 2. Мерджинг в граф
        graph = InMemoryGraph()
        self._merge_results(graph, parse_results)

        # 3. V01: LinkerEngine индексирует файлы
        for entry in file_entries:
            try:
                with open(entry.path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                self.linker.index_file(entry.path, lines, entry.lang)
            except Exception:
                pass

        # 4. Кросс-языковые мосты
        self._build_cross_file_bridges(graph)

        return graph

    def _parse_files(self, files: List[FileEntry]) -> Dict[str, ParseResult]:
        """Парсит файлы и возвращает результаты."""
        results: Dict[str, ParseResult] = {}

        if self.use_threads and self.max_workers != 1:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers
            ) as executor:
                future_to_file = {
                    executor.submit(self._parse_single, entry): entry
                    for entry in files
                }
                for future in concurrent.futures.as_completed(future_to_file):
                    entry = future_to_file[future]
                    try:
                        result = future.result()
                        results[entry.path] = result
                    except Exception:
                        results[entry.path] = ParseResult(status="opaque", tokens=[])
        else:
            for entry in files:
                results[entry.path] = self._parse_single(entry)

        return results

    def _parse_single(self, entry: FileEntry) -> ParseResult:
        """Парсит один файл."""
        parser = get_parser(entry.lang)
        if not parser:
            return ParseResult(status="opaque", tokens=[])

        try:
            return parser.parse(entry.path)
        except Exception:
            return ParseResult(status="opaque", tokens=[])

    def _merge_results(self, graph: InMemoryGraph, results: Dict[str, ParseResult]) -> None:
        """Мерджит ParseResult в InMemoryGraph."""
        for file_path, result in results.items():
            node_id_map: Dict[str, str] = {}

            for node in result.nodes:
                if not node.file_path:
                    node = node.with_file_path(file_path)

                original_id = node.id
                unique_id = original_id
                counter = 1
                while graph.has_node(unique_id):
                    unique_id = f"{original_id}_{counter}"
                    counter += 1

                if unique_id != original_id:
                    node_id_map[original_id] = unique_id
                    node = NodeDTO(
                        id=unique_id, type=node.type, name=node.name,
                        line=node.line, col=node.col, end_line=node.end_line,
                        parent_id=node.parent_id, children_ids=list(node.children_ids),
                        raw_text=node.raw_text, file_path=node.file_path,
                        semantic_role=node.semantic_role,
                    )

                graph.add_node(node)

            # Переопределяем parent_id и children_ids
            for node_id in list(graph.nodes.keys()):
                node = graph.nodes[node_id]
                if node.file_path != file_path:
                    continue

                new_parent = node_id_map.get(node.parent_id) if node.parent_id else None
                new_children = [node_id_map.get(cid, cid) for cid in node.children_ids]

                if new_parent != node.parent_id or new_children != node.children_ids:
                    graph.nodes[node_id] = NodeDTO(
                        id=node.id, type=node.type, name=node.name,
                        line=node.line, col=node.col, end_line=node.end_line,
                        parent_id=new_parent, children_ids=new_children,
                        raw_text=node.raw_text, file_path=node.file_path,
                        semantic_role=node.semantic_role,
                    )

            # Индексируем символы
            for name, symbols in result.symbols.items():
                for symbol in symbols:
                    mapped_node_id = node_id_map.get(symbol.node_id, symbol.node_id)
                    if graph.has_node(mapped_node_id):
                        graph.index_symbol(name, mapped_node_id)

            # Строим внутрифайловые связи
            self._build_internal_edges(graph, file_path, node_id_map)

    def _build_internal_edges(self, graph: InMemoryGraph, file_path: str, node_id_map: Dict[str, str]) -> None:
        """Строит рёбра 'contains' на основе parent_id."""
        for node_id in list(graph.nodes.keys()):
            node = graph.nodes[node_id]
            if node.file_path != file_path:
                continue
            if node.parent_id:
                parent_id = node_id_map.get(node.parent_id, node.parent_id)
                if graph.has_node(parent_id) and graph.has_node(node_id):
                    try:
                        graph.add_edge(parent_id, node_id, "contains", confidence=1.0)
                    except GraphError:
                        pass

    def _build_cross_file_bridges(self, graph: InMemoryGraph) -> None:
        """Строит кросс-файловые связи по эвристикам."""
        symbol_nodes: Dict[str, List[str]] = {}
        for name, node_ids in graph.symbol_index.items():
            if len(name) < MIN_SYMBOL_LENGTH_FOR_BRIDGE:
                continue
            if len(node_ids) > 1:
                for i, node_id_a in enumerate(node_ids):
                    for node_id_b in node_ids[i + 1:]:
                        self._link_symbol_pair(graph, node_id_a, node_id_b)

    def _link_symbol_pair(self, graph: InMemoryGraph, node_id_a: str, node_id_b: str) -> None:
        """Создаёт связь между двумя узлами с одинаковым именем символа."""
        node_a = graph.get_node(node_id_a)
        node_b = graph.get_node(node_id_b)
        if not node_a or not node_b:
            return
        if node_a.type in ("function", "class", "assignment", "rule"):
            source, target = node_id_a, node_id_b
        elif node_b.type in ("function", "class", "assignment", "rule"):
            source, target = node_id_b, node_id_a
        else:
            try:
                graph.add_edge(node_id_a, node_id_b, "ref", confidence=0.7)
                graph.add_edge(node_id_b, node_id_a, "ref", confidence=0.7)
            except GraphError:
                pass
            return
        try:
            graph.add_edge(source, target, "ref", confidence=0.8)
        except GraphError:
            pass
