# Шаг 3: projects.yaml

## 3. `projects.yaml`

```powershell
copy projects.example.yaml projects.yaml
```

Заполнить по черновику шага 2 (формат и поля — комментарии в `projects.example.yaml`, таблица — `docs/INSTALL.md`,
шаг 3). Пути выгрузок — **от папки проекта**. Базы: строка соединения `Srvr="…";Ref="…";` или `File="…";`, роль —
как сказал человек; `dev_env: .dev.env` (путь от папки проекта), если логин базы в нём. `code_mcp` — все
серверы кода и метаданных проекта из его `.mcp.json`, граф метаданных (`1c-graph-metadata-mcp`) тоже;
`data_mcp` — только у песочницы, которую человек подтвердил на шаге 2, иначе строки нет.

Проверка для клона:

```powershell
uv run python -c "from pathlib import Path; from kd_rules_mcp.projects import load_catalog; print(list(load_catalog(Path('projects.yaml')).projects))"
```

Для образа (версия выбранного релиза вместо `X.Y.Z`, Python работает внутри контейнера):

```powershell
docker run --rm -v "${PWD}:/work" ghcr.io/egordoronchenko/kd-rules-mcp:X.Y.Z python -c "from pathlib import Path; from kd_rules_mcp.projects import load_catalog; print(list(load_catalog(Path('/work/projects.yaml')).projects))"
```

Проверка прошла — **закончить ход**: показать человеку `projects.yaml` целиком и спросить, всё ли верно. Ответы на
вопросы шага 2 — не согласие с файлом; шаг 4 — только после его «да» (правки по ответу — снова показать файл).
