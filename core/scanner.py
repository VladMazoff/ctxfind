from __future__ import annotations

"""Обход ФС, фильтрация, подготовка путей.

Контракты:
  - scan(paths, lang_filter, respect_gitignore, skip_patterns) -> List[FileEntry]
  - Фильтрация по расширениям, размеру, gitignore, пользовательским паттернам.
  - Простой gitignore-parser без внешних зависимостей (чистый Python).
"""

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from config import (
    ALL_EXTENSIONS,
    DEFAULT_FILE_SIZE_LIMIT_MB,
    DEFAULT_IGNORE_PATTERNS,
    DEFAULT_SCAN_LIMIT,
    EXT_TO_LANG,
)


@dataclass(frozen=True)
class FileEntry:
    """Запись о файле для индексации."""
    path: str
    rel_path: str
    lang: str
    size: int


class SimpleGitignoreParser:
    """Простой парсер .gitignore без внешних зависимостей.

    Поддерживает:
      - Комментарии (#)
      - Пустые строки
      - Паттерны с * и **
      - Паттерны с / (от корня)
      - Отрицание (!)
      - Директории (заканчиваются на /) — игнорируют всё содержимое
    """

    def __init__(self, patterns: List[str]):
        self.patterns: List[Tuple[str, bool, bool, bool, bool]] = []
        # (regex, is_negation, is_dir_only, anchored, is_dir_pattern)
        for line in patterns:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue

            negation = line.startswith("!")
            if negation:
                line = line[1:]

            dir_only = line.endswith("/")
            if dir_only:
                line = line[:-1]

            anchored = line.startswith("/")
            if anchored:
                line = line[1:]

            regex = self._pattern_to_regex(line, anchored, dir_only)
            self.patterns.append((regex, negation, dir_only, anchored, dir_only))

    def _pattern_to_regex(self, pattern: str, anchored: bool, is_dir: bool) -> str:
        """Конвертирует gitignore-паттерн в regex."""
        # Экранируем спецсимволы regex
        pattern = pattern.replace(".", r"\.")
        pattern = pattern.replace("+", r"\+")
        pattern = pattern.replace("(", r"\(")
        pattern = pattern.replace(")", r"\)")
        pattern = pattern.replace("$", r"\$")
        pattern = pattern.replace("^", r"\^")
        pattern = pattern.replace("{", r"\{")
        pattern = pattern.replace("}", r"\}")

        # ** — любое количество директорий
        pattern = pattern.replace("/**", r"(?:/.*)?")
        pattern = pattern.replace("**", r".*")

        # * — любые символы кроме /
        pattern = pattern.replace("*", r"[^/]*")

        # ? — один символ кроме /
        pattern = pattern.replace("?", r"[^/]")

        if anchored:
            base = r"^" + pattern
        else:
            base = r"(^|/)" + pattern

        # Если это директория — матчим и всё содержимое (через /)
        if is_dir:
            return base + r"(?:/.*)?$"
        else:
            return base + r"$"

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        """Проверяет, игнорируется ли путь."""
        rel_path = rel_path.replace("\\", "/")
        if rel_path.startswith("./"):
            rel_path = rel_path[2:]

        ignored = False
        for regex, negation, dir_only, anchored, is_dir_pattern in self.patterns:
            if dir_only and not is_dir:
                # Для файлов: проверяем, не находится ли файл внутри игнорируемой директории
                pass  # regex уже учитывает это через (?:/.*)?

            if re.search(regex, rel_path):
                if negation:
                    ignored = False
                else:
                    ignored = True

        return ignored


class Scanner:
    """Сканер файловой системы."""

    def __init__(
        self,
        lang_filter: Optional[List[str]] = None,
        respect_gitignore: bool = True,
        skip_patterns: Optional[List[str]] = None,
        size_limit_mb: int = DEFAULT_FILE_SIZE_LIMIT_MB,
        file_limit: Optional[int] = DEFAULT_SCAN_LIMIT,
    ):
        self.lang_filter = set(lang_filter) if lang_filter else None
        self.respect_gitignore = respect_gitignore
        self.skip_patterns = list(skip_patterns or [])
        self.size_limit_bytes = size_limit_mb * 1024 * 1024
        self.file_limit = file_limit
        self._builtin_patterns = list(DEFAULT_IGNORE_PATTERNS)
        self._builtin_patterns.extend(self.skip_patterns)

    def scan(self, paths: List[str]) -> List[FileEntry]:
        """Сканирует пути и возвращает отфильтрованный список файлов."""
        results: List[FileEntry] = []
        seen: Set[str] = set()

        for path in paths:
            abs_path = os.path.abspath(path)
            if not os.path.exists(abs_path):
                continue

            if os.path.isfile(abs_path):
                entry = self._process_file(abs_path, os.path.basename(abs_path))
                if entry and entry.path not in seen:
                    seen.add(entry.path)
                    results.append(entry)
            elif os.path.isdir(abs_path):
                self._scan_dir(abs_path, results, seen)

            if self.file_limit and len(results) >= self.file_limit:
                results = results[:self.file_limit]
                break

        return sorted(results, key=lambda e: e.path)

    def _scan_dir(
        self,
        root: str,
        results: List[FileEntry],
        seen: Set[str],
    ) -> None:
        """Рекурсивный обход директории."""
        # Загружаем .gitignore текущей директории
        gitignore = None
        if self.respect_gitignore:
            gi_path = os.path.join(root, ".gitignore")
            if os.path.exists(gi_path):
                gitignore = self._load_gitignore(gi_path)

        for dirpath, dirnames, filenames in os.walk(root):
            # Фильтруем поддиректории
            dirnames[:] = [
                d for d in dirnames
                if not self._should_skip_dir(d, dirpath, gitignore, root)
            ]

            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(file_path, root)

                if self._should_skip_file(
                    filename, rel_path, file_path, gitignore, root
                ):
                    continue

                entry = self._process_file(file_path, rel_path)
                if entry and entry.path not in seen:
                    seen.add(entry.path)
                    results.append(entry)

                    if self.file_limit and len(results) >= self.file_limit:
                        return

    def _process_file(self, file_path: str, rel_path: str) -> Optional[FileEntry]:
        """Создаёт FileEntry если файл проходит фильтры."""
        ext = os.path.splitext(file_path)[1].lower()
        lang = EXT_TO_LANG.get(ext)

        if not lang:
            return None

        if self.lang_filter and lang not in self.lang_filter:
            return None

        try:
            size = os.path.getsize(file_path)
        except OSError:
            return None

        if size > self.size_limit_bytes:
            return None

        return FileEntry(
            path=file_path,
            rel_path=rel_path,
            lang=lang,
            size=size,
        )

    def _should_skip_dir(
        self,
        dirname: str,
        dirpath: str,
        gitignore: Optional[SimpleGitignoreParser],
        root: str,
    ) -> bool:
        """Проверяет, нужно ли пропустить директорию."""
        for pattern in self._builtin_patterns:
            if fnmatch.fnmatch(dirname, pattern):
                return True
            if fnmatch.fnmatch(os.path.join(dirpath, dirname), pattern):
                return True

        if gitignore:
            rel_dir = os.path.relpath(os.path.join(dirpath, dirname), root)
            if gitignore.is_ignored(rel_dir, is_dir=True):
                return True

        return False

    def _should_skip_file(
        self,
        filename: str,
        rel_path: str,
        file_path: str,
        gitignore: Optional[SimpleGitignoreParser],
        root: str,
    ) -> bool:
        """Проверяет, нужно ли пропустить файл."""
        for pattern in self._builtin_patterns:
            if fnmatch.fnmatch(filename, pattern):
                return True
            if fnmatch.fnmatch(rel_path, pattern):
                return True
            if fnmatch.fnmatch(file_path, pattern):
                return True

        if gitignore:
            if gitignore.is_ignored(rel_path, is_dir=False):
                return True

        return False

    def _load_gitignore(self, gitignore_path: str) -> Optional[SimpleGitignoreParser]:
        """Загружает .gitignore."""
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return SimpleGitignoreParser(lines)
        except Exception:
            return None