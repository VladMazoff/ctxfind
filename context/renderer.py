from __future__ import annotations

"""Рендеринг чанков: JSON / Tree / Plain.

Контракты:
  - Renderer.render(chunks, format, use_color, is_tty) -> str.
  - Форматы: 'json', 'tree', 'plain'.
  - TTY-детекция: цвета только в TTY.
  - Пайпы: JSON по умолчанию.
"""

import json
import os
import sys
import platform
from typing import Dict, List, Optional

from config import JSON_INDENT, TREE_INDENT_STR
from core.query import Chunk


def enable_windows_ansi() -> bool:
    """Включает поддержку ANSI-цветов в Windows консоли.
    
    Returns:
        True если поддержка включена, False если не поддерживается.
    """
    if platform.system() != "Windows":
        return False
    
    try:
        import ctypes
        
        # Для Windows 10+ можно включить виртуальный терминал
        if sys.version_info >= (3, 8):
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                mode.value |= 0x0004
                if kernel32.SetConsoleMode(handle, mode):
                    return True
        
        # Для старых Windows (7/8) используем альтернативный метод
        # через colorama или ANSICON
        try:
            import colorama
            colorama.init()
            return True
        except ImportError:
            # Пробуем установить через system command
            os.system('color 07')  # Сбрасываем цвета консоли
            return False
            
    except (ImportError, AttributeError, OSError):
        return False


def is_tty() -> bool:
    """Проверяет, является ли stdout TTY и поддерживает ли цвета."""
    if not sys.stdout.isatty():
        return False
    
    # На Windows проверяем поддержку ANSI
    if platform.system() == "Windows":
        # Проверяем, можем ли мы использовать цвета
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            # Проверяем бит виртуального терминала
            return bool(mode.value & 0x0004)
        except:
            # Если не можем проверить, предполагаем что нет
            return False
    
    return True


class Renderer:
    """Рендерер чанков в различные форматы.

    Контракт:
        render(chunks, format, use_color) -> str

    Args:
        format: 'json', 'tree', 'plain'.
        use_color: включить ANSI-цвета (None = авто по TTY).
    """

    # ANSI-цвета
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
    
    # Windows альтернативы цветов (если ANSI не поддерживается)
    _WIN_COLORS = {
        "reset": "",
        "bold": "",
        "dim": "",
        "red": "",
        "green": "",
        "yellow": "",
        "blue": "",
        "magenta": "",
        "cyan": "",
        "white": "",
    }

    def __init__(
        self,
        format: str = "tree",
        use_color: Optional[bool] = None,
    ):
        self.format = format
        
        # Пытаемся включить ANSI на Windows если нужно
        self._ansi_enabled = False
        if platform.system() == "Windows":
            self._ansi_enabled = enable_windows_ansi()
        
        if use_color is None:
            self.use_color = is_tty() and self._ansi_enabled
        else:
            self.use_color = use_color and (self._ansi_enabled or platform.system() != "Windows")

    def render(self, chunks: List[Chunk]) -> str:
        """Рендерит чанки в выбранный формат."""
        if self.format == "json":
            return self._render_json(chunks)
        elif self.format == "tree":
            return self._render_tree(chunks)
        elif self.format == "plain":
            return self._render_plain(chunks)
        else:
            return self._render_tree(chunks)

    def _color(self, name: str, text: str) -> str:
        """Окрашивает текст если use_color=True."""
        if not self.use_color:
            return text
        
        # На Windows без ANSI используем простые маркеры
        if platform.system() == "Windows" and not self._ansi_enabled:
            # Возвращаем текст без цветов, но с маркерами для консоли
            markers = {
                "bold": f"*{text}*",
                "dim": f"({text})",
                "red": f"[!]{text}[/!]",
                "green": f"[+]{text}[/+]",
                "yellow": f"[?]{text}[/?]",
            }
            return markers.get(name, text)
        
        # Используем ANSI коды
        if self._ansi_enabled and name in self._COLORS:
            return f"{self._COLORS[name]}{text}{self._COLORS['reset']}"
        
        return text

    def _render_json(self, chunks: List[Chunk]) -> str:
        """Рендерит в JSON."""
        data = {
            "version": "0.1",
            "count": len(chunks),
            "chunks": [chunk.to_dict() for chunk in chunks],
        }
        return json.dumps(data, indent=JSON_INDENT, ensure_ascii=False)

    def _render_tree(self, chunks: List[Chunk]) -> str:
        """Рендерит в древовидный формат."""
        lines: List[str] = []

        for i, chunk in enumerate(chunks, 1):
            # Заголовок чанка
            file_name = os.path.basename(chunk.match.file_path)
            lines.append(self._color("bold", f"[{i}] {chunk.match.type}: {chunk.match.name}"))
            lines.append(self._color("dim", f"    📁 {file_name}:{chunk.match.line}:{chunk.match.col}"))

            # Context tree
            if chunk.context_tree:
                lines.append(self._color("cyan", "    Context:"))
                for j, node in enumerate(chunk.context_tree):
                    indent = TREE_INDENT_STR * j
                    icon = "📄" if j == 0 else "└─"
                    lines.append(f"        {indent}{icon} [{node.type}] {node.name}")

            # Relations
            if chunk.relations:
                lines.append(self._color("yellow", "    Relations:"))
                for edge in chunk.relations[:5]:  # max 5 relations
                    direction = "→" if edge.source == chunk.match.id else "←"
                    lines.append(f"        {direction} [{edge.type}] (conf={edge.confidence:.1f})")

            # Snippet
            if chunk.snippet:
                lines.append(self._color("green", "    Snippet:"))
                snippet_lines = chunk.snippet.splitlines()
                for sline in snippet_lines[:15]:  # max 15 lines
                    lines.append(f"        │ {sline}")
                if len(snippet_lines) > 15:
                    lines.append(f"        │ ... ({len(snippet_lines) - 15} more lines)")

            # Meta
            if chunk.meta.get("score"):
                lines.append(self._color("dim", f"    Score: {chunk.meta['score']:.3f}"))
            if chunk.meta.get("mode"):
                lines.append(self._color("dim", f"    Mode: {chunk.meta['mode']}"))

            lines.append("")

        return "\n".join(lines)

    def _render_plain(self, chunks: List[Chunk]) -> str:
        """Рендерит в plain text (без цветов, минималистично)."""
        lines: List[str] = []

        for chunk in chunks:
            file_name = os.path.basename(chunk.match.file_path)
            lines.append(f"{chunk.match.type} {chunk.match.name}")
            lines.append(f"  {file_name}:{chunk.match.line}:{chunk.match.col}")
            if chunk.snippet:
                for sline in chunk.snippet.splitlines()[:10]:
                    lines.append(f"  | {sline}")
            lines.append("")

        return "\n".join(lines)