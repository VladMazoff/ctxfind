from __future__ import annotations

"""CLI: argparse, TTY-детекция, пайпы.

Контракты:
  - main(argv) -> парсинг аргументов -> индексация -> поиск -> рендеринг.
  - Новые флаги V01: --min-score, --focus, --relations-only, --auto-expand,
    --aggressive, --smart, --compact, --full, --find-gaps, --group-hints.
  - Пайпы: JSON по умолчанию.
  - Код возврата: 0 (найдено), 1 (не найдено/ниже порога), 2 (ошибка).
"""

import argparse
import os
import sys
from typing import List, Optional
import io
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


from config import (
    DEFAULT_FALLBACK_TOP_K,
    DEFAULT_MAX_DEPTH,
    DEFAULT_SNIPPET_LINES,
    SCORING_RULES,
)
from context.chunk import ContextAssembler
from core.indexer import ProjectIndexer
from core.query import QueryEngine
from core.scanner import Scanner


def is_tty() -> bool:
    """Проверяет, что stdout -- TTY."""
    return sys.stdout.isatty()






def create_parser() -> argparse.ArgumentParser:
    """Создает парсер аргументов CLI."""
    parser = argparse.ArgumentParser(
        prog="ctxfind",
        description="Контекстно-зависимый поиск по кодовой базе (V01)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  ctxfind "User" ./src
  ctxfind "process_data" --mode graph --depth 2
  ctxfind "config" --format json | jq '.nodes[].match.score'
  ctxfind "class" --lang python --limit 5
  ctxfind ".tiles" ./pr-diff/ --smart --output json
  ctxfind "foo" --aggressive --find-gaps
        """,
    )

    parser.add_argument(
        "query",
        help="Поисковый запрос (имя символа, паттерн, CSS-селектор)",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Пути для сканирования (по умолчанию: текущая директория)",
    )

    parser.add_argument(
        "-m", "--mode",
        choices=["graph", "vector", "auto"],
        default="auto",
        help="Режим поиска (по умолчанию: auto)",
    )
    parser.add_argument(
        "-d", "--depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"Глубина обхода графа (по умолчанию: {DEFAULT_MAX_DEPTH})",
    )

    parser.add_argument(
        "-f", "--format",
        choices=["json", "tree", "plain"],
        default=None,
        help="Формат вывода (по умолчанию: tree в TTY, json в пайпе)",
    )
    parser.add_argument(
        "--output",
        dest="output_format",
        choices=["json", "tree", "plain"],
        default=None,
        help="Алиас для --format",
    )

    parser.add_argument(
        "-l", "--lang",
        action="append",
        help="Фильтр по языку (можно указать несколько раз)",
    )
    parser.add_argument(
        "--focus",
        dest="focus_langs",
        action="append",
        help="Сканировать только указанные языки (V01, алиас для --lang)",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Макс. количество результатов (по умолчанию: 10)",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=DEFAULT_SNIPPET_LINES,
        help=f"Строк в сниппете (по умолчанию: {DEFAULT_SNIPPET_LINES})",
    )
    parser.add_argument(
        "--size-limit",
        type=int,
        default=5,
        help="Лимит размера файла в МБ (по умолчанию: 5)",
    )

    parser.add_argument(
        "--min-score",
        type=int,
        default=SCORING_RULES["threshold_default"],
        help=f"Минимальный порог релевантности (по умолчанию: {SCORING_RULES['threshold_default']})",
    )

    parser.add_argument(
        "--smart",
        action="store_true",
        help="Макро: --min-score 30 --auto-expand --focus css,js,html",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="Игнорировать лимиты, собрать максимум контекста",
    )

    parser.add_argument(
        "--auto-expand",
        action="store_true",
        help="Генерировать производные маркеры (1 уровень, макс 3)",
    )
    parser.add_argument(
        "--relations-only",
        action="store_true",
        help="Показать только связи, скрыть сырые матчи",
    )
    parser.add_argument(
        "--find-gaps",
        action="store_true",
        help="Подсвечивать разрывы логики",
    )
    parser.add_argument(
        "--group-hints",
        action="store_true",
        default=None,
        help="Группировать relations по семантическим маркерам (по умолчанию: true в TTY)",
    )

    parser.add_argument(
        "--compact",
        action="store_true",
        help="Краткий вывод: только топ-3 матча, без дерева",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Максимум деталей: все дерево, все связи, мета-инфо",
    )

    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Отключить цвета",
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Игнорировать .gitignore",
    )

    return parser


def resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    """Разрешает приоритет флагов: --aggressive > --smart > индивидуальные."""

    if args.aggressive:
        args.min_score = SCORING_RULES["threshold_aggressive"]
        args.auto_expand = True
        args.find_gaps = True
        args.limit = max(args.limit, 50)
    elif args.smart:
        args.min_score = 30
        args.auto_expand = True
        if not args.lang:
            args.lang = ["css", "javascript", "html"]

    if args.focus_langs:
        if not args.lang:
            args.lang = []
        for fl in args.focus_langs:
            args.lang.extend(fl.split(","))

    if args.compact and args.full:
        args.compact = False

    if args.output_format:
        args.format = args.output_format
    if args.format is None:
        args.format = "tree" if is_tty() else "json"

    if args.group_hints is None:
        args.group_hints = is_tty()

    return args


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа CLI.

    Returns:
        0 -- найдены результаты
        1 -- ничего не найдено или ниже порога
        2 -- ошибка
    """
    parser = create_parser()
    args = parser.parse_args(argv)
    args = resolve_args(args)

    use_color = not args.no_color

    try:
        scanner = Scanner(
            lang_filter=args.lang,
            respect_gitignore=not args.no_gitignore,
            size_limit_mb=args.size_limit,
        )
        files = scanner.scan(args.paths)

        if not files:
            print("Нет файлов для индексации.", file=sys.stderr)
            return 1

        indexer = ProjectIndexer(use_threads=False)
        graph = indexer.scan(
            args.paths,
            lang_filter=args.lang,
            respect_gitignore=not args.no_gitignore,
            size_limit_mb=args.size_limit,
        )
 

        engine = QueryEngine(graph)
        engine.build_fallback(files)


        tree = engine.execute(
            query=args.query,
            mode=args.mode,
            depth=args.depth,
            snippet_lines=args.lines,
            lang_filter=args.lang,
            limit=args.limit,
            min_score=args.min_score,
            auto_expand=args.auto_expand,
            find_gaps=args.find_gaps,
            aggressive=args.aggressive,
        )

        if not tree.nodes:
            print(f"По запросу '{args.query}' ничего не найдено.", file=sys.stderr)
            return 1

         
        if args.relations_only:
            print(11)
            output = render_relations_only(tree, args.format, use_color)
        elif args.compact:
            print(22)
            output = render_compact(tree, args.format, use_color)
        elif args.full:
            print(33)
            output = render_full(tree, args.format, use_color, args.group_hints)
        else:
             
            output = render_standard(tree, args.format, use_color, args.group_hints)

            print(99)

        print(output)
        return 0

    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2


def render_relations_only(tree, fmt: str, use_color: bool) -> str:
    """Рендерит только связи."""
    all_edges = []
    for node in tree.nodes:
        all_edges.extend(node.cross_refs)
    all_edges.extend(tree.gap_edges)

    if fmt == "json":
        import json
        return json.dumps(
            {"relations": [e.to_dict() for e in all_edges]},
            indent=2,
            ensure_ascii=False,
        )

    lines = ["Relations:"]
    for e in all_edges:
        lines.append(f"  [{e.type}] {e.from_id} -> {e.to_id or 'empty'}  (conf={e.confidence:.2f})")
        lines.append(f"    {e.description}")
    return "\n".join(lines)


def render_compact(tree, fmt: str, use_color: bool) -> str:
    """Краткий вывод: топ-3 матча."""
    top3 = tree.nodes[:3]

    if fmt == "json":
        import json
        return json.dumps(
            {"top_matches": [n.match.to_dict() for n in top3]},
            indent=2,
            ensure_ascii=False,
        )

    lines = [f"Query: {tree.meta.get('query', '?')}", ""]
    for i, node in enumerate(top3, 1):
        m = node.match
        lines.append(f"{i}. [{m.score}] {m.file_path}:{m.line}  {m.text[:50]}")
    return "\n".join(lines)


def render_full(tree, fmt: str, use_color: bool, group_hints: bool) -> str:
    """Полный вывод со всеми деталями."""
    if fmt == "json":
        import json
        return json.dumps(tree.to_dict(), indent=2, ensure_ascii=False)

    return tree.to_tree_text(use_color=use_color)


def render_standard(tree, fmt: str, use_color: bool, group_hints: bool) -> str:
    """Стандартный вывод."""
    if fmt == "json":
        import json
         
        return json.dumps(tree.to_dict(), indent=2, ensure_ascii=False)

    return tree.to_tree_text(use_color=use_color)


if __name__ == "__main__":
    sys.exit(main())
