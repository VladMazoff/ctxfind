# ctxfind

**Контекстно-зависимый поиск по кодовой базе**

Быстрый поиск символов, классов и паттернов с анализом связей между файлами.

## Установка

```bash
git clone https://github.com/yourname/ctxfind
cd ctxfind
pip install -e .
```

## Использование

```bash
# Быстрый поиск (компактный вывод)
ctxfind ".tiles" ./project

# Древо связей
ctxfind ".tiles" ./project --format tree

# JSON для LLM/автоматизации
ctxfind ".tiles" ./project --format json
```

## Форматы вывода

| Формат | Описание |
|--------|----------|
| `compact` (default) | Группировка по языкам, сниппеты 60-80 символов |
| `tree` | ASCII-дерево с направленными связями |
| `json` | Машиночитаемый формат с узлами и рёбрами |

## Примеры

```bash
ctxfind "User" ./src --limit 5
ctxfind "process_data" --depth 2
ctxfind "class" --lang python
```

## Маркеры связей

- `<< def` — определение
- `>< mod` — модификатор
- `>> use` — использование

## CLI-флаги

| Флаг | Описание |
|------|----------|
| `-m, --mode` | `graph`, `vector`, `auto` |
| `-d, --depth` | Глубина обхода графа |
| `-f, --format` | `json`, `tree`, `plain`, `compact` |
| `-l, --lang` | Фильтр по языку |
| `--limit` | Макс. результатов (default: 10) |
| `--min-score` | Порог релевантности (default: 60) |
| `--no-color` | Отключить цвета |

## Лицензия

MIT