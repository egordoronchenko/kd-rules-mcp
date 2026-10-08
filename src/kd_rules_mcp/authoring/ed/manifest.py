"""Воспроизводимый manifest и свидетельство владения переданными байтами."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from typing import Literal

from .hook import valid_identifier
from .identity import IdentityMap, refuse
from .model import (
    AddHeaderProperty,
    AttributeDraft,
    AuthoringTarget,
    CanonicalHeaderProperty,
    ExtensionIdentity,
    Operation,
    PreparedAuthoring,
    ProfileComparison,
    ProfileReport,
    SourceSet,
)

Delivery = Literal["extension", "manual"]
GENERATOR_VERSION = "ed-authoring/1"
GENERATOR_VERSION_V2 = "ed-authoring/2"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def manager_decision_hash(inputs: Mapping[str, object]) -> str:
    """Отдельный контракт ed-manager/1; хеш не зависит от прежней сборки и времени."""
    return sha256(json_bytes(dict(inputs)))


def manager_changes(
    entities: Mapping[str, str],
    previous: Mapping[str, str] | None = None,
    *,
    reasons: tuple[str, ...] = (),
) -> dict[str, tuple[str, ...]]:
    """Дельта отпечатков по устойчивым ID; отпечаток включает и порождённый текст."""
    previous = previous or {}
    result = {
        "added": tuple(sorted(entities.keys() - previous.keys())),
        "changed": tuple(
            sorted(k for k in entities.keys() & previous.keys() if entities[k] != previous[k])
        ),
        "removed": tuple(sorted(previous.keys() - entities.keys())),
    }
    if reasons:
        result["reasons"] = reasons
    return result


def operation_dict(op: AddHeaderProperty) -> dict:
    draft = op.new_attribute
    return {
        "operation_id": op.operation_id,
        "target": asdict(op.target),
        "configuration_attribute": op.configuration_attribute,
        "format_property": op.format_property,
        "new_attribute": {
            "name": draft.name,
            "synonym": draft.synonym,
            "primitive": draft.primitive,
            "qualifiers": dict(draft.qualifiers),
        }
        if draft
        else None,
    }


def operation_from_dict(value: dict) -> Operation:
    if value.get("kind", "add_header_property") == "add_header_property":
        draft = AttributeDraft(**value["new_attribute"]) if value["new_attribute"] else None
        # Прочитанная метка не даёт свидетельства канонизации для генератора.
        op: Operation = CanonicalHeaderProperty(
            AuthoringTarget(**value["target"]),
            value["configuration_attribute"],
            value["format_property"],
            draft,
        )
    else:
        from .handlers import operation_from_input

        payload = {
            key: item for key, item in value.items() if key not in ("operation_id", "dependencies")
        }
        try:
            op = operation_from_input(payload)
        except (KeyError, TypeError, ValueError):
            refuse("owned_content_changed", "Решения в manifest изменены")
    if op.operation_id != value["operation_id"]:
        refuse("owned_content_changed", "Решения в manifest изменены")
    return op


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    schema_version: int
    generator_version: str
    identity: ExtensionIdentity
    identity_map: IdentityMap
    base_configuration_uuid: str
    source_set: SourceSet
    source_hashes: Mapping[str, str]
    operations: tuple[AddHeaderProperty, ...]
    file_hashes: Mapping[str, str]
    build_hash: str
    delivery: Delivery
    runtime_verified: bool
    unverified: tuple[str, ...]
    notices: tuple[str, ...]
    procedures: tuple[Mapping[str, object], ...] = ()
    handler_bindings: tuple[Mapping[str, object], ...] = ()
    dispatcher_name: str = ""
    dispatcher_order: tuple[str, ...] = ()
    # Не в operations: сервис первого среза читает это поле как прямые ПКС.
    handler_operations: tuple[Operation, ...] = ()
    # Первые 12 символов decision_hash итогового набора; в XML — свойство «Версия».
    extension_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_hashes", MappingProxyType(dict(self.source_hashes)))
        object.__setattr__(self, "file_hashes", MappingProxyType(dict(self.file_hashes)))

    def to_dict(self) -> dict:
        if self.schema_version == 1:
            if self.handler_operations:
                refuse(
                    "owned_content_changed", "Манифест первой версии содержит операции обработчиков"
                )
            operations = [operation_dict(op) for op in self.operations]
        else:
            from .handlers import canonical_operation_dict
            from .model import order_operations

            operations = [
                canonical_operation_dict(op)
                for op in order_operations((*self.operations, *self.handler_operations))
            ]
        payload = {
            "schema_version": self.schema_version,
            "generator_version": self.generator_version,
            "identity": asdict(self.identity),
            "identity_map": {
                "artifact_uuid": self.identity_map.artifact_uuid,
                "objects": dict(self.identity_map.objects),
                "borrowed": dict(self.identity_map.borrowed),
            },
            "base_configuration_uuid": self.base_configuration_uuid,
            "source_set": asdict(self.source_set),
            "source_hashes": dict(self.source_hashes),
            "operations": operations,
            "file_hashes": dict(self.file_hashes),
            "build_hash": self.build_hash,
            "delivery": self.delivery,
            "runtime_verified": self.runtime_verified,
            "unverified": list(self.unverified),
            "notices": list(self.notices),
        }
        if self.schema_version >= 2:
            payload["bindings"] = [dict(item) for item in self.handler_bindings]
            payload["dispatcher_name"] = self.dispatcher_name
            payload["dispatcher_order"] = list(self.dispatcher_order)
            payload["procedures"] = [dict(item) for item in self.procedures]
            payload["extension_version"] = self.extension_version
        return payload

    def to_bytes(self) -> bytes:
        return json_bytes(self.to_dict())

    def changed_inputs(self, current: ArtifactManifest) -> dict[str, tuple[str, ...]]:
        """Группы изменившихся входов и относительные имена; хеши наружу не выдаём."""
        changed: dict[str, set[str]] = {}
        for group, fields in {
            "configuration": ("project", "configuration"),
            "structure": ("structure_hash",),
            "routes": ("routes_hash",),
            "extensions": ("extensions", "extensions_hash"),
            "schemas": ("schemas_hash",),
            "files": ("document_hash",),
        }.items():
            if any(getattr(self.source_set, f) != getattr(current.source_set, f) for f in fields):
                changed[group] = set()
        for name in self.source_hashes.keys() | current.source_hashes.keys():
            if self.source_hashes.get(name) == current.source_hashes.get(name):
                continue
            changed.setdefault("files", set()).add(name)
            group = (
                "extensions"
                if name.startswith("extensions/")
                else "schemas"
                if name.startswith(("XDTOPackages/", "schemas/"))
                else "configuration"
                if name == "Configuration.xml"
                else "routes"
                if name.startswith(("ExchangePlans/", "Subsystems/"))
                else None
            )
            if group:
                changed.setdefault(group, set()).add(name)
        return {group: tuple(sorted(names)) for group, names in sorted(changed.items())}

    @classmethod
    def from_bytes(cls, content: bytes) -> ArtifactManifest:
        try:
            value = json.loads(content)
            source = value["source_set"]
            source["extensions"] = tuple(source["extensions"])
            version = value["schema_version"]
            parsed = tuple(operation_from_dict(op) for op in value["operations"])
            headers = tuple(op for op in parsed if isinstance(op, AddHeaderProperty))
            handlers = {}
            if version >= 2:
                handlers = {
                    "procedures": tuple(value["procedures"]),
                    "handler_bindings": tuple(value["bindings"]),
                    "dispatcher_name": value["dispatcher_name"],
                    "dispatcher_order": tuple(value["dispatcher_order"]),
                    "handler_operations": tuple(
                        op for op in parsed if not isinstance(op, AddHeaderProperty)
                    ),
                    "extension_version": value["extension_version"],
                }
            elif len(headers) != len(parsed):
                refuse(
                    "owned_content_changed", "Манифест первой версии содержит операции обработчиков"
                )
            return cls(
                version,
                value["generator_version"],
                ExtensionIdentity(**value["identity"]),
                IdentityMap(**value["identity_map"]),
                value["base_configuration_uuid"],
                SourceSet(**source),
                value["source_hashes"],
                headers,
                value["file_hashes"],
                value["build_hash"],
                value["delivery"],
                value["runtime_verified"],
                tuple(value["unverified"]),
                tuple(value["notices"]),
                **handlers,
            )
        except (KeyError, TypeError, ValueError, UnicodeError):
            refuse("owned_content_changed", "Прежний manifest повреждён")
            raise AssertionError("недостижимо") from None


def validate_previous(manifest: ArtifactManifest, files: Mapping[str, bytes]) -> None:
    """Manifest не хеширует себя: вместо этого сверяется собственное представление."""
    expected = {1: GENERATOR_VERSION, 2: GENERATOR_VERSION_V2}.get(manifest.schema_version)
    if (
        expected is None
        or manifest.generator_version != expected
        or set(files) != {*manifest.file_hashes, "manifest.json"}
        or files.get("manifest.json") != manifest.to_bytes()
        or any(sha256(files[p]) != h for p, h in manifest.file_hashes.items() if p in files)
    ):
        refuse(
            "owned_content_changed",
            "Содержимое прежнего результата изменено или содержит неизвестные файлы",
        )
    for path in files:
        if path in (
            "manifest.json",
            "instruction.md",
            "validation.json",
            "extension/Configuration.xml",
        ):
            continue
        pattern = re.fullmatch(
            r"(?:modules|extension)/CommonModules/([^/]+)/Ext/Module\.bsl|"
            r"extension/(?:Languages|CommonModules|Catalogs|Documents)/([^/]+)\.xml",
            path,
        )
        if pattern is None or not valid_identifier(pattern[1] or pattern[2]):
            refuse("owned_content_changed", "Прежний комплект содержит файл вне профиля")
    xml = {
        p.removeprefix("extension/"): b.decode("utf-8")
        for p, b in files.items()
        if p.startswith("extension/") and p.endswith(".xml")
    }
    if xml:
        from .identity import identity_map_from_xml

        actual = identity_map_from_xml(
            xml, manifest.base_configuration_uuid, manifest.identity.name
        )
        if actual != manifest.identity_map:
            refuse("owned_content_changed", "Карта UUID manifest не совпадает с прежними XML")


def overlay_report_view(prepared: PreparedAuthoring) -> PreparedAuthoring:
    """Комплект наложения описывает код; общие исходные проверки остаются полными.

    Проверка данных required_unfilled входит в check_profile и послойный валидатор,
    но блок запросов проверки данных доставляется только комплектом менеджера.
    """
    check = "ed.schema.required_unfilled"

    def profile(value: ProfileReport) -> ProfileReport:
        return replace(
            value,
            issues=tuple(i for i in value.issues if i.check != check),
            skipped=tuple(s for s in value.skipped if s.check != check),
        )

    def comparison(value: ProfileComparison) -> ProfileComparison:
        return replace(
            value,
            before=profile(value.before),
            after=profile(value.after),
            delta=replace(
                value.delta,
                new=tuple(i for i in value.delta.new if i.check != check),
                disappeared=tuple(i for i in value.delta.disappeared if i.check != check),
                new_relevant_skipped=tuple(
                    s for s in value.delta.new_relevant_skipped if s.check != check
                ),
            ),
        )

    return replace(
        prepared,
        selected_profiles=tuple(comparison(c) for c in prepared.selected_profiles),
        other_profiles=tuple(comparison(c) for c in prepared.other_profiles),
        skipped=tuple(s for s in prepared.skipped if s.check != check),
    )


def validation_dict(prepared: PreparedAuthoring) -> dict:
    prepared = overlay_report_view(prepared)

    def profiles(comparisons):
        return [
            {
                "version": c.before.version,
                "direction": c.before.direction,
                "before": [i.to_dict() for i in c.before.issues],
                "after": [i.to_dict() for i in c.after.issues],
                "new": [i.to_dict() for i in c.delta.new],
                "disappeared": [i.to_dict() for i in c.delta.disappeared],
                "new_relevant_skipped": [s.to_dict() for s in c.delta.new_relevant_skipped],
                "skipped_before": [s.to_dict() for s in c.before.skipped],
                "skipped_after": [s.to_dict() for s in c.after.skipped],
            }
            for c in comparisons
        ]

    return {
        "build_hash": prepared.build_hash,
        "runtime_verified": prepared.runtime_verified,
        "selected_profiles": profiles(prepared.selected_profiles),
        "other_profiles": profiles(prepared.other_profiles),
        "notices": [{**asdict(n), "notice_id": n.notice_id} for n in prepared.notices],
        "skipped": [s.to_dict() for s in prepared.skipped],
    }
