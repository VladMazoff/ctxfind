from __future__ import annotations

"""Конфигурация, константы, дефолтные лимиты и базовые исключения для ctxfind.
Все runtime-настройки централизованы здесь.
"""

import sys
from typing import Set, Dict, List

# ─────────────
# Runtime requirements
# ────────────
MIN_PYTHON_VERSION = (3, 8)


def check_python_version() -> None:
    """Проверяет минимальную версию Python."""
    if sys.version_info < MIN_PYTHON_VERSION:
        raise RuntimeError(
            f"ctxfind требует Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}+, "
            f"текущая версия: {sys.version_info.major}.{sys.version_info.minor}"
        )


# ────────────────────
# Расширения языков → парсер
# ────────────────────
LANG_EXTENSIONS: Dict[str, List[str]] = {
    "python": [".py", ".pyw"],
    "javascript": [".js", ".mjs", ".cjs", ".jsx"],
    "html": [".html", ".htm", ".xhtml"],
    "css": [".css", ".scss", ".sass", ".less"],
}

# Обратный индекс: расширение → язык
EXT_TO_LANG: Dict[str, str] = {}
for lang, exts in LANG_EXTENSIONS.items():
    for ext in exts:
        EXT_TO_LANG[ext] = lang

ALL_EXTENSIONS: Set[str] = set(EXT_TO_LANG.keys())

# ────────────────────
# Scoring Rules V01
# ────────────────────
MAX_SCORE = 100  # Жёсткий потолок для аддитивного скоринга

SCORING_RULES = {
    "exact_match": 45,           # Маркер встречается как токен
    "css_selector": 40,          # .tiles, #tiles в CSS
    "html_class_attr": 35,       # class="... tiles ..."
    "js_dom_api": 32,            # querySelector, classList, dataset
    "css_child_selector": 25,    # .tiles .tile, .tiles > *
    "proximity_bonus": 15,       # ±15 строк от другого высоко-скоренного матча
    "fallback_tfidf": 12,        # Базовый вес для векторного режима
    "context_block_bonus": 10,   # Внутри /* ===== Тайлы ===== */ или // ===== State =====
    "semantic_keyword_bonus": 8, # Рядом: tile, cards, grid, data-columns, masonry
    "threshold_default": 25,     # --min-score по умолчанию
    "threshold_aggressive": 0,   # --aggressive отключает фильтр
}

GAP_BLACKLIST = {"i", "j", "k", "x", "y", "tmp", "res", "data", "val", "_", "n"}

AUTO_EXPAND_RULES: Dict[str, List[str]] = {
    ".tiles": [".tile", ".tiles *", "[data-columns]"],
}

# ────────────────────
# Дефолтные лимиты
# ────────────────────
DEFAULT_MAX_DEPTH = 3           # глубина обхода графа
DEFAULT_SNIPPET_LINES = 15       # ±N строк вокруг совпадения
DEFAULT_FALLBACK_TOP_K = 10     # топ-N чанков в fallback
DEFAULT_FILE_SIZE_LIMIT_MB = 5  # пропускать файлы больше N МБ
DEFAULT_SCAN_LIMIT = None       # макс. количество файлов (None = без лимита)

# ────────────────────
# Сканер
# ────────────────────
DEFAULT_IGNORE_PATTERNS: List[str] = [
    # VCS
    ".git", ".svn", ".hg",
    # Python
    "__pycache__", ".venv", "venv", ".env", "env",
    "*.egg-info", "dist", "build",
    # Node
    "node_modules", ".npm", ".yarn",
    # IDE
    ".idea", ".vscode", ".vs",
    # OS
    ".DS_Store", "Thumbs.db",
    # Тестовые артефакты
    ".pytest_cache", ".mypy_cache", ".tox", ".coverage", "htmlcov",
    # Прочее
    ".gitignore",  # сам файл игнорируем как исходник
]

# Минимальная длина символа для кросс-языкового моста
MIN_SYMBOL_LENGTH_FOR_BRIDGE = 3

# ────────────────────
# Fallback / TF-IDF
# ────────────────────
TFIDF_MIN_DF = 1                # минимальная document frequency
TFIDF_MAX_DF_RATIO = 0.95       # максимальная DF как доля от корпуса
TFIDF_TOKEN_PATTERN = r"[a-zA-Z_][a-zA-Z0-9_]*"  # идентификаторы

# ────────────────────
# Вывод
# ────────────────────
JSON_INDENT = 2
TREE_INDENT_STR = "    "

# ────────────────────
# Базовые исключения
# ────────────────────


class CtxfindError(Exception):
    """Базовое исключение всего проекта."""
    pass


class ConfigError(CtxfindError):
    """Ошибка конфигурации или валидации аргументов."""
    pass


class ParseError(CtxfindError):
    """Ошибка парсинга конкретного файла."""
    pass


class GraphError(CtxfindError):
    """Ошибка внутри InMemoryGraph (дублирование ID, несуществующий узел и т.д.)."""
    pass


class QueryError(CtxfindError):
    """Ошибка выполнения поискового запроса."""
    pass


class FallbackError(CtxfindError):
    """Ошибка в TF-IDF fallback (например, пустой корпус)."""
    pass


class ScorerError(CtxfindError):
    """Ошибка в скоринг-движке."""
    pass


class LinkerError(CtxfindError):
    """Ошибка в linker (построение кросс-языковых связей)."""
    pass


class RendererError(CtxfindError):
    """Ошибка форматирования вывода."""
    pass
