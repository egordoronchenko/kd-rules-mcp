"""Комплект из проверенных решений: чистое порождение и проверка прежних байтов."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from kd2_rules_mcp.ed.schema.model import EdSchema

from .hook import generate_hook
from .identity import IdentityMap, make_identity_map, refuse
from .instruction import render_instruction
from .manifest import (
    GENERATOR_VERSION,
    ArtifactManifest,
    Delivery,
    json_bytes,
    sha256,
    validate_previous,
    validation_dict,
)
from .model import CanonicalHeaderProperty, PreparedAuthoring, order_operations
from .xml_dump import dump_extension, identity_roles, read_metadata


@dataclass(frozen=True, slots=True)
class RenderedAuthoring:
    files: Mapping[str, bytes]
    manifest: ArtifactManifest
    instruction: str
    status: str
    prepared: PreparedAuthoring

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


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
        if previous.identity != prepared.identity or previous.source_set != prepared.source_set:
            refuse(
                "owned_content_changed",
                "Решения идентичности или входы прежнего комплекта изменились",
            )
        operations = order_operations((*previous.operations, *prepared.operations))
        if operations != prepared.operations:
            if prepared.preparation_inputs is None:
                refuse(
                    "snapshot_mismatch",
                    "Для перепроверки объединения отсутствуют прочитанные входы подготовки",
                )
            from kd2_rules_mcp.validation.ed_authoring import prepare_authoring

            prepared = prepare_authoring(
                prepared.preparation_inputs, operations, prepared.identity, version_scope="manager"
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
    sources = {p: sha256(t.encode("utf-8")) for p, t in metadata.source_texts.items()}
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
