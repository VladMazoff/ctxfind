"""Вспомогательные утилиты для работы с текстом, нормализации и TTY."""

from __future__ import annotations

import re
import sys
from typing import List, Optional


def normalize_query(query: str) -> str:
    """Убирает CSS-префиксы (. # []) из запроса для токенизации."""
    return query.lstrip(".#[]").strip()


def extract_snippet(file_path: str, line: int, radius: int = 15) -> str:
    """Извлекает сниппет ±radius строк вокруг указанной строки.

    Returns:
        Строка сниппета или пустая строка при ошибке.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return ""

    if not (1 <= line <= len(lines)):
        return ""

    start = max(0, line - radius - 1)
    end = min(len(lines), line + radius)
    return "".join(lines[start:end])


def is_tty_stream(stream=None) -> bool:
    """Проверяет, является ли поток TTY.

    Args:
        stream: Поток для проверки (по умолчанию sys.stdout).
    """
    if stream is None:
        stream = sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def truncate_text(text: str, max_len: int = 80, suffix: str = "...") -> str:
    """Обрезает текст до max_len символов с добавлением suffix."""
    if len(text) <= max_len:
        return text
    return text[:max_len - len(suffix)] + suffix


def is_exact_token(query: str, text: str) -> bool:
    """Проверяет, что query встречается как целый токен в text."""
    clean = normalize_query(query)
    if not clean:
        return False
    pattern = re.compile(
        r"(?:^|[^\w-])" + re.escape(clean) + r"(?:[^\w-]|$)",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))
