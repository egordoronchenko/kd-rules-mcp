"""Чистые решения автора ED и их воспроизводимое происхождение (§2, §11)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Literal

from kd2_rules_mcp.ed.model import EdDocument, Expr, SourceFile, SourceSpan
from kd2_rules_mcp.ed.route_model import RouteProfile
from kd2_rules_mcp.ed.schema.model import EdSchema
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
from kd2_rules_mcp.validation.report import Issue, Skipped

Direction = Literal["send", "receive"]
Primitive = Literal["string", "boolean", "number", "date"]
FILLER = "ЗаполнитьПравилаКонвертацииОбъектов"


def normalized(value: object) -> object:
    """JSON-представление неизменяемых DTO, без зависимости от порядка словаря."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: normalized(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return sorted((str(normalized(k)), normalized(v)) for k, v in value.items())
    if isinstance(value, (tuple, list)):
        return [normalized(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((normalized(v) for v in value), key=str)
    if isinstance(value, Enum):
        return value.value
    return value


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthoringTarget:
    project: str
    configuration: str
    plan: str
    variant: str | None
    format_version: str
    direction: Direction
    pko_address: str

    def __post_init__(self) -> None:
        if self.direction not in ("send", "receive"):
            raise ValueError("Направление операции: send или receive")


@dataclass(frozen=True, slots=True)
class AttributeDraft:
    name: str
    synonym: str
    primitive: Primitive
    qualifiers: Mapping[str, str | int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "qualifiers", MappingProxyType(dict(self.qualifiers)))


@dataclass(frozen=True, slots=True)
class AddHeaderProperty:
    target: AuthoringTarget
    configuration_attribute: str
    format_property: str
    new_attribute: AttributeDraft | None = None

    @property
    def operation_id(self) -> str:
        # В подготовке сюда попадают только разрешённые канонические решения.
        target = self.target
        draft = self.new_attribute
        return digest(
            (
                target.project,
                target.configuration,
                target.plan,
                target.variant,
                target.format_version,
                target.direction,
                target.pko_address,
                self.configuration_attribute,
                self.format_property,
                (draft.name, draft.synonym, draft.primitive, draft.qualifiers) if draft else None,
            )
        )


@dataclass(frozen=True, slots=True)
class CanonicalHeaderProperty(AddHeaderProperty):
    """Операция после разрешения имён по уже прочитанным входам."""

    _canonical_id: str = field(default="", init=False, repr=False, compare=False)

    def is_canonical(self) -> bool:
        return bool(self._canonical_id) and self._canonical_id == self.operation_id


def _canonical_operation(
    target: AuthoringTarget, attribute: str, property_name: str, draft: AttributeDraft | None
) -> CanonicalHeaderProperty:
    """Закрытая фабрика канонизатора; replace и публичный конструктор не дают свидетельства."""
    operation = CanonicalHeaderProperty(target, attribute, property_name, draft)
    object.__setattr__(operation, "_canonical_id", operation.operation_id)
    return operation


def order_operations(operations: tuple[AddHeaderProperty, ...]) -> tuple[AddHeaderProperty, ...]:
    """Повтор решения — no-op; независимые решения сортируются по §3.3."""
    unique = {op.operation_id: op for op in operations}
    return tuple(
        sorted(
            unique.values(),
            key=lambda op: (
                0 if op.target.direction == "send" else 1,
                op.target.pko_address,
                op.format_property,
                op.configuration_attribute,
                op.operation_id,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class ExtensionIdentity:
    name: str
    prefix: str
    synonym: str = ""
    version: str = "0.1"
    compatibility_mode: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataProfile:
    """Сведения уже прочитанных описаний, без XML-писателя и файлового обхода."""

    dump_version: str = "2.20"
    run_mode: str = "ManagedApplication"
    script_variant: str = "Russian"
    use_purposes: tuple[str, ...] = ("PlatformApplication",)


@dataclass(frozen=True, slots=True)
class SourceSet:
    """Ожидаемые отпечатки входов; сервис поставляет их вместе со снимками."""

    project: str | None
    configuration: str | None
    document_hash: str
    schemas_hash: str
    structure_hash: str
    routes_hash: str
    extensions: tuple[str, ...]
    extensions_hash: str

    @classmethod
    def build(
        cls,
        document: EdDocument,
        schemas: Mapping[str, EdSchema | str],
        structure: StructureSnapshot,
        routes: RouteProfile,
        extension_sources: Mapping[str, str] | None = None,
        extensions: tuple[str, ...] = (),
    ) -> SourceSet:
        """Фабрика для читателя/фикстур; подготовка принимает готовые отпечатки.

        Вызывающий слой вычисляет их при чтении, вне измеряемой подготовки.
        AuthoringInputs.input_fingerprints позволяет сравнить с ожидаемым SourceSet
        без повторного обхода неизменяемых объектов.
        """
        return cls(
            routes.project,
            routes.configuration,
            digest(
                (
                    document.parser_version,
                    document.manager_version,
                    tuple((f.path, f.sha256) for f in document.files),
                )
            ),
            digest(
                tuple(
                    (
                        version,
                        (
                            schema.schema_id,
                            schema.reader_version,
                            tuple((s.path, s.sha256) for p in schema.packages for s in p.sources),
                        )
                        if isinstance(schema, EdSchema)
                        else schema,
                    )
                    for version, schema in sorted(schemas.items())
                )
            ),
            digest(structure),
            digest(routes),
            extensions,
            digest(extension_sources or {}),
        )


@dataclass(frozen=True, slots=True)
class AuthoringInputs:
    document: EdDocument
    schemas: Mapping[str, EdSchema | str]
    structure: StructureSnapshot
    routes: RouteProfile
    source_set: SourceSet
    extension_sources: Mapping[str, str] = field(default_factory=dict)
    metadata_profile: MetadataProfile = MetadataProfile()
    input_fingerprints: SourceSet | None = None

    def __post_init__(self) -> None:
        if self.input_fingerprints is None:
            object.__setattr__(self, "input_fingerprints", self.source_set)
        object.__setattr__(self, "schemas", MappingProxyType(dict(self.schemas)))
        object.__setattr__(
            self, "extension_sources", MappingProxyType(dict(self.extension_sources or {}))
        )


@dataclass(frozen=True, slots=True)
class Failure:
    id: str
    address: str
    message: str
    file: str = ""
    line: int = 0


class AuthoringPreconditionError(ValueError):
    """Все отказы ограниченного автора, без транспортного кода сервера."""

    code = "ed_authoring_precondition"

    def __init__(self, failures: tuple[Failure, ...]):
        self.failures = failures
        super().__init__("; ".join(f.message for f in failures))


@dataclass(frozen=True, slots=True)
class Notice:
    id: str
    operation_id: str
    address: str
    message: str
    version_keys: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    detail_key: str = ""

    @property
    def notice_id(self) -> str:
        kind = self.id + ("." + self.detail_key if self.detail_key else "")
        return f"{kind}:{self.operation_id}"


@dataclass(frozen=True, slots=True)
class GeneratedHook:
    source: SourceFile
    calls: Mapping[str, SourceSpan]
    arguments: Mapping[str, tuple[Expr, ...]]
    direction_spans: Mapping[str, SourceSpan]
    operations: tuple[AddHeaderProperty, ...] = ()


@dataclass(frozen=True, slots=True)
class Change:
    operation_id: str
    owner_id: str
    entity_id: str
    changed_fields: tuple[str, ...]
    origin: SourceSpan


@dataclass(frozen=True, slots=True)
class Projection:
    document: EdDocument
    changes: tuple[Change, ...]
    base_parse_status: str
    generated_operations_applied: int
    headers_only: bool


@dataclass(frozen=True, slots=True)
class ProfileReport:
    version: str
    direction: str
    issues: tuple[Issue, ...]
    skipped: tuple[Skipped, ...]


@dataclass(frozen=True, slots=True)
class ValidationDelta:
    new: tuple[Issue, ...]
    disappeared: tuple[Issue, ...]
    new_relevant_skipped: tuple[Skipped, ...]

    @property
    def no_new_issues(self) -> bool:
        return not self.new and not self.new_relevant_skipped


@dataclass(frozen=True, slots=True)
class ProfileComparison:
    before: ProfileReport
    after: ProfileReport
    delta: ValidationDelta


@dataclass(frozen=True, slots=True)
class PreparedAuthoring:
    operations: tuple[AddHeaderProperty, ...]
    identity: ExtensionIdentity
    projection_before: EdDocument
    projection_after: Projection
    structure_after: StructureSnapshot
    source_set: SourceSet
    generated_hook: GeneratedHook
    selected_profiles: tuple[ProfileComparison, ...]
    other_profiles: tuple[ProfileComparison, ...]
    notices: tuple[Notice, ...]
    skipped: tuple[Skipped, ...]
    build_hash: str
    runtime_verified: bool
    preparation_inputs: AuthoringInputs | None = field(default=None, repr=False, compare=False)


def runtime_verified(manager_version: int | None) -> bool:
    """Шаблон с guards не проходил T1/T3; v1/v3 дополнительно требуют T5/T4 (§11)."""
    conditions = {1: (False, False), 2: (False,), 3: (False, False)}
    return all(conditions.get(manager_version or 0, (False,)))
