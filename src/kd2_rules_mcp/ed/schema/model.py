"""Неизменяемая модель пакетов XDTO; координаты исходника и эффективные значения раздельны."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QName:
    namespace: str
    local: str

    def __str__(self) -> str:
        return f"{{{self.namespace}}}{self.local}"


@dataclass(frozen=True, slots=True)
class SchemaSource:
    source_id: str
    path: str
    sha256: str
    kind: str
    bytes: int


@dataclass(frozen=True, slots=True)
class SchemaSpan:
    source_id: str
    line: int
    xpath: str


@dataclass(frozen=True, slots=True)
class OriginStep:
    role: str
    namespace: str
    span: SchemaSpan
    via: QName | None = None


@dataclass(frozen=True, slots=True)
class SchemaImport:
    namespace: str
    location: str | None
    resolved_source_id: str | None
    status: str
    span: SchemaSpan


@dataclass(frozen=True, slots=True)
class Facet:
    kind: str
    lexical: str
    value_type: QName | None
    fixed: bool | None
    span: SchemaSpan


@dataclass(frozen=True, slots=True)
class SchemaProperty:
    id: str
    name: QName
    type_id: str | None
    type_ref: QName | None
    lower: int
    upper: int | None
    nillable: bool
    form: str | None
    explicit_attributes: frozenset[str]
    origin: tuple[OriginStep, ...]
    status: str


@dataclass(frozen=True, slots=True)
class SchemaType:
    id: str
    qname: QName | None
    kind: str
    base: QName | None
    members: tuple[QName, ...]
    variety: str
    properties: tuple[SchemaProperty, ...]
    facets: tuple[Facet, ...]
    open: bool
    abstract: bool
    ordered: bool
    sequenced: bool
    explicit_attributes: frozenset[str]
    origin: tuple[OriginStep, ...]
    status: str


@dataclass(frozen=True, slots=True)
class SchemaDiagnostic:
    code: str
    message: str
    span: SchemaSpan | None = None
    affected_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SchemaPackage:
    namespace: str
    metadata_name: str | None
    revision: str | None
    sources: tuple[SchemaSource, ...]
    imports: tuple[SchemaImport, ...]
    types: tuple[SchemaType, ...]
    origin_role: str
    counts: Mapping[str, int]
    diagnostics: tuple[SchemaDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class EdSchema:
    schema_id: str
    packages: tuple[SchemaPackage, ...]
    base_namespace: str
    extension_namespaces: tuple[str, ...]
    diagnostics: tuple[SchemaDiagnostic, ...]
    reader_version: str
    status: str
    types: Mapping[QName, SchemaType]
    by_id: Mapping[str, SchemaType]
    inherited: Mapping[str, tuple[SchemaProperty, ...]]


@dataclass(frozen=True, slots=True)
class ResolvedProperty:
    property_ids: tuple[str, ...]
    physical_paths: tuple[tuple[QName, ...], ...]
    effective_path: tuple[str, ...]
    status: str
    reason: str | None
    origin: tuple[OriginStep, ...]
