## Баги (Bugs)

### RecursionError при запуске из notepad++
Описание: Ошибка "maximum recursion depth exceeded while calling a Python object"
решил обновлением метода  def _render_json(self, tree: ContextTree) -> str:
        """Рендерит в JSON (упрощенная версия без циклических ссылок)."""
Проверить как это повлияет на полноценность вывода дерева.
