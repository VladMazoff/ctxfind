from __future__ import annotations

"""Поисковый движок: QueryOptions + QueryEngine (Extract→Score→Link→Fallback→Assemble).

Контракты:
  - QueryOptions: dataclass с всеми параметрами поиска.
  - QueryEngine: единая точка поиска.
  - execute(query: str, options: QueryOptions) -> ContextTree
  - Режимы: 'graph' (AST + скоринг), 'vector' (TF-IDF), 'auto' (приоритет графа).
  - V01: использует semantic_role из AST-узлов для скоринга.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from config import (
    AUTO_EXPAND_RULES,
    DEFAULT_FALLBACK_TOP_K,
    DEFAULT_MAX_DEPTH,
    DEFAULT_SNIPPET_LINES,
    SCORING_RULES,
    QueryError,
)
from core.fallback import FallbackEngine
from core.graph import EdgeDTO, InMemoryGraph
from core.indexer import ProjectIndexer
from core.linker import LinkerEngine, RelationEdge
from core.scanner import Scanner
from core.scorer import ScoredMatch, ScorerEngine, ScoreBreakdown
from context.chunk import ContextAssembler, ContextTree
from parsers.base import NodeDTO


@dataclass
class QueryOptions:
    """Конфигурация поискового запроса.

    Все флаги CLI транслируются сюда единообразно.
    """
    mode: str = "auto"                    # graph | vector | auto
    depth: int = DEFAULT_MAX_DEPTH        # глубина обхода графа
    snippet_lines: int = DEFAULT_SNIPPET_LINES  # ±N строк в сниппете
    lang_filter: Optional[List[str]] = None   # фильтр по языку
    limit: int = 10                       # макс. результатов
    min_score: int = SCORING_RULES["threshold_default"]  # мин. порог
    auto_expand: bool = False             # авто-расширение запроса
    find_gaps: bool = False               # поиск разрывов логики
    aggressive: bool = False              # агрессивный режим

    @classmethod
    def from_args(cls, args) -> "QueryOptions":
        """Создает QueryOptions из argparse.Namespace.

        Обрабатывает макро-флаги: --smart, --aggressive.
        """
        opts = cls()

        # Макро-флаги имеют приоритет
        if getattr(args, "aggressive", False):
            opts.min_score = SCORING_RULES["threshold_aggressive"]
            opts.auto_expand = True
            opts.find_gaps = True
            opts.limit = max(getattr(args, "limit", 10), 50)
            opts.aggressive = True
        elif getattr(args, "smart", False):
            opts.min_score = 30
            opts.auto_expand = True
            if not getattr(args, "lang", None):
                opts.lang_filter = ["css", "javascript", "html"]

        # Индивидуальные флаги (переопределяют макро)
        if hasattr(args, "mode") and args.mode:
            opts.mode = args.mode
        if hasattr(args, "depth") and args.depth is not None:
            opts.depth = args.depth
        if hasattr(args, "lines") and args.lines is not None:
            opts.snippet_lines = args.lines
        if hasattr(args, "lang") and args.lang:
            opts.lang_filter = list(args.lang)
        if hasattr(args, "focus_langs") and args.focus_langs:
            langs = []
            for fl in args.focus_langs:
                langs.extend(fl.split(","))
            if not opts.lang_filter:
                opts.lang_filter = []
            opts.lang_filter.extend(langs)
        if hasattr(args, "limit") and args.limit is not None:
            if not opts.aggressive:
                opts.limit = args.limit
        if hasattr(args, "min_score") and args.min_score is not None:
            if not opts.aggressive:
                opts.min_score = args.min_score
        if hasattr(args, "auto_expand") and args.auto_expand:
            opts.auto_expand = True
        if hasattr(args, "find_gaps") and args.find_gaps:
            opts.find_gaps = True

        return opts


class QueryEngine:
    """Единая точка поиска по проекту с V01 скорингом и линкингом.

    V01 изменения:
      - Использует semantic_role из AST-узлов при скоринге.
      - Интегрирован LinkerEngine из indexer.
      - Контракт execute(query, options) -> ContextTree.
    """

    def __init__(self, graph: InMemoryGraph):
        self.graph = graph
        self.scorer = ScorerEngine()
        self.linker = LinkerEngine()
        self.fallback = FallbackEngine(snippet_lines=DEFAULT_SNIPPET_LINES)
        self._fallback_ready = False
        self._file_entries: List = []

    def build_fallback(self, file_entries: List) -> None:
        """Строит fallback-индекс и linker-индекс по файлам."""
        self._file_entries = file_entries
        self.fallback.fit_files(file_entries)
        self._fallback_ready = True

        for entry in file_entries:
            try:
                with open(entry.path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                self.linker.index_file(entry.path, lines, entry.lang)
            except Exception:
                pass

    def execute(
        self,
        query: str,
        options: QueryOptions,
    ) -> ContextTree:
        """Выполняет поисковый запрос.

        Args:
            query: поисковый запрос.
            options: QueryOptions с параметрами поиска.

        Returns:
            ContextTree с TreeNode'ами.
        """
        if options.mode not in ("graph", "vector", "auto"):
            raise QueryError(f"Неизвестный режим: {options.mode}")

        min_score = options.min_score
        limit = options.limit

        if options.aggressive:
            min_score = SCORING_RULES["threshold_aggressive"]
            limit = max(limit, 50)

        all_queries = [query]
        expanded = []
        if options.auto_expand:
            expanded = self.scorer.auto_expand(query)
            all_queries.extend(expanded)

        all_matches: List[ScoredMatch] = []

        if options.mode in ("graph", "auto"):
            for q in all_queries:
                matches = self._search_graph_scored(q, options.depth, options.snippet_lines, options.lang_filter)
                all_matches.extend(matches)

        if options.mode == "vector" or (options.mode == "auto" and len(all_matches) < 3):
            if self._fallback_ready:
                for q in all_queries:
                    fb_matches = self.fallback.search_as_scored(q, top_k=limit, lang=None)
                    all_matches.extend(fb_matches)

        # Дедупликация по location_key (file:line)
        all_matches = self.scorer.deduplicate_by_location(all_matches)

        all_matches = self.scorer.apply_proximity_bonus(all_matches)

        if not options.aggressive:
            all_matches = self.scorer.filter_by_threshold(all_matches, min_score)

        # Дедупликация по node_id
        seen: Set[str] = set()
        unique_matches: List[ScoredMatch] = []
        for m in all_matches:
            if m.node_id not in seen:
                seen.add(m.node_id)
                unique_matches.append(m)

        unique_matches = self.scorer.sort_by_relevance(unique_matches)
        unique_matches = unique_matches[:limit]

        relations: List[RelationEdge] = []
        for q in all_queries:
            relations.extend(self.linker.build_relations(q))

        gap_edges: List[RelationEdge] = []
        if options.find_gaps:
            file_hits: Dict[str, int] = {}
            for m in unique_matches:
                file_hits[m.file_path] = file_hits.get(m.file_path, 0) + 1
            gap_edges = self.linker.detect_gaps(query, file_hits)

        assembler = ContextAssembler(
            max_lines_per_chunk=options.snippet_lines * 2,
            max_chunks=limit,
        )
        tree = assembler.assemble(
            matches=unique_matches,
            relations=relations,
            gap_edges=gap_edges,
            query=query,
            options={
                "snippet_lines": options.snippet_lines,
                "auto_expanded": expanded,
                "mode": options.mode,
                "min_score": min_score,
            },
        )

        return tree

    def _search_graph_scored(
        self,
        query: str,
        depth: int,
        snippet_lines: int,
        lang_filter: Optional[List[str]],
    ) -> List[ScoredMatch]:
        """Ищет через граф + скорит каждую строку файла.

        V01: Использует semantic_role из AST-узла если доступен.
        """
        matches: List[ScoredMatch] = []
        graph_nodes = self._find_graph_matches(query)

        for node in graph_nodes:
            if lang_filter and node.file_path:
                ext = os.path.splitext(node.file_path)[1].lower()
                from config import EXT_TO_LANG
                node_lang = EXT_TO_LANG.get(ext)
                if node_lang not in lang_filter:
                    continue

            if node.file_path and os.path.exists(node.file_path):
                try:
                    with open(node.file_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    if 1 <= node.line <= len(lines):
                        line_text = lines[node.line - 1]
                        ext = os.path.splitext(node.file_path)[1].lower()
                        from config import EXT_TO_LANG
                        node_lang = EXT_TO_LANG.get(ext)

                        scored = self.scorer.score_text_line(
                            query=query,
                            line_text=line_text,
                            file_path=node.file_path,
                            line_num=node.line,
                            lang=node_lang,
                        )
                        if scored:
                            scored.node_id = node.id
                            scored.text = node.raw_text or scored.text
                            scored.end_line = node.end_line or node.line
                            # V01: используем semantic_role из AST если есть
                            if node.semantic_role:
                                scored.semantic_role = node.semantic_role
                            scored.meta.update({
                                "graph_node_type": node.type,
                                "graph_parent": node.parent_id,
                                "mode": "graph",
                            })
                            matches.append(scored)
                except Exception:
                    pass

            if not any(m.node_id == node.id for m in matches):
                breakdown = ScoreBreakdown()
                breakdown.exact_match = SCORING_RULES["exact_match"]

                # V01: определяем semantic_role из AST
                semantic_role = node.semantic_role
                if not semantic_role:
                    if node.type in ("function", "class", "rule", "assignment"):
                        semantic_role = "definition"
                    elif node.type in ("call", "import"):
                        semantic_role = "usage"
                    elif node.type == "export":
                        semantic_role = "output"

                matches.append(ScoredMatch(
                    node_id=node.id,
                    text=node.raw_text or node.name,
                    file_path=node.file_path or "",
                    line=node.line,
                    col=node.col,
                    end_line=node.end_line or node.line,
                    score=min(SCORING_RULES["exact_match"], 100),
                    score_breakdown=breakdown,
                    match_type="exact",
                    semantic_role=semantic_role,
                    meta={
                        "graph_node_type": node.type,
                        "mode": "graph",
                        "fallback_from_graph": True,
                    },
                ))

        return matches

    def _find_graph_matches(self, query: str) -> List[NodeDTO]:
        """Находит узлы через граф: точный матч + регекс."""
        result: List[NodeDTO] = []
        seen: Set[str] = set()

        exact = self.graph.find_symbol(query)
        for node in exact:
            if node.id not in seen:
                seen.add(node.id)
                result.append(node)

        for name in self.graph.symbol_index:
            if name.lower() == query.lower():
                for node in self.graph.find_symbol(name):
                    if node.id not in seen:
                        seen.add(node.id)
                        result.append(node)

        try:
            pattern = re.compile(query, re.IGNORECASE)
            for node in self.graph.nodes.values():
                if pattern.search(node.name) and node.id not in seen:
                    seen.add(node.id)
                    result.append(node)
        except re.error:
            pass

        return result
