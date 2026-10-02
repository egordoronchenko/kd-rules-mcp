# Инструменты MCP — справочник

Все инструменты сервера `kd2-rules-mcp`: группа, назначение, параметры с типами и значениями по умолчанию, коды
ошибок. Порядок работы и когда что вызывать — скилл `kd2-rules-build`; устройство сервера —
[architecture.md](architecture.md).

Часть между маркерами генерирует `scripts/dump_tools.py` из самого сервера (описания — docstring и `Field` в
`src/kd2_rules_mcp/server.py`): после изменения инструментов — `uv run python scripts/dump_tools.py --write`,
он же обновляет копию `.claude/skills/kd2-rules-build/references/tools.md`. Тест `tests/test_tools_doc.py`
падает, если документ устарел. Ручная часть — после маркеров: ключи ответов и типичные коды ошибок по
инструментам; при изменении ответа инструмента её правят руками.

<!-- tools:begin — генерирует scripts/dump_tools.py, руками не править -->

Инструментов: 30.

| Группа | Инструменты |
|---|---|
| Проекты и структуры | [`project_list`](#project_list), [`structure_load_project`](#structure_load_project), [`structure_list`](#structure_list), [`structure_load_xml`](#structure_load_xml), [`structure_load_md83exp`](#structure_load_md83exp), [`structure_objects`](#structure_objects), [`structure_object`](#structure_object), [`structure_values`](#structure_values), [`structure_plan_content`](#structure_plan_content), [`structure_compare`](#structure_compare) |
| Кандидаты сопоставления | [`match_objects`](#match_objects), [`match_properties`](#match_properties), [`match_values`](#match_values) |
| Проекты правил | [`rules_open`](#rules_open), [`rules_create`](#rules_create), [`rules_projects`](#rules_projects), [`rules_overview`](#rules_overview), [`rules_list`](#rules_list), [`rules_get`](#rules_get), [`rules_save`](#rules_save), [`rules_pack`](#rules_pack) |
| Правки | [`rule_create`](#rule_create), [`rule_update`](#rule_update), [`rule_delete`](#rule_delete), [`pko_create_from_candidates`](#pko_create_from_candidates) |
| Проверки | [`rules_validate`](#rules_validate), [`handlers_export`](#handlers_export), [`handlers_locate`](#handlers_locate) |
| Регистрация и корреспондент | [`registration_build`](#registration_build), [`correspondent_draft`](#correspondent_draft) |

## Проекты и структуры

### `project_list`

Проекты 1С из projects.yaml: конфигурации, базы (роль), серверы кода, обмены.

`available` — папка проекта подключена к серверу; `structure_id` — под каким именем
`structure_load_project` загружает конфигурацию; `rules_dir` — папка живых правил проекта
(`writable` — туда можно сохранять `rules_save`). `folder` — папка проекта путём агента,
если она задана на сервере; иначе ключа нет. `code_mcp` и `data_mcp` — имена серверов
с префиксом `<проект>-`, как в `.mcp.json` агента. В корне ответа: `workspace` — рабочая
папка путём агента, `shared_mcp` — общие серверы без префикса.

Параметров нет.

### `structure_load_project`

Загружает структуру конфигурации проекта по projects.yaml, без путей и расширений.

Неизменённая выгрузка берётся из кэша (`reused`). Вызывайте в начале каждой задачи.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project` | string | обязательный | Проект из project_list (например, bp, zup) |
| `configuration` | string | `"full"` | Конфигурация проекта из project_list |
| `structure_id` | string \| null | `null` | Своё имя структуры; по умолчанию `<проект>-<конфигурация>` |
| `force` | boolean | `false` | Собрать заново, даже если выгрузка не изменилась (ручная правка XML) |

### `structure_list`

Загруженные структуры метаданных: идентификатор, конфигурация, версия, расширения.

Параметров нет.

### `structure_load_xml`

Собирает структуру из XML-выгрузки конфигурации и перечисленных расширений.

Неизменённая выгрузка (по `Configuration.xml` и `ConfigDumpInfo.xml`) повторно не
разбирается (`reused`); после ручной правки XML — `force`. Ответ — счётчики и
неразрешённые типы.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `structure_id` | string | обязательный | Новый идентификатор: латиница, цифры, _.- |
| `configuration_path` | string | обязательный | Каталог XML-выгрузки конфигурации (где Configuration.xml) |
| `extension_paths` | array of string \| null | `null` | Каталоги выгрузок расширений в порядке наложения; только эти |
| `force` | boolean | `false` | Собрать заново, даже если выгрузка не изменилась (ручная правка XML) |

### `structure_load_md83exp`

Загружает выгрузку MD83Exp (конфигурации без исходников); около минуты на 1 ГБ.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `structure_id` | string | обязательный | Новый идентификатор: латиница, цифры, _.- |
| `path` | string | обязательный | Файл выгрузки структуры MD83Exp (XML) |
| `force` | boolean | `false` | Собрать заново, даже если выгрузка не изменилась (ручная правка XML) |

### `structure_objects`

Объекты метаданных структуры по виду и подстроке имени или синонима.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `structure_id` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `kind` | string \| null | `null` | Вид объектов: Справочник, Документ, Перечисление… |
| `text` | string \| null | `null` | Подстрока имени или синонима |
| `offset` | integer ≥ 0 | `0` | Смещение страницы |
| `limit` | integer 1…200 | `50` | Размер страницы, не больше 200 |

### `structure_object`

Объект и его свойства (реквизиты, табличные части, измерения…) с типами, постранично.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `structure_id` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `name` | string | обязательный | Объект метаданных: `Вид.Имя` (`Документ.Ведомость`) или имя типа КД |
| `offset` | integer ≥ 0 | `0` | Смещение страницы |
| `limit` | integer 1…200 | `50` | Размер страницы, не больше 200 |

### `structure_values`

Значения перечисления или предопределённые элементы объекта.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `structure_id` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `name` | string | обязательный | Объект метаданных: `Вид.Имя` (`Документ.Ведомость`) или имя типа КД |
| `offset` | integer ≥ 0 | `0` | Смещение страницы |
| `limit` | integer 1…200 | `50` | Размер страницы, не больше 200 |

### `structure_plan_content`

Состав плана обмена: типы и признак авторегистрации.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `structure_id` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `exchange_plan` | string | обязательный | План обмена: `ПланОбмена.Имя` или имя |
| `offset` | integer ≥ 0 | `0` | Смещение страницы |
| `limit` | integer 1…200 | `50` | Размер страницы, не больше 200 |

### `structure_compare`

Различие двух структур: добавленные, удалённые, изменённые объекты и свойства.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `old_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `new_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `limit` | integer ≥ 1 | `50` | Сколько элементов каждого списка показать |

## Кандидаты сопоставления

### `match_objects`

Кандидаты ПКО: пары объектов по имени и виду (как автонастройка КД) и подсказки.

Стандартных объектов в двух типовых конфигурациях сотни — сужайте выдачу `text` и `kind`.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `source_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `target_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `kind` | string \| null | `null` | Вид объектов (Справочник…) |
| `confidence` | string \| null | `null` | Отбор по классу: «точно», «синоним КД», «по синониму», «нет пары» |
| `text` | string \| null | `null` | Подстрока имени или синонима объекта любой стороны |
| `offset` | integer ≥ 0 | `0` | Смещение страницы |
| `limit` | integer 1…200 | `50` | Размер страницы, не больше 200 |

### `match_properties`

Кандидаты ПКС пары объектов; `auto=false` — не применять без решения агента.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `source_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `target_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `source_object` | string | обязательный | Объект метаданных: `Вид.Имя` (`Документ.Ведомость`) или имя типа КД |
| `target_object` | string | обязательный | Объект метаданных: `Вид.Имя` (`Документ.Ведомость`) или имя типа КД |
| `confidence` | string \| null | `null` | Отбор по классу: «точно», «синоним КД», «по синониму», «нет пары» |
| `offset` | integer ≥ 0 | `0` | Смещение страницы |
| `limit` | integer 1…200 | `50` | Размер страницы, не больше 200 |

### `match_values`

Кандидаты ПКЗ: значения перечислений по имени.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `source_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `target_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `source_object` | string | обязательный | Объект метаданных: `Вид.Имя` (`Документ.Ведомость`) или имя типа КД |
| `target_object` | string | обязательный | Объект метаданных: `Вид.Имя` (`Документ.Ведомость`) или имя типа КД |
| `offset` | integer ≥ 0 | `0` | Смещение страницы |
| `limit` | integer 1…200 | `50` | Размер страницы, не больше 200 |

## Проекты правил

### `rules_open`

Открывает правила из XML в рабочий проект; ответ — идентификатор и сводка.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `path` | string | обязательный | Файл правил обмена или регистрации (XML) |

### `rules_create`

Новые пустые правила обмена для пары структур (заголовок как у КД, правил нет).

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `source_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `target_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |

### `rules_projects`

Открытые рабочие проекты правил со сводкой.

Параметров нет.

### `rules_overview`

Сводка проекта: вид, источник и приёмник, число правил по разделам, пути.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |

### `rules_list`

Правила раздела коротко: адрес, код, наименование, источник и приёмник.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `section` | string | обязательный | Раздел: pko, pvd, pod, algorithms, queries, parameters; у правил регистрации — registration |
| `text` | string \| null | `null` | Подстрока кода, имени или типа |
| `offset` | integer ≥ 0 | `0` | Смещение страницы |
| `limit` | integer 1…200 | `50` | Размер страницы, не больше 200 |

### `rules_get`

Одно правило правил обмена: поля, стороны, список ПКС и ПКЗ; длинный код обрезается.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `kind` | string | обязательный | Вид правила: pko, pks, pks_group, pkz, pvd, pod, algorithm, query, parameter |
| `key` | string | обязательный | Адрес правила: код (ПКО, ПВД, ПОД), имя (алгоритм, запрос, параметр), путь ПКС `группа/…/свойство-приёмник` или имя значения источника ПКЗ |
| `owner` | string | `""` | Код ПКО-владельца для pks, pks_group и pkz; иначе пусто |
| `limit` | integer ≥ 1 | `100` | Сколько ПКС и ПКЗ показать |

### `rules_save`

Сохраняет XML в рабочую папку или в `rules_dir` проекта; ответ — путь, размер, итог
проверки формата. Другие пути — `path_outside_workspace` со списком `writable`.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `path` | string | обязательный | Путь в рабочей папке (относительный — от неё) или в папке живых правил проекта (rules_dir из project_list, абсолютный путь) |
| `overwrite` | boolean | `false` | Заменить существующий файл |

### `rules_pack`

ZIP правил для загрузки в БСП: файлы байт в байт под именами, которые ждёт БСП.
Два файла — форма «Правила конвертации объектов», три (с регистрацией) — «Загрузить
правила синхронизации»; `load_with` — какая. Каждый файл проверяется по виду;
`warnings` — несогласованность частей (корреспондент не зеркален, регистрация — для
другой конфигурации).

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `folder` | string | `""` | Каталог комплекта со стандартными именами файлов (ExchangeRules.xml, CorrespondentExchangeRules.xml, RegistrationRules.xml), например папка плана обмена в rules_dir проекта |
| `exchange_rules` | string | `""` | ExchangeRules.xml — правила этой программы (вместо папки) |
| `correspondent_rules` | string | `""` | CorrespondentExchangeRules.xml — правила корреспондента |
| `registration_rules` | string | `""` | RegistrationRules.xml — правила регистрации (необязательно) |
| `path` | string | `""` | Куда записать ZIP: рабочая папка (относительный путь) или rules_dir; по умолчанию <папка>.zip в рабочей папке |
| `overwrite` | boolean | `false` | Заменить существующий архив |

## Правки

### `rule_create`

Создаёт правило; висячие ссылки на ПКО и отсутствующие объекты отклоняются.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `kind` | string | обязательный | Вид правила: pko, pks, pks_group, pkz, pvd, pod, algorithm, query, parameter |
| `key` | string | обязательный | Адрес правила: код (ПКО, ПВД, ПОД), имя (алгоритм, запрос, параметр), путь ПКС `группа/…/свойство-приёмник` или имя значения источника ПКЗ |
| `fields` | object \| null | `null` | Поля правила «тег или атрибут → значение» (например {"Наименование": "…", "ПриВыгрузке": "код"}); стороны ПКС — {"Источник": {"Имя": …, "Вид": …, "Тип": …}}. Меняются только переданные поля |
| `owner` | string | `""` | Код ПКО-владельца для pks, pks_group и pkz; иначе пусто |
| `source_structure` | string \| null | `null` | Структура стороны для проверки объектов и свойств; без неё не проверяется |
| `target_structure` | string \| null | `null` | Структура стороны для проверки объектов и свойств; без неё не проверяется |
| `group` | string | `""` | Группа списка правил, куда положить правило: путь кодов групп через `/` (например `Справочники`); пусто — корень списка. Только для pko, pvd, pod |

### `rule_update`

Меняет только переданные поля правила; при отказе правило остаётся прежним.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `kind` | string | обязательный | Вид правила: pko, pks, pks_group, pkz, pvd, pod, algorithm, query, parameter |
| `key` | string | обязательный | Адрес правила: код (ПКО, ПВД, ПОД), имя (алгоритм, запрос, параметр), путь ПКС `группа/…/свойство-приёмник` или имя значения источника ПКЗ |
| `fields` | object \| null | `null` | Поля правила «тег или атрибут → значение» (например {"Наименование": "…", "ПриВыгрузке": "код"}); стороны ПКС — {"Источник": {"Имя": …, "Вид": …, "Тип": …}}. Меняются только переданные поля |
| `owner` | string | `""` | Код ПКО-владельца для pks, pks_group и pkz; иначе пусто |
| `source_structure` | string \| null | `null` | Структура стороны для проверки объектов и свойств; без неё не проверяется |
| `target_structure` | string \| null | `null` | Структура стороны для проверки объектов и свойств; без неё не проверяется |

### `rule_delete`

Удаляет правило; ПКО, на которое ссылаются, не удаляется.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `kind` | string | обязательный | Вид правила: pko, pks, pks_group, pkz, pvd, pod, algorithm, query, parameter |
| `key` | string | обязательный | Адрес правила: код (ПКО, ПВД, ПОД), имя (алгоритм, запрос, параметр), путь ПКС `группа/…/свойство-приёмник` или имя значения источника ПКЗ |
| `owner` | string | `""` | Код ПКО-владельца для pks, pks_group и pkz; иначе пусто |
| `source_structure` | string \| null | `null` | Структура стороны для проверки объектов и свойств; без неё не проверяется |
| `target_structure` | string \| null | `null` | Структура стороны для проверки объектов и свойств; без неё не проверяется |

### `pko_create_from_candidates`

ПКО с ПКС по кандидатам: «точно» и «синоним КД» включены, без пары — выключены.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `code` | string | обязательный | Код нового ПКО |
| `source_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `target_structure` | string | обязательный | Идентификатор структуры в кэше (structure_list), например `zup-full` |
| `source_object` | string | обязательный | Объект метаданных: `Вид.Имя` (`Документ.Ведомость`) или имя типа КД |
| `target_object` | string | обязательный | Объект метаданных: `Вид.Имя` (`Документ.Ведомость`) или имя типа КД |
| `fields` | object \| null | `null` | Поля правила «тег или атрибут → значение» (например {"Наименование": "…", "ПриВыгрузке": "код"}); стороны ПКС — {"Источник": {"Имя": …, "Вид": …, "Тип": …}}. Меняются только переданные поля |
| `group` | string | `""` | Группа списка правил, куда положить правило: путь кодов групп через `/` (например `Справочники`); пусто — корень списка. Только для pko, pvd, pod |

## Проверки

### `rules_validate`

Проверяет формат, структуры и ссылки на алгоритмы (или правила регистрации).

Ответ — итог, невыполненные проверки и замечания постранично.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `source_structure` | string \| null | `null` | Структура источника (у правил регистрации — где живёт план обмена) |
| `target_structure` | string \| null | `null` | Структура приёмника правил обмена |
| `level` | string \| null | `null` | Только «ошибка» или только «предупреждение» |
| `check_prefix` | string \| null | `null` | Префикс идентификатора проверки: format., structure.… |
| `offset` | integer ≥ 0 | `0` | Смещение страницы |
| `limit` | integer 1…200 | `50` | Размер страницы, не больше 200 |

### `handlers_export`

Выносит обработчики и алгоритмы в BSL-обёртки для синтакс-чекера.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `folder` | string | обязательный | Каталог для .bsl-файлов в рабочей папке или rules_dir проекта |
| `limit` | integer ≥ 1 | `50` | Сколько файлов перечислить |

### `handlers_locate`

Правило, событие и строку внутри обработчика для строки BSL-обёртки.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `file_name` | string | обязательный | Имя .bsl-файла из handlers_export |
| `line` | integer ≥ 1 | обязательный | Номер строки файла (с 1) |

## Регистрация и корреспондент

### `registration_build`

Правила регистрации из состава плана обмена в новый рабочий проект.

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `structure_id` | string | обязательный | Структура конфигурации, где живёт план обмена |
| `exchange_plan` | string | обязательный | План обмена: имя или `ПланОбмена.Имя` |
| `rules_project_id` | string \| null | `null` | Проект правил обмена: объекты берутся из его ПВД, если objects нет |
| `objects` | array of object \| null | `null` | Явный выбор: [{"metadata_name": "Справочник.X", "name"?, "code"?, "unload_mode"?, "plan_filters"?: [{"plan_property", "object_property", "property_type", "comparison", "constant"}], "object_filters"?: [{"object_property", "property_type", "comparison", "constant_value"}]}]; отборы соединяются через «И» |

### `correspondent_draft`

Черновик правил обратного направления; обработчики — «перенести вручную».

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `project_id` | string | обязательный | Идентификатор рабочего проекта правил (rules_projects) |
| `codes` | array of string | обязательный | Коды ПКО для зеркалирования |
| `target_structure` | string \| null | `null` | Структура нового приёмника (источника исходных правил) для проверки |
| `limit` | integer ≥ 1 | `50` | Сколько обработчиков и ПКС перечислить |

## Коды ошибок

Ошибка инструмента — JSON `{"code", "message", …}` в тексте ошибки MCP; код — по первому подходящему классу исключения (порядок строк важен: подклассы раньше базовых).

| Код | Класс | Когда | Дополнительные поля |
|---|---|---|---|
| `structure_not_found` | `StructureNotFoundError` | Структуры с таким идентификатором нет в кэше. | `structures` — загруженные структуры |
| `object_not_found` | `ObjectNotFoundError` | Объекта метаданных нет в структуре; `suggestions` — похожие имена. | `suggestions` — похожие имена объектов |
| `project_not_found` | `ProjectNotFoundError` | Рабочего проекта с таким идентификатором нет. | — |
| `path_outside_workspace` | `WorkspacePathError` | Путь записи вне рабочей папки и папок живых правил проектов. | `workspace` — рабочая папка (путь агента), `writable` — все папки, куда разрешена запись (пути агента) |
| `unknown_field` | `UnknownFieldError` | Поле не входит в схему этого вида правила. | — |
| `duplicate_rule` | `DuplicateRuleError` | Код или имя уже заняты в своём списке. | — |
| `rule_not_found` | `RuleNotFoundError` | Правило по адресу не найдено. | — |
| `dangling_reference` | `DanglingReferenceError` | Ссылка на отсутствующее правило, объект, свойство или значение. | — |
| `edit_rejected` | `RuleEditError` | Правка правила отклонена. | — |
| `rules_format` | `RulesFormatError` | Файл правил не разбирается или не соответствует формату КД 2. | — |
| `structure_format` | `StructureFormatError` | Файл структуры метаданных не разбирается или не является ожидаемой выгрузкой. | — |
| `project_config` | `ProjectConfigError` | Ошибка в файле проектов или неизвестный проект, конфигурация, база. | — |
| `rejected` | `Kd2Error` | Прочий отказ сервера (базовый класс `Kd2Error`): причина — в `message`. | — |
| `invalid_argument` | `ValueError` | Недопустимое значение аргумента, например неизвестный уровень или класс. | — |

<!-- tools:end — ниже ручная часть, скрипт её не трогает -->

## Ответы инструментов

Ответ — JSON-объект (`structuredContent` MCP). Ниже — ключи верхнего уровня по коду `Kd2Service`
(`src/kd2_rules_mcp/service.py`); вложенные поля — в описании инструмента или в самом ответе.

**Страница списка** — общий вид постраничных ответов (`_slice`, `_page`, `src/kd2_rules_mcp/service.py:864-885`):
`items`, `total`, `offset`, `limit` (не больше 200), `has_more`.

**Проект правил** — общий вид ответа о рабочем проекте (`_project_view`, `src/kd2_rules_mcp/service.py:812`):
`project_id`, `kind` (`exchange` или `registration`), `source_path`, `saved_path`, `counts` (число правил по
разделам: `pko`, `pvd`, `pod`, `algorithms`, `queries`, `parameters` или `registration_rules`), `name`; у правил
обмена — `source`, `target`; у правил регистрации — `exchange_plan`.

**Итог правки** — `_edit_view` (`src/kd2_rules_mcp/service.py:1083`): `address` и, если не пусто, `warnings`,
`skipped`, `not_applied`, `unresolved`, `disabled` (до 200 строк, при большем числе — ещё `<ключ>_total`).

| Инструмент | Ключи ответа |
|---|---|
| `project_list` | `workspace` — рабочая папка путём агента; `shared_mcp` — общие серверы без префикса; `projects` — по проекту: `project`, `name`, `available`, `folder` — папка проекта путём агента, если задана на сервере (иначе ключа нет), `configurations` (`structure_id`, `dump`, `extensions`), `bases` (`role`, `configuration`, у песочницы — `data_mcp` с префиксом `<проект>-`), `code_mcp` — имена с префиксом `<проект>-`, `rules_dir` (`path`, `writable`) — если задан; `exchanges` — `plan`, `projects` |
| `structure_load_project` | как у `structure_load_xml` + `project`, `configuration` |
| `structure_list` | `structures` — `structure_id`, `configuration`, `synonym`, `version`, `source`, `source_path`, `extensions`, `loaded_at` |
| `structure_load_xml`, `structure_load_md83exp` | `structure_id`, `reused`, `counts`, `elapsed_s`, `message`; при неразрешённых типах — `unresolved_total`, `unresolved_top` (до 20) |
| `structure_objects` | страница списка: элементы — `name` (`Вид.Имя`), `type_name`, `synonym` |
| `structure_object` | `name`, `type_name`, `kind`, `synonym`, `attrs`, `properties` (страница списка) |
| `structure_values`, `structure_plan_content` | страница списка |
| `structure_compare` | `counts`, `limit`, `added_objects`, `removed_objects`, `added_properties`, `removed_properties`, `changed_properties`, `added_values`, `removed_values` (каждый список — до `limit`) |
| `match_objects` | страница списка: элементы — `confidence`, `auto`, `source`, `target` (`Вид.Имя`), `synonym`, `note` |
| `match_properties`, `match_values` | страница списка: элементы — `confidence`, `auto`, `source`, `target` (`name`, `kind`, `path`, `synonym`, `types` — до пяти), `path` (путь ПКС), `note` |
| `rules_open`, `rules_create`, `rules_overview` | проект правил |
| `rules_projects` | `projects` — список проектов правил |
| `rules_list` | страница списка: элементы — `address`, `code`, заполненные поля строки правила, флаги `Отключить`, `ИспользуетсяПриЗагрузке`, `pks_count` |
| `rules_get` | `kind`, `title`, `attrs`, `fields` (тексты длиннее 2000 символов обрезаны), `sides` (ПКС), `properties` (`total`, `items`) и `address` (ПКО), `values` (`total`, `items`), `nested` |
| `rules_save` | `project_id`, `path`, `size_bytes`, `counts`, `format_check` (`errors`, `warnings`, `skipped`, `by_check`, `text`) |
| `rules_pack` | `path`, `size_bytes`, `load_with` (форма загрузки в БСП), `files` (`file`, `path`, `size_bytes`, `rules`), `warnings` |
| `rule_create`, `rule_update`, `rule_delete`, `pko_create_from_candidates` | итог правки |
| `rules_validate` | `summary` (`errors`, `warnings`, `skipped`, `by_check`, `text`), `skipped` (`check`, `reason`), `issues` (страница списка: `level`, `check`, `address`, `message`) |
| `handlers_export` | `folder`, `count`, `removed` (число удалённых прежних обёрток), `files` (`file`, `address`, `event`), `has_more` |
| `handlers_locate` | `found`; если найдено — `address`, `event`, `handler_line`, иначе `file`, `line` |
| `registration_build` | проект правил + `warnings` |
| `correspondent_draft` | проект правил + `draft`, `missing`, `notes`, `handlers_total`, `handlers` (`address`, `event`, `note`, `code`), `disabled_total`, `disabled` (`address`, `reason`) |

## Ошибки по инструментам

Какой код когда возникает — по коду `Kd2Service` и модулей слоёв (вывод по коду, не полный перебор тестами):

| Ситуация | Код | Инструменты |
|---|---|---|
| структура не загружена | `structure_not_found` | все, что принимают идентификатор структуры |
| объекта нет в структуре | `object_not_found` | `structure_object`, `structure_values`, `structure_plan_content`, `match_properties`, `match_values`, `pko_create_from_candidates` |
| рабочего проекта правил нет (в том числе после перезапуска сервера) | `project_not_found` | все, что принимают `project_id` |
| путь записи вне рабочей папки и `rules_dir` | `path_outside_workspace` | `rules_save`, `rules_pack`, `handlers_export` |
| путь чтения серверу не виден или не найден; папка проекта не задана; `handlers_locate` до `handlers_export`; отрицательное смещение | `rejected` | `structure_load_*`, `rules_open`, `rules_pack`, `handlers_locate`, списки |
| файл правил не разбирается или не того вида | `rules_format` | `rules_open`, `rules_pack` |
| файл структуры не разбирается | `structure_format` | `structure_load_xml`, `structure_load_md83exp`, `structure_load_project` |
| ошибка в `projects.yaml`, неизвестный проект, конфигурация или база | `project_config` | `project_list`, `structure_load_project` |
| отказ правки | `unknown_field`, `duplicate_rule`, `rule_not_found`, `dangling_reference`, `edit_rejected` | `rule_create`, `rule_update`, `rule_delete`, `pko_create_from_candidates`, `rules_get` (`rule_not_found`) |
| неизвестный уровень или класс уверенности | `invalid_argument` | `rules_validate` (`level`), `match_*` (`confidence`) |
| непредвиденное исключение сервера (трассировка в логе сервера) | `internal` | все инструменты |

Аргумент вне схемы (например, `limit` больше 200 или пропущен обязательный параметр) отклоняет сам MCP SDK до
вызова сервиса: ответ — текст ошибки проверки аргументов (`validation error … Input should be less than or equal
to 200`), без JSON с `code` (проверено вызовом через клиент MCP 01.10.2026). Любое другое исключение внутри
инструмента сервер оборачивает в JSON: `Kd2Error` и `ValueError` — кодом из таблицы выше, прочие — кодом
`internal` и текстом исключения; трассировка пишется в лог сервера.
