from __future__ import annotations

"""Адаптер парсера Python на основе stdlib `ast`.

Контракты:
  - Поддерживает Python 3.8+ (graceful degradation для match/case).
  - Возвращает ParseResult с полной иерархией узлов.
  - Символы: def, class, import, вызовы функций.
  - V01: semantic_role (definition/usage/transformation/output).
"""

import ast
import os
import sys
from typing import List, Optional

from config import ParseError
from parsers.base import BaseParser, NodeDTO, ParseResult, SymbolDTO


class PythonParser(BaseParser):
    """Парсер Python-файлов через ast module."""

    _AST_TYPE_MAP = {
        ast.FunctionDef: "function",
        ast.AsyncFunctionDef: "function",
        ast.ClassDef: "class",
        ast.Module: "module",
        ast.Import: "import",
        ast.ImportFrom: "import",
        ast.Call: "call",
        ast.Name: "variable",
        ast.Attribute: "attribute",
        ast.Assign: "assignment",
        ast.AnnAssign: "assignment",
        ast.Expr: "expression",
    }

    # V01: semantic_role mapping
    _SEMANTIC_ROLE_MAP = {
        ast.FunctionDef: "definition",
        ast.AsyncFunctionDef: "definition",
        ast.ClassDef: "definition",
        ast.Import: "usage",
        ast.ImportFrom: "usage",
        ast.Call: "usage",
        ast.Assign: "definition",
        ast.AnnAssign: "definition",
        ast.Return: "output",
    }

    def parse(self, file_path: str) -> ParseResult:
        try:
            source = self._read_file(file_path)
        except Exception:
            return ParseResult(
                tokens=self._fallback_tokens(file_path),
                status="opaque",
            )

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return ParseResult(
                tokens=self._fallback_tokens(file_path),
                status="opaque",
            )

        result = ParseResult(status="ok")
        self._node_counter = 0
        self._file_path = file_path
        self._source_lines = source.splitlines()

        module_node = self._create_node(
            type="module",
            name=os.path.basename(file_path),
            ast_node=tree,
            parent_id=None,
        )
        result.nodes.append(module_node)

        self._visit(tree, parent_id=module_node.id, result=result)

        for i, node in enumerate(result.nodes):
            result.nodes[i] = node.with_file_path(file_path)

        return result

    def _visit(self, ast_node, parent_id, result):
        if not hasattr(ast_node, "lineno"):
            for child in ast.iter_child_nodes(ast_node):
                self._visit(child, parent_id, result)
            return None

        node_type = self._map_ast_type(ast_node)
        name = self._extract_name(ast_node)
        semantic_role = self._infer_semantic_role(ast_node)

        if node_type in ("function", "class", "import", "call", "assignment"):
            node = self._create_node(
                type=node_type,
                name=name,
                ast_node=ast_node,
                parent_id=parent_id,
                semantic_role=semantic_role,
            )
            result.nodes.append(node)

            if name:
                kind = self._infer_symbol_kind(node_type, ast_node)
                symbol = SymbolDTO(
                    name=name,
                    type=kind,
                    node_id=node.id,
                )
                result.add_symbol(symbol)

            current_parent = node.id
        else:
            current_parent = parent_id

        children_ids = []
        for child in ast.iter_child_nodes(ast_node):
            child_id = self._visit(child, current_parent, result)
            if child_id:
                children_ids.append(child_id)

        if node_type in ("function", "class", "import", "call", "assignment"):
            for i, n in enumerate(result.nodes):
                if n.id == node.id:
                    result.nodes[i] = NodeDTO(
                        id=n.id,
                        type=n.type,
                        name=n.name,
                        line=n.line,
                        col=n.col,
                        end_line=n.end_line,
                        parent_id=n.parent_id,
                        children_ids=children_ids,
                        raw_text=n.raw_text,
                        file_path=n.file_path,
                        semantic_role=n.semantic_role,
                    )
                    break

        return node.id if node_type in ("function", "class", "import", "call", "assignment") else None

    def _create_node(self, type, name, ast_node, parent_id, semantic_role=None):
        line = getattr(ast_node, "lineno", 1)
        col = getattr(ast_node, "col_offset", 0)
        end_line = getattr(ast_node, "end_lineno", line)

        raw_text = ""
        if line and end_line and line <= len(self._source_lines):
            start_idx = line - 1
            end_idx = min(end_line, len(self._source_lines))
            raw_text = chr(10).join(self._source_lines[start_idx:end_idx])

        self._node_counter += 1
        node_id = f"py_{self._node_counter}_{line}_{col}"

        return NodeDTO(
            id=node_id,
            type=type,
            name=name,
            line=line,
            col=col,
            end_line=end_line or line,
            parent_id=parent_id,
            raw_text=raw_text,
            file_path=self._file_path,
            semantic_role=semantic_role,
        )

    def _map_ast_type(self, ast_node):
        return self._AST_TYPE_MAP.get(type(ast_node), "opaque")

    def _infer_semantic_role(self, ast_node):
        """V01: Инферирует semantic_role из AST-типа."""
        return self._SEMANTIC_ROLE_MAP.get(type(ast_node), None)

    def _extract_name(self, ast_node):
        if isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return ast_node.name
        elif isinstance(ast_node, ast.Import):
            names = [alias.name for alias in ast_node.names]
            return ", ".join(names)
        elif isinstance(ast_node, ast.ImportFrom):
            module = ast_node.module or ""
            names = [alias.name for alias in ast_node.names]
            return f"{module}.{', '.join(names)}" if module else ", ".join(names)
        elif isinstance(ast_node, ast.Call):
            return self._format_call_name(ast_node.func)
        elif isinstance(ast_node, ast.Name):
            return ast_node.id
        elif isinstance(ast_node, ast.Attribute):
            return self._format_attribute_name(ast_node)
        elif isinstance(ast_node, (ast.Assign, ast.AnnAssign)):
            targets = []
            if isinstance(ast_node, ast.AnnAssign):
                targets.append(self._format_target(ast_node.target))
            else:
                for target in ast_node.targets:
                    targets.append(self._format_target(target))
            return ", ".join(t for t in targets if t)
        return ""

    def _format_call_name(self, func):
        if isinstance(func, ast.Name):
            return func.id
        elif isinstance(func, ast.Attribute):
            return self._format_attribute_name(func)
        return ""

    def _format_attribute_name(self, node):
        parts = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))

    def _format_target(self, target):
        if isinstance(target, ast.Name):
            return target.id
        elif isinstance(target, ast.Attribute):
            return self._format_attribute_name(target)
        elif isinstance(target, ast.Tuple):
            names = [self._format_target(elt) for elt in target.elts]
            return ", ".join(n for n in names if n)
        return ""

    def _infer_symbol_kind(self, node_type, ast_node):
        if node_type == "function":
            return "def"
        elif node_type == "class":
            return "def"
        elif node_type == "import":
            return "import"
        elif node_type == "call":
            return "call"
        elif node_type == "assignment":
            return "def"
        return "ref"
