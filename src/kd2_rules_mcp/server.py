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
import inspect
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
from mcp.types import Tool as MCPTool
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from kd2_rules_mcp.errors import (
    AmbiguousAddressError,
    DanglingReferenceError,
    DuplicateProjectError,
    DuplicateRuleError,
    EdAuthoringAckRequiredError,
    EdAuthoringIoError,
    EdAuthoringPathError,
    EdAuthoringPreconditionError,
    EdAuthoringResourceLimitError,
    EdAuthoringStaleError,
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
    RegistrationDeliveryError,
    RegistrationRetargetError,
    RegistrationToolError,
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
    (RegistrationToolError, "registration.precondition"),
    (RegistrationDeliveryError, "registration.delivery"),
    (RegistrationRetargetError, "registration.retarget"),
    (EdAuthoringAckRequiredError, "ed_authoring_ack_required"),
    (EdAuthoringStaleError, "ed_authoring_stale"),
    (EdAuthoringPathError, "ed_authoring_path"),
    (EdAuthoringResourceLimitError, "ed_authoring_resource_limit"),
    (EdAuthoringIoError, "ed_authoring_io"),
    (EdAuthoringPreconditionError, "ed_authoring_precondition"),
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

INSTRUCTIONS = """KD 2 exchange rules (ПравилаОбмена 2.01) and registration rules
(ПравилаРегистрации).
ПКО (object conversion rule), ПКС (property conversion rule), ПВД (data export rule),
ПОД (data cleanup rule in KD 2; data processing rule in ED), ПКПД (predefined data conversion
rule), ПКЗ (value conversion rule), ПРО (object registration rule).
Load configuration structures: project_list → structure_load_project, or
structure_load_xml/structure_load_md83exp. Open/create rules → match_* → rule_* or
pko_create_from_candidates → rules_validate → handlers_export for a syntax checker →
rules_save to workspace or project rules_dir. Compare versions with rules_diff.
rules_close removes the snapshot, keeping saved files. Use registration_build for registration
rules and correspondent_draft for reverse rules. The agent makes semantic decisions.
Retarget registration: registration_retarget preview → write with hash and notice IDs.
Lists use offset, limit≤200, has_more. Tool errors are JSON with code.
ED: ed_open → ed_overview → ed_list/get/locate → ed_validate. Reread with ed_close/open.
Managers: ed_create → ed_apply preview/apply → ed_validate → ed_authoring_build.
Manager projects persist; ed_close removes the project, keeping kits.
Layers: path + configuration_path + ordered extensions, or
project + module; extensions=null uses project settings, [] disables extensions.
ed_list/get return effective rules and origins by direction/headers_only; Слой/<id>/… selects
a source revision. ed_validate checks contexts and ed.layer.*; supply route_profile_id for
layer routes. Unknown code is skipped. Extension activation/order in the infobase is unverified.
XDTO: ed_schema_open with an explicit version → ed_schema_types/type; snapshots stay in memory,
reread with ed_schema_close/open. XSD is unsupported.
Routes: ed_routes for two dumps → ed_route_compare.
Direct header ПКС: snapshots → ed_authoring_candidates → ed_authoring_build preview → write
with hash and acknowledgements. A human installs the kit. Details: docs/tools.md."""

StructureId = Annotated[str, Field(description="Cached structure ID")]
ProjectId = Annotated[str, Field(description="Rules project ID")]
Offset = Annotated[int, Field(description="Page offset", ge=0)]
Limit = Annotated[int, Field(description="Page size", ge=1, le=200)]
ObjectName = Annotated[
    str,
    Field(description=("Вид.Имя or KD type name")),
]
RuleKind = Annotated[
    str,
    Field(
        description="Kind: pko, pks, pks_group, pkz, pvd, pod, algorithm, query, "
        "parameter, conversion, pro"
    ),
]
RuleKey = Annotated[
    str,
    Field(
        description=(
            "ПКО/ПВД/ПОД: code; algorithm/query/parameter: name; ПКС: group/…/target "
            "(source if empty, parameter name for parameter ПКС; [поиск], source→target, #N "
            "disambiguate); pks_group: group code path; ПКЗ: source value; "
            "ПРО: metadata object or code; conversion: empty or Конвертация"
        )
    ),
]
Owner = Annotated[str, Field(description="Owner ПКО code for pks/pks_group/pkz; otherwise empty")]
RuleGroup = Annotated[
    str,
    Field(description=("Group codes joined by /; empty = root")),
]
Fields = Annotated[
    dict[str, Any] | None,
    Field(description=("Tag/attribute → value map")),
]
OptionalStructure = Annotated[
    str | None,
    Field(description="Validation structure"),
]

ConfidenceFilter = Annotated[
    str | None,
    Field(description="Confidence: точно, синоним КД, по синониму, нет пары"),
]


def error_payload(error: Exception, service: Kd2Service | None = None) -> dict[str, Any]:
    """JSON ошибки инструмента: код, текст и сведения для выбора действия."""
    code = next((code for kind, code in ERROR_CODES if isinstance(error, kind)), "internal")
    payload: dict[str, Any] = {"code": code, "message": str(error)}
    if isinstance(
        error, (RegistrationRetargetError, RegistrationDeliveryError, RegistrationToolError)
    ):
        payload["code"] = error.code
    if isinstance(error, RegistrationToolError):
        payload.update(error.details)
    if isinstance(error, EdAuthoringPreconditionError):
        payload.update(error.details)
    if isinstance(error, (EdSchemaNotFoundError, StructureNotFoundError)):
        payload.update(getattr(error, "reopen_details", {}))
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


def _without_schema_titles(schema: dict[str, Any]) -> dict[str, Any]:
    """Убирает заголовки схем, сохраняя имена полей и содержимое значений/примеров."""
    result = {key: value for key, value in schema.items() if key != "title"}
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        value = result.get(key)
        if isinstance(value, dict):
            result[key] = {
                name: _without_schema_titles(child) if isinstance(child, dict) else child
                for name, child in value.items()
            }
    for key in (
        "items",
        "additionalItems",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
        "allOf",
        "anyOf",
        "oneOf",
        "prefixItems",
    ):
        value = result.get(key)
        if isinstance(value, dict):
            result[key] = _without_schema_titles(value)
        elif isinstance(value, list):
            result[key] = [
                _without_schema_titles(child) if isinstance(child, dict) else child
                for child in value
            ]
    return result


class _SchemaServer(MCPServer):
    """Компактные схемы выдачи; модели валидации SDK остаются исходными."""

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        return [
            tool.model_copy(
                update={
                    "description": inspect.cleandoc(tool.description or ""),
                    "input_schema": _without_schema_titles(tool.input_schema),
                    "output_schema": (
                        _without_schema_titles(tool.output_schema)
                        if tool.output_schema is not None
                        else None
                    ),
                }
            )
            for tool in tools
        ]


def create_server(service: Kd2Service) -> MCPServer:
    """Сервер MCP с инструментами поверх `service`."""
    server = _SchemaServer("kd2-rules-mcp", instructions=INSTRUCTIONS)

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
        """List 1C projects, configurations, infobases, MCP names and exchange plans. Call before
        structure loading or choosing writable rules_dir. Login is a boolean."""
        return await call(service.project_list)

    @server.tool()
    async def structure_load_project(
        project: Annotated[str, Field(description="Project from project_list")],
        configuration: Annotated[str, Field(description="Project configuration")] = "full",
        structure_id: Annotated[
            str | None,
            Field(description="New ID; default <project>-<configuration>"),
        ] = None,
        force: Annotated[
            bool,
            Field(description="Refresh manually edited XML"),
        ] = False,
    ) -> dict[str, Any]:
        """Load configured project metadata and extensions. Reuse unchanged caches; use force
        after manual XML edits."""
        return await call(
            service.structure_load_project, project, configuration, structure_id, force
        )

    @server.tool()
    async def structure_list() -> dict[str, Any]:
        """List cached structures, configuration names, versions and extensions. IDs are used
        for queries and matching."""
        return await call(service.structure_list)

    @server.tool()
    async def structure_load_xml(
        structure_id: Annotated[str, Field(description="New ID: A-Z, a-z, 0-9, _.-")],
        configuration_path: Annotated[
            str, Field(description="XML dump root with Configuration.xml")
        ],
        extension_paths: Annotated[
            list[str] | None,
            Field(description="Ordered extension dump roots"),
        ] = None,
        force: Annotated[
            bool,
            Field(description="Refresh manually edited XML"),
        ] = False,
    ) -> dict[str, Any]:
        """Load XML metadata with explicit extensions. Unchanged inputs reuse the cache; manual
        edits require force."""
        return await call(
            service.structure_load_xml,
            structure_id,
            configuration_path,
            extension_paths or [],
            force,
        )

    @server.tool()
    async def structure_load_md83exp(
        structure_id: Annotated[str, Field(description="New ID: A-Z, a-z, 0-9, _.-")],
        path: Annotated[str, Field(description="MD83Exp XML file")],
        force: Annotated[
            bool,
            Field(description="Refresh manually edited XML"),
        ] = False,
    ) -> dict[str, Any]:
        """Load an MD83Exp structure when configuration sources are unavailable. Query the
        returned structure ID."""
        return await call(service.structure_load_md83exp, structure_id, path, force)

    @server.tool()
    async def structure_objects(
        structure_id: StructureId,
        kind: Annotated[str | None, Field(description="Kind: Справочник, Документ, etc.")] = None,
        text: Annotated[str | None, Field(description="Name/synonym substring")] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """List metadata objects by kind, name or synonym. Expand typed properties with
        structure_object."""
        return await call(service.structure_objects, structure_id, kind, text, offset, limit)

    @server.tool()
    async def structure_object(
        structure_id: StructureId, name: ObjectName, offset: Offset = 0, limit: Limit = 50
    ) -> dict[str, Any]:
        """Read an object and paged typed properties, tabular sections and dimensions. Values:
        structure_values."""
        return await call(service.structure_object, structure_id, name, offset, limit)

    @server.tool()
    async def structure_values(
        structure_id: StructureId, name: ObjectName, offset: Offset = 0, limit: Limit = 50
    ) -> dict[str, Any]:
        """List enumeration values or predefined items. Use match_values for conversion
        candidates."""
        return await call(service.structure_values, structure_id, name, offset, limit)

    @server.tool()
    async def structure_plan_content(
        structure_id: StructureId,
        exchange_plan: Annotated[str, Field(description="Plan name or ПланОбмена.Имя")],
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """List exchange plan objects and automatic registration flags to choose registration
        objects. Live node settings are unknown."""
        return await call(
            service.structure_plan_content, structure_id, exchange_plan, offset, limit
        )

    @server.tool()
    async def structure_compare(
        old_structure: StructureId,
        new_structure: StructureId,
        limit: Annotated[int, Field(description="Maximum items per list", ge=1)] = 50,
    ) -> dict[str, Any]:
        """Compare metadata objects, properties and values. Rule project/XML changes use
        rules_diff."""
        return await call(service.structure_compare, old_structure, new_structure, limit)

    # --- Кандидаты -----------------------------------------------------------------------

    @server.tool()
    async def match_objects(
        source_structure: StructureId,
        target_structure: StructureId,
        kind: Annotated[str | None, Field(description="Object kind, e.g. Справочник")] = None,
        confidence: ConfidenceFilter = None,
        text: Annotated[
            str | None, Field(description="Either side's name/synonym substring")
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Return ПКО candidates by name/kind plus suggestions; narrow large results with
        text/kind. Apply auto=false or по синониму pairs only after an agent decision."""
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
        rules_project_id: Annotated[
            str | None,
            Field(description=("Rules project for ПКС coverage")),
        ] = None,
        code: Annotated[
            str | None,
            Field(description="Coverage ПКО code"),
        ] = None,
        uncovered: Annotated[
            bool,
            Field(description="Only uncovered pairs"),
        ] = False,
    ) -> dict[str, Any]:
        """Find ПКС candidates; auto=false needs an agent decision. Existing coverage requires
        rules_project_id and code."""
        return await call(
            service.match_properties,
            source_structure,
            target_structure,
            source_object,
            target_object,
            confidence,
            offset,
            limit,
            rules_project_id,
            code,
            uncovered,
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
        """Find enumeration/predefined ПКЗ candidates. Apply auto=false only after an agent
        decision."""
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
        path: Annotated[str, Field(description="Exchange/registration rules XML")],
        private: Annotated[
            bool,
            Field(description=("Isolate edits from other agents; always create a new project")),
        ] = False,
    ) -> dict[str, Any]:
        """Open rules XML; reuse the path's shared project. private=true creates an isolated
        project for independent edits."""
        return await call(service.rules_open, path, private)

    @server.tool()
    async def rules_create(
        source_structure: StructureId,
        target_structure: StructureId,
        project_id: Annotated[
            str | None,
            Field(description=("New ID; default new-<source>-<target>")),
        ] = None,
    ) -> dict[str, Any]:
        """Create empty rules with a KD-style header from two structures. Add explicit rules
        through rule_create or pko_create_from_candidates."""
        return await call(service.rules_create, source_structure, target_structure, project_id)

    @server.tool()
    async def rules_projects() -> dict[str, Any]:
        """List open rules projects with summaries. Workspace snapshots survive server restart;
        rules_close removes a snapshot."""
        return await call(service.rules_projects)

    @server.tool()
    async def rules_overview(project_id: ProjectId) -> dict[str, Any]:
        """Read counts, groups, configurations and paths; conversion counts filled events.
        Details: rules_list/get."""
        return await call(service.rules_overview, project_id)

    @server.tool()
    async def rules_list(
        project_id: ProjectId,
        section: Annotated[
            str,
            Field(
                description=(
                    "Section: pko, pvd, pod, algorithms, queries, parameters, pks, "
                    "conversion, registration"
                )
            ),
        ],
        text: Annotated[
            str | None,
            Field(description=("Row substring; pks: source/target")),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """List rule summaries and addresses. pks spans all ПКО; conversion is one row.
        Registration projects expose registration."""
        return await call(service.rules_list, project_id, section, text, offset, limit)

    @server.tool()
    async def rules_get(
        project_id: ProjectId,
        kind: RuleKind,
        key: RuleKey,
        owner: Owner = "",
        limit: Annotated[int, Field(description="Maximum ПКС/ПКЗ items", ge=1)] = 100,
    ) -> dict[str, Any]:
        """Read fields/children or conversion header/events. ПКС flags: handlers,
        transfer parameter, incoming-data use. Code: handlers_export. Text/children truncate;
        addresses support edits."""
        return await call(service.rules_get, project_id, kind, key, owner, limit)

    @server.tool()
    async def rules_save(
        project_id: ProjectId,
        path: Annotated[
            str,
            Field(description=("Workspace path or absolute rules_dir path")),
        ],
        overwrite: Annotated[bool, Field(description="Replace existing file")] = False,
    ) -> dict[str, Any]:
        """Save XML to workspace/rules_dir with format checks. Preserve unchanged line endings;
        validate rules and syntax before delivery."""
        return await call(service.rules_save, project_id, path, overwrite)

    @server.tool()
    async def rules_close(project_id: ProjectId) -> dict[str, Any]:
        """Close a project and delete its snapshot; saved files remain. Save pending changes
        first."""
        return await call(service.rules_close, project_id)

    @server.tool()
    async def rules_pack(
        folder: Annotated[
            str,
            Field(description=("Kit directory or explicit file paths")),
        ] = "",
        exchange_rules: Annotated[str, Field(description="Exchange rules file")] = "",
        correspondent_rules: Annotated[
            str, Field(description="Correspondent exchange rules file")
        ] = "",
        registration_rules: Annotated[
            str, Field(description="Optional registration rules file")
        ] = "",
        path: Annotated[
            str,
            Field(description=("ZIP path; default kit/file-stem.zip in workspace")),
        ] = "",
        overwrite: Annotated[bool, Field(description="Replace existing ZIP")] = False,
    ) -> dict[str, Any]:
        """Pack a БСП ZIP from a folder or files, preserving bytes and checking kinds. Inspect
        warnings for rule/registration mismatches."""
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
        """Create a rule; reject missing objects/dangling ПКО links. Infer ПКС Код/Порядок.
        Conversion events use rule_update."""
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
        """Update supplied fields atomically. Conversion accepts event code only; empty code
        deletes an event."""
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
            Field(description=("Child kind: pks, pks_group, pkz")),
        ],
        owner: Annotated[str, Field(description="Owner ПКО code")],
        fields: Annotated[
            dict[str, Any],
            Field(description=("Nonempty field map for all targets")),
        ],
        keys: Annotated[
            list[str] | None,
            Field(description=("Rule addresses; null = all except except_keys")),
        ] = None,
        except_keys: Annotated[
            list[str] | None,
            Field(description=("Addresses skipped when keys is null")),
        ] = None,
        source_structure: OptionalStructure = None,
        target_structure: OptionalStructure = None,
    ) -> dict[str, Any]:
        """Update identical fields in pks/pks_group/pkz children of one ПКО atomically. Unknown
        addresses or invalid edits reject the batch."""
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
        """Delete a rule after removing its ПКО references. Conversion events are deleted via
        rule_update with empty code."""
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
        code: Annotated[str, Field(description="New ПКО code")],
        source_structure: StructureId,
        target_structure: StructureId,
        source_object: ObjectName,
        target_object: ObjectName,
        fields: Fields = None,
        group: RuleGroup = "",
    ) -> dict[str, Any]:
        """Create ПКО/ПКС from auto=true точно/синоним КД pairs; disable unmatched targets.
        auto=false requires an agent decision."""
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
            Field(description=("Source or registration plan structure")),
        ] = None,
        target_structure: Annotated[
            str | None, Field(description="Exchange target structure")
        ] = None,
        level: Annotated[
            str | None,
            Field(description=("Severity: ошибка, предупреждение, error, warning")),
        ] = None,
        check_prefix: Annotated[str | None, Field(description="Check ID prefix")] = None,
        offset: Offset = 0,
        limit: Limit = 50,
        exchange_project_id: Annotated[
            str | None,
            Field(description=("Exchange project for registration ПВД coverage")),
        ] = None,
    ) -> dict[str, Any]:
        """Validate rules against structures; inspect skipped checks. Registration uses
        source_structure for the plan and exchange_project_id for ПВД coverage."""
        return await call(
            service.rules_validate,
            project_id,
            source_structure,
            target_structure,
            level,
            check_prefix,
            offset,
            limit,
            exchange_project_id,
        )

    @server.tool()
    async def rules_diff(
        left: Annotated[
            str,
            Field(description=("Left project ID or XML path")),
        ],
        right: Annotated[
            str,
            Field(description=("Right project ID or XML path")),
        ],
        include_header: Annotated[
            bool,
            Field(description="Compare ДатаВремяСоздания and Ид"),
        ] = False,
        order: Annotated[bool, Field(description="Include ПКС/ПКЗ reordering")] = False,
        section: Annotated[
            str | None,
            Field(
                description=(
                    "Section: header, pko, pks, pkz, pvd, pod, algorithms, queries, "
                    "parameters, registration"
                )
            ),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
        detail: Annotated[
            str | None,
            Field(description=("Detail: full/полный, brief/кратко/краткий")),
        ] = None,
    ) -> dict[str, Any]:
        """Compare same-kind rules by address. Counts and paged changes; full includes
        field/handler differences."""
        return await call(
            service.rules_diff,
            left,
            right,
            include_header,
            order,
            section,
            offset,
            limit,
            detail,
        )

    @server.tool()
    async def handlers_export(
        project_id: ProjectId,
        folder: Annotated[str, Field(description="BSL directory in workspace/rules_dir")],
        limit: Annotated[int, Field(description="Maximum files listed", ge=1)] = 50,
    ) -> dict[str, Any]:
        """Export BSL wrappers for a separate syntax checker. Map reported lines back to rules
        with handlers_locate."""
        return await call(service.handlers_export, project_id, folder, limit)

    @server.tool()
    async def handlers_locate(
        project_id: ProjectId,
        file_name: Annotated[str, Field(description="BSL filename from handlers_export")],
        line: Annotated[int, Field(description="File line, starting at 1", ge=1)],
    ) -> dict[str, Any]:
        """Map an exported syntax-checker wrapper line to rule/event/handler. Use the filename
        from handlers_export."""
        return await call(service.handlers_locate, project_id, file_name, line)

    # --- Регистрация и корреспондент -----------------------------------------------------

    @server.tool()
    async def registration_build(
        structure_id: Annotated[str, Field(description="Configuration structure ID")],
        exchange_plan: Annotated[str, Field(description="Plan name or ПланОбмена.Имя")],
        rules_project_id: Annotated[
            str | None,
            Field(description=("Exchange project for ПВД object selection")),
        ] = None,
        objects: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description=("Objects and nested plan/object filters; DTO shapes: docs/tools.md")
            ),
        ] = None,
        project_id: Annotated[
            str | None,
            Field(description=("New or existing registration project ID")),
        ] = None,
    ) -> dict[str, Any]:
        """Build registration from plan content, objects or ПВД. Existing projects replace
        selected filters only. Inspect warnings/losses for missing attributes or handlers."""
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
        codes: Annotated[list[str], Field(description="ПКО codes to mirror")],
        target_structure: Annotated[
            str | None,
            Field(description="Reverse target structure"),
        ] = None,
        limit: Annotated[
            int,
            Field(description="Maximum handlers, disabled ПКС and notes", ge=1),
        ] = 50,
        new_project_id: Annotated[
            str | None,
            Field(description=("New ID; default corr-<project_id>")),
        ] = None,
    ) -> dict[str, Any]:
        """Draft reverse ПКО; transfer handlers manually. Missing reverse metadata disables
        rules; unresolved ПКО links are cleared and reported."""
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
            str | None,
            Field(description=("Manager BSL path on agent machine")),
        ] = None,
        configuration_path: Annotated[
            str | None,
            Field(description=("Base XML root; required with path + extensions")),
        ] = None,
        extensions: Annotated[
            list[str] | None,
            Field(description=("Ordered roots; null = settings, [] = none")),
        ] = None,
        project: Annotated[
            str | None,
            Field(description=("Project instead of path, with module")),
        ] = None,
        module: Annotated[str | None, Field(description="Exact manager common module name")] = None,
        configuration: Annotated[str, Field(description="Project configuration")] = "full",
    ) -> dict[str, Any]:
        """Open a read-only ED module from path or project/module and ordered layers. Changed
        sources require ed_close/open; registration managers are unsupported."""
        return await call(
            service.ed_open, path, configuration_path, extensions, project, module, configuration
        )

    @server.tool()
    async def ed_overview(
        project_id: Annotated[str, Field(description="ED document/snapshot ID")],
    ) -> dict[str, Any]:
        """Return ED counts, coverage, versions and diagnostics. Complete coverage proves static
        declarations/bindings were read, not handler behavior."""
        return await call(service.ed_overview, project_id)

    @server.tool()
    async def ed_list(
        project_id: Annotated[str, Field(description="ED document/snapshot ID")],
        kind: Annotated[
            str,
            Field(
                description=(
                    "Kind: pko, pks, pktch, pod, pkpd, parameter, algorithm, handler, "
                    "dispatcher, support, unknown, version, diagnostic, layer, change, "
                    "hook, layer_unknown"
                )
            ),
        ],
        text: Annotated[
            str | None,
            Field(description="Name/address/side substring"),
        ] = None,
        format_object: Annotated[str | None, Field(description="Exact format object name")] = None,
        metadata_object: Annotated[
            str | None,
            Field(description="Exact metadata name (Вид.Имя)"),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
        direction: Annotated[
            str | None,
            Field(description="Layer direction: send, receive; null = union"),
        ] = None,
        headers_only: Annotated[
            bool, Field(description="Header-only context (interface 3)")
        ] = False,
        layer: Annotated[str | None, Field(description="Layer ID")] = None,
        entity_id: Annotated[
            str | None,
            Field(description="History logical_id; kind=change only"),
        ] = None,
        name_filter: Annotated[str | None, Field(description="Name substring")] = None,
    ) -> dict[str, Any]:
        """List ED source/effective entities with AND filters. kind=change gives history."""
        return await call(
            service.ed_list,
            project_id,
            kind,
            text,
            format_object,
            metadata_object,
            offset,
            limit,
            direction,
            headers_only,
            layer,
            entity_id,
            name_filter,
        )

    @server.tool()
    async def ed_get(
        project_id: Annotated[str, Field(description="ED document/snapshot ID")],
        address: Annotated[
            str, Field(description="ED address; Код/name also accepts handler methods")
        ],
        children_kind: Annotated[
            str | None,
            Field(description=("Child kind; null = all, reference = code links")),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
        include_text: Annotated[bool, Field(description="Include source text page")] = False,
        text_offset: Annotated[int, Field(description="Unicode character offset", ge=0)] = 0,
        text_limit: Annotated[
            int, Field(description="Unicode character page size", ge=1, le=8000)
        ] = 2000,
        direction: Annotated[
            str | None,
            Field(description=("Layer direction: send, receive; null = variants")),
        ] = None,
        headers_only: Annotated[
            bool, Field(description="Header-only context (interface 3)")
        ] = False,
    ) -> dict[str, Any]:
        """Read ED fields, paged children/diagnostics and optional text. Layers select effective
        direction/headers_only or source revision Слой/<id>/…."""
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
            direction,
            headers_only,
        )

    @server.tool()
    async def ed_locate(
        project_id: Annotated[str, Field(description="ED document/snapshot ID")],
        line: Annotated[int, Field(description="Selected file line, starting at 1", ge=1)],
        offset: Offset = 0,
        limit: Limit = 50,
        file_id: Annotated[
            str | None,
            Field(description="Source file ID; null = base"),
        ] = None,
    ) -> dict[str, Any]:
        """Locate a source line: entity, ancestors, associated rules. file_id selects the file."""
        return await call(service.ed_locate, project_id, line, offset, limit, file_id)

    @server.tool()
    async def ed_validate(
        project_id: Annotated[str, Field(description="ED document/snapshot ID")],
        level: Annotated[
            str | None, Field(description="Severity: ошибка, предупреждение or info")
        ] = None,
        check_prefix: Annotated[
            str | None,
            Field(description=("Check ID prefix")),
        ] = None,
        address_prefix: Annotated[
            str | None,
            Field(description=("Address prefix at `/` boundary, case-insensitive")),
        ] = None,
        section: Annotated[
            str,
            Field(description=("Section: issues or skipped")),
        ] = "issues",
        offset: Offset = 0,
        limit: Limit = 50,
        schema_id: Annotated[
            str | None,
            Field(description=("Format schema snapshot ID")),
        ] = None,
        structure_id: Annotated[
            str | None,
            Field(description=("Configuration structure ID")),
        ] = None,
        direction: Annotated[
            str | None,
            Field(description=("Direction: send, receive, both; null = both")),
        ] = "both",
        headers_only: Annotated[
            bool,
            Field(description="Header-only context (interface 3)"),
        ] = False,
        route_profile_id: Annotated[
            str | None,
            Field(description=("Matching layer route profile ID")),
        ] = None,
    ) -> dict[str, Any]:
        """Validate ED links; schema_id/structure_id add types. Managers add ed.writer.* and
        ed.handler.unknown_name. Inspect skipped; runtime unverified."""
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
            headers_only,
            route_profile_id,
        )

    @server.tool()
    async def ed_close(
        project_id: Annotated[str, Field(description="ED snapshot or manager project ID")],
    ) -> dict[str, Any]:
        """Close an ED snapshot or delete a durable manager project snapshot. Source files and
        published kits remain. Reopen read-only files with ed_open."""
        return await call(service.ed_close, project_id)

    @server.tool()
    async def ed_schema_open(
        format_version: Annotated[
            str,
            Field(description=("Format URI version, e.g. 1.8")),
        ],
        path: Annotated[
            str | None,
            Field(description=("XDTO Package.bin or description XML")),
        ] = None,
        project: Annotated[
            str | None,
            Field(description=("Project instead of path, with package")),
        ] = None,
        configuration: Annotated[str, Field(description="Project configuration")] = "full",
        package: Annotated[
            str | None,
            Field(description=("Exact XDTO package name")),
        ] = None,
        imports: Annotated[
            dict[str, str] | None,
            Field(description="Namespace URI → local package path"),
        ] = None,
        extensions: Annotated[
            list[str] | None,
            Field(description=("Ordered format extension package paths")),
        ] = None,
    ) -> dict[str, Any]:
        """Open XDTO for a version, local imports and explicit extensions. Inspect diagnostics;
        XSD and downloads are unsupported."""
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
        schema_id: Annotated[str, Field(description="Snapshot ID from ed_schema_open")],
        namespace: Annotated[str | None, Field(description="Exact namespace URI")] = None,
        kind: Annotated[str | None, Field(description="Type kind: object or value")] = None,
        text: Annotated[
            str | None, Field(description="Type name substring, case-insensitive")
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """List named object/value types by namespace, kind and name. Expand types with
        ed_schema_type using a qualified name or type ID."""
        return await call(service.ed_schema_types, schema_id, namespace, kind, text, offset, limit)

    @server.tool()
    async def ed_schema_type(
        schema_id: Annotated[str, Field(description="Snapshot ID from ed_schema_open")],
        qname: Annotated[
            str,
            Field(description=("Short/Clark type name or local type ID")),
        ],
        section: Annotated[
            str, Field(description="Section: properties, values, facets")
        ] = "properties",
        offset: Offset = 0,
        limit: Limit = 50,
        include_origin: Annotated[
            bool,
            Field(description="Include provenance graph"),
        ] = False,
    ) -> dict[str, Any]:
        """Read type properties, values or facets. Ambiguous names need Clark notation/type ID;
        include_origin adds provenance."""
        return await call(
            service.ed_schema_type, schema_id, qname, section, offset, limit, include_origin
        )

    @server.tool()
    async def ed_schema_close(
        schema_id: Annotated[str, Field(description="Snapshot ID from ed_schema_open")],
    ) -> dict[str, Any]:
        """Close a schema snapshot, preserving files; repeat returns closed=false. Reopen to
        reread changed packages."""
        return await call(service.ed_schema_close, schema_id)

    # --- Маршруты EnterpriseData -------------------------------------------------------------

    @server.tool()
    async def ed_routes(
        project: Annotated[
            str | None,
            Field(description="Project instead of path/profile_id"),
        ] = None,
        configuration: Annotated[
            str,
            Field(description=("Project configuration; path/profile_id: full")),
        ] = "full",
        path: Annotated[
            str | None,
            Field(description="XML dump root on agent machine"),
        ] = None,
        profile_id: Annotated[
            str | None,
            Field(description=("Existing route snapshot ID")),
        ] = None,
        section: Annotated[
            str,
            Field(description="Section: summary, plans, versions, variants, packages, skipped"),
        ] = "summary",
        plan: Annotated[
            str | None,
            Field(description=("Plan name, case-insensitive")),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
        force: Annotated[
            bool,
            Field(description="Reread source files"),
        ] = False,
        extensions: Annotated[
            list[str] | None,
            Field(description=("Ordered roots; null = settings, [] = none")),
        ] = None,
    ) -> dict[str, Any]:
        """Read version routes from project/dump/profile_id. Reuse unchanged snapshots; changed
        sources require force. Node versions and live settings are unknown."""
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
            extensions,
        )

    @server.tool()
    async def ed_route_compare(
        left_profile_id: Annotated[str, Field(description="Left route snapshot ID")],
        right_profile_id: Annotated[str, Field(description="Right route snapshot ID")],
        left_plan: Annotated[
            str | None,
            Field(description=("Left plan; required if several")),
        ] = None,
        right_plan: Annotated[
            str | None,
            Field(description=("Right plan; required if several")),
        ] = None,
        context: Annotated[
            str,
            Field(description=("Context: plan or without_node")),
        ] = "plan",
        section: Annotated[
            str,
            Field(description="Section: issues, versions, schema_diff, skipped"),
        ] = "issues",
        level: Annotated[
            str | None,
            Field(description="Severity: ошибка or предупреждение"),
        ] = None,
        check_prefix: Annotated[
            str | None,
            Field(description="Check ID prefix"),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Compare route snapshots; select plans if needed. Ready ed_open/ed_schema_open
        arguments or null with reason. Live compatibility is unverified."""
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

    @server.tool()
    async def ed_create(
        project_id: Annotated[str, Field(description="Durable manager ID")],
        mode: Annotated[str, Field(description="new, import, or rebind to fresh inputs")] = "new",
        project: Annotated[str | None, Field(description="Host from project_list")] = None,
        configuration: Annotated[str, Field(description="Host configuration")] = "full",
        configuration_path: Annotated[
            str | None, Field(description="Host dump instead of project")
        ] = None,
        extensions: Annotated[
            list[str] | None,
            Field(description="Ordered host extensions; null uses project settings"),
        ] = None,
        plan: Annotated[str | None, Field(description="Exchange plan name")] = None,
        format_version: Annotated[
            str | None, Field(description="One exact route version key")
        ] = None,
        interface_version: Annotated[int, Field(description="2 only")] = 2,
        identity: Annotated[
            dict[str, Any] | None,
            Field(
                description="name, prefix, module_name, synonym, version, compatibility_mode; "
                "default name includes project_id"
            ),
        ] = None,
        document_id: Annotated[str | None, Field(description="ed_open snapshot to import")] = None,
        schema_id: Annotated[str | None, Field(description="Optional open XDTO schema")] = None,
        structure_id: Annotated[
            str | None, Field(description="Optional loaded host structure")
        ] = None,
        offset: Offset = 0,
        limit: Limit = 20,
        section: Annotated[
            str, Field(description="import_report, notices, differences, plan_candidates")
        ] = "import_report",
    ) -> dict[str, Any]:
        """Create/reopen durable interface-2 manager; import preserves text. Revision/counts,
        profile/report, document_id, reference_manager/reason. Different arguments refuse;
        changed fingerprints warn. Missing inputs refuse quickly with reopen_calls.
        Atomic rebind keeps rules/receipts, validates and recovers writes.
        Missing plan: plan_candidates."""
        return await call(
            service.ed_create,
            project_id,
            mode,
            project,
            configuration,
            configuration_path,
            extensions,
            plan,
            format_version,
            interface_version,
            identity,
            document_id,
            schema_id,
            structure_id,
            offset,
            limit,
            section,
        )

    @server.tool()
    async def ed_apply(
        project_id: Annotated[str, Field(description="Manager project from ed_create")],
        expected_revision: Annotated[str, Field(description="Current project revision")],
        operations: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Up to 100 manager ops; forms: docs/tools.md. "
                "Algorithm address: Код/ or Алгоритм/. client_id refs; "
                "after_id omitted=append, null=first. Omit for saved preview. "
                "Manager: manager_name,title,generated_at,text_style; rebind bindings"
            ),
        ] = None,
        mode: Annotated[str, Field(description="preview or apply")] = "preview",
        expected_preview_hash: Annotated[
            str | None, Field(description="Current preview_hash for apply")
        ] = None,
        confirmations: Annotated[
            list[dict[str, Any]] | None, Field(description="Preview notices: code, notice_hash")
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
        section: Annotated[
            str, Field(description="summary, operations, changes, notices, failures, skipped")
        ] = "summary",
    ) -> dict[str, Any]:
        """Preview/apply atomically; last 8 previews survive restart. Pages use next_offset;
        truncated_by=size marks size limits. Apply adds document_id. Replays return replayed=true,
        current revision and live document_id."""
        return await call(
            service.ed_apply,
            project_id,
            expected_revision,
            operations,
            mode,
            expected_preview_hash,
            confirmations,
            offset,
            limit,
            section,
        )

    @server.tool()
    async def ed_authoring_candidates(
        target: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "Manager: schema_id, structure_id, direction; properties add "
                    "configuration_object, format_type; reference accepts project_id. "
                    "Overlay: docs/tools.md"
                )
            ),
        ],
        kind: Annotated[
            str, Field(description="Overlay: format/configuration; manager: objects/properties")
        ],
        text: Annotated[str, Field(description="Name/path substring, case-insensitive")] = "",
        offset: Offset = 0,
        limit: Limit = 50,
        configuration_attribute: Annotated[
            str | None,
            Field(description="Selected attribute"),
        ] = None,
        format_property: Annotated[
            str | None,
            Field(description="Selected format property"),
        ] = None,
        scope: Annotated[str | None, Field(description="overlay (default) or manager")] = None,
        reference_document_id: Annotated[
            str | None,
            Field(description="Typical manager snapshot or auto (both kinds)"),
        ] = None,
    ) -> dict[str, Any]:
        """Overlay/manager pairs; auto=false. Enums: needs_pkpd/value_pairs.
        Typical ПКС: merged references or reference_module conflicts, no code. Use next_offset."""
        return await call(
            service.ed_authoring_candidates,
            target,
            kind,
            text,
            offset,
            limit,
            configuration_attribute,
            format_property,
            scope,
            reference_document_id,
        )

    @server.tool()
    async def registration_retarget(
        project_id: Annotated[str, Field(description="Project")],
        exchange_plan: Annotated[str, Field(description="Plan")],
        node_properties: Annotated[dict, Field(description="Rename/null/{name,value}/list")],
        source: Annotated[dict, Field(description="Dump")],
        structure_id: Annotated[Any, Field(description="Structure ID")] = None,
        own_attributes: Annotated[Any, Field(description="name,type,synonym")] = None,
        node_values: Annotated[Any, Field(description="source,target,instruction")] = None,
        extension: Annotated[Any, Field(description="name,prefix")] = None,
        mode: Annotated[str, Field(description="preview/write")] = "preview",
        expected_preview_hash: Annotated[Any, Field(description="Hash")] = None,
        acknowledged_notices: Annotated[Any, Field(description="Notices")] = None,
        offset: Annotated[int, Field(description="Skip")] = 0,
        limit: Annotated[int, Field(description="Size")] = 50,
        deletion_mark_filter: Annotated[bool, Field(description="Exclude marked")] = False,
    ) -> dict[str, Any]:
        """Copy registration rules."""
        return await call(
            service.registration_retarget,
            project_id,
            exchange_plan,
            node_properties,
            source,
            structure_id,
            own_attributes,
            node_values,
            extension,
            mode,
            expected_preview_hash,
            acknowledged_notices,
            offset,
            limit,
            deletion_mark_filter,
        )

    @server.tool()
    async def ed_authoring_build(
        project: Annotated[str | None, Field(description="Overlay host from project_list")] = None,
        configuration: Annotated[
            str | None, Field(description="Overlay host configuration")
        ] = None,
        extension: Annotated[
            dict[str, Any] | None,
            Field(description=("Overlay kit identity; see docs/tools.md")),
        ] = None,
        operations: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description=(
                    "Overlay operations (max 100); targets/fields: docs/tools.md. "
                    "Forbidden with scope=manager"
                )
            ),
        ] = None,
        version_scope: Annotated[
            str | None,
            Field(description="Explicit manager scope consent"),
        ] = None,
        mode: Annotated[
            str,
            Field(description="Mode: preview or write"),
        ] = "preview",
        delivery: Annotated[str, Field(description="Kit form: extension or manual")] = "extension",
        output_dir: Annotated[
            str | None,
            Field(description=("Computed kit directory; normally omit")),
        ] = None,
        expected_preview_hash: Annotated[
            str | None, Field(description="Current preview build_hash for write")
        ] = None,
        acknowledged_notices: Annotated[
            list[str] | None,
            Field(description="Current required_acknowledgements IDs"),
        ] = None,
        offset: Offset = 0,
        limit: Limit = 50,
        section: Annotated[
            str,
            Field(
                description=(
                    "summary, operations, issues_before, issues_after, scopes, skipped; "
                    "manager also notices, files"
                )
            ),
        ] = "summary",
        level: Annotated[
            str | None, Field(description="Severity: ошибка or предупреждение")
        ] = None,
        check_prefix: Annotated[
            str | None,
            Field(description="Check ID prefix"),
        ] = None,
        address_prefix: Annotated[
            str | None, Field(description="Address prefix, case-insensitive")
        ] = None,
        drop_operations: Annotated[
            list[str] | None,
            Field(description=("Previous operation IDs to remove")),
        ] = None,
        scope: Annotated[str | None, Field(description="overlay (default) or manager")] = None,
        project_id: Annotated[
            str | None, Field(description="Manager project from ed_create")
        ] = None,
        expected_revision: Annotated[
            str | None, Field(description="Current manager revision")
        ] = None,
        route: Annotated[
            dict[str, Any] | None,
            Field(description="Manager route: plan, format_version; defaults to project"),
        ] = None,
        registration_objects: Annotated[
            list[str] | None,
            Field(description=("Manager: Kind.Name objects without PKO; registration only")),
        ] = None,
    ) -> dict[str, Any]:
        """Preview/write overlay or manager kits in workspace with current hash and required
        acknowledgements. Manager refuses legacy operations. Install/verify runtime separately."""
        return await call(
            service.ed_authoring_build,
            project,
            configuration,
            extension,
            operations,
            version_scope,
            mode,
            delivery,
            output_dir,
            expected_preview_hash,
            acknowledged_notices,
            offset,
            limit,
            section,
            level,
            check_prefix,
            address_prefix,
            drop_operations,
            scope,
            project_id,
            expected_revision,
            route,
            registration_objects,
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
