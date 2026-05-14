from __future__ import annotations

"""Адаптер парсера JavaScript на основе токенизатора + эвристик.

Контракты:
  - Поддерживает: функции, классы, импорты/экспорты, вызовы.
  - V01: DOM API detection (querySelector, classList, dataset, etc).
  - semantic_role: definition/usage/transformation/output.
"""

import os
import re
from typing import Dict, List, Optional, Tuple

from parsers.base import BaseParser, NodeDTO, ParseResult, SymbolDTO


class JavaScriptParser(BaseParser):
    """Парсер JavaScript через токенизацию и regex-эвристики."""

    _KEYWORDS = {
        "function", "class", "const", "let", "var", "import", "export",
        "from", "return", "if", "else", "for", "while", "async", "await",
        "try", "catch", "finally", "throw", "new", "this", "super",
        "extends", "static", "get", "set", "default", "as",
    }

    # V01: DOM API patterns for detection
    _DOM_API_PATTERNS = [
        r"querySelector\s*\(", r"querySelectorAll\s*\(",
        r"getElementById\s*\(", r"getElementsByClassName\s*\(",
        r"getElementsByTagName\s*\(", r"classList\s*\.",
        r"dataset\s*\.", r"addEventListener\s*\(",
        r"appendChild\s*\(", r"innerHTML\s*=", r"textContent\s*=",
        r"style\s*\.", r"setAttribute\s*\(", r"removeAttribute\s*\(",
    ]

    # Build token patterns using chr() to avoid quote issues
    _DQUOTE = chr(34)   # double quote
    _SQUOTE = chr(39)   # single quote
    _BTICK = chr(96)    # backtick

    _TOKEN_PATTERNS = [
        ("COMMENT", r"//.*?$|/\*.*?\*/"),
        ("STRING", _DQUOTE + r"(?:[^" + _DQUOTE + r"\\]|\\.)*" + _DQUOTE + r"|" + _SQUOTE + r"(?:[^" + _SQUOTE + r"\\]|\\.)*" + _SQUOTE + r"|" + _BTICK + r"(?:[^" + _BTICK + r"\\]|\\.)*" + _BTICK),
        ("NUMBER", r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"),
        ("ARROW", r"=>"),
        ("OPERATOR", r"===|!==|==|!=|<=|>=|&&|\|\||\+\+|--|[-+*/%=<>!&|^~:;,.]"),
        ("PAREN", r"[()\[\]{}]"),
        ("IDENT", r"[a-zA-Z_$][a-zA-Z0-9_$]*"),
        ("WHITESPACE", r"\s+"),
        ("OTHER", r"."),
    ]

    def __init__(self):
        self._token_regex = re.compile(
            "|".join(f"(?P<{name}>{pattern})" for name, pattern in self._TOKEN_PATTERNS),
            re.MULTILINE | re.DOTALL
        )
        self._dom_api_regex = re.compile(
            "|".join(f"({p})" for p in self._DOM_API_PATTERNS),
            re.IGNORECASE,
        )

    def parse(self, file_path: str) -> ParseResult:
        try:
            source = self._read_file(file_path)
        except Exception:
            return ParseResult(
                tokens=self._fallback_tokens(file_path),
                status="opaque",
            )

        result = ParseResult(status="ok")
        self._file_path = file_path
        self._source_lines = source.splitlines()
        self._node_counter = 0

        # Корневой узел модуля
        root_id = self._create_node_id(1, 0, "_root")
        root_node = NodeDTO(
            id=root_id,
            type="module",
            name=os.path.basename(file_path),
            line=1, col=0,
            end_line=len(self._source_lines) or 1,
            parent_id=None,
            raw_text=source[:200],
            file_path=file_path,
            semantic_role=None,
        )
        result.nodes.append(root_node)

        tokens = self._tokenize(source)
        self._parse_tokens(tokens, root_id, result)

        # V01: Post-process for DOM API calls
        self._detect_dom_api_calls(source, result)

        result.tokens = [t[1] for t in tokens if t[0] == "IDENT" and t[1] not in self._KEYWORDS]
        return result

    def _tokenize(self, source: str) -> List[Tuple[str, str, int, int]]:
        tokens = []
        line, col = 1, 0
        for match in self._token_regex.finditer(source):
            kind = match.lastgroup
            value = match.group()
            if kind == "WHITESPACE":
                for ch in value:
                    if ch == chr(10):
                        line += 1
                        col = 0
                    else:
                        col += 1
                continue
            if kind == "COMMENT":
                for ch in value:
                    if ch == chr(10):
                        line += 1
                        col = 0
                    else:
                        col += 1
                continue
            tokens.append((kind, value, line, col))
            col += len(value)
        return tokens

    def _parse_tokens(self, tokens, parent_id, result):
        i = 0
        while i < len(tokens):
            kind, value, line, col = tokens[i]

            if value == "function" or (value == "async" and i + 1 < len(tokens) and tokens[i + 1][1] == "function"):
                node, i = self._parse_function(tokens, i, parent_id, result)
                if node:
                    result.nodes.append(node)
            elif value == "class":
                node, i = self._parse_class(tokens, i, parent_id, result)
                if node:
                    result.nodes.append(node)
            elif value == "import":
                node, i = self._parse_import(tokens, i, parent_id, result)
                if node:
                    result.nodes.append(node)
            elif value == "export":
                node, i = self._parse_export(tokens, i, parent_id, result)
                if node:
                    result.nodes.append(node)
            elif value in ("const", "let", "var"):
                node, i = self._parse_variable(tokens, i, parent_id, result)
                if node:
                    result.nodes.append(node)
            elif kind == "IDENT" and i + 1 < len(tokens) and tokens[i + 1][1] == "(":
                # Вызов функции: foo(...) → usage
                node, i = self._parse_call(tokens, i, parent_id, result)
                if node:
                    result.nodes.append(node)
            else:
                i += 1

    def _parse_function(self, tokens, start, parent_id, result):
        i = start
        is_async = False
        if tokens[i][1] == "async":
            is_async = True
            i += 1
        if i >= len(tokens) or tokens[i][1] != "function":
            return None, start + 1
        i += 1
        func_name = "<anonymous>"
        if i < len(tokens) and tokens[i][0] == "IDENT":
            func_name = tokens[i][1]
            i += 1
        brace_depth = 0
        found_brace = False
        while i < len(tokens):
            if tokens[i][1] == "{":
                found_brace = True
                brace_depth = 1
                i += 1
                break
            i += 1
        if not found_brace:
            return None, i
        while i < len(tokens) and brace_depth > 0:
            if tokens[i][1] == "{":
                brace_depth += 1
            elif tokens[i][1] == "}":
                brace_depth -= 1
            i += 1
        line, col = tokens[start][2], tokens[start][3]
        node_id = self._create_node_id(line, col, f"_func_{func_name}")
        node = NodeDTO(
            id=node_id, type="function",
            name=("async " if is_async else "") + func_name,
            line=line, col=col,
            end_line=tokens[i - 1][2] if i > 0 else line,
            parent_id=parent_id,
            raw_text=self._get_raw_text(line, tokens[i - 1][2] if i > 0 else line),
            file_path=self._file_path,
            semantic_role="definition",
        )
        result.add_symbol(SymbolDTO(node_id=node_id, name=func_name, type="def"))
        return node, i

    def _parse_class(self, tokens, start, parent_id, result):
        i = start + 1
        class_name = "<anonymous>"
        if i < len(tokens) and tokens[i][0] == "IDENT":
            class_name = tokens[i][1]
            i += 1
        if i < len(tokens) and tokens[i][1] == "extends":
            i += 1
            if i < len(tokens) and tokens[i][0] == "IDENT":
                i += 1
        brace_depth = 0
        found_brace = False
        while i < len(tokens):
            if tokens[i][1] == "{":
                found_brace = True
                brace_depth = 1
                i += 1
                break
            i += 1
        if not found_brace:
            return None, i
        while i < len(tokens) and brace_depth > 0:
            if tokens[i][1] == "{":
                brace_depth += 1
            elif tokens[i][1] == "}":
                brace_depth -= 1
            i += 1
        line, col = tokens[start][2], tokens[start][3]
        node_id = self._create_node_id(line, col, f"_class_{class_name}")
        node = NodeDTO(
            id=node_id, type="class", name=class_name,
            line=line, col=col,
            end_line=tokens[i - 1][2] if i > 0 else line,
            parent_id=parent_id,
            raw_text=self._get_raw_text(line, tokens[i - 1][2] if i > 0 else line),
            file_path=self._file_path,
            semantic_role="definition",
        )
        result.add_symbol(SymbolDTO(node_id=node_id, name=class_name, type="def"))
        return node, i

    def _parse_import(self, tokens, start, parent_id, result):
        i = start + 1
        line, col = tokens[start][2], tokens[start][3]
        parts = []
        while i < len(tokens):
            if tokens[i][1] == ";":
                i += 1
                break
            if tokens[i][0] == "STRING":
                parts.append(tokens[i][1])
            elif tokens[i][0] == "IDENT" and tokens[i][1] not in self._KEYWORDS:
                parts.append(tokens[i][1])
            i += 1
        module_path = ""
        for p in parts:
            if p.startswith(chr(34)) or p.startswith(chr(39)) or p.startswith(chr(96)):
                module_path = p.strip(chr(34) + chr(39) + chr(96))
                break
        node_id = self._create_node_id(line, col, f"_import_{module_path[:20]}")
        node = NodeDTO(
            id=node_id, type="import", name=module_path or ", ".join(parts[:5]),
            line=line, col=col, end_line=line, parent_id=parent_id,
            raw_text=self._get_raw_text(line, line),
            file_path=self._file_path,
            semantic_role="usage",
        )
        if module_path:
            result.add_symbol(SymbolDTO(node_id=node_id, name=module_path, type="import"))
        return node, i

    def _parse_export(self, tokens, start, parent_id, result):
        i = start + 1
        line, col = tokens[start][2], tokens[start][3]
        is_default = False
        if i < len(tokens) and tokens[i][1] == "default":
            is_default = True
            i += 1
        parts = []
        while i < len(tokens):
            if tokens[i][1] == ";":
                i += 1
                break
            if tokens[i][0] == "IDENT":
                parts.append(tokens[i][1])
            i += 1
        name = " ".join(parts[:3]) if parts else "export"
        if is_default:
            name = "default " + name
        node_id = self._create_node_id(line, col, f"_export_{name[:20]}")
        node = NodeDTO(
            id=node_id, type="export", name=name,
            line=line, col=col, end_line=line, parent_id=parent_id,
            raw_text=self._get_raw_text(line, line),
            file_path=self._file_path,
            semantic_role="output",
        )
        return node, i

    def _parse_variable(self, tokens, start, parent_id, result):
        i = start + 1
        line, col = tokens[start][2], tokens[start][3]
        if i >= len(tokens) or tokens[i][0] != "IDENT":
            return None, start + 1
        var_name = tokens[i][1]
        i += 1
        if i < len(tokens) and tokens[i][1] == "=":
            i += 1
            if i < len(tokens):
                if tokens[i][1] == "function":
                    node, i = self._parse_function(tokens, i, parent_id, result)
                    if node:
                        node = NodeDTO(
                            id=node.id, type=node.type, name=var_name,
                            line=line, col=col, end_line=node.end_line,
                            parent_id=parent_id, raw_text=node.raw_text,
                            file_path=node.file_path, semantic_role="definition",
                        )
                        result.add_symbol(SymbolDTO(node_id=node.id, name=var_name, type="def"))
                    return node, i
                elif tokens[i][1] == "class":
                    node, i = self._parse_class(tokens, i, parent_id, result)
                    if node:
                        node = NodeDTO(
                            id=node.id, type=node.type, name=var_name,
                            line=line, col=col, end_line=node.end_line,
                            parent_id=parent_id, raw_text=node.raw_text,
                            file_path=node.file_path, semantic_role="definition",
                        )
                        result.add_symbol(SymbolDTO(node_id=node.id, name=var_name, type="def"))
                    return node, i
        while i < len(tokens) and tokens[i][1] != ";":
            i += 1
        if i < len(tokens) and tokens[i][1] == ";":
            i += 1
        node_id = self._create_node_id(line, col, f"_var_{var_name}")
        node = NodeDTO(
            id=node_id, type="variable", name=var_name,
            line=line, col=col, end_line=line, parent_id=parent_id,
            raw_text=self._get_raw_text(line, line),
            file_path=self._file_path,
            semantic_role="definition",
        )
        result.add_symbol(SymbolDTO(node_id=node_id, name=var_name, type="def"))
        return node, i


    def _parse_call(self, tokens, start, parent_id, result):
        """Парсит вызов функции как usage-узел."""
        i = start
        kind, func_name, line, col = tokens[i]

        # Пропускаем аргументы до закрывающей скобки
        paren_depth = 0
        while i < len(tokens):
            if tokens[i][1] == "(":
                paren_depth += 1
            elif tokens[i][1] == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    i += 1
                    break
            i += 1

        node_id = self._create_node_id(line, col, f"_call_{func_name}")
        node = NodeDTO(
            id=node_id,
            type="call",
            name=func_name,
            line=line,
            col=col,
            end_line=tokens[i - 1][2] if i > 0 else line,
            parent_id=parent_id,
            raw_text=self._get_raw_text(line, tokens[i - 1][2] if i > 0 else line),
            file_path=self._file_path,
            semantic_role="usage",
        )
        result.add_symbol(SymbolDTO(node_id=node_id, name=func_name, type="call"))
        return node, i

    def _detect_dom_api_calls(self, source: str, result: ParseResult) -> None:
        """V01: Detects DOM API calls and creates transformation nodes."""
        for i, line in enumerate(self._source_lines, 1):
            if self._dom_api_regex.search(line):
                # Find the nearest parent function/module
                parent_id = self._find_nearest_parent(i, result)
                node_id = self._create_node_id(i, 0, "_dom_api")
                node = NodeDTO(
                    id=node_id, type="dom_manipulation",
                    name=f"DOM API at line {i}",
                    line=i, col=0, end_line=i,
                    parent_id=parent_id,
                    raw_text=line.strip(),
                    file_path=self._file_path,
                    semantic_role="transformation",
                )
                result.nodes.append(node)

    def _find_nearest_parent(self, line: int, result: ParseResult) -> Optional[str]:
        """Finds the nearest enclosing function/module for a line."""
        best = None
        best_line = 0
        for node in result.nodes:
            if node.type in ("function", "module"):
                if node.line <= line and node.line >= best_line:
                    best = node.id
                    best_line = node.line
        return best

    def _create_node_id(self, line: int, col: int, suffix: str = "") -> str:
        self._node_counter += 1
        return f"js_{self._node_counter}_{line}_{col}{suffix}"

    def _get_raw_text(self, start_line: int, end_line: int) -> str:
        if start_line <= 0 or start_line > len(self._source_lines):
            return ""
        end_line = min(end_line, len(self._source_lines))
        return chr(10).join(self._source_lines[start_line - 1:end_line])
