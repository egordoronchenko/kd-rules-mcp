"""Воспроизводимый manifest и свидетельство владения переданными байтами."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
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
    PreparedAuthoring,
    SourceSet,
)

Delivery = Literal["extension", "manual"]
GENERATOR_VERSION = "ed-authoring/1"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


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


def operation_from_dict(value: dict) -> AddHeaderProperty:
    draft = AttributeDraft(**value["new_attribute"]) if value["new_attribute"] else None
    # Прочитанная метка не даёт свидетельства канонизации для генератора.
    op = CanonicalHeaderProperty(
        AuthoringTarget(**value["target"]),
        value["configuration_attribute"],
        value["format_property"],
        draft,
    )
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_hashes", MappingProxyType(dict(self.source_hashes)))
        object.__setattr__(self, "file_hashes", MappingProxyType(dict(self.file_hashes)))

    def to_dict(self) -> dict:
        return {
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
            "operations": [operation_dict(op) for op in self.operations],
            "file_hashes": dict(self.file_hashes),
            "build_hash": self.build_hash,
            "delivery": self.delivery,
            "runtime_verified": self.runtime_verified,
            "unverified": list(self.unverified),
            "notices": list(self.notices),
        }

    def to_bytes(self) -> bytes:
        return json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, content: bytes) -> ArtifactManifest:
        try:
            value = json.loads(content)
            source = value["source_set"]
            source["extensions"] = tuple(source["extensions"])
            return cls(
                value["schema_version"],
                value["generator_version"],
                ExtensionIdentity(**value["identity"]),
                IdentityMap(**value["identity_map"]),
                value["base_configuration_uuid"],
                SourceSet(**source),
                value["source_hashes"],
                tuple(operation_from_dict(op) for op in value["operations"]),
                value["file_hashes"],
                value["build_hash"],
                value["delivery"],
                value["runtime_verified"],
                tuple(value["unverified"]),
                tuple(value["notices"]),
            )
        except (KeyError, TypeError, ValueError, UnicodeError):
            refuse("owned_content_changed", "Прежний manifest повреждён")
            raise AssertionError("недостижимо") from None


def validate_previous(manifest: ArtifactManifest, files: Mapping[str, bytes]) -> None:
    """Manifest не хеширует себя: вместо этого сверяется собственное представление."""
    if (
        manifest.schema_version != 1
        or manifest.generator_version != GENERATOR_VERSION
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


def validation_dict(prepared: PreparedAuthoring) -> dict:
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
