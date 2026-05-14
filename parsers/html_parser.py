from __future__ import annotations

"""Адаптер парсера HTML на основе stdlib `html.parser`.

Контракты:
  - Поддерживает HTML, XHTML.
  - Извлекает: теги, атрибуты (class, id), текст, комментарии.
  - Символы: элементы (class/id -> style-связи для кросс-языковых мостов).
  - Иерархия: parent-child через стек тегов.
  - V01: semantic_role (definition для элементов с class/id).
"""

import os
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from parsers.base import BaseParser, NodeDTO, ParseResult, SymbolDTO


class HTMLParserAdapter(BaseParser, HTMLParser):
    """Парсер HTML-файлов через html.parser."""

    _SKIP_CONTENT_TAGS = {"script", "style"}

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self._reset_state()

    def _reset_state(self):
        self._file_path = ""
        self._source_lines: List[str] = []
        self._line_map: Dict[int, int] = {}
        self._nodes: List[NodeDTO] = []
        self._symbols: Dict[str, List[SymbolDTO]] = {}
        self._tokens: List[str] = []
        self._tag_stack: List[Tuple[str, str, int, int]] = []
        self._node_counter = 0
        self._current_skip_tag: Optional[str] = None
        self._text_buffer = ""
        self._text_line = 0
        self._text_col = 0

    def parse(self, file_path: str) -> ParseResult:
        try:
            source = self._read_file(file_path)
        except Exception:
            return ParseResult(
                tokens=self._fallback_tokens(file_path),
                status="opaque",
            )

        self._reset_state()
        self._file_path = file_path
        self._source_lines = source.splitlines()
        self._build_line_map(source)

        try:
            self.feed(source)
        except Exception:
            return ParseResult(
                tokens=self._fallback_tokens(file_path),
                status="opaque",
            )

        self._flush_text_node()

        if self._nodes:
            root_id = self._nodes[0].id
        else:
            root_id = self._create_node_id(1, 0)

        for i, node in enumerate(self._nodes):
            if node.parent_id is None and node.id != root_id:
                self._nodes[i] = NodeDTO(
                    id=node.id, type=node.type, name=node.name,
                    line=node.line, col=node.col, end_line=node.end_line,
                    parent_id=root_id, children_ids=list(node.children_ids),
                    raw_text=node.raw_text, file_path=node.file_path,
                    semantic_role=node.semantic_role,
                )

        self._rebuild_children_ids()

        for i, node in enumerate(self._nodes):
            self._nodes[i] = node.with_file_path(file_path)

        return ParseResult(
            nodes=self._nodes,
            symbols=self._symbols,
            tokens=self._tokens,
            status="ok" if self._nodes else "partial",
        )

    def _build_line_map(self, source: str) -> None:
        pos = 0
        for line_num, line in enumerate(self._source_lines, 1):
            for col in range(len(line) + 1):
                self._line_map[pos + col] = line_num
            pos += len(line) + 1

    def _get_line_col(self, offset: int) -> Tuple[int, int]:
        line = self._line_map.get(offset, 1)
        return line, 0

    def _create_node_id(self, line: int, col: int, suffix: str = "") -> str:
        self._node_counter += 1
        return f"html_{self._node_counter}_{line}_{col}{suffix}"

    def _create_node(self, type: str, name: str, line: int, col: int,
                     end_line: int, parent_id: Optional[str], raw_text: str = "") -> NodeDTO:
        node_id = self._create_node_id(line, col)
        return NodeDTO(
            id=node_id, type=type, name=name, line=line, col=col,
            end_line=end_line, parent_id=parent_id, raw_text=raw_text,
            file_path=self._file_path, semantic_role=None,
        )

    def _add_symbol(self, name: str, type: str, node_id: str) -> None:
        sym = SymbolDTO(node_id=node_id, name=name, type=type)
        self._symbols.setdefault(name, []).append(sym)

    def _flush_text_node(self) -> None:
        text = self._text_buffer.strip()
        if text and len(text) > 1:
            parent_id = self._tag_stack[-1][1] if self._tag_stack else None
            node = self._create_node(
                type="text", name=text[:50], line=self._text_line,
                col=self._text_col, end_line=self._text_line,
                parent_id=parent_id, raw_text=text,
            )
            self._nodes.append(node)
        self._text_buffer = ""

    def _rebuild_children_ids(self) -> None:
        parent_to_children: Dict[str, List[str]] = {}
        for node in self._nodes:
            if node.parent_id:
                parent_to_children.setdefault(node.parent_id, []).append(node.id)

        for i, node in enumerate(self._nodes):
            if node.id in parent_to_children:
                self._nodes[i] = NodeDTO(
                    id=node.id, type=node.type, name=node.name,
                    line=node.line, col=node.col, end_line=node.end_line,
                    parent_id=node.parent_id,
                    children_ids=parent_to_children[node.id],
                    raw_text=node.raw_text, file_path=node.file_path,
                    semantic_role=node.semantic_role,
                )

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self._flush_text_node()
        line, col = self.getpos()

        if tag in self._SKIP_CONTENT_TAGS:
            self._current_skip_tag = tag
            return

        parent_id = self._tag_stack[-1][1] if self._tag_stack else None

        attr_dict = {k: (v or "") for k, v in attrs}
        name = tag
        if "id" in attr_dict:
            name += f"#{attr_dict['id']}"
        if "class" in attr_dict:
            classes = attr_dict["class"].split()
            name += f".{'.'.join(classes)}"

        raw_text = f"<{tag}"
        for k, v in attrs:
            raw_text += f' {k}="{v}"' if v else f" {k}"
        raw_text += ">"

        # V01: semantic_role = definition если есть class/id (DOM-элемент определен)
        semantic_role = None
        if "class" in attr_dict or "id" in attr_dict:
            semantic_role = "definition"

        node = self._create_node(
            type="element", name=name, line=line, col=col,
            end_line=line, parent_id=parent_id, raw_text=raw_text,
        )
        node.semantic_role = semantic_role
        self._nodes.append(node)

        self._tag_stack.append((tag, node.id, line, col))

        if "class" in attr_dict:
            for cls in attr_dict["class"].split():
                self._add_symbol(cls, "style_class", node.id)
                self._tokens.append(cls)
        if "id" in attr_dict:
            self._add_symbol(attr_dict["id"], "style_id", node.id)
            self._tokens.append(attr_dict["id"])

        self._tokens.append(tag)

    def handle_endtag(self, tag: str) -> None:
        self._flush_text_node()
        while self._tag_stack and self._tag_stack[-1][0] != tag:
            self._tag_stack.pop()
        if self._tag_stack:
            self._tag_stack.pop()
        if self._current_skip_tag == tag:
            self._current_skip_tag = None

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if self._tag_stack and self._tag_stack[-1][0] == tag:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._current_skip_tag:
            return
        if not self._text_buffer:
            self._text_line, self._text_col = self.getpos()
        self._text_buffer += data

    def handle_comment(self, data: str) -> None:
        self._flush_text_node()
        line, col = self.getpos()
        parent_id = self._tag_stack[-1][1] if self._tag_stack else None
        node = self._create_node(
            type="comment", name=f"<!--{data[:30]}...-->",
            line=line, col=col, end_line=line, parent_id=parent_id,
            raw_text=f"<!--{data}-->",
        )
        self._nodes.append(node)

    def handle_decl(self, decl: str) -> None:
        self._flush_text_node()
        line, col = self.getpos()
        parent_id = self._tag_stack[-1][1] if self._tag_stack else None
        node = self._create_node(
            type="doctype", name=f"<!{decl}>",
            line=line, col=col, end_line=line, parent_id=parent_id,
            raw_text=f"<!{decl}>",
        )
        self._nodes.append(node)
