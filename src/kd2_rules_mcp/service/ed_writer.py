"""Сквозной авторинг менеджера: долговечная модель и читаемый снимок результата."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, NoReturn

from kd2_rules_mcp.authoring.ed.artifacts import artifact_name, validate_manager_files
from kd2_rules_mcp.authoring.ed.hook import valid_identifier
from kd2_rules_mcp.authoring.ed.manager_candidates import object_candidates, property_candidates
from kd2_rules_mcp.authoring.ed.manager_operations import parse_operation
from kd2_rules_mcp.authoring.ed.manager_render import (
    ManagerManifest,
    ManagerRoute,
    render_manager_kit,
    render_manager_route,
)
from kd2_rules_mcp.authoring.ed.manifest import sha256
from kd2_rules_mcp.authoring.ed.model import AuthoringPreconditionError, ExtensionIdentity
from kd2_rules_mcp.authoring.ed.workspace import ManagerWorkspace
from kd2_rules_mcp.authoring.ed.xml_dump import read_description, read_manager_host
from kd2_rules_mcp.ed import build_references
from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.executor_profile import ProfileDetection, detect_profile
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_import import import_manager
from kd2_rules_mcp.ed.writer_model import (
    Evidence,
    FormatBinding,
    Host,
    digest,
    json_bytes,
    json_value,
)
from kd2_rules_mcp.errors import (
    EdAuthoringAckRequiredError,
    EdAuthoringIoError,
    EdAuthoringPathError,
    EdAuthoringPreconditionError,
    EdAuthoringStaleError,
    EdSchemaNotFoundError,
    Kd2Error,
    ProjectNotFoundError,
)
from kd2_rules_mcp.projects import resolve
from kd2_rules_mcp.service.ed import EdProject
from kd2_rules_mcp.service.ed_authoring import EdAuthoringMixin, _failure, _mapping, _text
from kd2_rules_mcp.service.ed_authoring_views import compact_page, validate_options
from kd2_rules_mcp.service.ed_layers import checked_extensions, extension_paths
from kd2_rules_mcp.service.ed_views import address_of, validate_page
from kd2_rules_mcp.service.paths import Settings
from kd2_rules_mcp.structures.store import dump_fingerprint
from kd2_rules_mcp.validation.ed_links import validate_links
from kd2_rules_mcp.validation.ed_schema import validate_schema
from kd2_rules_mcp.validation.ed_structure import validate_structure
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key
from kd2_rules_mcp.validation.ed_writer import validate_writer
from kd2_rules_mcp.validation.report import Level


def _refuse(reason: str, message: str, **details) -> NoReturn:
    raise EdAuthoringPreconditionError(
        message, {"failures": [{"id": reason, "message": message}], **details}
    )


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

    def _manager_rebind_report(self, model, metadata: dict, detection: ProfileDetection):
        """Новые привязки проверяются и по форме писателя, и по схеме/структуре."""
        _, snapshot = self._manager_snapshot(model, publish=False)
        report = validate_writer(model, snapshot.document.files[0].text, detection=detection)
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
    ) -> dict[str, Any]:
        validate_page(offset, limit)
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
                        + re.sub(r"\W", "_", project_id)[:32]
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
                differences = [
                    {"field": k, "existing": metadata["arguments"].get(k), "requested": v}
                    for k, v in args.items()
                    if metadata["arguments"].get(k) != v
                ]
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
                    notices.extend(
                        compact_page(
                            {}, [{"id": i.check, **i.to_dict()} for i in report.issues], 0, 10
                        )["items"]
                    )
                    # Журнал публикуется первым; решение в сохранённой модели
                    # фиксирует новую сторону транзакции независимо от дальнейших правок.
                    with self.manager_workspace._disk_lock(project_id):
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
                    differences_page = compact_page({}, differences, offset, limit)
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
            return {
                "project_id": project_id,
                "revision": model.revision,
                "existing": existing,
                "document_id": generated_id,
                "counts": model.counts,
                "executor_profile": self._manager_profile(detection),
                "notices": notices,
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
                    offset,
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
        if metadata.get("dump_fingerprint") != actual_dump:
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

    def ed_apply(
        self,
        project_id: str,
        expected_revision: str,
        operations: list[dict],
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
        ):
            raise ValueError("mode: preview/apply; section: summary/operations/changes/notices")
        if mode == "apply" and section != "summary":
            raise ValueError("apply допускает только section=summary")
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
        parsed = tuple(parse_operation(op) for op in operations)
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
                    if (
                        not isinstance(result, dict)
                        or not isinstance(result.get("revision"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", result["revision"])
                        or result.get("project_id") != project_id
                        or not isinstance(decisions, dict)
                        or not decisions
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
                    return {
                        **result,
                        "replayed": True,
                        "applied": False,
                        "reason": "already_applied",
                        "revision": project.model.revision,
                        "future_revision": project.model.revision,
                        "counts": project.model.counts,
                        "document_id": current_id,
                    }
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
                "failures": compact_page({}, [json_value(f) for f in planned.failures], 0, 5),
                "skipped": compact_page({}, [{"client_id": s} for s in planned.skipped], 0, 5),
                "required_confirmations": [
                    {"code": n.code, "notice_hash": n.notice_hash} for n in planned.notices[:10]
                ],
                "confirmation_count": len(planned.notices),
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
                            "decisions": {
                                d.client_id: d.operation_hash
                                for d in updated.model.decisions
                                if d.client_id in selected_clients
                            },
                        },
                    )
                return result
            return compact_page(
                base,
                rows[section] if section != "summary" else summary_rows,
                offset,
                limit,
            )

    def _manager_validation(self, project_id: str, text: str, document_id: str):
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
            report = validate_writer(model, text, detection=detection)
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
        if kind == "properties" and (reference_document_id is not None or text):
            raise ValueError("properties не принимает text или reference_document_id")
        with self._lock:
            schema = self._ed_schemas.get(schema_id)
            if schema is None:
                raise EdSchemaNotFoundError("Схема формата не открыта")
            with self._structure(structure_id) as connection:
                provenance = dict(connection.execute("SELECT key,value FROM meta"))
                manager_id = value.get("project_id")
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
                if kind == "properties":
                    return property_candidates(
                        connection,
                        schema.schema,
                        _text(value.get("configuration_object"), "configuration_object"),
                        _text(value.get("format_type"), "format_type"),
                        direction=direction,
                        offset=offset,
                        limit=limit,
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
                    return {**page, "next_offset": page["offset"] + len(page["items"])}
                if manager_id is None and (provenance.get("source") != "xml" or root is None):
                    _refuse("model_invalid", "Для структуры без XML-источника нужен project_id")
                assert root is not None
                reference = self._ed_project(reference_document_id)
                if (
                    not (root / "Configuration.xml").is_file()
                    or reference.layered
                    or not reference.path.resolve().is_relative_to(root / "CommonModules")
                ):
                    _refuse("model_invalid", "Типовой менеджер должен быть из той же конфигурации")
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
                        reference.document.files[0].sha256,
                    )
                )
                applicability = Applicability.build(
                    reference.document,
                    ValidationProfile.build(schema.schema, schema.format_version, direction),
                )
                for rule in reference.document.pko:
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
                                    reference.document.files[0].sha256,
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
                                "address": address_of(rule, reference.index),
                            },
                        }
                    )
                return compact_page({}, rows, offset, min(limit, 200))

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
    ):
        validate_page(offset, limit)
        if section not in ("summary", "operations", "notices", "files", "issues_after", "skipped"):
            raise ValueError(
                "Раздел менеджера: summary, operations, notices, files, issues_after, skipped"
            )
        validate_options("issues_after", level, check_prefix, address_prefix)
        if mode not in ("preview", "write") or delivery != "extension":
            raise ValueError("Менеджер: mode=preview/write, delivery=extension")
        if mode == "write" and section != "summary":
            raise ValueError("write допускает только summary")
        if acknowledged_notices is not None and (
            not isinstance(acknowledged_notices, list)
            or any(not isinstance(n, str) for n in acknowledged_notices)
        ):
            raise ValueError("acknowledged_notices: список строк")
        with self._lock:
            model = self._manager_project(_text(project_id, "project_id")).model
            if expected_revision != model.revision:
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
                report = validate_writer(model, rendered.data, detection=detection)
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
                previous, previous_files = self._manager_previous(destination)
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
                    model,
                    rendered,
                    host,
                    selected,
                    executor_profile_id=model.executor_profile.profile_id,
                    previous_manifest=previous,
                    previous_files=previous_files if previous else None,
                    creation_fingerprint=metadata["creation_fingerprint"],
                )
                build_hash = digest(
                    (
                        model.revision,
                        kit.manifest.to_dict(),
                        detection,
                        inputs,
                        host_hash,
                        {p: sha256(b) for p, b in previous_files.items()},
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

                    base["status"] = self._write_artifact(
                        destination,
                        kit.files,
                        previous_files,
                        [],
                        previous_reader=self._manager_previous,
                        verify=verify,
                    )
                    base["written"] = True
                file_rows = [
                    {"name": p, "size": len(b), "sha256": sha256(b)} for p, b in kit.files.items()
                ]
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
                    "notices": notices,
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
                    else [{"kind": "notice", **n} for n in notices]
                    + [{"kind": "file", **f} for f in file_rows],
                    offset,
                    limit,
                )
            except AuthoringPreconditionError as error:
                raise _failure(error) from error
            except (OSError, UnicodeError) as error:
                raise EdAuthoringIoError(
                    "Ошибка чтения или записи комплекта менеджера", {}
                ) from error
