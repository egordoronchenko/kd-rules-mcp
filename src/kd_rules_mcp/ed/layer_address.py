"""Адреса действующего слоя: короткий alias, ревизия слоя и операция без правила."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

from .address import escape_segment, property_key
from .layer_model import (
    EffectiveContext,
    EntityState,
    EntityVersion,
    Hook,
    LayeredManager,
    LayerOperation,
    OperationKind,
)
from .model import Entity, ObjectRule, ProcessingRule, PropertyGroup, PropertyRule, Routine

_PREFIX = {"pko": "ПКО", "pod": "ПОД", "pkpd": "ПКПД", "parameters": "Параметр"}
_RULE_KINDS = (
    OperationKind.ADD,
    OperationKind.SET,
    OperationKind.DELETE,
    OperationKind.INIT_EXTENSION,
)


class AmbiguousLayerAddress(LookupError):
    """Короткому адресу соответствуют несколько сущностей."""

    def __init__(self, address: str, candidates: tuple[str, ...]):
        super().__init__(f"Неоднозначный адрес: {address}")
        self.address = address
        self.candidates = candidates


class LayerAddressNotFound(LookupError):
    """Адрес отсутствует в снимке слоя."""

    def __init__(self, address: str):
        super().__init__(address)
        self.address = address


@dataclass(frozen=True, slots=True)
class LayerAddressHit:
    """Одна ревизия, свойство, перехват, обработчик или операция."""

    address: str
    version: EntityVersion | None = None
    entity: Entity | None = None
    hook: Hook | None = None
    operation: LayerOperation | None = None
    routine: Routine | None = None


@dataclass(frozen=True, slots=True)
class LayerAddressIndex:
    """Поиск без учёта регистра. Конфликтный короткий адрес не выбирает кандидата сам."""

    by_address: dict[str, LayerAddressHit]
    conflicts: dict[str, tuple[str, ...]]
    by_context: dict[tuple[str, bool, str | None], tuple[str, ...]]

    def find(self, address: str) -> LayerAddressHit:
        key = address.casefold()
        if key in self.conflicts:
            raise AmbiguousLayerAddress(address, self.conflicts[key])
        for candidate, hit in self.by_address.items():
            if candidate.casefold() == key:
                return hit
        raise LayerAddressNotFound(address)

    def addresses_for(self, context: EffectiveContext) -> tuple[str, ...]:
        return self.by_context.get(
            (context.direction, context.headers_only, context.version_key), ()
        )


def build_layer_addresses(layered: LayeredManager) -> LayerAddressIndex:
    """Собирает адреса всех ревизий. Короткий alias берётся из отправки полного обмена."""
    rows: list[tuple[str, tuple, LayerAddressHit]] = []
    grouped: dict[tuple, list[EntityVersion]] = {}
    for version in layered.revisions:
        if version.payload is None:
            continue
        grouped.setdefault((version.revision_id, _payload_signature(version)), []).append(version)
    for versions in grouped.values():
        sample = versions[0]
        directions = {(item.direction, item.headers_only) for item in versions}
        if len(directions) > 1:
            sample = replace(sample, direction="", headers_only=False)
        rows.extend(_version_rows(sample))
    default = _default_context(layered)
    if default is not None:
        for version in default.entities:
            if version.state != EntityState.DELETED and version.payload is not None:
                rows.extend(_acting_rows(version))
    for layer_id, routine in layered.routines:
        if _is_handler(routine, layered):
            rows.append(_routine_row(layer_id, "Обработчик", routine))
    for hook in layered.hooks:
        rows.append(_routine_row(hook.origin.layer_id, "Перехват", hook.routine, hook=hook))
    ordinals: dict[str, int] = defaultdict(int)
    for operation in layered.operations:
        if operation.kind in _RULE_KINDS and _UNIT in operation.target_ref:
            continue
        ordinals[operation.origin.layer_id] += 1
        number = ordinals[operation.origin.layer_id]
        address = f"Слой/{escape_segment(operation.origin.layer_id)}/Операция/{number}"
        rows.append(
            (
                address,
                _span_key(operation.origin.span),
                LayerAddressHit(address, operation=operation),
            )
        )
    addresses, conflicts = _qualify(rows)
    default = _default_context(layered)
    context_map: dict[tuple[str, bool, str | None], tuple[str, ...]] = {}
    for context in layered.contexts:
        logical = {item.logical_id for item in context.entities}
        chosen = [
            hit.address
            for hit in addresses.values()
            if hit.version is not None
            and hit.version.logical_id in logical
            and _visible(hit, context, default)
            and _same_direction(hit.version, context)
        ]
        context_map[(context.direction, context.headers_only, context.version_key)] = tuple(chosen)
    return LayerAddressIndex(addresses, conflicts, context_map)


def _payload_signature(version: EntityVersion) -> tuple:
    """Разные нагрузки направлений не объединяются по одним именам полей."""
    return (version.state, version.payload, version.certainty, version.changes)


def _same_direction(version: EntityVersion, context: EffectiveContext) -> bool:
    if not version.direction:
        return True
    return version.direction == context.direction and version.headers_only == context.headers_only


def _visible(
    hit: LayerAddressHit, context: EffectiveContext, default: EffectiveContext | None
) -> bool:
    version = hit.version
    if version is None:
        return False
    if version.state == EntityState.DELETED and not hit.address.startswith("Слой/"):
        return False
    if hit.address.startswith("Слой/"):
        return True
    return default is not None and context is default


def _default_context(layered: LayeredManager) -> EffectiveContext | None:
    for context in layered.contexts:
        if context.direction == "send" and not context.headers_only:
            return context
    return layered.contexts[0] if layered.contexts else None


def _version_rows(version: EntityVersion) -> list[tuple[str, tuple, LayerAddressHit]]:
    payload = version.payload
    assert payload is not None
    prefix = _PREFIX.get(version.collection)
    if prefix is None:
        return []
    name = escape_segment(payload.name)
    layer = f"Слой/{escape_segment(version.layer_id)}/{prefix}/{name}"
    key = _span_key(payload.span)
    rows = [(layer, key, LayerAddressHit(layer, version, payload))]
    if isinstance(payload, ObjectRule):
        rows.extend(_property_rows(version, layer))
    return rows


def _acting_rows(version: EntityVersion) -> list[tuple[str, tuple, LayerAddressHit]]:
    payload = version.payload
    assert payload is not None
    prefix = _PREFIX.get(version.collection)
    if prefix is None:
        return []
    name = escape_segment(payload.name)
    key = _span_key(payload.span)
    short = f"{prefix}/{name}"
    explicit = f"Действующее/{prefix}/{name}"
    rows = [
        (short, key, LayerAddressHit(short, version, payload)),
        (explicit, key, LayerAddressHit(explicit, version, payload)),
    ]
    if isinstance(payload, ObjectRule):
        rows.extend(_property_rows(version, explicit))
    return rows


def _property_rows(
    version: EntityVersion, address: str
) -> list[tuple[str, tuple, LayerAddressHit]]:
    payload = version.payload
    assert isinstance(payload, ObjectRule)
    members: list[tuple[str, PropertyRule | PropertyGroup]] = [
        (f"{address}/ПКС", prop) for prop in payload.properties
    ]
    for group in payload.groups:
        segment = property_key(group.format_property, group.configuration_property)
        group_address = f"{address}/ПКТЧ/{segment}"
        members.append((f"{address}/ПКТЧ", group))
        members.extend((f"{group_address}/ПКС", prop) for prop in group.properties)
    rows = []
    for parent, member in members:
        segment = property_key(member.format_property, member.configuration_property)
        member_address = f"{parent}/{segment}"
        rows.append(
            (
                member_address,
                _span_key(member.span),
                LayerAddressHit(member_address, version, member),
            )
        )
    return rows


def _routine_row(
    layer_id: str, kind: str, routine: Routine, hook: Hook | None = None
) -> tuple[str, tuple, LayerAddressHit]:
    address = f"Слой/{escape_segment(layer_id)}/{kind}/{escape_segment(routine.name)}"
    return (
        address,
        _span_key(routine.span),
        LayerAddressHit(address, hook=hook, routine=routine, entity=routine),
    )


def _is_handler(routine: Routine, layered: LayeredManager) -> bool:
    if routine.roles & {"handler", "handler_helper"}:
        return True
    for context in layered.contexts:
        for chain in context.dispatch_chains:
            if chain.resolution != "call":
                continue
            for link in chain.links:
                if (
                    link.case is not None
                    and _callee(link.case).casefold() == routine.name.casefold()
                ):
                    return True
        for version in context.entities:
            payload = version.payload
            if isinstance(payload, (ObjectRule, ProcessingRule)):
                for binding in payload.events:
                    if binding.target_name.casefold() == routine.name.casefold():
                        return True
    return False


def _callee(case) -> str:
    parts = case.target.reference_parts or ()
    return parts[0] if parts else case.name


def _span_key(span) -> tuple:
    return (span.file_id, span.char_start, span.char_end)


def _qualify(
    rows: list[tuple[str, tuple, LayerAddressHit]],
) -> tuple[dict[str, LayerAddressHit], dict[str, tuple[str, ...]]]:
    groups: dict[str, list[tuple[str, tuple, LayerAddressHit]]] = defaultdict(list)
    for address, key, hit in rows:
        groups[address.casefold()].append((address, key, hit))
    addresses: dict[str, LayerAddressHit] = {}
    conflicts: dict[str, tuple[str, ...]] = {}
    for folded, group in groups.items():
        group.sort(key=lambda row: row[1])
        names: list[str] = []
        for number, (address, _key, hit) in enumerate(group, 1):
            final = f"{address}#{number}" if len(group) > 1 else address
            addresses[final] = LayerAddressHit(
                final, hit.version, hit.entity, hit.hook, hit.operation, hit.routine
            )
            names.append(final)
        if len(group) > 1:
            conflicts[folded] = tuple(names)
    return addresses, conflicts


_UNIT = "\x1f"
