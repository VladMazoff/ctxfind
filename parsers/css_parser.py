from __future__ import annotations

"""Адаптер парсера CSS на основе `tinycss2`.

Контракты:
  - Парсит CSS-файлы и <style>-блоки.
  - Извлекает: селекторы, правила, свойства, @media/@import.
  - Символы: class names, id names (для кросс-языковых мостов HTML<->CSS).
  - При отсутствии tinycss2 — fallback на regex-парсер.
  - V01: заполняет semantic_role (definition для CSS-правил).
"""

import os
import re
from typing import Dict, List, Optional, Tuple

from parsers.base import BaseParser, NodeDTO, ParseResult, SymbolDTO


class CSSParser(BaseParser):
    """Парсер CSS-файлов через tinycss2 или regex fallback."""

    def __init__(self):
        self._has_tinycss2 = self._check_tinycss2()

    def _check_tinycss2(self) -> bool:
        """Проверяет доступность tinycss2."""
        try:
            import tinycss2
            return True
        except ImportError:
            return False

    def parse(self, file_path: str) -> ParseResult:
        try:
            source = self._read_file(file_path)
        except Exception:
            return ParseResult(
                tokens=self._fallback_tokens(file_path),
                status="opaque",
            )

        if self._has_tinycss2:
            return self._parse_with_tinycss2(source, file_path)
        else:
            return self._parse_with_regex(source, file_path)

    def _parse_with_tinycss2(self, source: str, file_path: str) -> ParseResult:
        """Парсит CSS через tinycss2."""
        import tinycss2

        result = ParseResult(status="ok")
        self._file_path = file_path
        self._source_lines = source.splitlines()
        self._node_counter = 0

        # Корневой узел
        root_id = self._create_node_id(1, 0, "_root")
        root_node = NodeDTO(
            id=root_id,
            type="stylesheet",
            name=os.path.basename(file_path),
            line=1,
            col=0,
            end_line=len(self._source_lines) or 1,
            parent_id=None,
            raw_text=source[:200],
            file_path=file_path,
            semantic_role=None,
        )
        result.nodes.append(root_node)

        rules = tinycss2.parse_stylesheet(source, skip_comments=True, skip_whitespace=True)

        for rule in rules:
            if isinstance(rule, tinycss2.ast.QualifiedRule):
                self._process_qualified_rule(rule, root_id, result)
            elif isinstance(rule, tinycss2.ast.AtRule):
                self._process_at_rule(rule, root_id, result)
            elif isinstance(rule, tinycss2.ast.ParseError):
                result.status = "partial"

        result.tokens = self._extract_css_tokens(source)
        return result

    def _process_qualified_rule(self, rule, parent_id: str, result: ParseResult) -> None:
        """Обрабатывает CSS-правило (селектор + блок)."""
        selector_text = "".join(t.serialize() for t in rule.prelude).strip()
        line, col = self._get_line_col(rule.source_line if hasattr(rule, "source_line") else 1)

        node_id = self._create_node_id(line, col, f"_rule_{selector_text[:20]}")
        node = NodeDTO(
            id=node_id,
            type="rule",
            name=selector_text[:100],
            line=line,
            col=col,
            end_line=line,
            parent_id=parent_id,
            raw_text=selector_text + " { ... }",
            file_path=self._file_path,
            semantic_role="definition",  # V01: CSS-правило = определение стиля
        )
        result.nodes.append(node)

        # Извлекаем class/id из селектора
        classes = re.findall(r"\.([a-zA-Z_][a-zA-Z0-9_-]*)", selector_text)
        ids = re.findall(r"#([a-zA-Z_][a-zA-Z0-9_-]*)", selector_text)

        for cls in classes:
            result.add_symbol(SymbolDTO(
                node_id=node_id, name=cls, type="style_class"
            ))
        for id_name in ids:
            result.add_symbol(SymbolDTO(
                node_id=node_id, name=id_name, type="style_id"
            ))

        # Парсим declarations
        try:
            declarations = tinycss2.parse_declaration_list(rule.content)
            for decl in declarations:
                if hasattr(decl, "name"):
                    decl_id = self._create_node_id(line, col, f"_decl_{decl.name}")
                    decl_node = NodeDTO(
                        id=decl_id,
                        type="property",
                        name=decl.name,
                        line=line,
                        col=col,
                        end_line=line,
                        parent_id=node_id,
                        raw_text=f"{decl.name}: ...",
                        file_path=self._file_path,
                        semantic_role="definition",
                    )
                    result.nodes.append(decl_node)
        except Exception:
            pass

    def _process_at_rule(self, rule, parent_id: str, result: ParseResult) -> None:
        """Обрабатывает @-правило (@media, @import и т.д.)."""
        at_name = rule.at_keyword
        prelude_text = "".join(t.serialize() for t in rule.prelude).strip()
        line, col = self._get_line_col(rule.source_line if hasattr(rule, "source_line") else 1)

        node_id = self._create_node_id(line, col, f"_at_{at_name}")
        node = NodeDTO(
            id=node_id,
            type="at_rule",
            name=f"{at_name} {prelude_text[:50]}",
            line=line,
            col=col,
            end_line=line,
            parent_id=parent_id,
            raw_text=f"{at_name} {prelude_text} {{ ... }}",
            file_path=self._file_path,
            semantic_role="definition",
        )
        result.nodes.append(node)

        if at_name == "import":
            urls = re.findall(r'["\']([^"\']+)["\']', prelude_text)
            for url in urls:
                result.add_symbol(SymbolDTO(
                    node_id=node_id, name=url, type="import"
                ))

    def _parse_with_regex(self, source: str, file_path: str) -> ParseResult:
        """Fallback regex-парсер CSS."""
        result = ParseResult(status="partial")
        self._file_path = file_path
        self._source_lines = source.splitlines()
        self._node_counter = 0

        root_id = self._create_node_id(1, 0, "_root")
        root_node = NodeDTO(
            id=root_id,
            type="stylesheet",
            name=os.path.basename(file_path),
            line=1, col=0,
            end_line=len(self._source_lines) or 1,
            parent_id=None,
            raw_text=source[:200],
            file_path=file_path,
            semantic_role=None,
        )
        result.nodes.append(root_node)

        rule_pattern = re.compile(r'([^{]+)\{([^}]*)\}', re.DOTALL)

        line = 1
        for match in rule_pattern.finditer(source):
            selector = match.group(1).strip()
            block = match.group(2).strip()

            node_id = self._create_node_id(line, 0, f"_rule_{selector[:20]}")
            node = NodeDTO(
                id=node_id,
                type="rule",
                name=selector[:100],
                line=line, col=0,
                end_line=line,
                parent_id=root_id,
                raw_text=selector + " { " + block[:50] + " }",
                file_path=file_path,
                semantic_role="definition",
            )
            result.nodes.append(node)

            classes = re.findall(r"\.([a-zA-Z_][a-zA-Z0-9_-]*)", selector)
            ids = re.findall(r"#([a-zA-Z_][a-zA-Z0-9_-]*)", selector)
            for cls in classes:
                result.add_symbol(SymbolDTO(node_id=node_id, name=cls, type="style_class"))
            for id_name in ids:
                result.add_symbol(SymbolDTO(node_id=node_id, name=id_name, type="style_id"))

            for decl_match in re.finditer(r'([a-zA-Z-]+)\s*:\s*([^;]+)', block):
                prop_name = decl_match.group(1).strip()
                decl_id = self._create_node_id(line, 0, f"_decl_{prop_name}")
                decl_node = NodeDTO(
                    id=decl_id,
                    type="property",
                    name=prop_name,
                    line=line, col=0,
                    end_line=line,
                    parent_id=node_id,
                    raw_text=f"{prop_name}: {decl_match.group(2).strip()}",
                    file_path=file_path,
                    semantic_role="definition",
                )
                result.nodes.append(decl_node)

            line += selector.count(chr(10)) + block.count(chr(10)) + 1

        result.tokens = self._extract_css_tokens(source)
        return result

    def _create_node_id(self, line: int, col: int, suffix: str = "") -> str:
        self._node_counter += 1
        return f"css_{self._node_counter}_{line}_{col}{suffix}"

    def _get_line_col(self, line: int) -> Tuple[int, int]:
        return line, 0

    def _extract_css_tokens(self, source: str) -> List[str]:
        """Извлекает токены из CSS для fallback."""
        tokens = []
        tokens.extend(re.findall(r"\.([a-zA-Z_][a-zA-Z0-9_-]*)", source))
        tokens.extend(re.findall(r"#([a-zA-Z_][a-zA-Z0-9_-]*)", source))
        tokens.extend(re.findall(r"([a-zA-Z-]+)\s*:", source))
        return tokens
