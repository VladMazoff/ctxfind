from __future__ import annotations

"""Многоступенчатый скоринг-движок с аддитивными весами и потолком (Capped Additive).

Контракты:
  - ScoredMatch: базовое совпадение с оценкой и breakdown.
  - ScorerEngine: score_node(), score_text_line(), apply_proximity_bonus().
  - Capped Additive: суммируем веса эвристик, ограничиваем MAX_SCORE.
  - Breakdown: в метаданные пишем вклад каждой эвристики.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from config import (
    DEFAULT_SNIPPET_LINES,
    GAP_BLACKLIST,
    MAX_SCORE,
    SCORING_RULES,
    AUTO_EXPAND_RULES,
)
from utils.text import normalize_query


@dataclass
class ScoreBreakdown:
    """Вклад каждой эвристики в итоговый скор."""
    exact_match: int = 0
    css_selector: int = 0
    html_class_attr: int = 0
    js_dom_api: int = 0
    css_child_selector: int = 0
    proximity_bonus: int = 0
    fallback_tfidf: int = 0
    context_block_bonus: int = 0
    semantic_keyword_bonus: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            k: v for k, v in self.__dict__.items()
            if v > 0
        }

    def total(self) -> int:
        return sum(self.__dict__.values())


@dataclass
class ScoredMatch:
    """Базовое совпадение с оценкой.

    Поля:
        node_id: уникальный ID узла (или сгенерированный для fallback).
        text: текст совпадения (сырой текст узла или сниппет строки).
        file_path: путь к файлу.
        line: номер строки (1-based).
        col: номер колонки (0-based).
        end_line: конечная строка.
        score: итоговый скор (0-MAX_SCORE).
        score_breakdown: вклад каждой эвристики.
        match_type: тип совпадения для отладки.
        semantic_role: semantic_hint (definition/usage/transformation/output/gap).
        meta: произвольные метаданные.
    """
    node_id: str
    text: str
    file_path: str
    line: int
    col: int = 0
    end_line: int = 0
    score: int = 0
    score_breakdown: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    match_type: str = "unknown"
    semantic_role: Optional[str] = None
    meta: Dict = field(default_factory=dict)

    @property
    def location_key(self) -> str:
        """Нормализованный ключ для дедупликации: file:line."""
        return f"{self.file_path}:{self.line}"

    def to_dict(self) -> Dict:
        return {
            "node_id": self.node_id,
            "text": self.text,
            "file": self.file_path,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "score": self.score,
            "score_breakdown": self.score_breakdown.to_dict(),
            "match_type": self.match_type,
            "semantic_role": self.semantic_role,
            "meta": self.meta,
        }


class ScorerEngine:
    """Движок скоринга с Capped Additive правилом.

    Pipeline:
      1. Для каждого кандидата собираем веса всех применимых эвристик.
      2. Суммируем (аддитивность).
      3. Ограничиваем MAX_SCORE (cap).
      4. Сохраняем breakdown в метаданные.
    """

    # DOM API паттерны для JS
    JS_DOM_PATTERNS = [
        r"querySelector\s*\(",
        r"querySelectorAll\s*\(",
        r"classList\s*\.",
        r"dataset\s*\.",
        r"getElementById\s*\(",
        r"getElementsByClassName\s*\(",
        r"getElementsByTagName\s*\(",
        r"addEventListener\s*\(",
        r"appendChild\s*\(",
        r"innerHTML\s*=",
        r"textContent\s*=",
        r"style\s*\.",
    ]

    # CSS-селекторные паттерны
    CSS_SELECTOR_PATTERN = re.compile(
        r"(^|[^\w-])([.#][a-zA-Z_][a-zA-Z0-9_-]*)(\s*[,{\s]|$)"
    )
    CSS_CHILD_SELECTOR_PATTERN = re.compile(
        r"([.#][a-zA-Z_][a-zA-Z0-9_-]*)\s+[>+~]?\s*([.#]?[a-zA-Z_][a-zA-Z0-9_-]*)"
    )

    # HTML class attr паттерн
    # Используем chr() для избежания проблем с кавычками
    _Q1 = chr(34)   # double quote
    _Q2 = chr(39)   # single quote
    HTML_CLASS_PATTERN = re.compile(
        r"class" + r"\s*=\s*[" + _Q1 + _Q2 + r"]([^" + _Q1 + _Q2 + r"]*)[" + _Q1 + _Q2 + r"]",
        re.IGNORECASE,
    )

    # Semantic keywords
    SEMANTIC_KEYWORDS = {
        "tile", "tiles", "card", "cards", "grid", "masonry",
        "data-columns", "data-column", "column", "columns",
        "flex", "container", "wrapper", "item", "items",
        "list", "listing", "gallery", "panel", "panels",
    }

    # Context block markers
    CONTEXT_BLOCK_PATTERN = re.compile(
        r"(?:/\*|//|#|<!--)\s*={3,}\s*(.+?)\s*={3,}",
        re.IGNORECASE,
    )

    def __init__(self, max_score: int = MAX_SCORE):
        self.max_score = max_score
        self._js_dom_regex = re.compile(
            "|".join(f"({p})" for p in self.JS_DOM_PATTERNS),
            re.IGNORECASE,
        )

    # -- Публичные методы --

    def score_text_line(
        self,
        query: str,
        line_text: str,
        file_path: str,
        line_num: int,
        lang: Optional[str] = None,
    ) -> Optional[ScoredMatch]:
        """Скорит одну строку текста. Возвращает ScoredMatch или None если ниже порога.

        Args:
            query: поисковый запрос (может быть селектором, именем символа и т.д.).
            line_text: текст строки.
            file_path: путь к файлу.
            line_num: номер строки (1-based).
            lang: язык файла (python, javascript, html, css).

        Returns:
            ScoredMatch или None.
        """
        if not line_text or not query:
            return None

        breakdown = ScoreBreakdown()
        query_stripped = query.strip()
        line_lower = line_text.lower()
        query_lower = query_stripped.lower()

        # 1. Exact match -- маркер встречается как токен
        if self._is_exact_token_match(query_stripped, line_text):
            breakdown.exact_match = SCORING_RULES["exact_match"]

        # 2. CSS selector -- .tiles, #tiles в CSS-файлах
        if lang == "css" and query_stripped.startswith((".", "#")):
            if self._is_css_selector_match(query_stripped, line_text):
                breakdown.css_selector = SCORING_RULES["css_selector"]

        # 3. HTML class attr -- class="... tiles ..."
        if lang == "html":
            if self._is_html_class_match(query_stripped, line_text):
                breakdown.html_class_attr = SCORING_RULES["html_class_attr"]

        # 4. JS DOM API -- querySelector, classList, dataset
        if lang == "javascript":
            if self._is_js_dom_match(query_stripped, line_text):
                breakdown.js_dom_api = SCORING_RULES["js_dom_api"]

        # 5. CSS child selector -- .tiles .tile
        if lang == "css":
            if self._is_css_child_selector_match(query_stripped, line_text):
                breakdown.css_child_selector = SCORING_RULES["css_child_selector"]

        # 6. Context block bonus -- внутри /* ===== Тайлы ===== */
        if self._has_context_block_marker(line_text):
            breakdown.context_block_bonus = SCORING_RULES["context_block_bonus"]

        # 7. Semantic keyword bonus -- рядом семантические слова
        if self._has_semantic_keywords(line_text):
            breakdown.semantic_keyword_bonus = SCORING_RULES["semantic_keyword_bonus"]

        total = breakdown.total()
        if total == 0:
            return None

        # Cap
        capped = min(total, self.max_score)

        # Определяем semantic_role
        semantic_role = self._infer_semantic_role(line_text, lang, query_stripped)

        match = ScoredMatch(
            node_id=f"{file_path}:{line_num}",
            text=line_text.rstrip("\n"),
            file_path=file_path,
            line=line_num,
            col=line_text.lower().find(query_lower),
            end_line=line_num,
            score=capped,
            score_breakdown=breakdown,
            match_type=self._determine_match_type(breakdown),
            semantic_role=semantic_role,
            meta={
                "lang": lang,
                "raw_score": total,
                "capped": total > self.max_score,
            },
        )
        return match

    def apply_proximity_bonus(
        self,
        matches: List[ScoredMatch],
        window_lines: int = 15,
    ) -> List[ScoredMatch]:
        """Применяет proximity bonus: если два высоко-скоренных матча
        находятся в пределах +/-window_lines строк, оба получают бонус.

        Args:
            matches: список ScoredMatch (уже отскоренных).
            window_lines: радиус окна в строках.

        Returns:
            Список matches с примененным proximity bonus.
        """
        if len(matches) < 2:
            return matches

        # Сортируем по файлу и строке
        sorted_matches = sorted(matches, key=lambda m: (m.file_path, m.line))

        # Для каждого матча ищем соседей в окне
        for i, match in enumerate(sorted_matches):
            if match.score < SCORING_RULES["threshold_default"]:
                continue  # Только высоко-скоренные дают/получают бонус

            for j in range(i + 1, len(sorted_matches)):
                other = sorted_matches[j]
                if other.file_path != match.file_path:
                    break  # Другой файл -- прекращаем поиск

                distance = abs(other.line - match.line)
                if distance > window_lines:
                    break  # Вышли за окно

                # Оба получают proximity bonus (если еще не получали)
                if match.score_breakdown.proximity_bonus == 0:
                    match.score_breakdown.proximity_bonus = SCORING_RULES["proximity_bonus"]
                    match.score = min(match.score + SCORING_RULES["proximity_bonus"], self.max_score)

                if other.score_breakdown.proximity_bonus == 0:
                    other.score_breakdown.proximity_bonus = SCORING_RULES["proximity_bonus"]
                    other.score = min(other.score + SCORING_RULES["proximity_bonus"], self.max_score)

        return sorted_matches

    def filter_by_threshold(
        self,
        matches: List[ScoredMatch],
        threshold: int,
    ) -> List[ScoredMatch]:
        """Фильтрует матчи по минимальному порогу скора."""
        return [m for m in matches if m.score >= threshold]

    def sort_by_relevance(
        self,
        matches: List[ScoredMatch],
    ) -> List[ScoredMatch]:
        """Сортирует: score desc, затем file order, затем line asc."""
        return sorted(
            matches,
            key=lambda m: (-m.score, m.file_path, m.line),
        )

    def deduplicate_by_location(
        self,
        matches: List[ScoredMatch],
    ) -> List[ScoredMatch]:
        """Дедупликация по location_key (file:line), оставляя максимальный скор.

        Args:
            matches: список ScoredMatch.

        Returns:
            Список без дублей по location_key.
        """
        best_by_loc: Dict[str, ScoredMatch] = {}
        for m in matches:
            key = m.location_key
            if key not in best_by_loc or m.score > best_by_loc[key].score:
                best_by_loc[key] = m
        return list(best_by_loc.values())

    def is_gap_symbol(self, query: str) -> bool:
        """Проверяет, является ли запрос gap-символом (черный список)."""
        return query.lower().strip() in GAP_BLACKLIST

    def auto_expand(self, query: str) -> List[str]:
        """Генерирует производные маркеры по правилам AUTO_EXPAND_RULES.

        Returns:
            Список производных маркеров (макс 3).
        """
        query_stripped = query.strip()
        derivatives = list(AUTO_EXPAND_RULES.get(query_stripped, []))
        return derivatives[:3]

    # -- Внутренние эвристики --

    def _is_exact_token_match(self, query: str, line: str) -> bool:
        """Проверяет, что query встречается как целый токен в строке."""
        clean_query = normalize_query(query)
        if not clean_query:
            return False

        pattern = re.compile(
            r"(?:^|[^\w-])" + re.escape(clean_query) + r"(?:[^\w-]|$)",
            re.IGNORECASE,
        )
        return bool(pattern.search(line))

    def _is_css_selector_match(self, query: str, line: str) -> bool:
        """Проверяет, что query -- CSS-селектор в строке."""
        if not query.startswith((".", "#")):
            return False
        escaped = re.escape(query)
        pattern = re.compile(
            r"(?:^|\s|,|{)" + escaped + r"(?:\s*[,{>+~]|\s+[.#\[]|\s*$)",
            re.IGNORECASE,
        )
        return bool(pattern.search(line))

    def _is_html_class_match(self, query: str, line: str) -> bool:
        """Проверяет, что query -- class name внутри class="..."."""
        clean = normalize_query(query)
        if not clean:
            return False
        match = self.HTML_CLASS_PATTERN.search(line)
        if not match:
            return False
        classes = match.group(1).split()
        return clean.lower() in [c.lower() for c in classes]

    def _is_js_dom_match(self, query: str, line: str) -> bool:
        """Проверяет, что в строке JS есть DOM-манипуляция, связанная с query."""
        has_dom_api = bool(self._js_dom_regex.search(line))
        if not has_dom_api:
            return False
        clean = normalize_query(query)
        if not clean:
            return False
        return clean.lower() in line.lower()

    def _is_css_child_selector_match(self, query: str, line: str) -> bool:
        """Проверяет child selector: .tiles .tile, .tiles > *."""
        if not query.startswith((".", "#")):
            return False
        escaped = re.escape(query)
        pattern = re.compile(
            escaped + r"\s+[>+~]?\s*([.#]?[\w-]+)",
            re.IGNORECASE,
        )
        return bool(pattern.search(line))

    def _has_context_block_marker(self, line: str) -> bool:
        """Проверяет наличие контекстного блока /* ===== Name ===== */."""
        return bool(self.CONTEXT_BLOCK_PATTERN.search(line))

    def _has_semantic_keywords(self, line: str) -> bool:
        """Проверяет наличие семантических ключевых слов."""
        line_lower = line.lower()
        for kw in self.SEMANTIC_KEYWORDS:
            pattern = re.compile(
                r"(?:^|[^\w-])" + re.escape(kw) + r"(?:[^\w-]|$)",
                re.IGNORECASE,
            )
            if pattern.search(line_lower):
                return True
        return False

    def _infer_semantic_role(
        self,
        line_text: str,
        lang: Optional[str],
        query: str,
    ) -> Optional[str]:
        """Инферирует semantic_role по контексту строки."""
        line_lower = line_text.lower()

        # Definition: CSS-правило, function def, class def
        if lang == "css":
            if query in line_text and ("{" in line_text or ":" in line_text):
                return "definition"
        if lang in ("python", "javascript"):
            if any(kw in line_lower for kw in ("def ", "class ", "const ", "let ", "var ")):
                if query.lower() in line_lower:
                    return "definition"

        # Usage: вызов функции, обращение к переменной
        if lang in ("python", "javascript"):
            if "(" in line_text and query.lower() in line_lower:
                return "usage"

        # Transformation: присваивание, мутация
        if "=" in line_text and query.lower() in line_lower:
            if lang == "javascript" and any(api in line_lower for api in ("classlist", "dataset", "style", "innerhtml")):
                return "transformation"
            if lang == "css" and any(prop in line_lower for prop in ("transform", "animation", "transition")):
                return "transformation"

        # Output: return, print, console.log
        if any(kw in line_lower for kw in ("return ", "console.log", "print(", "yield ")):
            return "output"

        return None

    def _determine_match_type(self, breakdown: ScoreBreakdown) -> str:
        """Определяет доминирующий тип совпадения для отладки."""
        weights = {
            "exact": breakdown.exact_match,
            "css_selector": breakdown.css_selector,
            "html_class": breakdown.html_class_attr,
            "js_dom": breakdown.js_dom_api,
            "css_child": breakdown.css_child_selector,
            "proximity": breakdown.proximity_bonus,
            "fallback": breakdown.fallback_tfidf,
            "context_block": breakdown.context_block_bonus,
            "semantic": breakdown.semantic_keyword_bonus,
        }
        dominant = max(weights, key=weights.get)
        if weights[dominant] == 0:
            return "unknown"
        return dominant
