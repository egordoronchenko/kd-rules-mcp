"""Сервер MCP правил обмена КД 2: инструменты поверх `Kd2Service` (спецификация `mcp-service`).

Транспорт — streamable HTTP (`/mcp`), порт по умолчанию 8060 (design.md, Д1). Вызов
инструмента выполняется в рабочем потоке: загрузка большой структуры не останавливает сервер.

Ошибка инструмента — `ToolError` с JSON: `code` (по нему агент выбирает действие), `message`
на русском и дополнительные поля (`structures`, `suggestions`, `workspace`). Любое исключение
внутри инструмента оборачивается в этот JSON. `Kd2Error` и `ValueError` получают код из
`ERROR_CODES`; прочее исключение — код `internal` и текст исключения, трассировка пишется
в логгер `kd2_rules_mcp` (уровень ERROR). Каждый вызов даёт одну строку INFO: имя инструмента,
длительность и итог (`ok` или код ошибки). Уровень лога задаёт `main()` по переменной
`KD2_LOG_LEVEL` (по умолчанию INFO). Неизвестное значение не останавливает запуск: остаётся
INFO, в лог пишется предупреждение. `create_server` логирование не настраивает.
"""

import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from functools import partial
from typing import Annotated, Any

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from kd2_rules_mcp.errors import (
    AmbiguousAddressError,
    DanglingReferenceError,
    DuplicateProjectError,
    DuplicateRuleError,
    EdFormatError,
    EdReadError,
    EdResourceLimitError,
    EdRouteFormatError,
    EdRouteProfileNotFoundError,
    EdRouteReadError,
    EdRouteResourceLimitError,
    EdSchemaAmbiguousImportError,
    EdSchemaConflictError,
    EdSchemaFormatError,
    EdSchemaNotFoundError,
    EdSchemaProfileMismatchError,
    EdSchemaReadError,
    EdSchemaResourceLimitError,
    EdSchemaTypeNotFoundError,
    Kd2Error,
    ObjectNotFoundError,
    ProjectNotFoundError,
    RuleEditError,
    RuleNotFoundError,
    RulesFormatError,
    StructureFormatError,
    StructureNotFoundError,
    UnknownFieldError,
    WorkspacePathError,
)
from kd2_rules_mcp.projects import ProjectConfigError, is_loopback_host
from kd2_rules_mcp.service import Kd2Service, Settings

logger = logging.getLogger("kd2_rules_mcp")

# Код ошибки по классу; порядок важен — подклассы раньше базовых.
ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (EdSchemaNotFoundError, "ed_schema_not_found"),
    (EdSchemaTypeNotFoundError, "ed_schema_type_not_found"),
    (EdSchemaReadError, "ed_schema_read_error"),
    (EdSchemaFormatError, "ed_schema_format"),
    (EdSchemaConflictError, "ed_schema_conflict"),
    (EdSchemaAmbiguousImportError, "ed_schema_ambiguous_import"),
    (EdSchemaProfileMismatchError, "ed_schema_profile_mismatch"),
    (EdSchemaResourceLimitError, "ed_schema_resource_limit"),
    (EdResourceLimitError, "ed_resource_limit"),
    (EdFormatError, "ed_format"),
    (EdReadError, "ed_read_error"),
    (EdRouteProfileNotFoundError, "ed_route_profile_not_found"),
    (EdRouteReadError, "ed_route_read_error"),
    (EdRouteFormatError, "ed_route_format"),
    (EdRouteResourceLimitError, "ed_route_resource_limit"),
    (StructureNotFoundError, "structure_not_found"),
    (ObjectNotFoundError, "object_not_found"),
    (ProjectNotFoundError, "project_not_found"),
    (DuplicateProjectError, "duplicate_project"),
    (WorkspacePathError, "path_outside_workspace"),
    (UnknownFieldError, "unknown_field"),
    (DuplicateRuleError, "duplicate_rule"),
    (AmbiguousAddressError, "ambiguous_address"),
    (RuleNotFoundError, "rule_not_found"),
    (DanglingReferenceError, "dangling_reference"),
    (RuleEditError, "edit_rejected"),
    (RulesFormatError, "rules_format"),
    (StructureFormatError, "structure_format"),
    (ProjectConfigError, "project_config"),
    (Kd2Error, "rejected"),
    (ValueError, "invalid_argument"),
)

INSTRUCTIONS = """Сервер правил обмена «Конвертации данных 2» (ПравилаОбмена 2.01,
ПравилаРегистрации).
Порядок работы: загрузить структуры (structure_load_project по project_list; или
structure_load_xml / structure_load_md83exp по путям) →
открыть правила (rules_open) или создать пустые (rules_create) → смотреть кандидатов (match_*) и
править (rule_*, pko_create_from_candidates) → проверить (rules_validate, handlers_export для
синтакс-чекера) → сохранить в рабочую папку или rules_dir проекта (rules_save). Что изменилось
между двумя версиями правил или проектом и его файлом — rules_diff. Закрыть проект и
удалить его снимок — rules_close (файлы rules_save остаются). Правила регистрации —
registration_build, черновик обратного направления — correspondent_draft.
Смысловые решения принимает агент.
Списки постраничные (offset, limit ≤ 200, has_more). Ошибки — JSON с полем code.
Сервер читает модуль менеджера обмена через универсальный формат EnterpriseData только для чтения.
Порядок: ed_open → ed_overview → ed_list / ed_get / ed_locate → ed_validate.
Снимки ED живут в памяти; перечитать изменённый файл — ed_close и ed_open.
Схема формата XDTO: ed_schema_open с явной версией → ed_schema_types / ed_schema_type.
Схемы только в памяти; перечитать пакет — ed_schema_close и ed_schema_open.
XSD не поддерживается.
Маршруты версий формата: ed_routes двух выгрузок → ed_route_compare."""

StructureId = Annotated[
    str, Field(description="Идентификатор структуры в кэше (structure_list), например `zup-full`")
]
ProjectId = Annotated[
    str, Field(description="Идентификатор рабочего проекта правил (rules_projects)")
]
Offset = Annotated[int, Field(description="Смещение страницы", ge=0)]
Limit = Annotated[int, Field(description="Размер страницы, не больше 200", ge=1, le=200)]
ObjectName = Annotated[
    str,
    Field(description="Объект метаданных: `Вид.Имя` (`Документ.Ведомость`) или имя типа КД"),
]
RuleKind = Annotated[
    str,
    Field(
        description=(
            "Вид правила: pko, pks, pks_group, pkz, pvd, pod, algorithm, query, parameter, "
            "conversion. conversion — события конвертации, один экземпляр на файл; "
            "rule_create и rule_delete для него отклоняются"
        )
    ),
]
RuleKey = Annotated[
    str,
    Field(
        description=(
            "Адрес правила: код (ПКО, ПВД, ПОД), имя (алгоритм, запрос, параметр), путь ПКС "
            "`группа/…/свойство-приёмник` или имя значения источника ПКЗ. При совпадении имён "
            "в одном контейнере к звену ПКС добавляются квалификаторы `[поиск]` и `#N`. "
            "Для conversion — пустая строка или `Конвертация` (в ответах адрес `Конвертация`)"
        )
    ),
]
Owner = Annotated[str, Field(description="Код ПКО-владельца для pks, pks_group и pkz; иначе пусто")]
RuleGroup = Annotated[
    str,
    Field(
        description=(
            "Группа списка правил, куда положить правило: путь кодов групп через `/` "
            "(например `Справочники`); пусто — корень списка. Только для pko, pvd, pod"
        )
    ),
]
Fields = Annotated[
    dict[str, Any] | None,
    Field(
        description=(
            'Поля правила «тег или атрибут → значение» (например {"Наименование": "…", '
            '"ПриВыгрузке": "код"}); стороны ПКС — {"Источник": {"Имя": …, "Вид": …, '
            '"Тип": …}}. Меняются только переданные поля. У conversion — имя события → текст; '
            "пустая строка удаляет событие; поля заголовка отклоняются"
        )
    ),
]
OptionalStructure = Annotated[
    str | None,
    Field(description="Структура стороны для проверки объектов и свойств; без неё не проверяется"),
]

ConfidenceFilter = Annotated[
    str | None,
    Field(description="Отбор по классу: «точно», «синоним КД», «по синониму», «нет пары»"),
]


def error_payload(error: Exception, service: Kd2Service | None = None) -> dict[str, Any]:
    """JSON ошибки инструмента: код, текст и сведения для выбора действия."""
    code = next((code for kind, code in ERROR_CODES if isinstance(error, kind)), "internal")
    payload: dict[str, Any] = {"code": code, "message": str(error)}
    if isinstance(error, StructureNotFoundError) and service is not None:
        payload["structures"] = service.store.ids()
    if isinstance(error, ObjectNotFoundError):
        payload["suggestions"] = error.suggestions
    if isinstance(error, AmbiguousAddressError) and error.candidate_page is not None:
        payload["candidates"] = error.candidate_page
    if isinstance(error, WorkspacePathError) and service is not None:
        payload["workspace"] = service.settings.path_map.to_host(service.workspace.root.resolve())
        payload["writable"] = service.writable_dirs()
    return payload


def create_server(service: Kd2Service) -> MCPServer:
    """Сервер MCP с инструментами поверх `service`."""
    server = MCPServer("kd2-rules-mcp", instructions=INSTRUCTIONS)

    async def call(function: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> Any:
        # Имя метода сервиса совпадает с именем инструмента.
        name = function.__name__
        started = time.perf_counter()
        try:
            result = await anyio.to_thread.run_sync(partial(function, *args, **kwargs))
        except Exception as error:
            payload = error_payload(error, service)
            if not isinstance(error, (Kd2Error, ValueError)):
                logger.exception("Инструмент %s: непредвиденное исключение", name)
            _log_call(name, started, str(payload["code"]))
            raise ToolError(json.dumps(payload, ensure_ascii=False)) from error
        _log_call(name, started, "ok")
        return result

    # --- Структуры ---------------------------------------------------------------------------

    @server.tool()
    async def project_list() -> dict[str, Any]:
        """Проекты 1С из projects.yaml: конфигурации, базы (роль), серверы кода, обмены.

        `available` — папка проекта подключена к серверу; `structure_id` — под каким именем
        `structure_load_project` загружает конфигурацию; `rules_dir` — папка живых правил проекта
        (`writable` — туда можно сохранять `rules_save`). `folder` — папка проекта путём агента,
        если она задана на сервере; иначе ключа нет. У каждой базы `login` — задан ли логин
        (`true` или `false`): пара из `logins` в `projects.local.yaml` или `IB_USER` в `.dev.env`;
        имя и пароль в ответ не попадают. Если сервер не видит `projects.local.yaml`, признак
        считается только по `.dev.env`. `code_mcp`/`data_mcp` — имена как в `.mcp.json` проекта
        (работа из папки проекта 1С), `code_mcp_server`/`data_mcp_server` — с префиксом проекта
        (работа из папки сервера). В корне ответа: `workspace` — рабочая папка путём агента,
        `shared_mcp` — общие серверы без префикса.
        """
        return await call(service.project_list)

    @server.tool()
    async def structure_load_project(
        project: Annotated[str, Field(description="Проект из project_list (например, bp, zup)")],
        configuration: Annotated[
            str, Field(description="Конфигурация проекта из project_list")
        ] = "full",
        structure_id: Annotated[
            str | None,
            Field(description="Своё имя структуры; по умолчанию `<проект>-<конфигурация>`"),
        ] = None,
        force: Annotated[
            bool,
            Field(
                description="Собрать заново, даже если выгрузка не изменилась (ручная правка XML)"
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Загружает структуру конфигурации проекта по projects.yaml, без путей и расширений.

        Неизменённая выгрузка берётся из кэша (`reused`). Вызывайте в начале каждой задачи.
        """
        return await call(
            service.structure_load_project, project, configuration, structure_id, force
        )

    @server.tool()
    async def structure_list() -> dict[str, Any]:
        """Загруженные структуры метаданных: идентификатор, конфигурация, версия, расширения."""
        return await call(service.structure_list)

    @server.tool()
    async def structure_load_xml(
        structure_id: Annotated[
            str, Field(description="Новый идентификатор: латиница, цифры, _.-")
        ],
        configuration_path: Annotated[
            str, Field(description="Каталог XML-выгрузки конфигурации (где Configuration.xml)")
        ],
        extension_paths: Annotated[
            list[str] | None,
            Field(description="Каталоги выгрузок расширений в порядке наложения; только эти"),
        ] = None,
        force: Annotated[
            bool,
            Field(
                description="Собрать заново, даже если выгрузка не изменилась (ручная правка XML)"
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Собирает структуру из XML-выгрузки конфигурации и перечисленных расширений.

        Неизменённая выгрузка (по `Configuration.xml` и `ConfigDumpInfo.xml`) повторно не
        разбирается (`reused`); после ручной правки XML — `force`. Ответ — счётчики и
        неразрешённые типы.
        """
        return await call(
            service.structure_load_xml,
            structure_id,
            configuration_path,
            extension_paths or [],
            force,
        )

    @server.tool()
    async def structure_load_md83exp(
        structure_id: Annotated[
            str, Field(description="Новый идентификатор: латиница, цифры, _.-")
        ],
        path: Annotated[str, Field(description="Файл выгрузки структуры MD83Exp (XML)")],
        force: Annotated[
            bool,
            Field(
                description="Собрать заново, даже если выгрузка не изменилась (ручная правка XML)"
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Загружает выгрузку MD83Exp (конфигурации без исходников); около минуты на 1 ГБ."""
        return await call(service.structure_load_md83exp, structure_id, path, force)

    @server.tool()
    async def structure_objects(
        structure_id: StructureId,
        kind: Annotated[
            str | None, Field(description="Вид объектов: Справочник, Документ, Перечисление…")
        ] = None,
        text: Annotated[str | None, Field(description="Подстрока имени или синонима")] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Объекты метаданных структуры по виду и подстроке имени или синонима."""
        return await call(service.structure_objects, structure_id, kind, text, offset, limit)

    @server.tool()
    async def structure_object(
        structure_id: StructureId, name: ObjectName, offset: Offset = 0, limit: Limit = 50
    ) -> dict[str, Any]:
        """Объект и его свойства (реквизиты, табличные части, измерения…) с типами, постранично."""
        return await call(service.structure_object, structure_id, name, offset, limit)

    @server.tool()
    async def structure_values(
        structure_id: StructureId, name: ObjectName, offset: Offset = 0, limit: Limit = 50
    ) -> dict[str, Any]:
        """Значения перечисления или предопределённые элементы объекта."""
        return await call(service.structure_values, structure_id, name, offset, limit)

    @server.tool()
    async def structure_plan_content(
        structure_id: StructureId,
        exchange_plan: Annotated[str, Field(description="План обмена: `ПланОбмена.Имя` или имя")],
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Состав плана обмена: типы и признак авторегистрации."""
        return await call(
            service.structure_plan_content, structure_id, exchange_plan, offset, limit
        )

    @server.tool()
    async def structure_compare(
        old_structure: StructureId,
        new_structure: StructureId,
        limit: Annotated[
            int, Field(description="Сколько элементов каждого списка показать", ge=1)
        ] = 50,
    ) -> dict[str, Any]:
        """Различие двух структур: добавленные, удалённые, изменённые объекты и свойства."""
        return await call(service.structure_compare, old_structure, new_structure, limit)

    # --- Кандидаты -----------------------------------------------------------------------

    @server.tool()
    async def match_objects(
        source_structure: StructureId,
        target_structure: StructureId,
        kind: Annotated[str | None, Field(description="Вид объектов (Справочник…)")] = None,
        confidence: ConfidenceFilter = None,
        text: Annotated[
            str | None, Field(description="Подстрока имени или синонима объекта любой стороны")
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Кандидаты ПКО: пары объектов по имени и виду (как автонастройка КД) и подсказки.

        Стандартных объектов в двух типовых конфигурациях сотни — сужайте выдачу `text` и `kind`.
        """
        return await call(
            service.match_objects,
            source_structure,
            target_structure,
            kind,
            confidence,
            offset,
            limit,
            text,
        )

    @server.tool()
    async def match_properties(
        source_structure: StructureId,
        target_structure: StructureId,
        source_object: ObjectName,
        target_object: ObjectName,
        confidence: ConfidenceFilter = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Кандидаты ПКС пары объектов; `auto=false` — не применять без решения агента."""
        return await call(
            service.match_properties,
            source_structure,
            target_structure,
            source_object,
            target_object,
            confidence,
            offset,
            limit,
        )

    @server.tool()
    async def match_values(
        source_structure: StructureId,
        target_structure: StructureId,
        source_object: ObjectName,
        target_object: ObjectName,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Кандидаты ПКЗ: значения перечислений по имени."""
        return await call(
            service.match_values,
            source_structure,
            target_structure,
            source_object,
            target_object,
            offset,
            limit,
        )

    # --- Проекты правил ------------------------------------------------------------------

    @server.tool()
    async def rules_open(
        path: Annotated[str, Field(description="Файл правил обмена или регистрации (XML)")],
        private: Annotated[
            bool,
            Field(
                description=(
                    "Своя копия: всегда новый проект из файла, общий проект того же пути "
                    "не меняется и повторное открытие без флага его не находит. Для параллельной "
                    "работы двух агентов над одним файлом"
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Открывает правила из XML в рабочий проект; ответ — идентификатор и сводка.

        С `private: true` всегда создаётся новый проект `<идентификатор файла>-p<N>`
        (`reused: false`), общий проект того же файла не переиспользуется и не меняется.
        """
        return await call(service.rules_open, path, private)

    @server.tool()
    async def rules_create(
        source_structure: StructureId,
        target_structure: StructureId,
        project_id: Annotated[
            str | None,
            Field(
                description=(
                    "Свой идентификатор нового проекта правил; "
                    "пусто — выводится из источника и вида"
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Новые пустые правила обмена для пары структур (заголовок как у КД, правил нет)."""
        return await call(service.rules_create, source_structure, target_structure, project_id)

    @server.tool()
    async def rules_projects() -> dict[str, Any]:
        """Открытые рабочие проекты правил со сводкой."""
        return await call(service.rules_projects)

    @server.tool()
    async def rules_overview(project_id: ProjectId) -> dict[str, Any]:
        """Сводка проекта: вид, источник и приёмник, число правил по разделам, пути.
        У правил обмена `counts.conversion` — число заполненных событий конвертации."""
        return await call(service.rules_overview, project_id)

    @server.tool()
    async def rules_list(
        project_id: ProjectId,
        section: Annotated[
            str,
            Field(
                description=(
                    "Раздел: pko, pvd, pod, algorithms, queries, parameters, conversion; "
                    "у правил регистрации — registration. conversion — одна строка, "
                    "события конвертации"
                )
            ),
        ],
        text: Annotated[str | None, Field(description="Подстрока кода, имени или типа")] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Правила раздела коротко: адрес, код, наименование, источник и приёмник."""
        return await call(service.rules_list, project_id, section, text, offset, limit)

    @server.tool()
    async def rules_get(
        project_id: ProjectId,
        kind: RuleKind,
        key: RuleKey,
        owner: Owner = "",
        limit: Annotated[int, Field(description="Сколько ПКС и ПКЗ показать", ge=1)] = 100,
    ) -> dict[str, Any]:
        """Одно правило правил обмена: поля, стороны, список ПКС и ПКЗ; длинный код обрезается.

        Вид conversion — заполненные события конвертации (имя, число строк, текст с тем же
        пределом, что у других обработчиков) и реквизиты заголовка, которые уже лежат в модели.
        """
        return await call(service.rules_get, project_id, kind, key, owner, limit)

    @server.tool()
    async def rules_save(
        project_id: ProjectId,
        path: Annotated[
            str,
            Field(
                description="Путь в рабочей папке (относительный — от неё) или в папке живых "
                "правил проекта (rules_dir из project_list, абсолютный путь)"
            ),
        ],
        overwrite: Annotated[bool, Field(description="Заменить существующий файл")] = False,
    ) -> dict[str, Any]:
        """Сохраняет XML в рабочую папку или в `rules_dir` проекта; ответ — путь, размер, итог
        проверки формата. При записи поверх существующего файла концы строк неизменённых строк
        сохраняются. Другие пути — `path_outside_workspace` со списком `writable`."""
        return await call(service.rules_save, project_id, path, overwrite)

    @server.tool()
    async def rules_close(project_id: ProjectId) -> dict[str, Any]:
        """Закрывает рабочий проект и удаляет его снимок. Файлы `rules_save` не трогает."""
        return await call(service.rules_close, project_id)

    @server.tool()
    async def rules_pack(
        folder: Annotated[
            str,
            Field(
                description="Каталог комплекта со стандартными именами файлов (ExchangeRules.xml, "
                "CorrespondentExchangeRules.xml, RegistrationRules.xml), например папка плана "
                "обмена в rules_dir проекта"
            ),
        ] = "",
        exchange_rules: Annotated[
            str, Field(description="ExchangeRules.xml — правила этой программы (вместо папки)")
        ] = "",
        correspondent_rules: Annotated[
            str, Field(description="CorrespondentExchangeRules.xml — правила корреспондента")
        ] = "",
        registration_rules: Annotated[
            str, Field(description="RegistrationRules.xml — правила регистрации (необязательно)")
        ] = "",
        path: Annotated[
            str,
            Field(
                description="Куда записать ZIP: рабочая папка (относительный путь) или rules_dir; "
                "по умолчанию <папка>.zip в рабочей папке"
            ),
        ] = "",
        overwrite: Annotated[bool, Field(description="Заменить существующий архив")] = False,
    ) -> dict[str, Any]:
        """ZIP правил для загрузки в БСП: файлы байт в байт под именами, которые ждёт БСП.
        Два файла — форма «Правила конвертации объектов», три (с регистрацией) — «Загрузить
        правила синхронизации»; `load_with` — какая. Каждый файл проверяется по виду;
        `warnings` — несогласованность частей (корреспондент не зеркален, регистрация — для
        другой конфигурации)."""
        return await call(
            service.rules_pack,
            folder,
            exchange_rules,
            correspondent_rules,
            registration_rules,
            path,
            overwrite,
        )

    # --- Правки ---------------------------------------------------------------------------

    @server.tool()
    async def rule_create(
        project_id: ProjectId,
        kind: RuleKind,
        key: RuleKey,
        fields: Fields = None,
        owner: Owner = "",
        source_structure: OptionalStructure = None,
        target_structure: OptionalStructure = None,
        group: RuleGroup = "",
    ) -> dict[str, Any]:
        """Создаёт правило; висячие ссылки на ПКО и отсутствующие объекты отклоняются.
        У ПКС `Код` и `Порядок` без явных значений подставляются как у соседей.
        Вид conversion отклоняется: экземпляр один и создаётся вместе с правилами."""
        return await call(
            service.rule_create,
            project_id,
            kind,
            key,
            fields=fields,
            owner=owner,
            source_structure=source_structure,
            target_structure=target_structure,
            group=group,
        )

    @server.tool()
    async def rule_update(
        project_id: ProjectId,
        kind: RuleKind,
        key: RuleKey,
        fields: Fields = None,
        owner: Owner = "",
        source_structure: OptionalStructure = None,
        target_structure: OptionalStructure = None,
    ) -> dict[str, Any]:
        """Меняет только переданные поля правила; при отказе правило остаётся прежним.
        У conversion — только тексты событий; пустая строка удаляет событие;
        поля заголовка отклоняются."""
        return await call(
            service.rule_update,
            project_id,
            kind,
            key,
            fields=fields,
            owner=owner,
            source_structure=source_structure,
            target_structure=target_structure,
        )

    @server.tool()
    async def rule_update_many(
        project_id: ProjectId,
        kind: Annotated[
            str,
            Field(
                description=(
                    "Вид вложенных правил одного ПКО: pks, pks_group или pkz. "
                    "Правила верхнего уровня этим инструментом не меняются"
                )
            ),
        ],
        owner: Annotated[str, Field(description="Код ПКО, чьи ПКС, группы ПКС или ПКЗ меняются")],
        fields: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "Поля, одинаковые для всех целей: «тег или атрибут → значение» "
                    '(например {"НеЗамещать": true}); стороны ПКС — {"Источник": {"Имя": …, '
                    '"Вид": …, "Тип": …}}. Пустой набор отклоняется'
                )
            ),
        ],
        keys: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Какие правила менять: путь ПКС `группа/…/свойство-приёмник` или имя "
                    "значения источника ПКЗ. При совпадении имён в одном контейнере к звену ПКС "
                    "добавляются квалификаторы `[поиск]` и `#N`. Не задано — все правила этого "
                    "вида у ПКО, кроме except_keys. Неизвестный адрес — ошибка до правок"
                )
            ),
        ] = None,
        except_keys: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Адреса, которые пропустить, когда keys не задан: путь ПКС или имя значения "
                    "ПКЗ. При совпадении имён в одном контейнере к звену ПКС добавляются "
                    "квалификаторы `[поиск]` и `#N`. Неизвестный адрес — ошибка до правок"
                )
            ),
        ] = None,
        source_structure: OptionalStructure = None,
        target_structure: OptionalStructure = None,
    ) -> dict[str, Any]:
        """Меняет переданные поля у нескольких ПКС, групп ПКС или ПКЗ одного ПКО за один вызов,
        всё или ничего."""
        return await call(
            service.rule_update_many,
            project_id,
            kind,
            owner,
            fields,
            keys,
            except_keys,
            source_structure,
            target_structure,
        )

    @server.tool()
    async def rule_delete(
        project_id: ProjectId,
        kind: RuleKind,
        key: RuleKey,
        owner: Owner = "",
        source_structure: OptionalStructure = None,
        target_structure: OptionalStructure = None,
    ) -> dict[str, Any]:
        """Удаляет правило; ПКО, на которое ссылаются, не удаляется.
        Вид conversion отклоняется: чтобы убрать событие, передайте в rule_update пустую строку."""
        return await call(
            service.rule_delete,
            project_id,
            kind,
            key,
            owner,
            source_structure,
            target_structure,
        )

    @server.tool()
    async def pko_create_from_candidates(
        project_id: ProjectId,
        code: Annotated[str, Field(description="Код нового ПКО")],
        source_structure: StructureId,
        target_structure: StructureId,
        source_object: ObjectName,
        target_object: ObjectName,
        fields: Fields = None,
        group: RuleGroup = "",
    ) -> dict[str, Any]:
        """ПКО с ПКС по кандидатам: «точно» и «синоним КД» включены, без пары — выключены."""
        return await call(
            service.pko_create_from_candidates,
            project_id,
            code,
            source_structure,
            target_structure,
            source_object,
            target_object,
            fields,
            group,
        )

    # --- Проверки ---------------------------------------------------------------------------

    @server.tool()
    async def rules_validate(
        project_id: ProjectId,
        source_structure: Annotated[
            str | None,
            Field(description="Структура источника (у правил регистрации — где живёт план обмена)"),
        ] = None,
        target_structure: Annotated[
            str | None, Field(description="Структура приёмника правил обмена")
        ] = None,
        level: Annotated[
            str | None, Field(description="Только «ошибка» или только «предупреждение»")
        ] = None,
        check_prefix: Annotated[
            str | None, Field(description="Префикс идентификатора проверки: format., structure.…")
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Проверяет формат, структуры и ссылки на алгоритмы (или правила регистрации).

        Ответ — итог, невыполненные проверки и замечания постранично.
        """
        return await call(
            service.rules_validate,
            project_id,
            source_structure,
            target_structure,
            level,
            check_prefix,
            offset,
            limit,
        )

    @server.tool()
    async def rules_diff(
        left: Annotated[
            str,
            Field(
                description=(
                    "Левая сторона: идентификатор открытого проекта правил или путь к файлу XML"
                )
            ),
        ],
        right: Annotated[
            str,
            Field(
                description=(
                    "Правая сторона: идентификатор открытого проекта правил или путь к файлу XML"
                )
            ),
        ],
        include_header: Annotated[
            bool,
            Field(description="Сравнивать изменчивые поля заголовка ДатаВремяСоздания и Ид"),
        ] = False,
        order: Annotated[bool, Field(description="Показывать перестановку ПКС и ПКЗ")] = False,
        section: Annotated[
            str | None,
            Field(
                description=(
                    "Раздел: header, pko, pks, pkz, pvd, pod, algorithms, queries, parameters, "
                    "registration; пусто — все"
                )
            ),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Смысловой дифф двух версий правил одного вида: что добавлено, удалено или изменено.

        Сторона — открытый проект или файл. Ответ — сводка по разделам и страница изменений,
        без XML.
        """
        return await call(
            service.rules_diff, left, right, include_header, order, section, offset, limit
        )

    @server.tool()
    async def handlers_export(
        project_id: ProjectId,
        folder: Annotated[
            str, Field(description="Каталог для .bsl-файлов в рабочей папке или rules_dir проекта")
        ],
        limit: Annotated[int, Field(description="Сколько файлов перечислить", ge=1)] = 50,
    ) -> dict[str, Any]:
        """Выносит обработчики и алгоритмы в BSL-обёртки для синтакс-чекера."""
        return await call(service.handlers_export, project_id, folder, limit)

    @server.tool()
    async def handlers_locate(
        project_id: ProjectId,
        file_name: Annotated[str, Field(description="Имя .bsl-файла из handlers_export")],
        line: Annotated[int, Field(description="Номер строки файла (с 1)", ge=1)],
    ) -> dict[str, Any]:
        """Правило, событие и строку внутри обработчика для строки BSL-обёртки."""
        return await call(service.handlers_locate, project_id, file_name, line)

    # --- Регистрация и корреспондент -----------------------------------------------------

    @server.tool()
    async def registration_build(
        structure_id: Annotated[
            str, Field(description="Структура конфигурации, где живёт план обмена")
        ],
        exchange_plan: Annotated[str, Field(description="План обмена: имя или `ПланОбмена.Имя`")],
        rules_project_id: Annotated[
            str | None,
            Field(description="Проект правил обмена: объекты берутся из его ПВД, если objects нет"),
        ] = None,
        objects: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description=(
                    'Явный выбор: [{"metadata_name": "Справочник.X", "name"?, "code"?, '
                    '"unload_mode"?, "plan_filters"?: [{"plan_property", "object_property", '
                    '"property_type", "comparison", "constant"}], "object_filters"?: '
                    '[{"object_property", "property_type", "comparison", '
                    '"constant_value"}]}]; отборы соединяются через «И»'
                )
            ),
        ] = None,
        project_id: Annotated[
            str | None,
            Field(
                description=(
                    "Свой идентификатор нового проекта правил; "
                    "пусто — выводится из источника и вида"
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Правила регистрации из состава плана обмена в новый рабочий проект."""
        return await call(
            service.registration_build,
            structure_id,
            exchange_plan,
            rules_project_id,
            objects,
            project_id,
        )

    @server.tool()
    async def correspondent_draft(
        project_id: ProjectId,
        codes: Annotated[list[str], Field(description="Коды ПКО для зеркалирования")],
        target_structure: Annotated[
            str | None,
            Field(
                description="Структура нового приёмника (источника исходных правил) для проверки"
            ),
        ] = None,
        limit: Annotated[
            int, Field(description="Сколько обработчиков и ПКС перечислить", ge=1)
        ] = 50,
        new_project_id: Annotated[
            str | None,
            Field(
                description=(
                    "Свой идентификатор нового проекта правил; "
                    "пусто — выводится из источника и вида"
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Черновик правил обратного направления; обработчики — «перенести вручную»."""
        return await call(
            service.correspondent_draft,
            project_id,
            codes,
            target_structure,
            limit,
            new_project_id,
        )

    # --- EnterpriseData ----------------------------------------------------------------------

    @server.tool()
    async def ed_open(
        path: Annotated[
            str,
            Field(
                description=(
                    "Путь к модулю менеджера обмена (`…/CommonModules/<Имя>/Ext/Module.bsl`), "
                    "как на машине агента"
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Открывает неизменяемый снимок менеджера EnterpriseData только в памяти.

        Повторное открытие возвращает прежний снимок и source_changed; перечитать — ed_close/open.
        """
        return await call(service.ed_open, path)

    @server.tool()
    async def ed_overview(
        project_id: Annotated[str, Field(description="Идентификатор снимка из ed_open")],
    ) -> dict[str, Any]:
        """Обзор ED: счётчики, покрытие, упоминания версий и сводка диагностик.

        complete означает структурную полноту чтения, а не корректность обмена.
        """
        return await call(service.ed_overview, project_id)

    @server.tool()
    async def ed_list(
        project_id: Annotated[str, Field(description="Идентификатор снимка из ed_open")],
        kind: Annotated[
            str,
            Field(
                description=(
                    "Вид: pko, pks, pktch, pod, pkpd, parameter, algorithm, handler, "
                    "dispatcher, support, unknown, version, diagnostic"
                )
            ),
        ],
        text: Annotated[
            str | None,
            Field(description="Подстрока в имени, адресе и сторонах; без поиска в теле кода"),
        ] = None,
        format_object: Annotated[
            str | None, Field(description="Точное имя объекта формата; для ПКС наследуется от ПКО")
        ] = None,
        metadata_object: Annotated[
            str | None,
            Field(description="Точное имя метаданных (Вид.Имя); для ПКС наследуется от ПКО"),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Страница сущностей ED в порядке исходника; фильтры сторон объединяются AND."""
        return await call(
            service.ed_list, project_id, kind, text, format_object, metadata_object, offset, limit
        )

    @server.tool()
    async def ed_get(
        project_id: Annotated[str, Field(description="Идентификатор снимка из ed_open")],
        address: Annotated[
            str, Field(description="Адрес ED из ed_list; конфликт уточняется суффиксом #1, #2")
        ],
        children_kind: Annotated[
            str | None,
            Field(
                description=(
                    "Вид непосредственных детей; null — все виды. "
                    "reference — ссылки из кода обработчика, алгоритма или события"
                )
            ),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
        include_text: Annotated[
            bool, Field(description="Включить страницу исходного текста сущности")
        ] = False,
        text_offset: Annotated[
            int, Field(description="Смещение текста в символах Unicode", ge=0)
        ] = 0,
        text_limit: Annotated[
            int, Field(description="Размер страницы текста, от 1 до 8000 символов", ge=1, le=8000)
        ] = 2000,
    ) -> dict[str, Any]:
        """Поля и страницы детей, областей, тегов, условий и диагностик сущности ED.

        По умолчанию исходного текста нет. ПКС группы доступны через отдельный get группы.
        """
        return await call(
            service.ed_get,
            project_id,
            address,
            children_kind,
            offset,
            limit,
            include_text,
            text_offset,
            text_limit,
        )

    @server.tool()
    async def ed_locate(
        project_id: Annotated[str, Field(description="Идентификатор снимка из ed_open")],
        line: Annotated[
            int, Field(description="Номер физической строки единственного файла, начиная с 1", ge=1)
        ],
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Классификация строки ED и страница сущностей: внутренняя, предки, связанные правила."""
        return await call(service.ed_locate, project_id, line, offset, limit)

    @server.tool()
    async def ed_validate(
        project_id: Annotated[str, Field(description="Идентификатор снимка из ed_open")],
        level: Annotated[
            str | None, Field(description="Только «ошибка» или только «предупреждение»")
        ] = None,
        check_prefix: Annotated[
            str | None,
            Field(
                description=(
                    "Префикс идентификатора проверки, например ed.handler.; "
                    "пустая строка — без отбора"
                )
            ),
        ] = None,
        address_prefix: Annotated[
            str | None,
            Field(
                description=(
                    "Префикс адреса: запись проходит, если адрес равен префиксу или "
                    "начинается с него и «/» (ПКО/Товар отбирает ПКО/Товар/ПКС/Код "
                    "и не отбирает ПКО/ТоварПрочее). Без учёта регистра; пустая строка — без отбора"
                )
            ),
        ] = None,
        section: Annotated[
            str,
            Field(
                description=(
                    "Раздел: issues (по умолчанию) — страница замечаний и краткая сводка "
                    "пропусков без текстов причин; skipped — страница записей «не проверено» "
                    "без страницы замечаний"
                )
            ),
        ] = "issues",
        offset: Offset = 0,
        limit: Limit = 50,
        schema_id: Annotated[
            str | None,
            Field(
                description="Снимок из ed_schema_open: проверяет имена, пути и значения "
                "в выбранной версии формата"
            ),
        ] = None,
        structure_id: Annotated[
            str | None,
            Field(
                description="Структура из structure_load_project/xml/md83exp: проверяет "
                "объекты, реквизиты и типы конфигурации"
            ),
        ] = None,
        direction: Annotated[
            str,
            Field(
                description="Направление проверки: send, receive или both; both объединяет "
                "замечания двух проходов"
            ),
        ] = "both",
    ) -> dict[str, Any]:
        """Проверяет открытый модуль менеджера: связность, со схемой и структурой — декларации.

        Ошибка связности — обработчик не будет вызван или модуль не скомпилируется;
        ошибка структуры — статическая ссылка на метаданные отсутствует.
        Для проверок формата передайте schema_id; для стороны конфигурации — structure_id.
        Сопоставление типов требует оба снимка. Пропуски — непроверенные группы или
        непрозрачные условия/источники, поэтому отсутствие замечаний не доказывает полноту.
        summary считается до отборов и называет, что не проверено. В разделе issues сводка
        skipped — только числа, без текстов причин; тексты — в разделе skipped.
        Версия и активные пространства имён берутся из открытой схемы. BSL не исполняется.
        """
        return await call(
            service.ed_validate,
            project_id,
            level,
            check_prefix,
            offset,
            limit,
            schema_id,
            structure_id,
            direction,
            section,
            address_prefix,
        )

    @server.tool()
    async def ed_close(
        project_id: Annotated[str, Field(description="Идентификатор снимка из ed_open")],
    ) -> dict[str, Any]:
        """Удаляет снимок ED из памяти; исходный файл остаётся неизменным."""
        return await call(service.ed_close, project_id)

    @server.tool()
    async def ed_schema_open(
        format_version: Annotated[
            str,
            Field(
                description="Версия формата обмена, например `1.8`: сверяется с URI пакета; "
                "это не редакция из имени пакета (`1_8_6`)"
            ),
        ],
        path: Annotated[
            str | None,
            Field(
                description="Путь к пакету XDTO из выгрузки: `XDTOPackages/<имя>/Ext/Package.bin` "
                "или описание `XDTOPackages/<имя>.xml`; вместо project и package"
            ),
        ] = None,
        project: Annotated[
            str | None, Field(description="Проект из project_list; вместе с package, вместо path")
        ] = None,
        configuration: Annotated[
            str, Field(description="Конфигурация проекта из project_list")
        ] = "full",
        package: Annotated[
            str | None,
            Field(
                description="Точное имя пакета XDTO в выгрузке проекта, например "
                "`EnterpriseData_1_8_6`; подходящая версия сама не подбирается"
            ),
        ] = None,
        imports: Annotated[
            dict[str, str] | None,
            Field(
                description="Импорты, которые не нашлись сами: URI пространства имён → путь пакета "
                "(для project импорты ищутся среди пакетов той же конфигурации)"
            ),
        ] = None,
        extensions: Annotated[
            list[str] | None,
            Field(
                description="Пути пакетов расширений формата по порядку; каждый должен "
                "импортировать базовый пакет"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Открывает схему формата обмена — пакет XDTO из выгрузки конфигурации — и возвращает
        schema_id, счётчики и статус (partial — часть типов или импортов не разрешена, подробности
        в diagnostics_summary). Только чтение; файлы XSD не поддерживаются."""
        return await call(
            service.ed_schema_open,
            format_version,
            path,
            project,
            configuration,
            package,
            imports,
            extensions,
        )

    @server.tool()
    async def ed_schema_types(
        schema_id: Annotated[str, Field(description="Идентификатор ed_schema_open")],
        namespace: Annotated[str | None, Field(description="Точный URI типов")] = None,
        kind: Annotated[str | None, Field(description="Вид типа: object или value")] = None,
        text: Annotated[
            str | None, Field(description="Подстрока имени типа без учёта регистра")
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Страница именованных типов схемы: объекты формата (`Справочник.…`, `Документ.…`),
        ключевые свойства, перечисления; отбор по URI, виду и подстроке имени."""
        return await call(service.ed_schema_types, schema_id, namespace, kind, text, offset, limit)

    @server.tool()
    async def ed_schema_type(
        schema_id: Annotated[str, Field(description="Идентификатор ed_schema_open")],
        qname: Annotated[
            str,
            Field(
                description="Имя типа: `Справочник.Валюты` (ищется в базовом пакете и "
                "расширениях), полное `{URI}Имя` из ed_schema_types или идентификатор "
                "локального типа"
            ),
        ],
        section: Annotated[
            str, Field(description="Раздел: properties, values или facets")
        ] = "properties",
        offset: Offset = 0,
        limit: Limit = 50,
        include_origin: Annotated[
            bool,
            Field(
                description="Добавить происхождение типа, свойств и общий граф страницы; "
                "по умолчанию они опущены"
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Один тип схемы: свойства (вместе с развёрнутыми ключевыми и общими свойствами — путь в
        XML и имя, под которым свойство пишут в правиле), значения перечисления или фасеты;
        у свойства — тип, границы, признаки ссылки и табличной части.
        Происхождение добавляется только при include_origin=true."""
        return await call(
            service.ed_schema_type, schema_id, qname, section, offset, limit, include_origin
        )

    @server.tool()
    async def ed_schema_close(
        schema_id: Annotated[str, Field(description="Идентификатор ed_schema_open")],
    ) -> dict[str, Any]:
        """Удаляет снимок схемы из памяти; повторное закрытие возвращает closed=false."""
        return await call(service.ed_schema_close, schema_id)

    # --- Маршруты EnterpriseData -------------------------------------------------------------

    @server.tool()
    async def ed_routes(
        project: Annotated[
            str | None,
            Field(description="Проект из project_list; вместо path и profile_id"),
        ] = None,
        configuration: Annotated[
            str,
            Field(
                description="Конфигурация проекта; выбирает основную выгрузку, расширения не "
                "накладываются. С path и profile_id допустимо только значение full"
            ),
        ] = "full",
        path: Annotated[
            str | None,
            Field(
                description="Путь к корню XML-выгрузки конфигурации (каталог с Configuration.xml), "
                "как на машине агента; вместо project и profile_id"
            ),
        ] = None,
        profile_id: Annotated[
            str | None,
            Field(
                description="Снимок из ed_routes; вместо project и path, без force. "
                "После вытеснения или перезапуска откройте выгрузку снова"
            ),
        ] = None,
        section: Annotated[
            str,
            Field(
                description="Раздел: summary, plans, versions, variants, packages или skipped. "
                "summary — шапка снимка; остальные — страница"
            ),
        ] = "summary",
        plan: Annotated[
            str | None,
            Field(
                description="Имя плана обмена для страниц plans, versions и variants, без учёта "
                "регистра; в ответе — написание из конфигурации. Без имени страница не сужается "
                "до одного плана"
            ),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
        force: Annotated[
            bool,
            Field(
                description="Прочитать выгрузку заново. Прежний идентификатор сохраняется, пока "
                "его не вытеснит давность; при тех же байтах идентификатор не меняется"
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Читает маршруты версий формата одной основной выгрузки.

        Ровно один источник: project, path или profile_id. Снимок только в памяти.
        Повтор без force возвращает прежний и помечает stale, если файлы изменились.
        """
        return await call(
            service.ed_routes,
            project,
            configuration,
            path,
            profile_id,
            section,
            plan,
            offset,
            limit,
            force,
        )

    @server.tool()
    async def ed_route_compare(
        left_profile_id: Annotated[str, Field(description="Левый снимок из ed_routes")],
        right_profile_id: Annotated[str, Field(description="Правый снимок из ed_routes")],
        left_plan: Annotated[
            str | None,
            Field(
                description="Имя плана слева, без учёта регистра. Обязательно, если планов "
                "через универсальный формат несколько; в ответе — написание из конфигурации"
            ),
        ] = None,
        right_plan: Annotated[
            str | None,
            Field(description="Имя плана справа, те же правила, что у left_plan"),
        ] = None,
        context: Annotated[
            str,
            Field(
                description="plan — карта узла; without_node — глобальная карта, "
                "планы не задаются, URI сообщения неизвестен"
            ),
        ] = "plan",
        section: Annotated[
            str,
            Field(
                description="Страница: issues, versions, schema_diff или skipped. "
                "summary и пропуски относятся ко всему отчёту"
            ),
        ] = "issues",
        level: Annotated[
            str | None,
            Field(description="Отбор страницы issues: «ошибка» или «предупреждение»"),
        ] = None,
        check_prefix: Annotated[
            str | None,
            Field(
                description="Префикс идентификатора проверки для страницы issues, например "
                "ed.route. Неизвестный префикс даёт пустую страницу, итог не меняется"
            ),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Сравнивает два снимка маршрутов и возвращает статическую совместимость.

        Планы укажите явно, если их несколько. Поля ed_open и ed_schema_open — готовые
        аргументы, когда путь или пакет определён однозначно; иначе null и причина.
        """
        return await call(
            service.ed_route_compare,
            left_profile_id,
            right_profile_id,
            left_plan,
            right_plan,
            context,
            section,
            level,
            check_prefix,
            offset,
            limit,
        )

    return server


def _log_call(name: str, started: float, outcome: str) -> None:
    """Строка INFO: имя инструмента, длительность в миллисекундах, итог."""
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info("%s %d мс %s", name, elapsed_ms, outcome)


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _configure_logging() -> None:
    """Уровень — `KD2_LOG_LEVEL` (имя уровня `logging`, по умолчанию INFO).

    Неизвестное значение не останавливает запуск: уровень INFO и предупреждение в лог.
    """
    raw = os.environ.get("KD2_LOG_LEVEL", "INFO").strip()
    level = logging.getLevelNamesMapping().get(raw.upper())
    if level is None:
        logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
        logging.getLogger().setLevel(logging.INFO)
        logger.warning("Неизвестное значение KD2_LOG_LEVEL «%s», используется INFO", raw)
        return
    logging.basicConfig(level=level, format=_LOG_FORMAT)
    logging.getLogger().setLevel(level)


class _BearerTokenMiddleware:
    """Проверяет `Authorization: Bearer` на `/mcp`.

    Свой ASGI-вызов, без `BaseHTTPMiddleware`: тот буферизует тело и ломает поток
    streamable HTTP. Сравнение токена — `hmac.compare_digest`.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = b"Bearer " + token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") == "http"
            and _is_mcp_path(scope)
            and not _bearer_ok(scope, self._expected)
        ):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _is_mcp_path(scope: Scope) -> bool:
    path = scope.get("path", "")
    return isinstance(path, str) and path.rstrip("/") == "/mcp"


def _bearer_ok(scope: Scope, expected: bytes) -> bool:
    presented = b""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            presented = bytes(value)
            break
    return hmac.compare_digest(presented, expected)


def create_app(
    service: Kd2Service, token: str | None = None, *, host: str = "127.0.0.1"
) -> Starlette:
    """ASGI-приложение MCP (`/mcp`).

    `host` передаётся в `streamable_http_app`, как это делает `MCPServer.run`: для петли
    SDK включает защиту от DNS rebinding. При заданном токене запрос к `/mcp` без
    `Authorization: Bearer <token>` отвечает 401.
    """
    app = create_server(service).streamable_http_app(streamable_http_path="/mcp", host=host)
    if token:
        app.add_middleware(_BearerTokenMiddleware, token=token)
    return app


def main() -> None:
    """Запуск сервера по HTTP; настройки — переменные окружения `KD2_*`.

    Уровень лога — `KD2_LOG_LEVEL` (имя уровня `logging`, по умолчанию `INFO`).
    Неизвестное значение не останавливает запуск: остаётся `INFO`, в лог пишется
    предупреждение. `create_server` логирование не настраивает.

    Токен `KD2_TOKEN` (если задан) проверяется на `/mcp`: заголовок
    `Authorization: Bearer`. Хост не петлевой (петля — `127.0.0.1`, `localhost`, `::1`)
    без токена — отказ при старте. Исключение — контейнер. В образе `KD2_HOST=0.0.0.0`:
    это адрес внутри контейнера, наружу порт публикует compose, а не процесс.
    Dockerfile ставит `KD2_IN_CONTAINER=1`. Пока эта переменная задана, проверка
    «хост вне петли без токена» молчит — иначе контейнер не стартовал бы никогда.
    Снаружи защиту даёт публикация порта: по умолчанию `127.0.0.1`, для команды —
    `bind` в `projects.local.yaml`.
    """
    _configure_logging()
    settings = Settings.from_env()
    if (
        "KD2_IN_CONTAINER" not in os.environ
        and not is_loopback_host(settings.host)
        and not settings.token
    ):
        raise SystemExit(
            f"KD2_HOST «{settings.host}» вне петлевого интерфейса, а KD2_TOKEN не задан. "
            "Для сервера на внешнем интерфейсе задайте token."
        )
    uvicorn.run(
        create_app(Kd2Service(settings), settings.token, host=settings.host),
        host=settings.host,
        port=settings.port,
    )
