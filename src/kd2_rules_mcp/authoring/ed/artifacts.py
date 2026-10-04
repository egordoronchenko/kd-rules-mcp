"""Чистая политика имён и владения комплектом; файловую систему обслуживает сервис."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from uuid import UUID

from lxml import etree

from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key

from .hook import valid_identifier
from .identity import IdentityMap, refuse
from .manifest import ArtifactManifest, json_bytes, sha256, validate_previous
from .model import SourceSet, digest, order_operations
from .render import RenderedAuthoring
from .xml_dump import M, serialize


def artifact_name(extension_name: str, base_uuid: str) -> str:
    if not valid_identifier(extension_name):
        raise ValueError("Недопустимое имя расширения")
    return extension_name + "-" + str(UUID(base_uuid))[:8]


def previous_artifact(files: Mapping[str, bytes]) -> ArtifactManifest | None:
    """Пустой каталог допустим; чужие файлы отличает сервис до вызова этой функции."""
    if not files:
        return None
    if "manifest.json" not in files:
        refuse("owned_content_changed", "Каталог не содержит собственного manifest")
    manifest = ArtifactManifest.from_bytes(files["manifest.json"])
    validate_previous(manifest, files)
    return manifest


def with_source_hashes(bundle: RenderedAuthoring, sources: Mapping[str, str]) -> RenderedAuthoring:
    """Закрепляет реальные относительные пути входов, не меняя порождённые файлы."""
    if any(
        not name or name.startswith("/") or "\\" in name or ":" in name or ".." in name.split("/")
        for name in sources
    ):
        raise ValueError("Исходные файлы manifest требуют относительных путей выгрузки")
    manifest = replace(bundle.manifest, source_hashes=sources)
    return replace(
        bundle, manifest=manifest, files={**bundle.files, "manifest.json": manifest.to_bytes()}
    )


def combine_artifacts(bundles: Sequence[RenderedAuthoring]) -> RenderedAuthoring:
    """Объединяет уже проверенные менеджеры одной конфигурации без второго hook.

    Общие XML-описания могут различаться только ChildObjects: список модулей
    конфигурации и новые реквизиты одного владельца. Иные расхождения — отказ.
    """
    if not bundles:
        raise ValueError("Нужен хотя бы один комплект")
    if len(bundles) == 1:
        return bundles[0]
    bundles = sorted(bundles, key=lambda b: b.prepared.generated_hook.source.path)
    first = bundles[0]
    files: dict[str, bytes] = {}
    objects: dict[str, str] = {}
    borrowed: dict[str, str] = {}
    sources: dict[str, str] = {}
    drafts = {}
    for bundle in bundles:
        index = build_addresses(bundle.prepared.projection_before)
        for operation in bundle.prepared.operations:
            if operation.new_attribute:
                rule = index.find(operation.target.pko_address)
                assert isinstance(rule, ObjectRule)
                owner, _ = metadata_key(rule.configuration_object.value)
                key = (owner, operation.new_attribute.name.casefold())
                if key in drafts and drafts[key] != operation.new_attribute:
                    refuse(
                        "identifier_conflict",
                        "Разные черновики одного реквизита",
                        address=operation.target.pko_address,
                    )
                drafts[key] = operation.new_attribute
        if (
            bundle.manifest.identity != first.manifest.identity
            or bundle.manifest.base_configuration_uuid != first.manifest.base_configuration_uuid
            or bundle.manifest.delivery != first.manifest.delivery
        ):
            refuse("snapshot_mismatch", "Комплекты относятся к разным конфигурациям или решениям")
        for key, value in bundle.manifest.source_hashes.items():
            if key in sources and sources[key] != value:
                refuse("snapshot_mismatch", "Разные снимки одного исходного файла", key)
            sources[key] = value
        objects.update(bundle.manifest.identity_map.objects)
        borrowed.update(bundle.manifest.identity_map.borrowed)
        for path, content in bundle.files.items():
            if path in ("manifest.json", "validation.json", "instruction.md"):
                continue
            old = files.get(path)
            if old is None or old == content:
                files[path] = content
                continue
            if not path.startswith("extension/") or not path.endswith(".xml"):
                refuse("identifier_conflict", "Два менеджера порождают разное содержимое", path)
            parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
            left, right = etree.fromstring(old, parser), etree.fromstring(content, parser)
            child_tag = f"{{{M}}}ChildObjects"
            left_children, right_children = left[0].find(child_tag), right[0].find(child_tag)
            if left_children is None or right_children is None:
                refuse("identifier_conflict", "Конфликт общих описаний", path)
            left[0].remove(left_children)
            right[0].remove(right_children)
            if serialize(left) != serialize(right):
                refuse("identifier_conflict", "Разные свойства одного владельца", path)
            merged: dict[tuple[str, str], etree._Element] = {}
            for child in (*left_children, *right_children):
                name = child.findtext(f"{{{M}}}Properties/{{{M}}}Name") or child.text or ""
                key = (str(child.tag), name)
                if key in merged and serialize(merged[key]) != serialize(child):
                    refuse("identifier_conflict", "Разные черновики одного реквизита", path)
                merged[key] = child
            left_children[:] = [merged[k] for k in sorted(merged)]
            left[0].append(left_children)
            files[path] = serialize(left)
    operations = order_operations(tuple(op for b in bundles for op in b.manifest.operations))
    source = first.manifest.source_set
    combined_source = SourceSet(
        source.project,
        source.configuration,
        digest(tuple(b.manifest.source_set.document_hash for b in bundles)),
        digest(tuple(b.manifest.source_set.schemas_hash for b in bundles)),
        source.structure_hash,
        source.routes_hash,
        source.extensions,
        source.extensions_hash,
    )
    build_hash = digest(tuple(b.manifest.build_hash for b in bundles))
    import json

    files["validation.json"] = json_bytes(
        {
            "build_hash": build_hash,
            "runtime_verified": False,
            "managers": [json.loads(b.files["validation.json"]) for b in bundles],
        }
    )
    instruction = "\n\n".join(b.instruction.rstrip() for b in bundles) + "\n"
    files["instruction.md"] = instruction.encode("utf-8")
    manifest = replace(
        first.manifest,
        identity_map=IdentityMap(first.manifest.identity_map.artifact_uuid, objects, borrowed),
        source_set=combined_source,
        source_hashes=sources,
        operations=operations,
        file_hashes={p: sha256(b) for p, b in sorted(files.items())},
        build_hash=build_hash,
        unverified=tuple(dict.fromkeys(s for b in bundles for s in b.manifest.unverified)),
        notices=tuple(n for b in bundles for n in b.manifest.notices),
    )
    files["manifest.json"] = manifest.to_bytes()
    return RenderedAuthoring(files, manifest, instruction, "ready", first.prepared)
