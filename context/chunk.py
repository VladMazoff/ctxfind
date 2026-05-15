from __future__ import annotations

"""Модели контекстного дерева: TreeNode, ContextTree, ContextAssembler.

Контракты:
  - TreeNode: узел с match, parents, children, cross_refs, snippet.
  - ContextTree: корневое дерево с meta.
  - ContextAssembler: сборка TreeNode из ScoredMatch + RelationEdge.
  - Сериализация: только to_dict(). Логика представления — в Renderer.
"""

import os
from typing import Dict, List, Optional, Set

from config import DEFAULT_SNIPPET_LINES, MAX_SCORE
from core.scorer import ScoredMatch
from core.linker import RelationEdge


class TreeNode:
    """Узел контекстного дерева."""

    def __init__(
        self,
        match: ScoredMatch,
        parents: Optional[List["TreeNode"]] = None,
        children: Optional[List["TreeNode"]] = None,
        cross_refs: Optional[List[RelationEdge]] = None,
        semantic_role: Optional[str] = None,
        snippet: str = "",
        meta: Optional[Dict] = None,
    ):
        self.match = match
        self.parents = parents or []
        self.children = children or []
        self.cross_refs = cross_refs or []
        self.semantic_role = semantic_role or match.semantic_role
        self.snippet = snippet or match.text
        self.meta = meta or {}

    def add_child(self, node: "TreeNode") -> None:
        self.children.append(node)

    def add_parent(self, node: "TreeNode") -> None:
        self.parents.append(node)

    def add_cross_ref(self, edge: RelationEdge) -> None:
        self.cross_refs.append(edge)

    def to_dict(self) -> Dict:
        return {
            "match": self.match.to_dict(),
            "parents": [p.to_dict() for p in self.parents],
            "children": [c.to_dict() for c in self.children],
            "cross_refs": [e.to_dict() for e in self.cross_refs],
            "semantic_role": self.semantic_role,
            "snippet": self.snippet,
            "meta": self.meta,
        }

    # В классе ContextTree добавьте 
    def to_dict_safe(self, max_nodes: int = 100) -> Dict:
        """Безопасное преобразование дерева в словарь без циклических ссылок."""
        from collections import deque
        
        result = {
            "root": None,
            "nodes": [],
            "edges": [],
        }
        
        visited = set()
        queue = deque([self.root])
        nodes_processed = 0
        
        while queue and nodes_processed < max_nodes:
            node = queue.popleft()
            node_id = id(node)
            
            if node_id in visited:
                continue
                
            visited.add(node_id)
            nodes_processed += 1
            
            # Добавляем информацию об узле
            node_data = {
                "id": node_id,
                "text": node.match.text[:200] if hasattr(node.match, 'text') else str(node.match)[:200],
                "score": getattr(node.match, 'score', 0),
                "semantic_role": node.semantic_role,
                "snippet_preview": node.snippet[:100] if node.snippet else "",
            }
            result["nodes"].append(node_data)
            
            # Обрабатываем связи
            for child in node.children[:10]:  # Ограничиваем количество детей
                child_id = id(child)
                result["edges"].append({
                    "from": node_id,
                    "to": child_id,
                    "type": "child"
                })
                if child_id not in visited:
                    queue.append(child)
            
            for parent in node.parents[:10]:
                result["edges"].append({
                    "from": id(parent),
                    "to": node_id,
                    "type": "parent"
                })
        
        return result


class ContextTree:
    """Корневое дерево контекста поиска."""

    def __init__(self, root: Optional[TreeNode] = None, meta: Optional[Dict] = None):
        self.root = root or TreeNode(
            match=ScoredMatch(node_id="root", text="query_root", file_path="", line=0, score=0)
        )
        self.nodes: List[TreeNode] = []
        self.gap_edges: List[RelationEdge] = []
        self.meta = meta or {}

    def add_node(self, node: TreeNode) -> None:
        self.root.add_child(node)
        self.nodes.append(node)

    def add_gap_edge(self, edge: RelationEdge) -> None:
        self.gap_edges.append(edge)

    def to_dict(self) -> Dict:
        return {
            "root": self.root.to_dict(),
            "nodes": [n.to_dict() for n in self.nodes],
            "gap_edges": [e.to_dict() for e in self.gap_edges],
            "meta": self.meta,
        }


class ContextAssembler:
    """Собирает и форматирует TreeNode из ScoredMatch + RelationEdge."""

    def __init__(self, max_lines_per_chunk: Optional[int] = None, max_chunks: int = 10, group_hints: bool = True):
        self.max_lines_per_chunk = max_lines_per_chunk
        self.max_chunks = max_chunks
        self.group_hints = group_hints

    def assemble(self, matches, relations, gap_edges, query="", options=None):
        opts = options or {}
        snippet_lines = opts.get("snippet_lines", DEFAULT_SNIPPET_LINES)

        # Дедупликация по node_id
        seen = set()
        unique_matches = []
        for m in matches:
            if m.node_id not in seen:
                seen.add(m.node_id)
                unique_matches.append(m)

        node_map = {}
        for m in unique_matches:
            snippet = self._extract_snippet(m, snippet_lines)
            node = TreeNode(match=m, snippet=snippet, meta={"query": query, "auto_expanded": opts.get("auto_expanded", False)})
            node_map[m.node_id] = node

        # Связываем cross-refs
        for edge in relations:
            if edge.from_id in node_map:
                node_map[edge.from_id].add_cross_ref(edge)
            if edge.to_id and edge.to_id in node_map:
                reverse = RelationEdge(from_id=edge.to_id, to_id=edge.from_id, type=edge.type,
                                       direction="in" if edge.direction == "out" else "out",
                                       confidence=edge.confidence, description=edge.description, meta=edge.meta)
                node_map[edge.to_id].add_cross_ref(reverse)

        self._build_hierarchy(node_map)

        if self.max_lines_per_chunk:
            for nid in list(node_map.keys()):
                node_map[nid] = self._trim_snippet(node_map[nid])

        tree = ContextTree(meta={
            "query": query, "scoring_applied": True,
            "linker_edges_count": len(relations),
            "auto_expanded": opts.get("auto_expanded", []),
            "is_gap": len(gap_edges) > 0,
        })

        sorted_nodes = sorted(node_map.values(), key=lambda n: (-n.match.score, n.match.file_path, n.match.line))
        for node in sorted_nodes[:self.max_chunks]:
            tree.add_node(node)

        for gap in gap_edges:
            tree.add_gap_edge(gap)

        return tree

    def _extract_snippet(self, match, snippet_lines):
        if not match.file_path or not os.path.exists(match.file_path):
            return match.text
        try:
            with open(match.file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return match.text
        start = max(0, match.line - snippet_lines - 1)
        end = min(len(lines), (match.end_line or match.line) + snippet_lines)
        return "".join(lines[start:end])

    def _build_hierarchy(self, node_map):
        by_file = {}
        for node in node_map.values():
            by_file.setdefault(node.match.file_path, []).append(node)
        for file_path, nodes in by_file.items():
            nodes.sort(key=lambda n: n.match.line)
            for i, node in enumerate(nodes):
                for j in range(i - 1, -1, -1):
                    prev = nodes[j]
                    if prev.match.line < node.match.line:
                        if node.match.line - prev.match.line <= DEFAULT_SNIPPET_LINES * 2:
                            node.add_parent(prev)
                            prev.add_child(node)
                        break

    def _trim_snippet(self, node):
        if not self.max_lines_per_chunk:
            return node
        lines = node.snippet.splitlines()
        if len(lines) <= self.max_lines_per_chunk:
            return node
        half = self.max_lines_per_chunk // 2
        start = max(0, node.match.line - half)
        end = start + self.max_lines_per_chunk
        if end > len(lines):
            end = len(lines)
            start = max(0, end - self.max_lines_per_chunk)
        trimmed = "\n".join(lines[start:end])
        return TreeNode(match=node.match, parents=node.parents, children=node.children,
                        cross_refs=node.cross_refs, semantic_role=node.semantic_role,
                        snippet=trimmed, meta={**node.meta, "trimmed": True, "original_lines": len(lines)})
