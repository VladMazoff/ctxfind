from __future__ import annotations

"""In-memory структуры графа: узлы, рёбра, индексы.

Контракты:
  - InMemoryGraph: хранит всё в dict/list, без внешних БД.
  - nodes: кэш всех узлов по id.
  - adj: смежность source → [EdgeDTO].
  - symbol_index: имя_символа → [node_id].
  - add_edge: добавляет связь с меткой.
  - traverse: BFS/DFS обход до глубины.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from collections import deque

from parsers.base import NodeDTO


class GraphError(Exception):
    """Ошибка внутри InMemoryGraph."""
    pass


# ──────────────────────────────────────────────
# EdgeDTO
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class EdgeDTO:
    """Ребро графа — направленная связь между узлами.

    Поля:
        source: ID исходного узла.
        target: ID целевого узла.
        type: метка связи: 'call', 'import', 'applies', 'extends',
              'defines', 'ref', 'contains', 'parent'.
        confidence: уверенность в связи (0.0–1.0), для эвристических мостов.
        meta: произвольные метаданные.
    """
    source: str
    target: str
    type: str
    confidence: float = 1.0
    meta: Dict[str, str] = field(default_factory=dict)


# ──────────────────────────────────────────────
# InMemoryGraph
# ──────────────────────────────────────────────

class InMemoryGraph:
    """In-memory граф проекта.

    Потокобезопасность: не потокобезопасен, предполагается
    построение в одном потоке перед использованием.
    """

    def __init__(self):
        # Узлы по ID
        self.nodes: Dict[str, NodeDTO] = {}
        # Смежность: source_id → [EdgeDTO]
        self.adj: Dict[str, List[EdgeDTO]] = {}
        # Обратная смежность: target_id → [EdgeDTO]
        self.rev_adj: Dict[str, List[EdgeDTO]] = {}
        # Индекс символов: имя → [node_id]
        self.symbol_index: Dict[str, List[str]] = {}
        # Индекс файлов: путь → [node_id]
        self.file_index: Dict[str, List[str]] = {}

    # ── Узлы ──

    def add_node(self, node: NodeDTO) -> None:
        """Добавляет узел в граф.

        Raises:
            GraphError: если узел с таким ID уже существует.
        """
        if node.id in self.nodes:
            raise GraphError(f"Узел с ID '{node.id}' уже существует")
        self.nodes[node.id] = node
        if node.file_path:
            self.file_index.setdefault(node.file_path, []).append(node.id)

    def get_node(self, node_id: str) -> Optional[NodeDTO]:
        """Возвращает узел по ID или None."""
        return self.nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        """Проверяет существование узла."""
        return node_id in self.nodes

    def remove_node(self, node_id: str) -> None:
        """Удаляет узел и все связанные рёбра."""
        if node_id not in self.nodes:
            return
        del self.nodes[node_id]
        if node_id in self.adj:
            del self.adj[node_id]
        if node_id in self.rev_adj:
            del self.rev_adj[node_id]
        for src in list(self.adj.keys()):
            self.adj[src] = [e for e in self.adj[src] if e.target != node_id]
        for tgt in list(self.rev_adj.keys()):
            self.rev_adj[tgt] = [e for e in self.rev_adj[tgt] if e.source != node_id]
        for name, ids in list(self.symbol_index.items()):
            self.symbol_index[name] = [i for i in ids if i != node_id]
            if not self.symbol_index[name]:
                del self.symbol_index[name]
        for path, ids in list(self.file_index.items()):
            self.file_index[path] = [i for i in ids if i != node_id]
            if not self.file_index[path]:
                del self.file_index[path]

    # ── Рёбра ──

    def add_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        confidence: float = 1.0,
        meta: Optional[Dict[str, str]] = None,
    ) -> None:
        """Добавляет направленное ребро.

        Raises:
            GraphError: если source или target не существуют.
        """
        if not self.has_node(source):
            raise GraphError(f"Исходный узел '{source}' не существует")
        if not self.has_node(target):
            raise GraphError(f"Целевой узел '{target}' не существует")

        edge = EdgeDTO(
            source=source,
            target=target,
            type=edge_type,
            confidence=confidence,
            meta=meta or {},
        )
        self.adj.setdefault(source, []).append(edge)
        self.rev_adj.setdefault(target, []).append(edge)

    def get_edges_from(self, node_id: str) -> List[EdgeDTO]:
        """Возвращает исходящие рёбра узла."""
        return list(self.adj.get(node_id, []))

    def get_edges_to(self, node_id: str) -> List[EdgeDTO]:
        """Возвращает входящие рёбра узла."""
        return list(self.rev_adj.get(node_id, []))

    def get_neighbors(
        self,
        node_id: str,
        direction: str = "both",
        edge_type: Optional[str] = None,
    ) -> List[NodeDTO]:
        """Возвращает соседние узлы."""
        result: List[NodeDTO] = []
        seen: Set[str] = set()

        if direction in ("out", "both"):
            for edge in self.get_edges_from(node_id):
                if edge_type is None or edge.type == edge_type:
                    if edge.target not in seen:
                        seen.add(edge.target)
                        node = self.get_node(edge.target)
                        if node:
                            result.append(node)

        if direction in ("in", "both"):
            for edge in self.get_edges_to(node_id):
                if edge_type is None or edge.type == edge_type:
                    if edge.source not in seen:
                        seen.add(edge.source)
                        node = self.get_node(edge.source)
                        if node:
                            result.append(node)

        return result

    # ── Индексы ──

    def index_symbol(self, name: str, node_id: str) -> None:
        """Добавляет символ в индекс."""
        self.symbol_index.setdefault(name, []).append(node_id)

    def find_symbol(self, name: str) -> List[NodeDTO]:
        """Ищет узлы по имени символа."""
        node_ids = self.symbol_index.get(name, [])
        return [self.nodes[nid] for nid in node_ids if nid in self.nodes]

    def find_by_file(self, file_path: str) -> List[NodeDTO]:
        """Возвращает все узлы файла."""
        node_ids = self.file_index.get(file_path, [])
        return [self.nodes[nid] for nid in node_ids if nid in self.nodes]

    # ── Обход ──

    def traverse(
        self,
        start_ids: List[str],
        direction: str = "both",
        max_depth: int = 3,
        edge_type: Optional[str] = None,
    ) -> List[NodeDTO]:
        """BFS-обход графа от стартовых узлов.

        Args:
            start_ids: начальные узлы.
            direction: 'out', 'in', 'both'.
            max_depth: максимальная глубина (0 = только стартовые).
            edge_type: фильтр по типу ребра.

        Returns:
            Список всех достижимых узлов (включая стартовые).
        """
        visited: Set[str] = set()
        queue: deque = deque()

        for sid in start_ids:
            if self.has_node(sid):
                visited.add(sid)
                queue.append((sid, 0))

        while queue:
            current_id, depth = queue.popleft()
            if depth >= max_depth:
                continue

            if direction in ("out", "both"):
                for edge in self.get_edges_from(current_id):
                    if edge_type is None or edge.type == edge_type:
                        if edge.target not in visited:
                            visited.add(edge.target)
                            queue.append((edge.target, depth + 1))

            if direction in ("in", "both"):
                for edge in self.get_edges_to(current_id):
                    if edge_type is None or edge.type == edge_type:
                        if edge.source not in visited:
                            visited.add(edge.source)
                            queue.append((edge.source, depth + 1))

        return [self.nodes[nid] for nid in visited if nid in self.nodes]

    # ── Статистика ──

    def stats(self) -> Dict[str, int]:
        """Возвращает статистику графа."""
        total_edges = sum(len(edges) for edges in self.adj.values())
        return {
            "nodes": len(self.nodes),
            "edges": total_edges,
            "symbols": len(self.symbol_index),
            "files": len(self.file_index),
        }