# Правила обмена КД 2 и сервер kd-rules-mcp

MCP-сервер `kd-rules-mcp` разбирает, правит и проверяет правила обмена «Конвертации данных 2» по структурам
метаданных двух конфигураций 1С. Что во что конвертировать, решаешь ты — и пишешь решения в отчёт. Знания — в
`.agents/skills/` (скиллы `kd-`, `kd2-`, `kd3-`); сами они могут не открыться, ниже — что прочитать и когда.

## Порядок работы

1. `project_list` — проекты: папка (`folder`), базы и роли, серверы MCP для 1С (`code_mcp`, `data_mcp` у
   песочниц, `shared_mcp`), папка живых правил (`rules_dir`), обмены, рабочая папка (`workspace`).
2. Структуры обеих сторон — `structure_load_project`; правила — `rules_open` и сразу `rules_validate` с обеими
   структурами (замечания «до правок»), новые — `rules_create`.
3. Кандидаты — `match_objects` (с `text`), `match_properties`, `match_values`; `auto: false` — только твоим решением.
4. Правки ПКО → ПКС → ПВД → ПРО; проверки — `rules_validate` без новых замечаний, обработчики — `handlers_export` →
   синтакс-чекер → `handlers_locate`; сохранение — `rules_save` (с ошибками проверки — отказ `rules_save_errors`).
   Проекты правил переживают перезапуск (снимки): открытые — `rules_projects`, законченный — `rules_close`.

## Типовые цепочки

- Новые правила: `rules_create` → `match_objects` → `pko_create_from_candidates` → `match_properties` →
  `rule_update` → ПВД `rule_create` → `rules_validate` → `rules_save` → `registration_build` → `rules_pack`.
- Доработка: `rules_open` → `rules_validate` → `rules_trace`/`rules_get` → `rule_update` → `rules_validate` →
  `handlers_export` → `rules_diff` → `rules_save`; правка загрузки — и в правилах корреспондента.
- После обновления конфигурации: новая структура под другим `structure_id` → `structure_compare` →
  `rules_validate` с новой структурой → правки → `rules_save`; модуль менеджера — `ed_create(mode="rebind")`.
- Универсальный формат: `ed_open` → `ed_overview` → `ed_schema_open` → `ed_validate(schema_id, structure_id)`.
- Две базы через профиль: `project_exchanges` → его шаги → `live_base_check(exchange=…)` → `live_rules_check`.

## Что прочитать и когда

- Любая задача с правилами — сначала `.agents/skills/kd2-rules-build/SKILL.md` (порядок, чек-лист).
- Перед первым вызовом серверов MCP для 1С — `.agents/skills/kd2-rules-build/references/mcp-1c.md`.
- ПКО — `references/pko.md`, поиск и дубли — `search.md`, ПКС — `pks.md`, перечисления — `pkz.md`, ПВД — `pvd.md`,
  обработчики — `handlers.md`, ПРО — `pro.md` (в `.agents/skills/kd2-rules-build/references/`; без скиллов — те же
  файлы через `reference_get`). Проверки — `references/checks.md`, инструменты — `references/tools.md`, сокращения
  — `references/glossary.md`.
- Жалоба на живой обмен (задвоился, не нашёл, не выгрузился) — `.agents/skills/kd2-exchange-pitfalls/SKILL.md`.
- Универсальный формат (правила — код модуля менеджера обмена) — `.agents/skills/kd3-rules/SKILL.md`.
- Установить или обновить сервер, подключить ещё проект — `.agents/skills/kd-install/SKILL.md`.

## Серверы MCP для 1С

Называй их ролями: сервер кода проекта-источника — для выгрузочных обработчиков, проекта-приёмника — для
загрузочных; общие — синтакс-чекер, проверка кода, справка платформы, БСП, шаблоны; сервер данных — только у
песочниц. Имя роли — из `project_list` и списка серверов твоего клиента. Общие правила работы с ними — набор
comol `ai_rules_1c` в папке проекта (`folder`): `.cursor/rules/mcp-policy.mdc` и
`.cursor/rules/mcp-first-search.mdc` (для Codex — `.codex/rules/`, список — `files` в `.ai-rules.json`) —
прочитай их перед первым вызовом, затем дельту `references/mcp-1c.md`. Набора нет — скажи человеку, что
работа с серверами 1С будет хуже (https://github.com/comol/ai_rules_1c). В папке обмена (есть `KD-EXCHANGE.md`)
подключены серверы обеих сторон: чей сервер — по таблице в `KD-EXCHANGE.md`; без неё — вторая сторона по
`references/second-side.md`.

## Где остановиться и спросить человека

- Пароли и логины баз не спрашивать в чате и не писать в файлы; перезапуск сервера — после `rules_save` всего.
- Запись в репозиторий проекта 1С вне `rules_dir` (правила агента, скиллы, `.mcp.json`, код) — только с его «да»;
  коммит живых правил — только файлы `rules_dir`, по правилам проекта.
- Загрузка правил и запись в базу — только песочницы и с согласия, боевая — никогда; физическое удаление данных —
  отдельное «да». Скрипты `kdbase` — из клона сервера: путь спросить, если его нет в правилах проекта.
- Отказ `path_outside_workspace` — сохранить в рабочую папку или `rules_dir`; разрешённые папки не расширять.
