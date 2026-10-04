"""Комплект из проверенных решений: чистое порождение и проверка прежних байтов."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from kd2_rules_mcp.ed.schema.model import EdSchema

from .handler_render import (
    binding_record,
    pko_names_from_document,
    procedure_records,
    render_handler_module,
)
from .handlers import HandlerOperationsPlan
from .hook import generate_hook
from .identity import IdentityMap, make_identity_map, refuse
from .instruction import render_handlers_instruction, render_instruction
from .manifest import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_V2,
    ArtifactManifest,
    Delivery,
    json_bytes,
    sha256,
    validate_previous,
    validation_dict,
)
from .model import (
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    AuthoringInputs,
    CanonicalHeaderProperty,
    ExtensionIdentity,
    PreparedAuthoring,
    digest,
    order_operations,
)
from .xml_dump import DumpMetadata, dump_extension, identity_roles, read_metadata


@dataclass(frozen=True, slots=True)
class RenderedAuthoring:
    files: Mapping[str, bytes]
    manifest: ArtifactManifest
    instruction: str
    status: str
    prepared: PreparedAuthoring

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


def _source_hashes(prepared: PreparedAuthoring, metadata: DumpMetadata) -> dict[str, str]:
    """Те же относительные отпечатки входов, что у комплекта первого среза."""
    sources = {path: sha256(text.encode("utf-8")) for path, text in metadata.source_texts.items()}
    for source in prepared.projection_before.files:
        parts = source.path.replace("\\", "/").split("/")
        relative = (
            "/".join(parts[parts.index("CommonModules") :])
            if "CommonModules" in parts
            else "modules/" + parts[-1]
        )
        sources[relative] = source.sha256
    assert prepared.preparation_inputs is not None
    for version, schema in sorted(prepared.preparation_inputs.schemas.items()):
        if isinstance(schema, EdSchema):
            for number, package in enumerate(schema.packages):
                for part, source in enumerate(package.sources):
                    filename = source.path.replace("\\", "/").rsplit("/", 1)[-1]
                    sources[f"schemas/{version}/{number}/{part}/{filename}"] = source.sha256
    return sources


def render_authoring(
    prepared: PreparedAuthoring,
    descriptions: Mapping[str, str],
    *,
    delivery: Delivery = "extension",
    previous_manifest: ArtifactManifest | None = None,
    previous_files: Mapping[str, bytes] | None = None,
    identity_map: IdentityMap | None = None,
) -> RenderedAuthoring:
    """Ни чтения, ни записи: прежние файлы передаются вызывающим вместе с manifest.

    Дополнение повторно проверяет union ядром A. Его входные снимки сохранены
    в PreparedAuthoring; при их отсутствии дополнение безопасно отклоняется.
    """
    if delivery not in ("extension", "manual"):
        refuse("delivery_unsupported", "Форма выдачи: extension или manual")
    if bool(previous_manifest) != (previous_files is not None):
        refuse("owned_content_changed", "Прежний manifest и все файлы нужны вместе")
    if not prepared.operations or any(
        not isinstance(op, CanonicalHeaderProperty) or not op.is_canonical()
        for op in prepared.operations
    ):
        refuse("unprepared_operations", "Нужны канонические операции результата подготовки")
    if any(not c.delta.no_new_issues for c in prepared.selected_profiles):
        refuse("new_issues", "Выбранные профили результата подготовки содержат новые замечания")
    if prepared.preparation_inputs is None:
        refuse("snapshot_mismatch", "Отсутствуют прочитанные входы результата подготовки")
    previous = previous_manifest
    if previous is not None:
        assert previous_files is not None
        validate_previous(previous, previous_files)
        if previous.schema_version != 1:
            refuse(
                "owned_content_changed",
                "Комплект с обработчиками пересобирается отдельным просмотром",
            )
        if previous.identity != prepared.identity or previous.source_set != prepared.source_set:
            refuse(
                "owned_content_changed",
                "Решения идентичности или входы прежнего комплекта изменились",
            )
        operations = order_operations((*previous.operations, *prepared.operations))
        headers = tuple(op for op in operations if isinstance(op, AddHeaderProperty))
        if len(headers) != len(operations):
            refuse("owned_content_changed", "Манифест первой версии содержит операции обработчиков")
        if headers != prepared.operations:
            if prepared.preparation_inputs is None:
                refuse(
                    "snapshot_mismatch",
                    "Для перепроверки объединения отсутствуют прочитанные входы подготовки",
                )
            from kd2_rules_mcp.validation.ed_authoring import prepare_authoring

            prepared = prepare_authoring(
                prepared.preparation_inputs, headers, prepared.identity, version_scope="manager"
            )
    hook = generate_hook(
        prepared.projection_before,
        prepared.operations,
        prepared.identity,
        path=prepared.generated_hook.source.path,
    )
    if (
        hook.source.text != prepared.generated_hook.source.text
        or hook.calls != prepared.generated_hook.calls
    ):
        refuse("snapshot_mismatch", "Перехватчик не совпадает с проверенным результатом ядра")
    metadata = read_metadata(prepared, descriptions)
    sources = _source_hashes(prepared, metadata)
    if previous and any(sources.get(p) != h for p, h in previous.source_hashes.items()):
        refuse("owned_content_changed", "Исходные описания прежнего комплекта изменились")
    paths, borrowed = identity_roles(metadata)
    ids = make_identity_map(
        metadata.configuration.uuid,
        prepared.identity.name,
        paths,
        borrowed,
        previous=previous.identity_map if previous else None,
        external=identity_map,
    )
    try:
        xml_files = dump_extension(prepared, metadata, ids)
    except ValueError:
        refuse("metadata_profile_unsupported", "Решения содержат символы, недопустимые в XML")
    module_path = "CommonModules/" + metadata.module.name + "/Ext/Module.bsl"
    content = prepared.generated_hook.source.text.encode("utf-8")
    result: dict[str, bytes] = {"modules/" + module_path: content}
    if delivery == "extension":
        result.update({"extension/" + p: b for p, b in xml_files.items()})
        result["extension/" + module_path] = content
    result["validation.json"] = json_bytes(validation_dict(prepared))
    instruction = render_instruction(
        prepared, metadata, delivery, (*result, "manifest.json", "instruction.md")
    )
    result["instruction.md"] = instruction.encode("utf-8")
    manifest = ArtifactManifest(
        1,
        GENERATOR_VERSION,
        prepared.identity,
        ids,
        metadata.configuration.uuid,
        prepared.source_set,
        sources,
        prepared.operations,
        {p: sha256(b) for p, b in sorted(result.items())},
        prepared.build_hash,
        delivery,
        prepared.runtime_verified,
        tuple(s.reason for s in prepared.skipped),
        tuple(n.notice_id for n in prepared.notices),
    )
    result["manifest.json"] = manifest.to_bytes()
    status = (
        "unchanged"
        if previous_files is not None and dict(result) == dict(previous_files)
        else "ready"
    )
    return RenderedAuthoring(
        MappingProxyType(dict(sorted(result.items()))), manifest, instruction, status, prepared
    )


def render_handlers_authoring(
    inputs: AuthoringInputs,
    plan: HandlerOperationsPlan,
    identity: ExtensionIdentity,
    descriptions: Mapping[str, str],
    *,
    delivery: Delivery = "extension",
    previous_manifest: ArtifactManifest | None = None,
    previous_files: Mapping[str, bytes] | None = None,
    identity_map: IdentityMap | None = None,
    drop_operations: frozenset[str] = frozenset(),
    keep_handlers_version: bool = False,
    form_evidence: Mapping[str, bool] | None = None,
) -> RenderedAuthoring:
    """Полный комплект версии 2. ``plan`` — итоговый набор: функция к нему ничего не добавляет.

    Состав любой: только обработчики, прямые ПКС вместе с обработчиками (XML нового реквизита
    пишет код первого среза), алгоритмические ПКС. Без единой привязки и без алгоритмической
    ПКС комплект остаётся формой v1 через ``render_authoring``. Прежний комплект — файлы с диска
    версии 1 или 2. Тот же план даёт те же байты и без прежнего комплекта; совпадение с ним —
    ``status="unchanged"``. Снятая операция должна быть перечислена в ``drop_operations``.
    ``keep_handlers_version`` сохраняет v2 после снятия последней привязки и для прямого
    менеджера внутри общего комплекта v2; проверку прежних файлов выполняет сервис.
    """
    if delivery not in ("extension", "manual"):
        refuse("delivery_unsupported", "Форма выдачи: extension или manual")
    if bool(previous_manifest) != (previous_files is not None):
        refuse("owned_content_changed", "Прежний manifest и все файлы нужны вместе")
    algorithmic = any(isinstance(op, AddAlgorithmicHeaderProperty) for op in plan.operations)
    if (
        not plan.bindings
        and not algorithmic
        and not (
            keep_handlers_version or (previous_manifest and previous_manifest.schema_version == 2)
        )
    ):
        refuse(
            "unprepared_operations",
            "комплект только из прямых ПКС собирается `render_authoring`",
        )
    if plan.manager_interface != inputs.document.manager_version:
        refuse("unprepared_operations", "Интерфейс плана не совпадает с менеджером входов")
    previous = previous_manifest
    if previous is not None:
        assert previous_files is not None
        validate_previous(previous, previous_files)
        if previous.schema_version >= 2:
            from .artifacts import previous_artifact

            previous_artifact(previous_files)
        if previous.identity != identity or previous.source_set != inputs.source_set:
            refuse(
                "owned_content_changed",
                "Решения идентичности или входы прежнего комплекта изменились",
            )
    previous_ids = (
        {op.operation_id for op in (*previous.operations, *previous.handler_operations)}
        if previous is not None
        else set()
    )
    plan_ids = {op.operation_id for op in plan.operations}
    unknown = sorted(set(drop_operations) - previous_ids)
    if unknown:
        refuse(
            "unprepared_operations",
            "Идентификаторы drop_operations отсутствуют в прежнем комплекте: " + ", ".join(unknown),
        )
    kept = sorted(set(drop_operations) & plan_ids)
    if kept:
        refuse(
            "unprepared_operations",
            "Идентификаторы drop_operations остаются в плане: " + ", ".join(kept),
        )
    removed = sorted(previous_ids - plan_ids - set(drop_operations))
    if removed:
        refuse(
            "owned_content_changed",
            "Снятые операции не перечислены в drop_operations: " + ", ".join(removed),
        )
    headers = tuple(op for op in plan.operations if isinstance(op, AddHeaderProperty))
    from kd2_rules_mcp.validation.ed_authoring import (
        _prepare_metadata_shell,
        prepare_authoring,
    )

    if headers:
        prepared = prepare_authoring(inputs, headers, identity, version_scope="manager")
        # Тот же запрет, что у комплекта первого среза: прямые ПКС не добавляют замечаний.
        if any(not item.delta.no_new_issues for item in prepared.selected_profiles):
            refuse(
                "new_issues",
                "Выбранные профили результата подготовки содержат новые замечания",
            )
    else:
        prepared = _prepare_metadata_shell(inputs, identity)
    metadata = read_metadata(prepared, descriptions)
    sources = _source_hashes(prepared, metadata)
    if previous and any(sources.get(path) != item for path, item in previous.source_hashes.items()):
        refuse("owned_content_changed", "Исходные описания прежнего комплекта изменились")
    paths, borrowed = identity_roles(metadata)
    ids = make_identity_map(
        metadata.configuration.uuid,
        identity.name,
        paths,
        borrowed,
        previous=previous.identity_map if previous else None,
        external=identity_map,
    )
    try:
        xml_files = dump_extension(prepared, metadata, ids)
    except ValueError:
        refuse("metadata_profile_unsupported", "Решения содержат символы, недопустимые в XML")
    try:
        module = (
            prepared.generated_hook.source.text
            if not plan.bindings
            else render_handler_module(
                plan.operations,
                plan.bindings,
                prefix=identity.prefix,
                interface=plan.manager_interface,
                dispatcher_name=plan.dispatcher_name,
                dispatcher_order=plan.dispatcher_order,
                pko_names=pko_names_from_document(inputs.document),
            )
        )
    except ValueError as error:
        refuse("unprepared_operations", str(error))
    content = module.encode("utf-8")
    module_path = "CommonModules/" + metadata.module.name + "/Ext/Module.bsl"
    result: dict[str, bytes] = {"modules/" + module_path: content}
    if delivery == "extension":
        result.update({"extension/" + path: payload for path, payload in xml_files.items()})
        result["extension/" + module_path] = content
    ordered = order_operations(plan.operations)
    procedures = (
        procedure_records(
            ordered,
            plan.bindings,
            dispatcher_name=plan.dispatcher_name,
            dispatcher_order=plan.dispatcher_order,
            runtime_verified=plan.runtime_verified,
        )
        if plan.bindings
        else ()
    )
    by_name = {binding.handler_name: binding for binding in plan.bindings}
    bindings = tuple(binding_record(by_name[name]) for name in plan.dispatcher_order)
    build_hash = digest(
        (
            GENERATOR_VERSION_V2,
            plan.decision_hash,
            identity,
            inputs.source_set,
            plan.runtime_verified,
            plan.dispatcher_order,
        )
    )
    validation = validation_dict(prepared)
    validation.update(
        build_hash=build_hash,
        runtime_verified=plan.runtime_verified,
        schema_version=2,
        generator_version=GENERATOR_VERSION_V2,
        handlers={
            "dispatcher_name": plan.dispatcher_name,
            "dispatcher_order": list(plan.dispatcher_order),
            "bindings": [dict(item) for item in bindings],
        },
    )
    result["validation.json"] = json_bytes(validation)
    instruction = render_handlers_instruction(
        plan,
        extension_name=identity.name,
        prefix=identity.prefix,
        build_hash=build_hash,
        interface=plan.manager_interface,
        delivery=delivery,
        paths=tuple(sorted((*result, "manifest.json", "instruction.md"))),
        form_evidence=form_evidence,
        pko_names=pko_names_from_document(inputs.document),
    )
    result["instruction.md"] = instruction.encode("utf-8")
    manifest = ArtifactManifest(
        2,
        GENERATOR_VERSION_V2,
        identity,
        ids,
        metadata.configuration.uuid,
        inputs.source_set,
        sources,
        tuple(op for op in ordered if isinstance(op, AddHeaderProperty)),
        {path: sha256(payload) for path, payload in sorted(result.items())},
        build_hash,
        delivery,
        plan.runtime_verified,
        tuple(item.reason for item in prepared.skipped),
        tuple(dict.fromkeys(item.notice_id for item in plan.notices)),
        procedures,
        bindings,
        plan.dispatcher_name,
        plan.dispatcher_order,
        tuple(op for op in ordered if not isinstance(op, AddHeaderProperty)),
    )
    result["manifest.json"] = manifest.to_bytes()
    status = (
        "unchanged"
        if previous_files is not None and dict(result) == dict(previous_files)
        else "ready"
    )
    return RenderedAuthoring(
        MappingProxyType(dict(sorted(result.items()))),
        manifest,
        instruction,
        status,
        prepared,
    )
