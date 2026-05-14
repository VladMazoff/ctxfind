from __future__ import annotations

"""Построитель кросс-языковых связей (RelationEdge)."""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from config import GAP_BLACKLIST, LinkerError, MIN_SYMBOL_LENGTH_FOR_BRIDGE


@dataclass
class RelationEdge:
    """Типизированная связь между двумя точками в кодовой базе."""
    from_id: str
    to_id: Optional[str]
    type: str
    direction: str
    confidence: float
    description: str
    meta: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "from_id": self.from_id,
            "to_id": self.to_id,
            "type": self.type,
            "direction": self.direction,
            "confidence": self.confidence,
            "description": self.description,
            "meta": self.meta,
        }


class LinkerEngine:
    """Движок построения кросс-языковых связей."""

    # Build regex patterns programmatically to avoid quote issues
    _QUOTE_CHARS = chr(39) + chr(34)  # single + double quote

    CSS_SELECTOR_RE = re.compile(r"([.#])([a-zA-Z_][a-zA-Z0-9_-]*)")

    # Use chr() to avoid literal quotes in source
    _Q1 = chr(39)   # single quote
    _Q2 = chr(34)   # double quote

    @classmethod
    def _make_class_attr_re(cls):
        q1, q2 = cls._Q1, cls._Q2
        pattern = r"class" + r"\s*=\s*[" + q1 + q2 + r"]([^" + q1 + q2 + r"]*)[" + q1 + q2 + r"]"
        return re.compile(pattern, re.IGNORECASE)

    @classmethod
    def _make_id_attr_re(cls):
        q1, q2 = cls._Q1, cls._Q2
        pattern = r"id" + r"\s*=\s*[" + q1 + q2 + r"]([^" + q1 + q2 + r"]*)[" + q1 + q2 + r"]"
        return re.compile(pattern, re.IGNORECASE)

    @classmethod
    def _make_js_selector_re(cls):
        q1, q2 = cls._Q1, cls._Q2
        pattern = (
            r"(?:querySelector|querySelectorAll|getElementById|getElementsByClassName)"
            + r"\s*\(\s*[" + q1 + q2 + r"]([^" + q1 + q2 + r"]+)[" + q1 + q2 + r"]"
        )
        return re.compile(pattern, re.IGNORECASE)

    @classmethod
    def _make_js_classlist_re(cls):
        q1, q2 = cls._Q1, cls._Q2
        pattern = (
            r"(?:classList\.(?:add|remove|toggle|contains))"
            + r"\s*\(\s*[" + q1 + q2 + r"]([^" + q1 + q2 + r"]+)[" + q1 + q2 + r"]"
        )
        return re.compile(pattern, re.IGNORECASE)

    def __init__(self):
        self._css_selectors: Dict[str, List[Tuple[str, int, str]]] = {}
        self._html_classes: Dict[str, List[Tuple[str, int, str]]] = {}
        self._html_ids: Dict[str, List[Tuple[str, int, str]]] = {}
        self._js_refs: Dict[str, List[Tuple[str, int, str]]] = {}
        self._css_custom_props: Dict[str, List[Tuple[str, int, str]]] = {}

        # Compile patterns
        self.HTML_CLASS_RE = self._make_class_attr_re()
        self.HTML_ID_RE = self._make_id_attr_re()
        self.JS_SELECTOR_RE = self._make_js_selector_re()
        self.JS_CLASSLIST_RE = self._make_js_classlist_re()
        self.CSS_CUSTOM_PROP_RE = re.compile(r"(--[a-zA-Z_][a-zA-Z0-9_-]*)\s*:")
        self.CSS_CUSTOM_PROP_USAGE_RE = re.compile(r"var\s*\(\s*(--[a-zA-Z_][a-zA-Z0-9_-]*)")

    def index_file(self, file_path: str, lines: List[str], lang: str) -> None:
        """Индексирует файл для построения связей."""
        for line_num, line in enumerate(lines, start=1):
            line_stripped = line.strip()
            if not line_stripped:
                continue

            if lang == "css":
                self._index_css_line(file_path, line_num, line_stripped)
            elif lang == "html":
                self._index_html_line(file_path, line_num, line_stripped)
            elif lang == "javascript":
                self._index_js_line(file_path, line_num, line_stripped)

    def build_relations(self, query: str) -> List[RelationEdge]:
        """Строит все RelationEdge для данного запроса."""
        edges: List[RelationEdge] = []
        clean_query = query.lstrip(".#[]")

        edges.extend(self._link_css_to_html(query, clean_query))
        edges.extend(self._link_html_to_js(query, clean_query))
        edges.extend(self._link_css_child_selectors(query, clean_query))
        edges.extend(self._link_css_custom_props(query, clean_query))

        edges.sort(key=lambda e: e.confidence, reverse=True)
        return edges

    def detect_gaps(self, query: str, file_hits: Dict[str, int]) -> List[RelationEdge]:
        """Обнаруживает gap: символ найден в 2+ файлах без явных связей."""
        clean = query.strip().lstrip(".#[]")
        if clean.lower() in GAP_BLACKLIST:
            return []

        if len(file_hits) < 2:
            return []

        edges: List[RelationEdge] = []
        files = sorted(file_hits.keys())

        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                edges.append(RelationEdge(
                    from_id=f"{files[i]}:gap",
                    to_id=f"{files[j]}:gap",
                    type="gap",
                    direction="cross",
                    confidence=0.5,
                    description=f"Gap: {clean} найден в {files[i]} и {files[j]} без явного пути",
                    meta={
                        "symbol": clean,
                        "files": [files[i], files[j]],
                        "hits": {files[i]: file_hits[files[i]], files[j]: file_hits[files[j]]},
                    },
                ))

        return edges

    def group_by_semantic_marker(self, edges: List[RelationEdge]) -> Dict[str, List[RelationEdge]]:
        """Группирует связи по семантическим маркерам."""
        groups: Dict[str, List[RelationEdge]] = {}
        for edge in edges:
            marker = edge.meta.get("selector") or edge.meta.get("class") or edge.meta.get("id") or edge.type
            groups.setdefault(marker, []).append(edge)
        return groups

    def _index_css_line(self, file_path: str, line_num: int, line: str) -> None:
        """Индексирует CSS-строку."""
        for match in self.CSS_SELECTOR_RE.finditer(line):
            prefix, name = match.groups()
            selector = f"{prefix}{name}"
            self._css_selectors.setdefault(selector, []).append(
                (file_path, line_num, line)
            )

        for match in self.CSS_CUSTOM_PROP_RE.finditer(line):
            prop = match.group(1)
            self._css_custom_props.setdefault(prop, []).append(
                (file_path, line_num, line)
            )

    def _index_html_line(self, file_path: str, line_num: int, line: str) -> None:
        """Индексирует HTML-строку."""
        for match in self.HTML_CLASS_RE.finditer(line):
            classes = match.group(1).split()
            for cls in classes:
                self._html_classes.setdefault(cls, []).append(
                    (file_path, line_num, line)
                )

        for match in self.HTML_ID_RE.finditer(line):
            id_val = match.group(1)
            self._html_ids.setdefault(id_val, []).append(
                (file_path, line_num, line)
            )

    def _index_js_line(self, file_path: str, line_num: int, line: str) -> None:
        """Индексирует JS-строку."""
        for match in self.JS_SELECTOR_RE.finditer(line):
            selector = match.group(1)
            self._js_refs.setdefault(selector, []).append(
                (file_path, line_num, line)
            )

        for match in self.JS_CLASSLIST_RE.finditer(line):
            cls = match.group(1)
            self._js_refs.setdefault(f".{cls}", []).append(
                (file_path, line_num, line)
            )

    def _link_css_to_html(self, query: str, clean_query: str) -> List[RelationEdge]:
        """CSS selector -> HTML class/id usage."""
        edges: List[RelationEdge] = []

        if not query.startswith((".", "#")):
            return edges

        if query.startswith("."):
            class_name = clean_query
            css_defs = self._css_selectors.get(query, [])
            html_usages = self._html_classes.get(class_name, [])

            for css_file, css_line, css_raw in css_defs:
                for html_file, html_line, html_raw in html_usages:
                    if css_file == html_file:
                        continue
                    edges.append(RelationEdge(
                        from_id=f"{css_file}:{css_line}",
                        to_id=f"{html_file}:{html_line}",
                        type="html_usage",
                        direction="out",
                        confidence=1.0,
                        description=f"{query}   {html_raw.strip()[:60]}",
                        meta={"selector": query, "class": class_name},
                    ))

        elif query.startswith("#"):
            id_name = clean_query
            css_defs = self._css_selectors.get(query, [])
            html_usages = self._html_ids.get(id_name, [])

            for css_file, css_line, css_raw in css_defs:
                for html_file, html_line, html_raw in html_usages:
                    if css_file == html_file:
                        continue
                    edges.append(RelationEdge(
                        from_id=f"{css_file}:{css_line}",
                        to_id=f"{html_file}:{html_line}",
                        type="html_usage",
                        direction="out",
                        confidence=1.0,
                        description=f"{query}   {html_raw.strip()[:60]}",
                        meta={"selector": query, "id": id_name},
                    ))

        return edges

    def _link_html_to_js(self, query: str, clean_query: str) -> List[RelationEdge]:
        """HTML class/id -> JS DOM manipulation."""
        edges: List[RelationEdge] = []

        js_refs = self._js_refs.get(query, [])
        if not js_refs:
            js_refs = self._js_refs.get(f".{clean_query}", [])

        html_usages = []
        html_usages.extend(self._html_classes.get(clean_query, []))
        html_usages.extend(self._html_ids.get(clean_query, []))

        for html_file, html_line, html_raw in html_usages:
            for js_file, js_line, js_raw in js_refs:
                if html_file == js_file:
                    continue
                edges.append(RelationEdge(
                    from_id=f"{html_file}:{html_line}",
                    to_id=f"{js_file}:{js_line}",
                    type="js_manipulation",
                    direction="out",
                    confidence=0.9,
                    description=f"{query}   {js_raw.strip()[:60]}",
                    meta={"selector": query, "api": self._extract_js_api(js_raw)},
                ))

        css_defs = self._css_selectors.get(query, [])
        for css_file, css_line, css_raw in css_defs:
            for js_file, js_line, js_raw in js_refs:
                if css_file == js_file:
                    continue
                edges.append(RelationEdge(
                    from_id=f"{css_file}:{css_line}",
                    to_id=f"{js_file}:{js_line}",
                    type="js_manipulation",
                    direction="cross",
                    confidence=0.85,
                    description=f"{query}   {js_raw.strip()[:60]}",
                    meta={"selector": query, "api": self._extract_js_api(js_raw)},
                ))

        return edges

    def _link_css_child_selectors(self, query: str, clean_query: str) -> List[RelationEdge]:
        """CSS child selectors: .parent .child."""
        edges: List[RelationEdge] = []

        if not query.startswith((".", "#")):
            return edges

        parent_defs = self._css_selectors.get(query, [])
        for pfile, pline, praw in parent_defs:
            child_pattern = re.compile(
                re.escape(query) + r"\s+[>+~]?\s*([.#]?[\w-]+)",
                re.IGNORECASE,
            )
            for match in child_pattern.finditer(praw):
                child_sel = match.group(1)
                edges.append(RelationEdge(
                    from_id=f"{pfile}:{pline}",
                    to_id=f"{pfile}:{pline}",
                    type="child_selector",
                    direction="out",
                    confidence=0.95,
                    description=f"{query} -> {child_sel}   {praw.strip()[:60]}",
                    meta={"parent": query, "child": child_sel},
                ))

        for pfile, pline, praw in parent_defs:
            for match in self.CSS_CUSTOM_PROP_RE.finditer(praw):
                prop = match.group(1)
                edges.append(RelationEdge(
                    from_id=f"{pfile}:{pline}",
                    to_id=f"{pfile}:{pline}",
                    type="custom_prop",
                    direction="out",
                    confidence=0.9,
                    description=f"{query} -> {prop}   {praw.strip()[:60]}",
                    meta={"selector": query, "property": prop},
                ))

        return edges

    def _link_css_custom_props(self, query: str, clean_query: str) -> List[RelationEdge]:
        """CSS custom properties: --prop definition -> var(--prop) usage."""
        edges: List[RelationEdge] = []

        if not query.startswith("--"):
            return edges

        defs = self._css_custom_props.get(query, [])
        for dfile, dline, draw in defs:
            for prop, usages in self._css_custom_props.items():
                if prop != query:
                    continue
                for ufile, uline, uraw in usages:
                    if ufile == dfile and uline != dline:
                        if "var" in uraw and query in uraw:
                            edges.append(RelationEdge(
                                from_id=f"{dfile}:{dline}",
                                to_id=f"{ufile}:{uline}",
                                type="custom_prop",
                                direction="out",
                                confidence=0.95,
                                description=f"{query} -> var({query})   {uraw.strip()[:60]}",
                                meta={"property": query},
                            ))

        return edges

    def _extract_js_api(self, line: str) -> str:
        """Извлекает название DOM API из JS-строки."""
        apis = [
            "querySelector", "querySelectorAll", "getElementById",
            "getElementsByClassName", "classList.add", "classList.remove",
            "classList.toggle", "dataset", "addEventListener",
        ]
        line_lower = line.lower()
        for api in apis:
            if api.lower() in line_lower:
                return api
        return "dom_api"
