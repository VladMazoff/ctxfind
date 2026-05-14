from __future__ import annotations

"""Базовые типы и интерфейс для парсеров."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class NodeDTO:
    """AST-узел с поддержкой semantic_role для V01 скоринга."""
    id: str
    type: str
    name: str
    line: int = 0
    col: int = 0
    end_line: int = 0
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    raw_text: str = ""
    file_path: str = ""
    # V01: semantic_role для скоринга (definition/usage/transformation/output)
    semantic_role: Optional[str] = None

    def with_file_path(self, path: str) -> "NodeDTO":
        return NodeDTO(
            id=self.id, type=self.type, name=self.name, line=self.line,
            col=self.col, end_line=self.end_line, parent_id=self.parent_id,
            children_ids=list(self.children_ids), raw_text=self.raw_text,
            file_path=path, semantic_role=self.semantic_role,
        )


@dataclass
class SymbolDTO:
    """Именованный символ в коде."""
    node_id: str
    name: str
    type: str = "unknown"


@dataclass
class ParseResult:
    """Результат парсинга одного файла."""
    status: str = "ok"
    tokens: List[str] = field(default_factory=list)
    nodes: List[NodeDTO] = field(default_factory=list)
    symbols: Dict[str, List[SymbolDTO]] = field(default_factory=dict)

    def add_symbol(self, symbol: SymbolDTO) -> None:
        """Добавляет символ в индекс."""
        self.symbols.setdefault(symbol.name, []).append(symbol)


class BaseParser:
    """Абстрактный базовый класс парсера."""

    def parse(self, file_path: str) -> ParseResult:
        """Парсит файл и возвращает ParseResult."""
        raise NotImplementedError

    def _read_file(self, file_path: str) -> str:
        """Читает файл в строку."""
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    def _fallback_tokens(self, file_path: str) -> List[str]:
        """Возвращает fallback-токены при ошибке парсинга."""
        try:
            source = self._read_file(file_path)
            import re
            return re.findall(r"[a-zA-Z_][a-zA-Z0-9_-]*", source)
        except Exception:
            return []
