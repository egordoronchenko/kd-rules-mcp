"""Сквозной авторинг менеджера: долговечная модель и читаемый снимок результата."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, NoReturn

from kd_rules_mcp.authoring.ed.artifacts import artifact_name, validate_manager_files
from kd_rules_mcp.authoring.ed.hook import valid_identifier
from kd_rules_mcp.authoring.ed.instruction import render_data_preflight
from kd_rules_mcp.authoring.ed.manager_candidates import object_candidates, property_candidates
from kd_rules_mcp.authoring.ed.manager_operations import parse_operation
from kd_rules_mcp.authoring.ed.manager_render import (
    ManagerManifest,
    ManagerRoute,
    render_manager_kit,
    render_manager_route,
)
from kd_rules_mcp.authoring.ed.manifest import sha256
from kd_rules_mcp.authoring.ed.model import AuthoringPreconditionError, ExtensionIdentity
from kd_rules_mcp.authoring.ed.plan_stubs import add_plan_stubs
from kd_rules_mcp.authoring.ed.workspace import ManagerWorkspace
from kd_rules_mcp.authoring.ed.xml_dump import (
    SubscriptionAddition,
    read_description,
    read_manager_host,
)
from kd_rules_mcp.ed import build_references
from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.executor_profile import ProfileDetection, detect_profile
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.schema import load_schema
from kd_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd_rules_mcp.ed.writer import new_manager, render
from kd_rules_mcp.ed.writer_import import import_manager
from kd_rules_mcp.ed.writer_model import (
    Evidence,
    FormatBinding,
    Host,
    digest,
    json_bytes,
    json_value,
)
from kd_rules_mcp.errors import (
    EdAuthoringAckRequiredError,
    EdAuthoringIoError,
    EdAuthoringPathError,
    EdAuthoringPreconditionError,
    EdAuthoringStaleError,
    EdSchemaNotFoundError,
    Kd2Error,
    ProjectNotFoundError,
)
from kd_rules_mcp.projects import resolve
from kd_rules_mcp.service.ed import EdProject
from kd_rules_mcp.service.ed_authoring import EdAuthoringMixin, _failure, _mapping, _text
from kd_rules_mcp.service.ed_authoring_views import (
    MANAGER_NOTICES_BYTES,
    PAGE_BYTES,
    compact_page,
    json_size,
    validate_options,
)
from kd_rules_mcp.service.ed_layers import checked_extensions, extension_paths, select_views
from kd_rules_mcp.service.ed_names import common_module_names
from kd_rules_mcp.service.ed_preflight import key_data_instruction, with_key_data_instruction
from kd_rules_mcp.service.ed_reopen import with_reopen_hints
from kd_rules_mcp.service.ed_views import address_of, validate_page
from kd_rules_mcp.service.extension_delivery import (
    delivered_input_fingerprint,
    delivery_options,
    manager_input_receipt,
    prepare_user_delivery,
)
from kd_rules_mcp.service.paths import Settings
from kd_rules_mcp.structures.store import dump_fingerprint
from kd_rules_mcp.structures.xmldump import registration_events
from kd_rules_mcp.validation.ed_handler_enum import validate_enum_handlers
from kd_rules_mcp.validation.ed_links import validate_links
from kd_rules_mcp.validation.ed_plan import (
    subscription_source_objects,
    validate_plan_content,
    validate_plan_pods,
    validate_plan_registration,
)
from kd_rules_mcp.validation.ed_required import CHECK as REQUIRED_UNFILLED
from kd_rules_mcp.validation.ed_required import PARTIAL as REQUIRED_UNFILLED_PARTIAL
from kd_rules_mcp.validation.ed_required import RequiredUnfilled
from kd_rules_mcp.validation.ed_schema import validate_schema
from kd_rules_mcp.validation.ed_sender import validate_sender_handlers
from kd_rules_mcp.validation.ed_structure import validate_structure
from kd_rules_mcp.validation.ed_structure_snapshot import metadata_key
from kd_rules_mcp.validation.ed_writer import validate_writer
from kd_rules_mcp.validation.report import Level, ValidationReport

MAX_PREVIEW_PACKETS = 8


def _manager_address_aliases(value: Any) -> Any:
    """Адрес читателя Обработчик и адрес писателя Код обозначают один метод."""
    if isinstance(value, list):
        return [_manager_address_aliases(item) for item in value]
    if isinstance(value, dict):
        result = {key: _manager_address_aliases(item) for key, item in value.items()}
        address = result.get("address")
        if isinstance(address, str) and address.casefold().startswith("обработчик/"):
            result["address"] = "Код/" + address.split("/", 1)[1]
        return result
    return value


MANAGER_CANDIDATES_BYTES = 8192


def _compact_property_candidates(result: dict) -> dict:
    """Общее окно четырёх разделов: уменьшение по размеру не теряет соседние страницы."""
    sections = ("properties", "table_parts", "unmatched_format", "unmatched_configuration")
    offset = result["properties"]["offset"]
    limit = result["properties"]["limit"]
    while True:
        for section in sections:
            page = result[section]
            page["limit"] = limit
            page["items"] = page["items"][:limit]
            page["next_offset"] = offset + len(page["items"])
            page["has_more"] = page["next_offset"] < page["total"]
        result["next_offset"] = offset + limit
        result["has_more"] = any(result[s]["has_more"] for s in sections)
        if json_size(result) <= MANAGER_CANDIDATES_BYTES:
            return result
        if limit == 1:
            _refuse(
                "candidate_row_too_large",
                "Одна запись кандидатов превышает бюджет ответа; "
                "проверьте длины имён и число деклараций типового модуля",
                response_bytes=json_size(result),
                budget_bytes=MANAGER_CANDIDATES_BYTES,
            )
        limit -= 1
        result["truncated_by"] = "size"


def _refuse(reason: str, message: str, **details) -> NoReturn:
    raise EdAuthoringPreconditionError(
        message, {"failures": [{"id": reason, "message": message}], **details}
    )


def _creation_differences(existing: dict, requested: dict) -> list[dict]:
    """Служебный признак проверки расширений показывается аргументом extensions."""
    rows = [
        {"field": k, "existing": existing.get(k), "requested": v}
        for k, v in requested.items()
        if k != "extensions_checked" and existing.get(k) != v
    ]
    if existing.get("extensions_checked", False) != requested.get("extensions_checked", False):
        rows = [r for r in rows if r["field"] != "extensions"]
        rows.append(
            {
                "field": "extensions",
                "existing": existing.get("extensions")
                if existing.get("extensions_checked")
                else None,
                "requested": requested.get("extensions")
                if requested.get("extensions_checked")
                else None,
            }
        )
    return rows


def _check_project_path(folder: Path) -> None:
    """Учитывает самый длинный путь тела/квитанции до первой записи в Win32."""
    if (
        os.name == "nt"
        and max(
            len(str(folder / name / ("f" * 64 + suffix)))
            for name, suffix in (("bodies", ".bsl"), ("applied", ".json"))
        )
        >= 260
    ):
        raise EdAuthoringPathError(
            "Путь проекта слишком длинный для Windows; сократите project_id или путь рабочей папки."
        )


@contextmanager
def _rebind_io():
    """Отказ по вводу-выводу включает вход/выход блокировки и все записи журнала."""
    try:
        yield
    except OSError as error:
        raise EdAuthoringIoError(
            "Не удалось записать перепривязку проекта; проверьте доступ к рабочей папке. "
            "Следующее обращение восстановит согласованное состояние."
        ) from error


def _atomic_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    """Служебные JSON публикуются целиком, без частично записанного содержимого."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".service-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(temporary, path)
            except FileExistsError:
                previous = json.loads(path.read_bytes())
                if previous != json_value(value):
                    _refuse(
                        "model_invalid",
                        "Проект создаётся с другими входами",
                        differences=[
                            {
                                "field": key,
                                "existing": previous.get("arguments", {}).get(key),
                                "requested": requested,
                            }
                            for key, requested in value["arguments"].items()
                            if previous.get("arguments", {}).get(key) != requested
                        ]
                        or [
                            {
                                "field": "fingerprints",
                                "existing": previous.get("creation_fingerprint"),
                                "requested": value.get("creation_fingerprint"),
                            }
                        ],
                    )
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class EdWriterMixin(EdAuthoringMixin):
    """Авторские проекты отделены от read-only снимков и overlay-авторинга."""

    def _manager_name_scope(self, metadata: dict, structure_id: str | None = None) -> dict:
        """Общая область берётся из выбранной структуры и явно выбранных расширений."""
        args = metadata["arguments"]
        structure_id = structure_id or args.get("structure_id")
        if not structure_id or not self.store.exists(structure_id):
            return {}
        sources = self.store.meta(structure_id)
        context = common_module_names(
            sources, self._read_path, extensions=tuple(args["extensions"])
        )
        return {"common_modules": context[0], "global_methods": context[1]} if context else {}

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.manager_workspace = ManagerWorkspace(self.workspace.root)
        self._manager_documents: dict[str, str] = {}

    def _manager_project(self, project_id: str):
        try:
            project = self.manager_workspace.get(project_id)
            path = self.manager_workspace.directory / project_id / "manager.ed.json"
            self._safe_path(path)
            if not path.is_file() or self._hash_file(path) != project.snapshot_hash:
                # Успешно опубликованный снимок другого сервиса становится текущим.
                # Повреждённый снимок не чинится и не перезаписывается.
                self.manager_workspace = ManagerWorkspace(self.workspace.root)
                project = self.manager_workspace.get(project_id)
            return project
        except KeyError as error:
            raise ProjectNotFoundError(f"Проект менеджера «{project_id}» не существует") from error

    def _manager_file(self, project_id: str, name: str) -> Path:
        # get проверяет допустимость ID; _safe_path исключает выход через ссылки.
        self._manager_project(project_id)
        path = self.manager_workspace.directory / project_id / name
        self._safe_path(path)
        return path

    def _manager_preview_packet(self, project_id: str, preview_hash: str | None) -> list[dict]:
        if not isinstance(preview_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", preview_hash):
            _refuse("preview_packet_missing", "Хеш preview неизвестен; пришлите operations")
        path = self._manager_file(project_id, "previews/" + preview_hash + ".json")
        if not path.is_file():
            _refuse(
                "preview_packet_missing",
                "Пакет preview неизвестен или вытеснен; пришлите operations",
            )
        try:
            packet = json.loads(path.read_bytes())
            operations = packet["operations"]
            if (
                packet["preview_hash"] != preview_hash
                or not isinstance(operations, list)
                or packet["packet_hash"] != digest(operations)
            ):
                raise ValueError("Повреждён пакет")
        except (ValueError, KeyError, TypeError):
            _refuse("preview_packet_corrupt", "Повреждён пакет preview; пришлите operations")
        return operations

    def _manager_save_preview(
        self, project_id: str, revision: str, preview_hash: str, operations: list[dict]
    ) -> None:
        with ExitStack() as stack:
            try:
                stack.enter_context(self.manager_workspace._disk_lock(project_id))
            except EdAuthoringStaleError:
                _refuse("project_busy", "Проект занят другим процессом; повторите preview позже")
            current = self._manager_project(project_id)
            self.manager_workspace._check_current(current)
            if current.model.revision != revision:
                raise EdAuthoringStaleError(
                    "Проект изменён во время preview", {"revision": current.model.revision}
                )
            path = self._manager_file(project_id, "previews/" + preview_hash + ".json")
            _atomic_json(
                path,
                {
                    "preview_hash": preview_hash,
                    "packet_hash": digest(operations),
                    "revision": revision,
                    "operations": operations,
                },
            )
            packets = sorted(
                path.parent.glob("*.json"),
                key=lambda p: (p.stat().st_mtime_ns, p.name),
                reverse=True,
            )
            for old in packets[MAX_PREVIEW_PACKETS:]:
                self._safe_path(old)
                old.unlink(missing_ok=True)

    def _manager_reference(
        self,
        root: Path,
        roots: tuple[Path, ...],
        plan: str | None,
        key: str,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[dict | None, dict | None]:
        """Точное соответствие ключа версии доказанной записи карты плана."""
        snapshot, _, _ = self._open_snapshot(
            root, None, None, False, extensions=roots, read_files_only=True
        )
        plans = snapshot.profile.plans
        selected = next(
            (p for p in plans if p.plan_name.casefold() == (plan or "").casefold()), None
        )
        if plan is None:
            candidates = [p for p in plans if p.is_ed is True and key in p.effective_map()]
            selected = candidates[0] if len(candidates) == 1 else None
        elif selected is None:
            _refuse(
                "exchange_plan_missing",
                "План не найден; выберите plan из plan_candidates",
                plan_candidates=compact_page(
                    {}, [{"plan": p.plan_name} for p in plans if p.is_ed is True], offset, limit
                ),
            )
        if selected is None:
            return None, {
                "id": "reference_route_ambiguous",
                "message": f"Нет единственного плана с доказанным маршрутом версии {key}",
            }
        if selected.is_ed is not True:
            return None, {
                "id": "plan_not_ed",
                "message": f"План {selected.plan_name} не подтверждён как обмен через формат",
            }
        name = selected.effective_map().get(key)
        if not name:
            return None, {
                "id": "format_version_not_mapped",
                "message": f"В карте плана {selected.plan_name} "
                f"нет доказанного модуля версии {key}",
            }
        manager = next(
            (m for m in snapshot.profile.managers if m.name.casefold() == (name or "").casefold()),
            None,
        )
        if manager is None or not manager.path:
            return None, {
                "id": "manager_source_unavailable",
                "message": f"Исходник типового модуля {name} для версии {key} не определён",
            }
        return {"name": manager.name, "path": self._host((root / manager.path).resolve())}, None

    def _manager_metadata(self, project_id: str) -> dict:
        try:
            path = self._manager_file(project_id, "creation.json")
            metadata = json.loads(path.read_bytes())
            if not metadata.get("pending_rebind"):
                return metadata
            # Журнал восстанавливается ДО следующего apply. Квитанция решения
            # остаётся в модели при дальнейших правках, в отличие от её ревизии.
            with self.manager_workspace._disk_lock(project_id):
                metadata = json.loads(path.read_bytes())
                pending = metadata.get("pending_rebind")
                if not pending:
                    return metadata
                model = self._manager_project(project_id).model
                committed = model.revision == pending["revision"] or any(
                    d.client_id == pending.get("client_id")
                    and d.operation_hash == pending.get("operation_hash")
                    for d in model.decisions
                )
                recovered = (
                    pending["metadata"]
                    if committed
                    else {k: v for k, v in metadata.items() if k != "pending_rebind"}
                )
                recovered = {
                    **recovered,
                    "rebind_recovery": {
                        "id": "rebind_recovered",
                        "outcome": "committed" if committed else "rolled_back",
                    },
                }
                _atomic_json(path, recovered)
                return recovered
        except (OSError, ValueError) as error:
            raise EdAuthoringIoError("Не прочитаны входы проекта менеджера", {}) from error

    def _manager_check_reader_id(self, document_id: str) -> None:
        """Коллизии запрещены в обоих направлениях, включая проекты на диске."""
        if len(document_id) > 128:
            return  # Такой ID допустим читателю, но не ManagerWorkspace.
        folder = self.manager_workspace._folder(document_id)
        self._safe_path(folder)
        if any((folder / name).is_file() for name in ("manager.ed.json", "creation.json")):
            _refuse(
                "document_id_owned_by_manager",
                f"Идентификатор снимка «{document_id}» занят проектом менеджера; "
                "закройте его через ed_close или выберите другой путь модуля",
            )

    def _manager_structure_provenance(self, structure_id: str, metadata: dict) -> None:
        args = metadata["arguments"]
        root = self._read_path(args["configuration_path"]).resolve()
        names = [
            read_description(
                "Configuration.xml",
                (self._read_path(p) / "Configuration.xml").read_text("utf-8-sig"),
                "Configuration",
            ).name
            for p in args["extensions"]
        ]
        meta = self.store.meta(structure_id)
        if (
            meta.get("source") != "xml"
            or Path(meta.get("source_path", "")).resolve() != root
            or json.loads(meta.get("extensions", "[]")) != names
        ):
            _refuse(
                "ed.author.snapshot_mismatch",
                "Структура не соответствует выбранной выгрузке и расширениям",
            )

    def _manager_bindings(
        self, args: dict, *, import_fingerprint: str | None = None
    ) -> tuple[dict, Host, tuple[FormatBinding, ...]]:
        """Снимает свежие привязки, не меняя авторскую модель."""
        root = self._read_path(args["configuration_path"])
        roots = tuple(self._read_path(p) for p in args["extensions"])
        metadata = {
            "arguments": args,
            "schema_sources": [],
            "dump_fingerprint": dump_fingerprint((root, *roots)),
        }
        host = Host(
            configuration=args["configuration_path"],
            structure_id=args["structure_id"] or "",
            extensions=tuple(Evidence(self._host(p), dump_fingerprint((p,))) for p in roots),
        )
        if host.structure_id:
            self._manager_structure_provenance(host.structure_id, metadata)
            _, fingerprint = self._ed_structure_snapshot(host.structure_id)
            host = replace(host, structure_hash=fingerprint or "")
        key = args["format_version"]
        bindings = (FormatBinding(key, "", ""),)
        if args["schema_id"]:
            schema = self._ed_schemas.get(args["schema_id"])
            if schema is None:
                raise EdSchemaNotFoundError("Схема формата не открыта")
            if schema.format_version != key:
                _refuse("route_scope_conflict", "Версия схемы отличается от ключа проекта")
            if self._schema_changed(schema):
                raise EdAuthoringStaleError(
                    "Схема изменилась; повторите ed_schema_close/open",
                    {"schema_id": args["schema_id"]},
                )
            sources = [
                (self._host_text(s.path), s.sha256)
                for p in schema.schema.packages
                for s in p.sources
            ]
            metadata["schema_sources"] = sources
            metadata["schema_packages"] = [
                {
                    "name": p.metadata_name,
                    "path": self._host_text(p.sources[0].path),
                    "namespace": p.namespace,
                    "role": p.origin_role,
                }
                for p in schema.schema.packages
            ]
            bindings = (
                FormatBinding(
                    key, schema.schema.packages[0].namespace, digest(sources), args["schema_id"]
                ),
            )
        if args["mode"] == "import":
            if import_fingerprint is None:
                source = self._ed_project(args["document_id"])
                import_fingerprint = digest(tuple(s.sha256 for s in source.document.files))
            metadata["import_fingerprint"] = import_fingerprint
        metadata["creation_fingerprint"] = digest(
            (
                args,
                metadata["schema_sources"],
                metadata["dump_fingerprint"],
                host.structure_hash,
                metadata.get("import_fingerprint"),
            )
        )
        return metadata, host, bindings

    def _manager_plan_content(self, model, metadata: dict):
        report, content, _, _ = self._manager_plan_delivery(model, metadata)
        return report, content

    def _manager_plan_delivery(
        self,
        model,
        metadata,
        registration_objects=(),
        plan_name=None,
        *,
        registered_objects=(),
        plan_stubs=True,
    ):
        structure = (
            self._ed_structure_snapshot(model.host.structure_id)[0]
            if model.host.structure_id
            else None
        )
        names = []
        pko_objects = {metadata_key(rule.configuration_object)[0] for rule in model.pko}
        if not isinstance(registration_objects, (tuple, list)) or any(
            not isinstance(name, str) for name in registration_objects
        ):
            raise ValueError("registration_objects: список Вид.Имя")
        for full_name in registration_objects:
            kind, dot, name = full_name.partition(".")
            key = kind.casefold(), name.casefold()
            obj = structure.objects.get(key) if structure else None
            if not dot or obj is None:
                _refuse(
                    "registration_object_missing", "Объект отсутствует в структуре: " + full_name
                )
            if key in pko_objects:
                _refuse("registration_object_has_pko", "У объекта уже есть ПКО: " + full_name)
            if not registration_events(obj.kind, obj.name):
                _refuse(
                    "registration_object_unsupported",
                    "Объект не поддерживает регистрацию: " + full_name,
                )
            names.append(obj.kind + "." + obj.name)
        if not isinstance(registered_objects, (tuple, list)) or any(
            not isinstance(name, str) for name in registered_objects
        ):
            raise ValueError("registered_objects: список Вид.Имя")
        for full_name in registered_objects:
            kind, dot, name = full_name.partition(".")
            if (
                not dot
                or structure is None
                or (kind.casefold(), name.casefold()) not in structure.objects
            ):
                _refuse("registered_object_missing", "Объект отсутствует в структуре: " + full_name)
        selected = plan_name or metadata["arguments"]["plan"]
        report, content = validate_plan_content(model, structure, selected, tuple(names))
        if names and any(i.check == "ed.plan.content_unchecked" for i in report.issues):
            _refuse(
                "registration_plan_unchecked",
                "Состав плана недоступен для объектов только для регистрации",
            )
        registration_report, subscriptions = validate_plan_registration(
            structure, selected, content, tuple(names)
        )
        report.extend(registration_report)
        report.extend(
            validate_plan_pods(
                model,
                structure,
                selected,
                content,
                tuple(names),
                tuple(registered_objects),
                plan_stubs=plan_stubs,
            )[0]
        )
        return report, content, subscriptions, tuple(sorted(set(names)))

    def _manager_content_descriptions(self, metadata: dict, missing):
        root = self._read_path(metadata["arguments"]["configuration_path"])
        result = []
        for kind, name, folder in missing:
            relative = folder + "/" + name + ".xml"
            result.append(
                read_description(relative, (root / relative).read_text("utf-8-sig"), kind, name)
            )
        return tuple(result)

    def _manager_bound_schema(self, metadata: dict):
        """Привязанная схема из открытого снимка или сохранённых путей пакетов."""
        args = metadata["arguments"]
        if not args["schema_id"]:
            return None
        opened = self._ed_schemas.get(args["schema_id"])
        if opened is not None:
            schema = opened.schema
        else:
            packages = metadata.get("schema_packages")
            if not packages:
                _refuse(
                    "schema_snapshot_unavailable",
                    "Откройте привязанную schema_id заново для проверок схемы перед сборкой",
                    schema_id=args["schema_id"],
                )
            paths = [(p, self._read_path(p["path"])) for p in packages]
            imports = {p["namespace"]: path for p, path in paths}
            schema = load_schema(
                paths[0][1],
                extensions=tuple(path for p, path in paths if p["role"] == "extension"),
                locate_import=imports.get,
            )
        return schema

    def _manager_schema_checks(
        self, model, metadata: dict, *, required_unfilled: list[RequiredUnfilled] | None = None
    ) -> ValidationReport:
        """Проверяет прямые типы, границы и обязательные источники перед сборкой."""
        args = metadata["arguments"]
        _, snapshot = self._manager_snapshot(model, publish=False)
        schema = self._manager_bound_schema(metadata)
        if schema is None:
            return validate_enum_handlers(
                model, snapshot.document, ValidationProfile.build(None, args["format_version"])
            )
        structure = (
            self._ed_structure_snapshot(args["structure_id"])[0] if args["structure_id"] else None
        )
        profile = ValidationProfile.build(schema, args["format_version"], "both")
        report = validate_schema(
            snapshot.document,
            schema,
            snapshot.index,
            profile,
            structure,
            include_value_ranges=True,
            legacy_atomic_only=False,
            required_unfilled=required_unfilled,
        )
        result = ValidationReport(
            issues=[
                i
                for i in report.issues
                if i.check
                in {
                    "ed.schema.value_range",
                    "ed.schema.type_incompatible",
                    "ed.schema.required_source",
                    "ed.schema.reference_type_partial",
                    REQUIRED_UNFILLED,
                    REQUIRED_UNFILLED_PARTIAL,
                }
            ],
            skipped=[s for s in report.skipped if s.check == REQUIRED_UNFILLED],
        )
        result.extend(validate_enum_handlers(model, snapshot.document, profile))
        return result

    def _manager_enum_validation(self, project_id, document, profile):
        metadata = self._manager_metadata(project_id)
        if profile.schema is None:
            args = metadata["arguments"]
            opened = self._ed_schemas.get(args["schema_id"] or "")
            profile = ValidationProfile.build(
                opened.schema if opened else None, args["format_version"]
            )
        return validate_enum_handlers(self._manager_project(project_id).model, document, profile)

    def _manager_sender_validation(self, project_id, document, profile, structure):
        model = self._manager_project(project_id).model
        args = self._manager_metadata(project_id)["arguments"]
        if not profile.format_version:
            profile = ValidationProfile.build(
                profile.schema, args["format_version"], profile.direction
            )
        additions = validate_plan_content(model, structure, args["plan"])[1]
        return validate_sender_handlers(
            model, document, profile, structure, args["plan"], additions
        )

    def _manager_key_instruction(self, model, metadata):
        args = metadata["arguments"]
        schema = self._manager_bound_schema(metadata)
        if schema is None or not args["structure_id"]:
            return ""
        _, snapshot = self._manager_snapshot(model, publish=False)
        return key_data_instruction(
            snapshot.document,
            ValidationProfile.build(schema, args["format_version"]),
            self._ed_structure_snapshot(args["structure_id"])[0],
        )

    def _manager_rebind_report(self, model, metadata: dict, detection: ProfileDetection):
        """Новые привязки проверяются и по форме писателя, и по схеме/структуре."""
        _, snapshot = self._manager_snapshot(model, publish=False)
        report = validate_writer(
            model,
            snapshot.document.files[0].text,
            detection=detection,
            **self._manager_name_scope(metadata),
        )
        report.extend(
            validate_links(snapshot.document, snapshot.index, build_references(snapshot.document))
        )
        args = metadata["arguments"]
        schema = self._ed_schemas.get(args["schema_id"] or "")
        structure = (
            self._ed_structure_snapshot(args["structure_id"])[0] if args["structure_id"] else None
        )
        profile = ValidationProfile.build(
            schema.schema if schema else None, schema.format_version if schema else None, "both"
        )
        if schema:
            report.extend(
                validate_schema(
                    snapshot.document, schema.schema, snapshot.index, profile, structure
                )
            )
        if structure:
            report.extend(validate_structure(snapshot.document, structure, snapshot.index, profile))
        report.issues = list(dict.fromkeys(report.issues))
        return report

    def _manager_detection(self, metadata: dict, profile_id: str = "") -> ProfileDetection:
        args = metadata["arguments"]
        root = self._read_path(args["configuration_path"])
        roots = tuple(self._read_path(p) for p in args["extensions"])
        return detect_profile(root, roots, requested_profile=profile_id or None)

    @staticmethod
    def _manager_profile(detection: ProfileDetection) -> dict:
        return {
            "profile_id": detection.profile.profile_id if detection.profile else None,
            "verified": detection.verified,
            "mismatches": [asdict(m) for m in detection.mismatches],
        }

    def _manager_snapshot(self, model, *, publish: bool = True) -> tuple[str, EdProject]:
        rendered = render(model, "preserve" if model.source_files else "canonical")
        document_id = "ed-manager-" + digest((model.project_id, model.revision))[:24]
        self._manager_check_reader_id(document_id)
        path = self.manager_workspace.directory / model.project_id / "generated.bsl"
        document = read_manager_text(rendered.data.decode("utf-8"), path=self._host(path))
        index = build_addresses(document)
        entities = {e.entity_id: e for e in document.entities()}
        by_address = {a.casefold(): e for a, e in index.by_address.items()}
        for entity in entities.values():
            by_address.setdefault(address_of(entity, index).casefold(), entity)
        snapshot = EdProject(
            path, document, index, entities, by_address, manager_project_id=model.project_id
        )
        if publish:
            previous = self._manager_documents.get(model.project_id)
            if previous and previous != document_id:
                self._ed_projects.pop(previous, None)
            self._ed_projects[document_id] = snapshot
            self._manager_documents[model.project_id] = document_id
        return document_id, snapshot

    def _manager_require_open_inputs(self, args: dict, metadata: dict) -> None:
        """Отказ до чтения маршрутов/слоёв: повторное создание не восстанавливает входы."""
        from kd_rules_mcp.service.ed_reopen import reopen_details

        schema_missing = bool(args.get("schema_id") and args["schema_id"] not in self._ed_schemas)
        structure_missing = bool(
            args.get("structure_id") and not self.store.exists(args["structure_id"])
        )
        details = reopen_details(args, metadata, schema=schema_missing, structure=structure_missing)
        missing = [
            {"kind": kind, "id": args[kind + "_id"]}
            for kind, absent in (("schema", schema_missing), ("structure", structure_missing))
            if absent
        ]
        if missing:
            _refuse(
                "manager_inputs_not_open",
                "Входы проекта не открыты: выполните reopen_calls "
                "(ed_schema_open, structure_load_project/structure_load_xml). "
                "При новых идентификаторах используйте ed_create mode=rebind",
                missing_inputs=missing,
                **details,
            )

    @with_reopen_hints
    def ed_create(
        self,
        project_id: str,
        mode: str = "new",
        project: str | None = None,
        configuration: str = "full",
        configuration_path: str | None = None,
        extensions: list[str] | None = None,
        plan: str | None = None,
        format_version: str | None = None,
        interface_version: int = 2,
        identity: dict | None = None,
        document_id: str | None = None,
        schema_id: str | None = None,
        structure_id: str | None = None,
        offset: int = 0,
        limit: int = 20,
        section: str = "import_report",
    ) -> dict[str, Any]:
        validate_page(offset, limit)
        if section not in ("import_report", "notices", "differences", "plan_candidates"):
            raise ValueError("section: import_report, notices, differences или plan_candidates")
        _text(project_id, "project_id")
        if mode not in ("new", "import", "rebind"):
            raise ValueError("mode: new, import или rebind")
        rebinding = mode == "rebind"
        extensions_checked = project is not None or extensions is not None
        old_args = {}
        with self._lock:
            if project_id in self._ed_projects:
                _refuse("model_invalid", "Идентификатор занят снимком ED")
            folder = self.manager_workspace._folder(project_id)
            self._safe_path(folder)
            _check_project_path(folder)
            if (folder / "manager.ed.json").is_file():
                if project_id not in self.manager_workspace.ids():
                    self.manager_workspace = ManagerWorkspace(self.workspace.root)
                old_args = self._manager_metadata(project_id)["arguments"]
        if old_args and not rebinding:
            # Отказ по аргументам предшествует проверкам формы и новых входов:
            # иначе interface=3 или другая configuration теряют differences.
            requested_path = configuration_path
            if configuration_path is not None:
                # Неподключённый новый путь тоже другой аргумент. Сравнение
                # не читает его; ниже обычная проверка доступа остаётся обязательной.
                with suppress(Kd2Error):
                    requested_path = self._host(self._read_path(configuration_path).resolve())
            requested = {
                "mode": mode,
                "project": project,
                "configuration": configuration,
                "interface_version": interface_version,
                "document_id": document_id,
                **({"plan": plan} if plan is not None else {}),
                **({"format_version": format_version} if format_version is not None else {}),
                **(
                    {"configuration_path": requested_path} if configuration_path is not None else {}
                ),
                "schema_id": schema_id,
                "structure_id": structure_id,
            }
            differences = [
                {"field": k, "existing": old_args.get(k), "requested": v}
                for k, v in requested.items()
                if old_args.get(k) != v
            ]
            if differences:
                _refuse(
                    "creation_arguments_changed",
                    "Проект создан с другими аргументами; используйте mode=rebind",
                    differences=differences,
                )
            if identity is None:
                identity = old_args["identity"]
            self._manager_require_open_inputs(old_args, self._manager_metadata(project_id))
        if rebinding:
            with self._lock:
                old_args = self._manager_metadata(project_id)["arguments"]
            if project is None and configuration_path is None:
                project = old_args["project"]
                configuration = old_args["configuration"]
                configuration_path = None if project else old_args["configuration_path"]
            plan = plan or old_args["plan"]
            format_version = format_version or old_args["format_version"]
            identity = identity if identity is not None else old_args["identity"]
            if extensions is None and project is None:
                extensions = old_args["extensions"]
                extensions_checked = old_args.get("extensions_checked", False)
            mode = old_args["mode"]
            document_id = old_args["document_id"]
            schema_id = schema_id or old_args["schema_id"]
            structure_id = structure_id or old_args["structure_id"]
        if type(interface_version) is not int or interface_version != 2:
            _refuse("unsupported_form", "W1 поддерживает только интерфейс менеджера 2")
        if not rebinding and (mode == "import") != (document_id is not None):
            raise ValueError("document_id нужен только для mode=import")
        if project is not None:
            if configuration_path is not None:
                raise ValueError("Укажите project либо configuration_path")
            config = self._catalog().configuration(project, configuration)
            folder = self.settings.project_dirs.get(project)
            if folder is None:
                raise ValueError("Папка проекта не подключена")
            configuration_path = self._host(resolve(folder, config.dump))
            if extensions is None:
                extensions = [self._host(resolve(folder, p)) for p in config.extensions]
            extensions_checked = True
        elif configuration != "full":
            raise ValueError("configuration применим только к project")
        if configuration_path is None and document_id:
            with self._lock:
                source = self._ed_project(document_id)
                if (
                    source.path.parent.name == "Ext"
                    and source.path.parents[2].name == "CommonModules"
                ):
                    configuration_path = self._host(source.path.parents[3])
        root = self._read_path(_text(configuration_path, "configuration_path")).resolve()
        roots = extension_paths(extensions or [], self._read_path)
        self._limit(len(roots), 16, "расширения")
        checked_extensions(root, roots)
        if not (root / "Configuration.xml").is_file():
            _refuse("model_invalid", "Нужна XML-выгрузка конфигурации с Configuration.xml")
        plan = _text(plan, "plan")
        key = _text(format_version, "format_version")
        reference_manager, reference_reason = self._manager_reference(
            root,
            roots,
            plan,
            key,
            offset=offset if section == "plan_candidates" else 0,
            limit=limit,
        )
        identity_data = _mapping(
            identity or {},
            "identity",
            {"name", "prefix", "synonym", "version", "compatibility_mode", "module_name"},
        )
        extension = ExtensionIdentity(
            **{
                **asdict(
                    ExtensionIdentity(
                        "кд3м_Менеджер_"
                        + re.sub(r"[^A-Za-zА-Яа-яЁё0-9_]", "_", project_id)[:32]
                        + "_"
                        + digest(project_id)[:12],
                        "кд3м_",
                        "Менеджер обмена ED",
                    )
                ),
                **{k: v for k, v in identity_data.items() if k != "module_name"},
            }
        )
        if not isinstance(extension.synonym, str) or not isinstance(extension.version, str):
            raise ValueError("identity: synonym и version должны быть строками")
        if extension.compatibility_mode is not None and not isinstance(
            extension.compatibility_mode, str
        ):
            raise ValueError("identity: compatibility_mode — строка либо null")
        module_name = identity_data.get("module_name", "МенеджерОбмена")
        for name in (module_name, extension.name, extension.prefix, plan):
            if not isinstance(name, str) or not valid_identifier(name):
                _refuse("model_invalid", "Недопустимое имя 1С")
        try:
            render_manager_route(
                ManagerRoute(plan, key), module_name=module_name, prefix=extension.prefix
            )
        except AuthoringPreconditionError as error:
            raise _failure(error) from error
        args = {
            "mode": mode,
            "project": project,
            "configuration": configuration,
            "configuration_path": self._host(root),
            "extensions": [self._host(p) for p in roots],
            "extensions_checked": extensions_checked,
            "plan": plan,
            "format_version": key,
            "interface_version": interface_version,
            "identity": {**asdict(extension), "module_name": module_name},
            "document_id": document_id,
            "schema_id": schema_id,
            "structure_id": structure_id,
        }
        with self._lock:
            notices = []
            validation = None
            # Другой сервис мог создать проект после загрузки нашего workspace.
            with suppress(KeyError):
                self.manager_workspace.get(project_id)
            folder = self.manager_workspace.directory / project_id
            self._safe_path(folder)
            if (
                project_id not in self.manager_workspace.ids()
                and (folder / "manager.ed.json").is_file()
            ):
                self.manager_workspace = ManagerWorkspace(self.workspace.root)
            if project_id in self._ed_projects:
                _refuse("model_invalid", "Идентификатор занят снимком ED")
            if project_id in self.manager_workspace.ids():
                metadata = self._manager_metadata(project_id)
                differences = _creation_differences(metadata["arguments"], args)
                model = self._manager_project(project_id).model
                if differences and not rebinding:
                    _refuse(
                        "creation_arguments_changed",
                        "Проект создан с другими аргументами; используйте mode=rebind",
                        differences=differences,
                    )
                if structure_id:
                    self._manager_structure_provenance(structure_id, {"arguments": args})
                if not rebinding:
                    differences.extend(self._manager_input_differences(model, metadata)[1])
                if rebinding:
                    fresh, host, bindings = self._manager_bindings(
                        args,
                        import_fingerprint=metadata.get("import_fingerprint")
                        or digest(tuple(s.sha256 for s in model.source_files)),
                    )
                    detection = self._manager_detection(fresh)
                    operation = parse_operation(
                        {
                            "client_id": "service-rebind-" + digest((model.revision, fresh)),
                            "kind": "manager",
                            "action": "update",
                            "patch": {
                                "host": json_value(host),
                                "format_bindings": json_value(bindings),
                                "executor_profile": json_value(detection.model_profile()),
                            },
                        }
                    )
                    planned = self.manager_workspace.preview(
                        project_id, (operation,), expected_revision=model.revision
                    )
                    if planned.failures or planned.notices:
                        _refuse(
                            "model_invalid",
                            "Не удалось перепривязать менеджер",
                            failures=[json_value(f) for f in planned.failures],
                        )
                    report = self._manager_rebind_report(planned.model, fresh, detection)
                    validation = {
                        "errors": sum(i.level == Level.ERROR for i in report.issues),
                        "warnings": sum(i.level == Level.WARNING for i in report.issues),
                        "issue_count": len(report.issues),
                    }
                    notices.extend({"id": i.check, **i.to_dict()} for i in report.issues)
                    # Журнал публикуется первым; решение в сохранённой модели
                    # фиксирует новую сторону транзакции независимо от дальнейших правок.
                    with _rebind_io(), self.manager_workspace._disk_lock(project_id):
                        current = self._manager_project(project_id)
                        if current.model.revision != model.revision:
                            raise EdAuthoringStaleError(
                                "Проект изменён во время перепривязки",
                                {"revision": current.model.revision},
                            )
                        self.manager_workspace._check_current(current)
                        _atomic_json(
                            folder / "creation.json",
                            {
                                **metadata,
                                "pending_rebind": {
                                    "revision": planned.model.revision,
                                    "metadata": fresh,
                                    "client_id": operation.client_id,
                                    "operation_hash": next(
                                        d.operation_hash
                                        for d in planned.model.decisions
                                        if d.client_id == operation.client_id
                                    ),
                                },
                            },
                        )
                        model = self.manager_workspace._persist(planned.model).model
                        _atomic_json(folder / "creation.json", fresh)
                    metadata = fresh
                else:
                    detection = self._manager_detection(metadata, model.executor_profile.profile_id)
                if differences and not rebinding:
                    differences_page = compact_page(
                        {}, differences, offset if section == "differences" else 0, limit
                    )
                    notices.append(
                        {
                            "id": "creation_inputs_changed",
                            "differences": differences_page["items"],
                            "difference_count": differences_page["total"],
                            "next_offset": differences_page["next_offset"],
                            "has_more": differences_page["has_more"],
                            "hint": 'ed_create(mode="rebind")',
                        }
                    )
                existing = True
            else:
                if rebinding:
                    raise ProjectNotFoundError(f"Проект менеджера «{project_id}» не существует")
                # Проверяем ID до записи служебного файла или чтения большого импорта.
                with suppress(KeyError):
                    self.manager_workspace.get(project_id)
                folder = self.manager_workspace.directory / project_id
                self._safe_path(folder)
                if (folder / "creation.json").is_file():
                    previous = json.loads((folder / "creation.json").read_bytes())
                    differences = [
                        {
                            "field": k,
                            "existing": previous.get("arguments", {}).get(k),
                            "requested": v,
                        }
                        for k, v in args.items()
                        if previous.get("arguments", {}).get(k) != v
                    ]
                    if differences:
                        _refuse(
                            "model_invalid",
                            "Незавершённое создание имеет другие входы; "
                            "закройте проект через ed_close",
                            differences=differences,
                        )
                metadata, host, bindings = self._manager_bindings(args)
                detection = self._manager_detection(metadata)
                if mode == "import":
                    source = self._ed_project(document_id or "")
                    if source.layered:
                        _refuse(
                            "unsupported_form", "Импорт требует отдельный снимок исходного модуля"
                        )
                    model, _ = import_manager(
                        source.document,
                        project_id=project_id,
                        manager_name=module_name,
                        host=host,
                        format_bindings=bindings,
                        executor_profile=detection.model_profile(),
                    )
                    if model.header.interface_version != 2:
                        _refuse("unsupported_form", "Импорт W1 поддерживает только интерфейс 2")
                else:
                    model = replace(
                        new_manager(project_id=project_id, manager_name=module_name),
                        host=host,
                        format_bindings=bindings,
                        executor_profile=detection.model_profile(),
                    ).with_revision()
                # Разбор порождённого текста до фиксации проекта: отказ не оставляет модель.
                self._manager_snapshot(model, publish=False)
                _atomic_json(folder / "creation.json", metadata, exclusive=True)
                model = self.manager_workspace.create(model).model
                existing = False
            generated_id, _ = self._manager_snapshot(model)
            if metadata.get("rebind_recovery"):
                notices.append(metadata["rebind_recovery"])
            if not detection.verified:
                notices.insert(0, {"id": detection.code})
            if metadata["arguments"]["mode"] == "import" and not metadata["arguments"].get(
                "extensions_checked", False
            ):
                notices.append(
                    {
                        "id": "extensions_unverified",
                        "message": "Расширения не проверялись; "
                        "передайте extensions=[] или явный список",
                    }
                )
            notices_offset = offset if section == "notices" else 0
            notices_page = compact_page({}, notices, notices_offset, limit)
            return {
                "project_id": project_id,
                "revision": model.revision,
                "existing": existing,
                "document_id": generated_id,
                "counts": model.counts,
                "executor_profile": self._manager_profile(detection),
                "notices": notices_page["items"],
                "notice_count": notices_page["total"],
                "notices_offset": notices_offset,
                "notices_has_more": notices_page["has_more"],
                "notices_next_offset": notices_page["next_offset"],
                **({"notices_truncated_by": "size"} if notices_page.get("truncated_by") else {}),
                "reference_manager": reference_manager,
                **({"reference_manager_reason": reference_reason} if reference_reason else {}),
                **({"rebound": True, "validation": validation} if rebinding else {}),
                "import_report": compact_page(
                    {
                        "counts": model.import_report.counts,
                        "diagnostics_count": len(model.import_report.diagnostics),
                    },
                    [
                        {"kind": "diagnostic", "code": code, "address": address}
                        for code, address in model.import_report.diagnostics
                    ]
                    + [asdict(e) for e in model.import_report.entries],
                    offset if section == "import_report" else 0,
                    limit,
                ),
            }

    def _manager_input_differences(self, model, metadata: dict) -> tuple[dict, list[dict]]:
        """Хеши схемы проверяются с диска, структура — по долговечному кэшу."""
        hashes = {}
        changed = []
        for path, expected in metadata["schema_sources"]:
            local = self._read_path(path)
            actual = self._hash_file(local) if local.is_file() else "missing"
            hashes[path] = actual
            if actual != expected:
                changed.append(
                    {"field": "schema", "path": path, "existing": expected, "requested": actual}
                )
        if model.host.structure_id:
            _, actual = self._ed_structure_snapshot(model.host.structure_id)
            hashes["structure"] = actual
            if actual != model.host.structure_hash:
                changed.append(
                    {
                        "field": "structure",
                        "existing": model.host.structure_hash,
                        "requested": actual,
                    }
                )
        args = metadata["arguments"]
        actual_dump = dump_fingerprint(
            tuple(self._read_path(p) for p in (args["configuration_path"], *args["extensions"]))
        )
        hashes["configuration"] = actual_dump
        if (
            metadata.get("dump_fingerprint") != actual_dump
            and delivered_input_fingerprint(self, model.project_id, metadata) != actual_dump
        ):
            changed.append(
                {
                    "field": "configuration",
                    "existing": metadata.get("dump_fingerprint"),
                    "requested": actual_dump,
                }
            )
        return hashes, changed

    def _manager_inputs(self, model, metadata: dict) -> dict:
        hashes, changed = self._manager_input_differences(model, metadata)
        if changed:
            raise EdAuthoringStaleError(
                "Входы проекта менеджера изменились",
                {"changed_inputs": [d["field"] for d in changed], "differences": changed},
            )
        return hashes

    @with_reopen_hints
    def ed_apply(
        self,
        project_id: str,
        expected_revision: str,
        operations: list[dict] | None = None,
        mode: str = "preview",
        expected_preview_hash: str | None = None,
        confirmations: list[dict] | None = None,
        offset: int = 0,
        limit: int = 50,
        section: str = "summary",
    ) -> dict[str, Any]:
        validate_page(offset, limit)
        if mode not in ("preview", "apply") or section not in (
            "summary",
            "operations",
            "changes",
            "notices",
            "failures",
            "skipped",
        ):
            raise ValueError(
                "mode: preview/apply; section: summary/operations/changes/notices/failures/skipped"
            )
        if mode == "apply" and section != "summary":
            raise ValueError("apply допускает только section=summary")
        if operations is None and mode == "apply":
            with self._lock:
                operations = self._manager_preview_packet(project_id, expected_preview_hash)
        if not isinstance(operations, list):
            raise ValueError("operations: нужен список")
        self._limit(len(operations), 100, "операции")
        manager_fields = {
            "manager_name",
            "title",
            "generated_at",
            "text_style",
        }
        for operation in operations:
            if isinstance(operation, dict) and operation.get("kind") == "manager":
                patch = operation.get("patch", {})
                forbidden = (set(patch) - manager_fields) if isinstance(patch, dict) else set()
                clear = operation.get("clear", [])
                if isinstance(clear, list) and all(isinstance(field, str) for field in clear):
                    forbidden.update(set(clear) - manager_fields)
                if forbidden:
                    _refuse(
                        "manager_binding_requires_rebind",
                        'Входы проекта меняются только через ed_create с mode="rebind"',
                        fields=sorted(forbidden),
                    )
        parsed = tuple(parse_operation(_manager_address_aliases(op)) for op in operations)
        acknowledgements = tuple(
            (
                _text(_mapping(c, "confirmation", {"code", "notice_hash"}).get("code"), "code"),
                _text(c.get("notice_hash"), "notice_hash"),
            )
            for c in (confirmations or [])
        )
        with self._lock:
            project = self._manager_project(project_id)
            metadata = self._manager_metadata(project_id)
            inputs = self._manager_inputs(project.model, metadata)
            self.manager_workspace.preview(project_id, (), expected_revision=project.model.revision)
            receipt_key = digest((expected_revision, operations, expected_preview_hash))
            receipt_path = self._manager_file(project_id, "applied/" + receipt_key + ".json")
            if mode == "apply" and receipt_path.is_file():
                try:
                    receipt = json.loads(receipt_path.read_bytes())
                    result = receipt["result"]
                    decisions = receipt["decisions"]
                    receipt_rows = receipt.get("rows")
                    skipped_rows = receipt.get("skipped_rows")
                    if (
                        not isinstance(result, dict)
                        or not isinstance(result.get("revision"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", result["revision"])
                        or result.get("project_id") != project_id
                        or not isinstance(decisions, dict)
                        or not decisions
                        or (
                            receipt_rows is not None
                            and (
                                not isinstance(receipt_rows, list)
                                or not all(isinstance(row, dict) for row in receipt_rows)
                            )
                        )
                        or (
                            skipped_rows is not None
                            and (
                                not isinstance(skipped_rows, list)
                                or not all(isinstance(row, dict) for row in skipped_rows)
                            )
                        )
                        or not all(
                            isinstance(k, str)
                            and isinstance(v, str)
                            and re.fullmatch(r"[0-9a-f]{64}", v)
                            for k, v in decisions.items()
                        )
                    ):
                        raise ValueError("Неверная форма квитанции")
                except (ValueError, KeyError, TypeError):
                    _refuse(
                        "receipt_corrupt",
                        "Повреждена квитанция применённого пакета",
                        receipt=receipt_path.name,
                    )
                known = {d.client_id: d.operation_hash for d in project.model.decisions}
                if decisions and all(known.get(k) == v for k, v in decisions.items()):
                    current_id, _ = self._manager_snapshot(project.model)
                    replay = {
                        **result,
                        "replayed": True,
                        "applied": False,
                        "reason": "already_applied",
                        "revision": project.model.revision,
                        "future_revision": project.model.revision,
                        "counts": project.model.counts,
                        "document_id": current_id,
                    }
                    if receipt_rows is not None:
                        if skipped_rows is not None:
                            replay["skipped"] = compact_page({}, skipped_rows, 0, min(limit, 5))
                        header = {
                            k: v
                            for k, v in replay.items()
                            if k
                            not in {
                                "items",
                                "offset",
                                "limit",
                                "total",
                                "has_more",
                                "next_offset",
                                "truncated_by",
                            }
                        }
                        return compact_page(header, receipt_rows, offset, limit)
                    if offset != result.get("offset", 0):
                        _refuse(
                            "receipt_page_unavailable",
                            "Старая квитанция хранит только одну страницу; "
                            "используйте текущий document_id для навигации",
                            revision=project.model.revision,
                            document_id=current_id,
                        )
                    return replay
            if expected_revision != project.model.revision:
                raise EdAuthoringStaleError(
                    "Ревизия менеджера устарела", {"revision": project.model.revision}
                )
            planned = self.manager_workspace.preview(
                project_id, parsed, expected_revision=expected_revision
            )
            preview_hash = digest((planned.preview_hash, inputs))
            if mode == "apply" and expected_preview_hash != preview_hash:
                raise EdAuthoringStaleError(
                    "Хеш просмотра менеджера устарел",
                    {"revision": project.model.revision, "preview_hash": preview_hash},
                )
            rows = {
                "operations": [json_value(o) for o in planned.operations],
                "changes": [json_value(c) for c in planned.changes],
                "notices": [json_value(n) for n in planned.notices],
                "failures": [json_value(f) for f in planned.failures],
                "skipped": [{"client_id": s} for s in planned.skipped],
            }
            summary_rows = (
                [{"kind": "notice", **n} for n in rows["notices"]]
                + [
                    {
                        "kind": "change",
                        "address": c.address,
                        "action": c.action,
                        "fields": list(c.fields),
                    }
                    for c in planned.changes
                ]
                + [
                    {
                        "kind": "operation",
                        "client_id": o.operation.client_id,
                        "operation_kind": o.operation.kind,
                        "action": o.operation.action,
                        "result_id": o.result_id,
                        "operation_hash": o.operation_hash,
                    }
                    for o in planned.operations
                ]
            )
            base = {
                "project_id": project_id,
                "revision": project.model.revision,
                "future_revision": planned.future_revision,
                "preview_hash": preview_hash,
                "applied": False,
                "section": section,
                "counts": planned.model.counts,
                "failures": compact_page(
                    {}, rows["failures"], offset if section == "failures" else 0, min(limit, 5)
                ),
                "skipped": compact_page(
                    {}, rows["skipped"], offset if section == "skipped" else 0, min(limit, 5)
                ),
                "required_confirmations": [
                    {"code": n.code, "notice_hash": n.notice_hash} for n in planned.notices[:10]
                ],
                "confirmation_count": len(planned.notices),
                "confirmations_has_more": len(planned.notices) > 10,
                "confirmations_next_offset": min(10, len(planned.notices)),
                "change_count": len(planned.changes),
                "operation_count": len(planned.operations),
            }
            if planned.future_revision == project.model.revision and not planned.failures:
                base["reason"] = "already_applied" if parsed else "no_operations"
            if mode == "apply":
                # Порождаем и читаем будущий снимок до атомарной смены модели.
                self._manager_snapshot(planned.model, publish=False)
                missing = [
                    n for n in planned.notices if (n.code, n.notice_hash) not in acknowledgements
                ]
                if missing:
                    raise EdAuthoringAckRequiredError("Подтвердите замечания просмотра", base)
                if planned.failures:
                    raise EdAuthoringPreconditionError(
                        "Пакет содержит ошибки",
                        {
                            **base,
                            "failures": [
                                {
                                    "id": f.reason,
                                    "address": f.address,
                                    "message": f.message,
                                    "references": list(f.references),
                                }
                                for f in planned.failures[:5]
                            ],
                            "failure_count": len(planned.failures),
                            "failures_has_more": len(planned.failures) > 5,
                            "failures_next_offset": min(5, len(planned.failures)),
                        },
                    )
                updated = self.manager_workspace.apply(
                    project_id,
                    parsed,
                    expected_revision=expected_revision,
                    expected_preview_hash=planned.preview_hash,
                    confirmations=acknowledgements,
                )
                current_id, _ = self._manager_snapshot(updated.model)
                base.update(
                    revision=updated.model.revision,
                    future_revision=updated.model.revision,
                    document_id=current_id,
                    counts=updated.model.counts,
                    applied=updated.model.revision != project.model.revision,
                )
                result = compact_page(
                    base,
                    summary_rows,
                    offset,
                    limit,
                )
                selected_clients = {op.client_id for op in parsed}
                if base["applied"]:
                    _atomic_json(
                        receipt_path,
                        {
                            "result": result,
                            "rows": summary_rows,
                            "skipped_rows": [{"client_id": s} for s in planned.skipped],
                            "decisions": {
                                d.client_id: d.operation_hash
                                for d in updated.model.decisions
                                if d.client_id in selected_clients
                            },
                        },
                    )
                return result
            self._manager_save_preview(project_id, project.model.revision, preview_hash, operations)
            return compact_page(
                base,
                rows[section] if section != "summary" else summary_rows,
                offset,
                limit,
            )

    def _manager_validation(
        self, project_id: str, text: str, document_id: str, structure_id: str | None = None
    ):
        with self._lock:
            model = self._manager_project(project_id).model
            current_id = "ed-manager-" + digest((project_id, model.revision))[:24]
            if document_id != current_id:
                raise EdAuthoringStaleError(
                    "Снимок менеджера устарел",
                    {"revision": model.revision, "document_id": current_id},
                )
            detection = self._manager_detection(
                self._manager_metadata(project_id), model.executor_profile.profile_id
            )
            report = validate_writer(
                model,
                text,
                detection=detection,
                **self._manager_name_scope(self._manager_metadata(project_id), structure_id),
            )
            report.extend(self._manager_plan_content(model, self._manager_metadata(project_id))[0])
            return report, {
                "writer": {
                    "project_id": project_id,
                    "revision": model.revision,
                    "executor_profile": self._manager_profile(detection),
                }
            }

    def _manager_check_document(self, project_id: str, document_id: str) -> None:
        model = self._manager_project(project_id).model
        current_id = "ed-manager-" + digest((project_id, model.revision))[:24]
        if document_id != current_id:
            raise EdAuthoringStaleError(
                "Снимок менеджера устарел", {"revision": model.revision, "document_id": current_id}
            )

    def _manager_close(self, project_id: str) -> dict | None:
        # Закрытие допускает повреждённый manifest и незавершённый creation.json.
        if len(project_id) > 128 or project_id in self._ed_projects:
            return None  # Коллизии ID запрещены; обычный снимок закрывает читатель.
        folder = self.manager_workspace._folder(project_id)
        self._safe_path(folder)
        if not folder.is_dir() or not any(
            (folder / name).is_file() for name in ("manager.ed.json", "creation.json")
        ):
            return None
        with self.manager_workspace._disk_lock(project_id):
            for path in folder.rglob("*"):
                self._safe_path(path)
            for child in folder.iterdir():
                if child.name == ".lock":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        folder.rmdir()
        self.manager_workspace = ManagerWorkspace(self.workspace.root)
        document_id = self._manager_documents.pop(project_id, None)
        if document_id:
            self._ed_projects.pop(document_id, None)
        return {"project_id": project_id, "closed": True}

    def _manager_candidates(self, target, kind, text, offset, limit, reference_document_id):
        validate_page(offset, limit)
        if not isinstance(text, str):
            raise ValueError("text должен быть строкой")
        value = _mapping(
            target,
            "target",
            {
                "schema_id",
                "structure_id",
                "direction",
                "configuration_object",
                "format_type",
                "project_id",
            },
        )
        if kind not in ("objects", "properties"):
            raise ValueError("kind: objects или properties")
        schema_id = _text(value.get("schema_id"), "schema_id")
        structure_id = _text(value.get("structure_id"), "structure_id")
        direction = _text(value.get("direction"), "direction")
        if kind == "objects" and any(k in value for k in ("configuration_object", "format_type")):
            raise ValueError("Выбранная пара относится только к kind=properties")
        if kind == "properties" and text:
            raise ValueError("properties не принимает text")
        with self._lock:
            schema = self._ed_schemas.get(schema_id)
            if schema is None:
                raise EdSchemaNotFoundError("Схема формата не открыта")
            with self._structure(structure_id) as connection:
                provenance = dict(connection.execute("SELECT key,value FROM meta"))
                manager_id = value.get("project_id")
                metadata = None
                if manager_id is not None:
                    model = self._manager_project(_text(manager_id, "project_id")).model
                    metadata = self._manager_metadata(manager_id)
                    self._manager_structure_provenance(structure_id, metadata)
                    root = self._read_path(model.host.configuration).resolve()
                else:
                    root = (
                        self._read_path(self._host_text(provenance["source_path"])).resolve()
                        if provenance.get("source_path")
                        else None
                    )
                if kind == "properties" and reference_document_id is None:
                    return _compact_property_candidates(
                        property_candidates(
                            connection,
                            schema.schema,
                            _text(value.get("configuration_object"), "configuration_object"),
                            _text(value.get("format_type"), "format_type"),
                            direction=direction,
                            offset=offset,
                            limit=limit,
                        )
                    )
                if reference_document_id is None:
                    page = object_candidates(
                        connection,
                        schema.schema,
                        direction=direction,
                        text=text,
                        offset=offset,
                        limit=limit,
                    )
                    hint = (
                        [
                            {
                                "id": "reference_manager_hint",
                                "message": "По именам пары не найдены; подключите типовой "
                                "менеджер для смысловых переименований",
                                "hint": 'reference_document_id="auto"',
                            }
                        ]
                        if page["total"] == 0
                        else []
                    )
                    return {
                        **page,
                        "next_offset": page["offset"] + len(page["items"]),
                        "notices": hint,
                    }
                if manager_id is None and (provenance.get("source") != "xml" or root is None):
                    _refuse("model_invalid", "Для структуры без XML-источника нужен project_id")
                assert root is not None
                roots = ()
                automatic_reference = reference_document_id == "auto"
                if automatic_reference:
                    if metadata is not None:
                        roots = extension_paths(
                            metadata["arguments"]["extensions"], self._read_path
                        )
                    else:
                        try:
                            names = json.loads(provenance.get("extensions", "[]"))
                            paths = json.loads(provenance.get("extension_paths", "[]"))
                            if (
                                not isinstance(names, list)
                                or not isinstance(paths, list)
                                or len(names) != len(paths)
                                or not all(isinstance(n, str) for n in names)
                                or not all(
                                    isinstance(p, str) and Path(p).is_absolute() for p in paths
                                )
                            ):
                                raise ValueError("Нет путей расширений")
                            roots = extension_paths(paths, self._read_path)
                            checked_extensions(root, roots)
                        except (ValueError, Kd2Error, OSError):
                            _refuse(
                                "structure_extension_paths_unavailable",
                                "Пути расширений не сохранены или недоступны: "
                                "нужен target.project_id или явный документ reference_document_id",
                            )
                        self._manager_structure_provenance(
                            structure_id,
                            {
                                "arguments": {
                                    "configuration_path": self._host(root),
                                    "extensions": [self._host(p) for p in roots],
                                }
                            },
                        )
                    plan = metadata["arguments"]["plan"] if metadata is not None else None
                    key = (
                        metadata["arguments"]["format_version"]
                        if metadata is not None
                        else schema.format_version
                    )
                    reference_info, reference_reason = self._manager_reference(
                        root, roots, plan, key
                    )
                    if reference_info is None:
                        _refuse(
                            "reference_manager_unavailable",
                            "Типовой менеджер этой версии не определён; используйте ed_routes "
                            "и передайте reference_document_id явно",
                            reference_manager_reason=reference_reason,
                        )
                    opened = self.ed_open(
                        path=reference_info["path"],
                        **(
                            {
                                "configuration_path": self._host(root),
                                "extensions": [self._host(p) for p in roots],
                            }
                            if roots
                            else {}
                        ),
                    )
                    if opened.get("source_changed"):
                        raise EdAuthoringStaleError(
                            "Типовой менеджер изменился; закройте reference_document_id "
                            "через ed_close и повторите reference_document_id=auto",
                            {"reference_document_id": opened["project_id"]},
                        )
                    reference_document_id = opened["project_id"]
                reference = self._ed_project(reference_document_id)
                if (
                    not (root / "Configuration.xml").is_file()
                    or (reference.layered and not automatic_reference)
                    or not any(
                        reference.path.resolve().is_relative_to(folder / "CommonModules")
                        for folder in (root, *(roots if automatic_reference else ()))
                    )
                ):
                    _refuse("model_invalid", "Типовой менеджер должен быть из той же конфигурации")
                reference_document, reference_index = reference.document, reference.index
                if reference.layered:
                    view = select_views(reference.layered, direction, False)[0]
                    reference_document, reference_index = view.document, view.index
                source_fingerprint = (
                    digest(tuple(f.sha256 for f in reference_document.files))
                    if reference.layered
                    else reference_document.files[0].sha256
                )
                if kind == "properties":
                    return _compact_property_candidates(
                        property_candidates(
                            connection,
                            schema.schema,
                            _text(value.get("configuration_object"), "configuration_object"),
                            _text(value.get("format_type"), "format_type"),
                            direction=direction,
                            offset=offset,
                            limit=limit,
                            reference_document=reference_document,
                            reference_document_id=reference_document_id,
                            reference_index=reference_index,
                        )
                    )
                # Собираем все страницы перед объединением: offset применяется к общему списку.
                result = object_candidates(
                    connection, schema.schema, direction=direction, text=text, limit=200
                )
                rows = list(result["items"])
                while result["has_more"]:
                    result = object_candidates(
                        connection,
                        schema.schema,
                        direction=direction,
                        text=text,
                        offset=result["offset"] + len(result["items"]),
                        limit=200,
                    )
                    rows.extend(result["items"])
                namespaces = {
                    t.qname.local: t.qname.namespace
                    for t in schema.schema.types.values()
                    if t.qname
                }
                config_names = {
                    (k.casefold(), n.casefold()): (k, n)
                    for k, n in connection.execute("SELECT kind, name FROM objects")
                }
                present = {
                    (r["configuration"].casefold(), r["format_type"].casefold()) for r in rows
                }
                binding = digest(
                    (
                        sha256(connection.serialize()),
                        schema.schema.schema_id,
                        source_fingerprint,
                    )
                )
                applicability = Applicability.build(
                    reference_document,
                    ValidationProfile.build(schema.schema, schema.format_version, direction),
                )
                for rule in reference_document.pko:
                    if applicability.evaluate(rule, direction) is False:
                        continue
                    cfg, _ = metadata_key(rule.configuration_object.value)
                    fmt = rule.format_object.value if rule.format_object else None
                    if cfg is None or fmt not in namespaces or cfg not in config_names:
                        continue
                    full_name = ".".join(config_names[cfg])
                    if text.casefold() not in (full_name + " " + fmt).casefold():
                        continue
                    pair = (full_name.casefold(), fmt.casefold())
                    if pair in present:
                        continue
                    present.add(pair)
                    rows.append(
                        {
                            "candidate_id": digest(
                                (
                                    source_fingerprint,
                                    binding,
                                    full_name,
                                    fmt,
                                    direction,
                                )
                            ),
                            "configuration": full_name,
                            "format_type": fmt,
                            "namespace": namespaces[fmt],
                            "direction": direction,
                            "confidence": "reference",
                            "reason": "так в типовом модуле",
                            "auto": False,
                            "origin": {
                                "document_id": reference_document_id,
                                "address": address_of(rule, reference_index),
                            },
                        }
                    )
                return compact_page({"notices": []}, rows, offset, min(limit, 200))

    def _manager_previous(
        self, destination: Path
    ) -> tuple[ManagerManifest | None, dict[str, bytes]]:
        self._safe_path(destination)
        if not destination.exists():
            return None, {}
        if not destination.is_dir():
            raise EdAuthoringPathError("Каталог занят чужим файлом", {})
        payloads = {}
        directories = []
        for path in destination.rglob("*"):
            self._safe_path(path)
            if path.is_file():
                payloads[path.relative_to(destination).as_posix()] = path.read_bytes()
                self._limit(len(payloads), 1000, "файлы комплекта")
                self._limit(sum(map(len, payloads.values())), 32 * 1024 * 1024, "байты комплекта")
            elif path.is_dir():
                directories.append(path.relative_to(destination).as_posix())
        if not payloads:
            if directories:
                raise EdAuthoringPathError("В каталоге чужие подкаталоги", {})
            return None, {}
        if "manifest.json" not in payloads:
            raise EdAuthoringPathError("В каталоге чужие файлы", {})
        try:
            manifest = ManagerManifest.from_bytes(payloads["manifest.json"])
            validate_manager_files(manifest.to_dict(), payloads)
            allowed = {
                p.as_posix() for name in payloads for p in Path(name).parents if p.as_posix() != "."
            }
            if set(directories) - allowed:
                raise EdAuthoringPathError("В каталоге чужие подкаталоги", {})
            return manifest, payloads
        except AuthoringPreconditionError as error:
            raise _failure(error) from error

    def _manager_host(self, metadata, route):
        root = self._read_path(metadata["arguments"]["configuration_path"])
        description = (root / "Configuration.xml").read_text("utf-8-sig")
        config = read_description("Configuration.xml", description, "Configuration")
        language = config.props.get("DefaultLanguage", "").removeprefix("Language.")
        names = [
            "Configuration.xml",
            "Languages/" + language + ".xml",
            "ExchangePlans/" + route.plan_name + ".xml",
        ]
        sources = {
            name: self._read_path(self._host(root / name)).read_text("utf-8-sig") for name in names
        }
        identity = {
            k: v for k, v in metadata["arguments"]["identity"].items() if k != "module_name"
        }
        return read_manager_host(
            sources, route.plan_name, identity=ExtensionIdentity(**identity)
        ), digest(sources)

    def _manager_build(
        self,
        project_id,
        expected_revision,
        route,
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
        registration_objects=None,
        registered_objects=None,
        plan_stubs=True,
    ):
        validate_page(offset, limit)
        if section not in ("summary", "operations", "notices", "files", "issues_after", "skipped"):
            raise ValueError(
                "Раздел менеджера: summary, operations, notices, files, issues_after, skipped"
            )
        validate_options("issues_after", level, check_prefix, address_prefix)
        options = delivery_options(delivery)
        if mode not in ("preview", "write"):
            raise ValueError("Менеджер: mode=preview/write")
        if type(plan_stubs) is not bool:
            raise ValueError("plan_stubs: true или false")
        if mode == "write" and section != "summary":
            raise ValueError("write допускает только summary")
        if acknowledged_notices is not None and (
            not isinstance(acknowledged_notices, list)
            or any(not isinstance(n, str) for n in acknowledged_notices)
        ):
            raise ValueError("acknowledged_notices: список строк")
        with self._lock:
            model = self._manager_project(_text(project_id, "project_id")).model
            if expected_revision is None:
                expected_revision = model.revision
            elif expected_revision != model.revision:
                raise EdAuthoringStaleError(
                    "Ревизия менеджера устарела", {"revision": model.revision}
                )
            metadata = self._manager_metadata(project_id)
            args = metadata["arguments"]
            route_data = _mapping(route or {}, "route", {"plan", "format_version"})
            selected = ManagerRoute(
                _text(route_data.get("plan", args["plan"]), "route.plan"),
                _text(
                    route_data.get("format_version", args["format_version"]), "route.format_version"
                ),
            )
            if not isinstance(selected.plan_name, str) or not valid_identifier(selected.plan_name):
                _refuse("route_scope_conflict", "Недопустимое имя плана")
            try:
                inputs = self._manager_inputs(model, metadata)
                detection = self._manager_detection(metadata, model.executor_profile.profile_id)
                rendered = render(model, "preserve" if model.source_files else "canonical")
                report = validate_writer(
                    model, rendered.data, detection=detection, **self._manager_name_scope(metadata)
                )
                required_unfilled: list[RequiredUnfilled] = []
                report.extend(
                    self._manager_schema_checks(
                        model, metadata, required_unfilled=required_unfilled
                    )
                )
                registration_objects = (
                    registration_objects if registration_objects is not None else []
                )
                registered_objects = registered_objects if registered_objects is not None else []
                content_report, missing_content, missing_subscriptions, registration_names = (
                    self._manager_plan_delivery(
                        model,
                        metadata,
                        registration_objects,
                        selected.plan_name,
                        registered_objects=registered_objects,
                        plan_stubs=plan_stubs,
                    )
                )
                report.extend(content_report)
                _, snapshot = self._manager_snapshot(model, publish=False)
                structure = (
                    self._ed_structure_snapshot(args["structure_id"])[0]
                    if args["structure_id"]
                    else None
                )
                report.extend(
                    validate_sender_handlers(
                        model,
                        snapshot.document,
                        ValidationProfile.build(
                            self._manager_bound_schema(metadata), str(selected.version_key)
                        ),
                        structure,
                        selected.plan_name,
                        missing_content,
                        registration_names,
                    )
                )
                structure = (
                    self._ed_structure_snapshot(model.host.structure_id)[0]
                    if model.host.structure_id
                    else None
                )
                _, stub_objects = validate_plan_pods(
                    model, structure, selected.plan_name, missing_content, registration_names
                )
                delivery_model, pod_stubs = add_plan_stubs(
                    model, stub_objects if plan_stubs else ()
                )
                if pod_stubs:
                    rendered = render(
                        delivery_model, "preserve" if model.source_files else "canonical"
                    )
                    report.extend(
                        validate_writer(
                            delivery_model,
                            rendered.data,
                            detection=detection,
                            **self._manager_name_scope(metadata),
                        )
                    )
                delivered_events = {
                    (subscription.event, source)
                    for subscription, sources in missing_subscriptions
                    for source in sources
                }
                if len(delivered_events) != sum(
                    i.check == "ed.plan.registration_unsubscribed" for i in content_report.issues
                ):
                    _refuse(
                        "registration_subscription_missing",
                        "Нет типовой подписки для дополнения источников",
                    )
                subscriptions = []
                for subscription, sources in missing_subscriptions:
                    description = self._manager_content_descriptions(
                        metadata, (("EventSubscription", subscription.name, "EventSubscriptions"),)
                    )[0]
                    if description.uuid.casefold() != subscription.uuid.casefold():
                        _refuse(
                            "snapshot_mismatch",
                            "UUID подписки не совпадает со структурой: " + subscription.name,
                        )
                    subscriptions.append(SubscriptionAddition(description, sources))
                # Источник заимствованной подписки должен быть заимствован расширением;
                # объекты штатного состава плана заимствуются без изменения состава.
                subscription_objects = subscription_source_objects(
                    missing_subscriptions, missing_content
                )
                root = self._read_path(args["configuration_path"])
                for _, name, folder in subscription_objects:
                    if not (root / folder / (name + ".xml")).is_file():
                        _refuse(
                            "subscription_source_unavailable",
                            "Источник подписки отсутствует в выгрузке основной конфигурации: "
                            + folder
                            + "/"
                            + name,
                        )
                errors = [
                    {"id": i.check, **i.to_dict()}
                    for i in report.issues
                    if i.level == Level.ERROR
                    and not (
                        i.check == "ed.writer.profile"
                        and i.message.startswith("executor_profile_unverified:")
                    )
                ]
                if errors:
                    raise EdAuthoringPreconditionError(
                        "Менеджер не прошёл проверки", {"failures": errors}
                    )
                host, host_hash = self._manager_host(metadata, selected)
                destination = self._destination(
                    artifact_name(host.identity.name, host.configuration_uuid), output_dir
                )
                previous, previous_files = (
                    (None, {})
                    if options["mode"] == "user_extension"
                    else self._manager_previous(destination)
                )
                if previous and previous.project_id != project_id:
                    _refuse(
                        "kit_owned_by_other_project",
                        f"Комплект проекта «{previous.project_id}» нельзя заменить "
                        f"проектом «{project_id}». Выберите другое identity.name "
                        "или закройте прежний проект.",
                        owner_project_id=previous.project_id,
                        project_id=project_id,
                    )
                kit = render_manager_kit(
                    delivery_model,
                    rendered,
                    host,
                    selected,
                    executor_profile_id=model.executor_profile.profile_id,
                    previous_manifest=previous,
                    previous_files=previous_files if previous else None,
                    creation_fingerprint=metadata["creation_fingerprint"],
                    content_objects=self._manager_content_descriptions(metadata, missing_content),
                    subscription_additions=tuple(subscriptions),
                    subscription_objects=self._manager_content_descriptions(
                        metadata, subscription_objects
                    ),
                    registration_objects=registration_names,
                    registered_objects=tuple(sorted(set(registered_objects or ()))),
                    pod_stubs=pod_stubs,
                    data_preflight=render_data_preflight(required_unfilled),
                )
                kit = with_key_data_instruction(
                    kit, self._manager_key_instruction(model, metadata), previous, previous_files
                )
                user_delivery = (
                    prepare_user_delivery(
                        self,
                        options,
                        self._read_path(args["configuration_path"]),
                        dict(kit.files),
                        destination,
                        project_id,
                    )
                    if options["mode"] == "user_extension"
                    else None
                )
                build_hash = digest(
                    (
                        model.revision,
                        kit.manifest.to_dict(),
                        detection,
                        inputs,
                        host_hash,
                        {p: sha256(b) for p, b in previous_files.items()},
                        user_delivery.fingerprint if user_delivery else None,
                    )
                )
                notices = (
                    [{"id": "executor_profile_unverified", **self._manager_profile(detection)}]
                    if not detection.verified
                    else []
                )
                notices.extend(
                    {"id": i.check + ":" + digest(i.to_dict())[:16], **i.to_dict()}
                    for i in report.issues
                    if i.level == Level.WARNING
                )
                if previous and previous.creation_fingerprint != metadata["creation_fingerprint"]:
                    notices.append(
                        {
                            "id": "kit_creation_inputs_changed",
                            "project_id": project_id,
                            "previous": previous.creation_fingerprint,
                            "current": metadata["creation_fingerprint"],
                        }
                    )
                required = [n["id"] for n in notices]
                base = {
                    "scope": "manager",
                    "project_id": project_id,
                    "revision": model.revision,
                    "document_id": self._manager_snapshot(model)[0],
                    "section": section,
                    "build_hash": build_hash,
                    "status": kit.status,
                    "written": False,
                    "output_dir": self._host(destination),
                    "counts": model.counts,
                    "runtime_verified": False,
                    "required_acknowledgements": required[:20],
                    "acknowledgement_count": len(required),
                    "acknowledgements_has_more": len(required) > 20,
                    "acknowledgements_next_offset": min(20, len(required)),
                    "validation": {
                        "errors": 0,
                        "warnings": sum(i.level == Level.WARNING for i in report.issues),
                        "profile_unverified": not detection.verified,
                    },
                }
                if mode == "write":
                    if expected_preview_hash != build_hash:
                        raise EdAuthoringStaleError(
                            "Хеш сборки менеджера устарел",
                            {"revision": model.revision, "build_hash": build_hash},
                        )
                    if set(required) - set(acknowledged_notices or []):
                        raise EdAuthoringAckRequiredError("Подтвердите замечания сборки", base)

                    def verify():
                        self.manager_workspace.preview(
                            project_id, (), expected_revision=model.revision
                        )
                        current_inputs = self._manager_inputs(model, metadata)
                        current_detection = self._manager_detection(
                            metadata, model.executor_profile.profile_id
                        )
                        _, current_host_hash = self._manager_host(metadata, selected)
                        if (current_inputs, current_detection, current_host_hash) != (
                            inputs,
                            detection,
                            host_hash,
                        ):
                            raise EdAuthoringStaleError(
                                "Входы сборки изменились во время записи", {}
                            )

                    base["status"] = (
                        user_delivery.write(verify, lambda: manager_input_receipt(self, metadata))
                        if user_delivery
                        else self._write_artifact(
                            destination,
                            kit.files,
                            previous_files,
                            [],
                            previous_reader=self._manager_previous,
                            verify=verify,
                        )
                    )
                    base["written"] = True
                file_rows = [
                    {"name": p, "size": len(b), "sha256": sha256(b)}
                    for p, b in (user_delivery.files if user_delivery else kit.files).items()
                ]
                if user_delivery:
                    base["delivery"] = {**options, "files": user_delivery.merge.rows()}
                issues = [
                    i.to_dict()
                    for i in report.issues
                    if (not level or i.level.value == level)
                    and (not check_prefix or i.check.startswith(check_prefix))
                    and (
                        not address_prefix
                        or i.address.casefold().startswith(address_prefix.casefold())
                    )
                ]
                rows = {
                    "notices": [
                        n
                        for n in notices
                        if (not level or n.get("level") == level)
                        and (not check_prefix or n.get("check", "").startswith(check_prefix))
                        and (
                            not address_prefix
                            or n.get("address", "").casefold().startswith(address_prefix.casefold())
                        )
                    ],
                    "files": file_rows,
                    "operations": [
                        {
                            "client_id": d.client_id,
                            "operation_hash": d.operation_hash,
                            "result_ids": list(d.result_ids),
                        }
                        for d in model.decisions
                    ],
                    "issues_after": issues,
                    "skipped": [s.to_dict() for s in report.skipped],
                }
                return compact_page(
                    base,
                    rows[section]
                    if section != "summary"
                    else [{"kind": "notice", **n} for n in rows["notices"]]
                    + [{"kind": "file", **f} for f in file_rows],
                    offset,
                    limit,
                    budget=MANAGER_NOTICES_BYTES if section == "notices" else PAGE_BYTES,
                )
            except AuthoringPreconditionError as error:
                raise _failure(error) from error
            except (OSError, UnicodeError) as error:
                raise EdAuthoringIoError(
                    "Ошибка чтения или записи комплекта менеджера", {}
                ) from error
