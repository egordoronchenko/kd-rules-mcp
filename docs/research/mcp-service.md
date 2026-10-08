# Сервер MCP: инструменты и развёртывание

Дата: 27.09.2026. Этап 7: инструменты, Docker, подключение.

## 7.1. Поверхность инструментов

Код: `src/kd_rules_mcp/service/` (логика инструментов, `PathMap`, `Settings`), `src/kd_rules_mcp/server.py`
(регистрация в `MCPServer` из SDK `mcp` 2.2, коды ошибок, запуск), точка входа `kd-rules-mcp`. Тесты:
`tests/test_server.py` — клиент MCP в том же процессе вызывает каждый из 31 инструмента (на 27.09.2026 их было 27;
`project_list`, `structure_load_project`, `rules_pack` и `rules_close` добавлены позже). Полный справочник с параметрами, ответами
и кодами ошибок — [`docs/tools.md`](../tools.md), генерируется из сервера (`scripts/dump_tools.py`).

| Группа | Инструменты |
|---|---|
| Проекты и структуры | `project_list`, `structure_load_project`, `structure_list`, `structure_load_xml`, `structure_load_md83exp`, `structure_objects`, `structure_object`, `structure_values`, `structure_plan_content`, `structure_compare` |
| Кандидаты | `match_objects`, `match_properties`, `match_values` |
| Проекты правил | `rules_open`, `rules_create`, `rules_projects`, `rules_overview`, `rules_list`, `rules_get`, `rules_save`, `rules_close`, `rules_pack` |
| Правки | `rule_create`, `rule_update`, `rule_delete`, `pko_create_from_candidates` |
| Проверки | `rules_validate`, `handlers_export`, `handlers_locate` |
| Генерация | `registration_build`, `correspondent_draft` |

- У каждого параметра — описание в схеме (тест проверяет); у сервера — инструкция с порядком работы.
- `rules_pack` — ZIP для загрузки в БСП из папки комплекта (стандартные имена) или из файлов: два файла —
  форма «Правила конвертации объектов» (`ЗагрузитьПравила`, ветка `ЭтоАрхив`), три с регистрацией —
  «Загрузить правила синхронизации» (`ЗагрузитьКомплектПравил`); файлы байт в байт, без каталогов (БСП считает
  всё, что найдёт в распакованном архиве). Каждый файл проверяется по виду, `warnings` — корреспондент не
  зеркален или регистрация для другой конфигурации. Запись — как у `rules_save`. Проверка архива в песочнице —
  `bsp_check.py --archive`.
- Компактность: списки постраничные (`offset`, `limit` ≤ 200, `has_more`); `rules_save` возвращает путь, размер,
  число правил по разделам и итог проверки формата, но не XML; тексты обработчиков в `rules_get` обрезаются до
  2000 символов (полный код — через `handlers_export`).
- Ошибки: `ToolError` с JSON `{"code", "message", …}`. Коды: `structure_not_found` (+ `structures` — загруженные),
  `object_not_found` (+ `suggestions`), `project_not_found`, `path_outside_workspace` (+ `workspace`, `writable`),
  `unknown_field`, `duplicate_rule`, `rule_not_found`, `dangling_reference`, `edit_rejected`, `rules_format`,
  `structure_format`, `project_config`, `rejected`, `invalid_argument`. SDK добавляет перед текстом «Error executing tool <имя>: ».
- Пути: агент передаёт пути своей машины; `KD2_PATH_MAP` (`путь_агента=локальный;…`, длинный префикс первым,
  регистр не важен) переводит их в пути контейнера и обратно в ответах. Запись — только в рабочую папку
  (`KD2_WORKSPACE`) и папки живых правил проектов (`KD2_RULES_DIRS`: `проект=путь;…`; без переменной —
  `rules_dir` проектов от их папок): другой путь внутри исходников проекта отклоняется с кодом
  `path_outside_workspace` (+ `writable` — куда можно, путями агента). Проверка — после `resolve` (`..`,
  ссылки), относительный путь — от рабочей папки.
- Вызовы идут в рабочем потоке (`anyio.to_thread`): загрузка большой структуры не останавливает сервер; правки
  проектов сериализованы блокировкой.
- Настройки: `KD2_HOST` (по умолчанию 127.0.0.1), `KD2_PORT` (8060), `KD2_CACHE_DIR` (`cache`), `KD2_WORKSPACE`
  (`workspace`), `KD2_PATH_MAP`, `KD2_RULES_DIRS`, `KD2_LOG_LEVEL` (с 01.10.2026: уровень лога, по умолчанию
  `INFO`; строка на вызов инструмента, трассировки непредвиденных исключений).

Проверено также настоящим HTTP: `kd-rules-mcp` на порту 8061, клиент по URL получил 27 инструментов и ответ
`structure_list`.

## 7.2. Docker

Файлы: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, тест `tests/test_deploy.py` (маркер `deploy`, запускается
с `KD2_DEPLOY_URL`).

- Образ `python:3.12-slim` в две стадии, зависимости строго по `uv.lock`, 255 МБ; сервер — `kd-rules-mcp` от
  пользователя uid 1000 (точка входа от root только отдаёт ему тома и переключается через `setpriv`).
- `docker-compose.yml`: контейнер `kd_rules_mcp`, порт 8060, `restart: unless-stopped`; `projects.yaml` и
  `structures\` → только на чтение; кэш структур — именованный том `kd2_structures_cache`; рабочая папка —
  `workspace\` репозитория → `/data/workspace` (сохранённые правила видны на машине). Папки проектов
  (`/projects/<проект>`, только чтение) и `KD2_PATH_MAP` (перевод путей агента) — в `docker-compose.override.yml`,
  его пишет `scripts/setup_local.py`.

Проверка (из git worktree и повторно из основного репозитория): `docker compose up -d --build` → тест
развёртывания (структура загружена, правила сохранены в `workspace\`, запись в `tests\data\` отклонена с
`path_outside_workspace`) → `docker compose up -d --force-recreate` → та же структура `reused: true` без разбора.
В контейнере: `running`, `restart=unless-stopped`, `/projects` и `/structures` — `ro`.

**Находка: медленная файловая система Docker Desktop.** Загрузка ЗУП КОРП (`main` + 3 расширения) через контейнер —
570 с, из них сборка — 51 с (локально 13 с). Остальное — отпечаток выгрузки `dump_fingerprint`: обход и `stat`
41 тыс. файлов `main` через монтирование Windows-каталога занимает 336 с только на листинг, 401 с целиком. Отпечаток
считается при каждом `structure_load_xml`, в том числе когда структура не изменилась. Запросы к уже загруженной
структуре (`structure_*`, `match_*`, проверки) отпечаток не считают и отвечают сразу.

Решение (вариант A): выгрузка с `ConfigDumpInfo.xml` отпечатывается по содержимому
`Configuration.xml` и `ConfigDumpInfo.xml` — Конфигуратор переписывает их при каждой выгрузке (версии всех объектов);
без `ConfigDumpInfo.xml` (маленькие выгрузки) — прежний полный обход. Правку XML без повторной выгрузки такой отпечаток
не видит: для неё у `structure_load_xml` и `structure_load_md83exp` параметр `force`. Через контейнер ЗУП КОРП (`main` +
3 расширения): первая загрузка 38,5 с (было 570 с), повторная 0,2 с (`reused`, было 7–9 мин).

## 7.3. Подключение

`kd-rules-mcp` → `http://<сервер MCP>:8060/mcp` в `.mcp.json` и `.cursor/mcp.json`; раздел «Сервер
`kd-rules-mcp`» в `AGENTS.md` (инструменты, пути, рабочая папка, коды ошибок).

Проверка: `claude mcp list` в репозитории показывает сервер (статус «ожидает одобрения», как у `1C-docs-mcp` —
серверы проектного `.mcp.json` включает пользователь один раз); новая сессия `claude -p` с `--mcp-config .mcp.json`
перечислила все 27 инструментов. Через адрес сервера в сети (`<сервер MCP>:8060`) загружена реальная структура ЗУП КОРП (2443 объекта,
88 644 свойства, без неразрешённых типов) и прочитан документ `ВедомостьНаВыплатуЗарплатыВБанк`.

## Профили проектов и настройка машины

Сделано 27.09.2026 после пилота: пути и адреса больше не зашиты в файлы репозитория.

- `projects.yaml` (пример — `projects.example.yaml`; в форке команды можно хранить в git) — проект → конфигурации (выгрузка и расширения, пути от папки проекта) → базы (роль
  `песочница`/`боевая`, строка соединения); `mcp_config` и `code_mcp` — серверы поиска по коду проекта из его
  `.mcp.json`; `shared_mcp` — общие серверы 1С; `exchanges` — план обмена и проекты. Разбор и проверка
  согласованности — `src/kd_rules_mcp/projects.py`.
- `projects.local.yaml` (не в git, пример — `projects.local.example.yaml`) — папка каждого проекта на этой машине
  (любые диски), `server_url`, необязательно `workspace`, `onec_platform` (путь к `1cv8.exe`) и `logins` (`<проект>.<база>: {user,
  password}` — пользователь 1С для баз с авторизацией; пароль только в личном файле).
- `scripts/setup_local.py` пишет `docker-compose.override.yml` (папки проектов `:ro` как `/projects/<проект>`,
  существующие `rules_dir` — на запись как `/rules/<проект>`, `KD2_PROJECT_DIRS`, `KD2_RULES_DIRS`,
  `KD2_PATH_MAP`), `.mcp.json` и `.cursor/mcp.json` (наш сервер, общие серверы, серверы кода
  проектов с префиксом `<проект>-`). В базовом `docker-compose.yml` путей машины нет.
- Инструменты `project_list` и `structure_load_project(project, configuration="full")` — структура
  `<проект>-<конфигурация>` без путей и списка расширений; ошибки профилей — код `project_config`.
- `bsp_check.py --project … --base …` берёт подключение из `projects.yaml` и отказывается работать с базой не
  роли «песочница», пользователя 1С — из `logins`, иначе из `.dev.env` проекта (`dev_env` базы: `IB_USER`,
  `IB_PASSWORD`); `kd_check.py` запускает `1cv8.exe` (`KD2_1CV8`, `onec_platform` или последняя установленная).
- `exchange_check.py --source/--target <проект>.<база>` — только песочницы с `data_mcp`: адрес из `.mcp.json`
  проекта, заголовок Basic из логина базы (`projects.data_endpoint`); код 1С отправляется инструменту
  `vcexecutecode` одной строкой (многострочный сервер данных не выполняет, ответ пустой).
- `data_mcp` базы — её `1c-data-mcp` (HTTP-сервис в ИБ, отдельное расширение): `setup_local.py` подключает его
  агентам как `<проект>-1c-data-mcp` только у песочниц, `project_list` показывает имя. Общий `1c-qa` (менеджер
  тест-клиента 1С) — в `shared_mcp`.
  Песочницы: БП (песочница БП), ДО (с пользователями), ЗУП (контур разработки); все на одном `<сервер 1С>`.
- Найдено при переходе: абсолютный путь агента вне подключённых папок в Linux-контейнере не абсолютный
  (`C:\…`) и раньше сохранялся как файл с таким именем внутри рабочей папки. Теперь такой путь при записи —
  `path_outside_workspace`, при чтении — «путь серверу не виден».
