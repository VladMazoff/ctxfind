from __future__ import annotations

"""Рендеринг ContextTree в различные форматы: JSON / Tree / Plain / Compact / Relations.

Контракты:
  - Renderer.render(tree, format, use_color, is_tty) -> str.
  - Форматы: 'json', 'tree', 'plain', 'compact', 'relations'.
  - TTY-детекция: цвета только в TTY.
  - Пайпы: JSON по умолчанию.
"""

import json
import os
import sys
import platform
from typing import Dict, List, Optional

from config import JSON_INDENT, TREE_INDENT_STR
from context.chunk import ContextTree, TreeNode
from utils.text import is_tty_stream, truncate_text


def enable_windows_ansi() -> bool:
    """Включает поддержку ANSI-цветов в Windows консоли.

    Returns:
        True если поддержка включена, False если не поддерживается.
    """
    if platform.system() != "Windows":
        return False

    try:
        import ctypes

        if sys.version_info >= (3, 8):
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE

            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                mode.value |= 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                if kernel32.SetConsoleMode(handle, mode):
                    return True

        try:
            import colorama
            colorama.init()
            return True
        except ImportError:
            os.system('color 07')
            return False

    except (ImportError, AttributeError, OSError):
        return False


class Renderer:
    """Рендерер ContextTree в различные форматы.

    Контракт:
        render(tree, format, use_color) -> str

    Args:
        format: 'json', 'tree', 'plain', 'compact', 'relations'.
        use_color: включить ANSI-цвета (None = авто по TTY).
    """

    _COLORS = {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "blue": "\033[34m",
        "magenta": "\033[35m",
        "cyan": "\033[36m",
        "white": "\033[37m",
    }

    def __init__(
        self,
        format: str = "tree",
        use_color: Optional[bool] = None,
    ):
        self.format = format

        self._ansi_enabled = False
        if platform.system() == "Windows":
            self._ansi_enabled = enable_windows_ansi()

        if use_color is None:
            self.use_color = is_tty_stream() and self._ansi_enabled
        else:
            self.use_color = use_color and (self._ansi_enabled or platform.system() != "Windows")

    def render(self, tree: ContextTree) -> str:
        """Рендерит ContextTree в выбранный формат."""
       
        if self.format == "json":
            return self._render_json(tree)
        elif self.format == "tree":
            return self._render_tree(tree)
        elif self.format == "plain":
            return self._render_plain(tree)
        elif self.format == "compact":
            return self._render_compact(tree)
        elif self.format == "relations":
            return self._render_relations(tree)
        else:
            return self._render_tree(tree)

    def _color(self, name: str, text: str) -> str:
        """Окрашивает текст если use_color=True."""
        if not self.use_color:
            return text

        if platform.system() == "Windows" and not self._ansi_enabled:
            markers = {
                "bold": f"*{text}*",
                "dim": f"({text})",
                "red": f"[!]{text}[/!]",
                "green": f"[+]{text}[/+]",
                "yellow": f"[?]{text}[/?]",
            }
            return markers.get(name, text)

        if self._ansi_enabled and name in self._COLORS:
            return f"{self._COLORS[name]}{text}{self._COLORS['reset']}"

        return text

    # -- JSON --

    def _render_jsonOLD(self, tree: ContextTree) -> str:
        """Рендерит в JSON."""
        data = {
            "version": "0.1",
            "count": len(tree.nodes),
            "query": tree.meta.get("query", ""),
            "tree": tree.to_dict(),
        }
        quit()
        return json.dumps(data, indent=JSON_INDENT, ensure_ascii=False)

    def _render_json(self, tree: ContextTree) -> str:
        """Рендерит в JSON (упрощенная версия без циклических ссылок)."""
        # Собираем только базовую информацию без связей
        nodes_data = []
        for node in tree.nodes[:100]:  # Лимит на количество узлов
            nodes_data.append({
                "text": node.match.text[:200],
                "score": node.match.score,
                "file": node.match.file_path,
                "line": node.match.line,
                "col": node.match.col,
                "type": node.match.match_type,
                "semantic_role": node.semantic_role,
                "snippet_preview": node.snippet[:100] if node.snippet else "",
            })
        
        data = {
            "version": "0.1",
            "count": len(tree.nodes),
            "query": tree.meta.get("query", ""),
            "nodes": nodes_data,
            "gap_edges_count": len(tree.gap_edges),
        }
        return json.dumps(data, indent=JSON_INDENT, ensure_ascii=False)
 
 
    # -- Tree (ASCII-дерево) --

    def _render_tree(self, tree: ContextTree) -> str:
        """Рендерит в древовидный формат."""
        lines: List[str] = [
            "Context Tree:",
            f"  Query: {tree.meta.get('query', 'unknown')}",
            f"  Total nodes: {len(tree.nodes)}",
            f"  Gap edges: {len(tree.gap_edges)}",
            "",
        ]

        for i, child in enumerate(tree.root.children, 1):
            lines.append(f"--- Result {i} ---")
            lines.append(self._render_tree_node(child, indent=0))
            lines.append("")

        return "\n".join(lines)

    def _render_tree_node(self, node: TreeNode, indent: int = 0, indent_str: str = "    ") -> str:
        """Рендерит один TreeNode в ASCII-дерево."""
        prefix = indent_str * indent
        score_str = f"[{node.match.score}]" if node.match.score > 0 else ""
        role_str = f"({node.semantic_role})" if node.semantic_role else ""
        header = f"{prefix}> {truncate_text(node.match.text, 50)} {score_str} {role_str}".rstrip()
        lines = [header]

        if node.parents:
            lines.append(f"{prefix}  ^ parents:")
            for p in node.parents:
                lines.append(f"{prefix}    {truncate_text(p.match.text, 40)} [{p.match.score}]")

        if node.cross_refs:
            lines.append(f"{prefix}  <> cross_refs:")
            for ref in node.cross_refs:
                conf = f"{ref.confidence:.1f}"
                lines.append(f"{prefix}    [{ref.type}] {truncate_text(ref.description, 50)} (conf={conf})")

        if node.children:
            lines.append(f"{prefix}  v children:")
            for child in node.children:
                lines.append(self._render_tree_node(child, indent + 1, indent_str))

        snippet_preview = node.snippet.replace(chr(10), " ")[:80]
        if snippet_preview:
            lines.append(f'{prefix}  "{snippet_preview}..."')

        return "\n".join(lines)

    # -- Plain --

    def _render_plain(self, tree: ContextTree) -> str:
        """Рендерит в plain text (без цветов, минималистично)."""
        lines: List[str] = []

        for node in tree.nodes:
            file_name = os.path.basename(node.match.file_path)
            lines.append(f"{node.match.match_type} {truncate_text(node.match.text, 50)}")
            lines.append(f"  {file_name}:{node.match.line}:{node.match.col}")
            if node.snippet:
                for sline in node.snippet.splitlines()[:10]:
                    lines.append(f"  | {sline}")
            lines.append("")

        return "\n".join(lines)

    # -- Compact --

    def _render_compact(self, tree: ContextTree) -> str:
        """Краткий вывод: топ-3 матча."""
        top3 = tree.nodes[:3]
        lines = [f"Query: {tree.meta.get('query', '?')}", ""]

        for i, node in enumerate(top3, 1):
            m = node.match
            lines.append(f"{i}. [{m.score}] {m.file_path}:{m.line}  {truncate_text(m.text, 50)}")

        return "\n".join(lines)

    # -- Relations only --

    def _render_relations(self, tree: ContextTree) -> str:
        """Рендерит только связи."""
        all_edges = []
        for node in tree.nodes:
            all_edges.extend(node.cross_refs)
        all_edges.extend(tree.gap_edges)

        lines = ["Relations:"]
        for e in all_edges:
            lines.append(f"  [{e.type}] {e.from_id} -> {e.to_id or 'empty'}  (conf={e.confidence:.2f})")
            lines.append(f"    {e.description}")

        return "\n".join(lines)
