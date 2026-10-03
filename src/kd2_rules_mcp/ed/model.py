"""Неизменяемая модель прочитанного менеджера EnterpriseData."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from enum import StrEnum


class ParseStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class Classification(StrEnum):
    TRIVIA = "trivia"
    DECLARATIVE = "declarative"
    OPAQUE_CODE = "opaque_code"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    file_id: str
    line_start: int
    line_end: int
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class SourceFile:
    file_id: str
    path: str
    text: str
    sha256: str
    line_offsets: tuple[int, ...]
    encoding: str = "utf-8"
    bom: bool = False
    newline: str = "\n"

    @property
    def lines(self) -> int:
        return len(self.line_offsets)

    def span(self, start: int, end: int) -> SourceSpan:
        if not 0 <= start <= end <= len(self.text):
            raise ValueError("Диапазон вне исходного файла")
        return SourceSpan(
            self.file_id,
            max(1, bisect_right(self.line_offsets, start)),
            max(1, bisect_right(self.line_offsets, max(start, end - 1))),
            start,
            end,
        )


@dataclass(frozen=True, slots=True)
class SourceTag:
    name: str
    span: SourceSpan
    raw_text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Entity:
    entity_id: str
    kind: str
    name: str
    span: SourceSpan
    raw_text: str
    regions: tuple[str, ...] = ()
    tag_ids: tuple[str, ...] = ()
    guards: tuple[str, ...] = ()
    status: ParseStatus = ParseStatus.COMPLETE


@dataclass(frozen=True, slots=True)
class Expr:
    raw: str
    span: SourceSpan
    literal_type: str | None = None
    literal_value: str | int | float | bool | None = None
    reference_parts: tuple[str, ...] = ()
    guards: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Field[T]:
    presence: str = "absent"
    value: T | None = None
    assignments: tuple[Expr, ...] = ()


@dataclass(frozen=True, slots=True)
class FormalParameter:
    name: str | None
    by_value: bool
    default: Expr | None
    raw: str
    span: SourceSpan


@dataclass(frozen=True, slots=True, kw_only=True)
class Routine(Entity):
    routine_kind: str
    parameters_raw: str
    parameters: tuple[FormalParameter, ...]
    exported: bool
    roles: frozenset[str]
    body_span: SourceSpan


@dataclass(frozen=True, slots=True, kw_only=True)
class HandlerBinding(Entity):
    owner_id: str
    event: str
    target_name: str
    target_id: str | None = None
    resolution: str = "missing"


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatcherCase(Entity):
    dispatcher_id: str
    literal_name: str
    target: Expr
    arguments: tuple[Expr, ...]
    returns: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class PropertyRule(Entity):
    owner_id: str
    group_id: str | None
    configuration_property: str
    format_property: str
    algorithm_flag: int = 0
    conversion_rule: str = ""
    namespace: str = ""
    condition_name: str = ""
    argument_presence: tuple[bool, ...] = ()
    raw_arguments: tuple[Expr, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PropertyGroup(Entity):
    owner_id: str
    configuration_property: str
    format_property: str
    namespace: str = ""
    condition_name: str = ""
    properties: tuple[PropertyRule, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchSet(Entity):
    owner_id: str
    value_raw: str
    fields: tuple[str, ...]
    ordinal: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ObjectRule(Entity):
    procedure_name: str
    declared_name: str | None
    configuration_object: Field[Expr] = field(default_factory=Field[Expr])
    format_object: Field[str] = field(default_factory=Field[str])
    group_flag: Field[bool] = field(default_factory=Field[bool])
    identification: Field[str] = field(default_factory=Field[str])
    events: tuple[HandlerBinding, ...] = ()
    properties: tuple[PropertyRule, ...] = ()
    groups: tuple[PropertyGroup, ...] = ()
    search_sets: tuple[SearchSet, ...] = ()
    extensions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessingRule(Entity):
    procedure_name: str
    declared_name: str | None
    configuration_selection: Field[Expr] = field(default_factory=Field[Expr])
    format_selection: Field[str] = field(default_factory=Field[str])
    clear_data: Field[bool] = field(default_factory=Field[bool])
    events: tuple[HandlerBinding, ...] = ()
    used_pko: tuple[RuleRef, ...] = ()


@dataclass(frozen=True, slots=True)
class RuleRef:
    name: str
    span: SourceSpan
    target_id: str | None = None
    resolution: str = "missing"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueMapping(Entity):
    configuration_value: Expr
    format_value: Expr
    direction: str
    ordinal: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PredefinedRule(Entity):
    declared_name: str | None
    configuration_type: Field[Expr] = field(default_factory=Field[Expr])
    format_type: Field[str] = field(default_factory=Field[str])
    mappings: tuple[ValueMapping, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Parameter(Entity):
    default: Expr | None = None
    default_source: str = "implicit"


@dataclass(frozen=True, slots=True, kw_only=True)
class Guard(Entity):
    expression_raw: str
    branch: str
    parent_id: str | None = None
    known_direction: str | None = None
    guard_kind: str = "opaque"


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleUse(Entity):
    rule_id: str | None
    target_name: str
    direction: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownFragment(Entity):
    owner_id: str | None
    reason: str
    severity: str = "warning"
    candidate_kind: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Diagnostic(Entity):
    code: str
    severity: str
    message: str
    owner_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class VersionMention(Entity):
    value: str
    context: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Conversion(Entity):
    title: str | None = None
    generated_at_raw: str | None = None
    events: tuple[HandlerBinding, ...] = ()
    entrypoints: tuple[str, ...] = ()
    format_version_mentions: tuple[VersionMention, ...] = ()


@dataclass(frozen=True, slots=True)
class CoverageSegment:
    span: SourceSpan
    classification: Classification


@dataclass(frozen=True, slots=True)
class Coverage:
    segments: tuple[CoverageSegment, ...]
    line_classes: tuple[Classification, ...]
    entity_covered_lines: int
    coverage_ratio: float
    classified_ratio: float

    @property
    def counts(self) -> dict[str, int]:
        return {kind.value: self.line_classes.count(kind) for kind in Classification}

    def classify_line(self, line: int) -> Classification:
        if not 1 <= line <= len(self.line_classes):
            raise ValueError("Номер строки вне файла")
        return self.line_classes[line - 1]


@dataclass(frozen=True, slots=True)
class EdDocument:
    files: tuple[SourceFile, ...]
    conversion: Conversion
    manager_version: int | None
    routines: tuple[Routine, ...]
    pko: tuple[ObjectRule, ...]
    pod: tuple[ProcessingRule, ...]
    pkpd: tuple[PredefinedRule, ...]
    parameters: tuple[Parameter, ...]
    guards: tuple[Guard, ...]
    rule_uses: tuple[RuleUse, ...]
    dispatcher_cases: tuple[DispatcherCase, ...]
    unknown: tuple[UnknownFragment, ...]
    diagnostics: tuple[Diagnostic, ...]
    coverage: Coverage
    parse_status: ParseStatus
    parser_version: str = "1"
    tags: tuple[SourceTag, ...] = ()

    def entities(self) -> tuple[Entity, ...]:
        result: list[Entity] = [self.conversion, *self.routines, *self.pod, *self.pkpd]
        result.extend(self.parameters)
        result.extend(self.guards)
        result.extend(self.rule_uses)
        result.extend(self.dispatcher_cases)
        result.extend(self.unknown)
        result.extend(self.diagnostics)
        result.extend(self.conversion.events)
        result.extend(self.conversion.format_version_mentions)
        for rule in self.pko:
            result.extend((rule, *rule.events, *rule.properties, *rule.search_sets))
            for group in rule.groups:
                result.extend((group, *group.properties))
        for pod in self.pod:
            result.extend(pod.events)
        for predefined in self.pkpd:
            result.extend(predefined.mappings)
        return tuple(result)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "pko": len(self.pko),
            "pod": len(self.pod),
            "pkpd": len(self.pkpd),
            "pks": sum(
                len(r.properties) + sum(len(g.properties) for g in r.groups) for r in self.pko
            ),
            "pktch": sum(len(r.groups) for r in self.pko),
            "search_sets": sum(len(r.search_sets) for r in self.pko),
            "values": sum(len(r.mappings) for r in self.pkpd),
            "parameters": len(self.parameters),
            "algorithms": sum("algorithm" in r.roles for r in self.routines),
            "handlers": sum("handler" in r.roles for r in self.routines),
            "dispatchers": sum("dispatcher" in r.roles for r in self.routines),
            "support": sum("support" in r.roles for r in self.routines),
            "unknown": len(self.unknown),
        }
