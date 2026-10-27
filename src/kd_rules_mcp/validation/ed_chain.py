"""Звенья пути объекта через модуль менеджера: общая часть `ed_trace` и `ed_pair_validate`.

Новой грамматики здесь нет: только модель читателя, профиль применимости, схема и слои.
Звено не бывает `ok`, если условие, слой или чтение не доказаны: тогда `partial` или `unknown`
с причиной. `XDTO:N` — строки `ОбменДаннымиXDTOСервер` БСП 3.1.12.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from kd_rules_mcp.ed import model as ed
from kd_rules_mcp.ed.address import AddressIndex, escape_segment
from kd_rules_mcp.ed.layer_model import Certainty, EntityVersion
from kd_rules_mcp.ed.refs import ReferenceIndex
from kd_rules_mcp.ed.schema.model import SchemaType
from kd_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd_rules_mcp.validation.ed_structure_snapshot import COLLECTIONS

OK = "ok"
NOT_APPLICABLE = "not_applicable"
PARTIAL = "partial"
UNKNOWN = "unknown"

# Порядок «хуже» для свёртки состояний пути: неизвестное звено не делает путь известным.
_RANK = {OK: 0, PARTIAL: 1, UNKNOWN: 2, NOT_APPLICABLE: 3}

# Варианты идентификации, при которых исполнитель ищет по полям (XDTO:1896–1907).
SEARCH_BY_FIELDS = frozenset(
    {"ПоПолямПоиска", "СначалаПоУникальномуИдентификаторуПотомПоПолямПоиска"}
)


@dataclass(frozen=True, slots=True)
class LinkState:
    """Состояние звена: `ok`, `not_applicable`, `partial`, `unknown` и причины."""

    status: str
    reasons: tuple[str, ...] = ()

    def worst(self, other: LinkState) -> LinkState:
        if _RANK[other.status] > _RANK[self.status]:
            return LinkState(other.status, tuple(dict.fromkeys((*self.reasons, *other.reasons))))
        if other.reasons and other.status != OK:
            return LinkState(self.status, tuple(dict.fromkeys((*self.reasons, *other.reasons))))
        return self

    def view(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.reasons:
            result["reasons"] = list(self.reasons)
        return result


@dataclass(frozen=True, slots=True)
class PropertySchema:
    """Свойство формата ПКС в выбранной схеме. `key=None` — схема не дала ответа."""

    status: str
    path: str | None = None
    key: bool | None = None
    reason: str | None = None


def metadata_name(expression: ed.Expr | None) -> tuple[str | None, str]:
    """Полное имя объекта метаданных из `Метаданные.<Коллекция>.<Имя>` или строки."""
    if expression is None:
        return None, "configuration_object_unavailable"
    if expression.literal_type == "string" and isinstance(expression.literal_value, str):
        return expression.literal_value, "resolved"
    if expression.raw.strip().casefold() == "неопределено":
        return None, "structure_source"
    parts = expression.reference_parts
    if len(parts) != 3 or parts[0].casefold() != "метаданные":
        return None, "dynamic_configuration_object"
    kind = COLLECTIONS.get(parts[1].casefold())
    if kind is None:
        return None, "unknown_collection"
    return f"{kind}.{parts[2]}", "resolved"


def same_object(left: str | None, right: str | None) -> bool:
    """Имена объектов метаданных — без учёта регистра, как идентификаторы 1С."""
    return bool(left and right and left.strip().casefold() == right.strip().casefold())


def properties(rule: ed.ObjectRule) -> Iterator[tuple[ed.PropertyGroup | None, ed.PropertyRule]]:
    """ПКС шапки, затем ПКС табличных частей с их группой."""
    for prop in rule.properties:
        yield None, prop
    for group in rule.groups:
        for prop in group.properties:
            yield group, prop


def format_path(group: ed.PropertyGroup | None, prop: ed.PropertyRule) -> str:
    """Имя свойства формата ПКС; у табличной части — `<таблица>.<свойство>`."""
    name = prop.format_property.strip()
    if group is None:
        return name
    return f"{group.format_property.strip()}.{name}" if name else ""


@dataclass(frozen=True)
class ChainView:
    """Один модуль в одном направлении: документ, адреса, применимость, слои и схема."""

    project_id: str
    document: ed.EdDocument
    index: AddressIndex
    direction: str
    profile: ValidationProfile
    applicability: Applicability
    versions: Mapping[str, EntityVersion] = field(default_factory=dict)
    references: ReferenceIndex | None = None
    layered: bool = False
    _unknown_owners: frozenset[str] = frozenset()
    _pko_by_name: Mapping[str, tuple[ed.ObjectRule, ...]] = field(default_factory=dict)
    _table_order: Mapping[str, tuple[int, int, int]] = field(default_factory=dict)
    _used: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        project_id: str,
        document: ed.EdDocument,
        index: AddressIndex,
        direction: str,
        profile: ValidationProfile,
        *,
        versions: Mapping[str, EntityVersion] | None = None,
        references: ReferenceIndex | None = None,
        layered: bool = False,
        order_uses: tuple[ed.RuleUse, ...] = (),
    ) -> ChainView:
        """`order_uses` — вызовы заполнения исходного модуля: в действующем документе слоёв
        вызовы собраны в порядке объявления правил, а таблица заполняется в порядке вызовов."""
        if direction not in ("send", "receive"):
            raise ValueError("Направление звеньев: send или receive")
        by_name: dict[str, list[ed.ObjectRule]] = defaultdict(list)
        for rule in document.pko:
            # Правило по имени исполнитель ищет точно (XDTO:504, 795, 1554).
            by_name[rule.name].append(rule)
        files = {source.file_id: n for n, source in enumerate(document.files)}
        order: dict[str, tuple[int, int, int]] = {}
        # Строки таблицы правил идут в порядке вызовов заполнения; `Найти` берёт первую
        # (XDTO:4519 — ПОД по типу формата, XDTO:8344 — ПОД по объекту метаданных).
        for n, use in enumerate((*order_uses, *document.rule_uses)):
            if use.rule_id and use.rule_id not in order:
                order[use.rule_id] = (0, n, 0)
        for rule in (*document.pod, *document.pko):
            order.setdefault(
                rule.entity_id, (1, files.get(rule.span.file_id, 0), rule.span.char_start)
            )
        return cls(
            project_id,
            document,
            index,
            direction,
            profile,
            Applicability.build(document, profile),
            MappingProxyType(dict(versions or {})),
            references,
            layered,
            frozenset(item.owner_id for item in document.unknown if item.owner_id),
            MappingProxyType({k: tuple(v) for k, v in by_name.items()}),
            MappingProxyType(order),
            frozenset(use.rule_id for use in document.rule_uses if use.rule_id),
        )

    def address(self, entity: ed.Entity) -> str:
        addresses = self.index.by_id.get(entity.entity_id)
        if addresses:
            return addresses[0]
        return f"{entity.kind}/{escape_segment(entity.name)}"

    def state(self, entity: ed.Entity, parent: ed.Entity | None = None) -> LinkState:
        """Применимость в направлении и версии, полнота чтения и определённость слоя."""
        result = LinkState(OK)
        truth = self.applicability.evaluate(entity, self.direction, parent)
        if isinstance(entity, (ed.ObjectRule, ed.ProcessingRule)) and (
            entity.entity_id not in self._used
        ):
            # Процедура правила, которую заполнение не вызывает, в таблицу не попадает.
            if self.document.parse_status == ed.ParseStatus.COMPLETE:
                return LinkState(NOT_APPLICABLE, ("not_added",))
            result = result.worst(LinkState(PARTIAL, ("not_added_unknown",)))
        if truth is False:
            result = result.worst(LinkState(NOT_APPLICABLE, ("condition_false",)))
        elif truth is None:
            result = result.worst(LinkState(PARTIAL, ("opaque_condition",)))
        if entity.status == ed.ParseStatus.PARTIAL:
            result = result.worst(LinkState(PARTIAL, ("reader_partial",)))
        if entity.entity_id in self._unknown_owners:
            result = result.worst(LinkState(PARTIAL, ("unknown_fragment",)))
        version = self.versions.get(entity.entity_id)
        if version is not None and version.certainty == Certainty.UNKNOWN:
            result = result.worst(LinkState(UNKNOWN, ("layer_unknown",)))
        elif version is not None and version.certainty == Certainty.CONDITIONAL:
            result = result.worst(LinkState(PARTIAL, ("layer_conditional",)))
        return result

    def origin(self, entity: ed.Entity) -> dict[str, Any] | None:
        """Какой слой дал действующую версию сущности; без слоёв — `None`."""
        if not self.layered:
            return None
        version = self.versions.get(entity.entity_id)
        if version is None:
            return {"layer_id": "base", "state": "base", "certainty": "known"}
        result: dict[str, Any] = {
            "layer_id": version.layer_id,
            "state": version.state,
            "certainty": version.certainty,
        }
        changed = sorted({change.origin.layer_id for change in version.changes})
        if changed:
            result["changed_by"] = changed
        return result

    def pko_named(self, name: str) -> tuple[ed.ObjectRule, ...]:
        return self._pko_by_name.get(name, ())

    def table_order(self, rule: ed.Entity) -> tuple[int, int, int]:
        return self._table_order.get(rule.entity_id, (2, 0, rule.span.char_start))

    def pods(self) -> tuple[ed.ProcessingRule, ...]:
        """ПОД в порядке строк таблицы правил."""
        return tuple(sorted(self.document.pod, key=self.table_order))

    def order_unknown(self, rules: list[ed.ProcessingRule] | list[ed.ObjectRule]) -> bool:
        """Правила из разных файлов слоёв: расширение может добавить строку до базовых."""
        return self.layered and len({rule.span.file_id for rule in rules}) > 1

    def pko_first(self, name: str) -> tuple[ed.ObjectRule | None, LinkState]:
        """ПКО, которое найдёт `Найти` по имени (XDTO:504, 795, 7458): первая действующая строка."""
        rules = [r for r in self.pko_named(name) if self.state(r).status != NOT_APPLICABLE]
        if not rules:
            return None, LinkState(NOT_APPLICABLE, ("pod_pko_missing",))
        rules.sort(key=self.table_order)
        state = LinkState(OK)
        if len(rules) > 1:
            earlier = self.state(rules[0])
            reason = "duplicate_name" if earlier.status == OK else "duplicate_name_unknown"
            state = LinkState(OK if earlier.status == OK else PARTIAL, (reason,))
            if self.order_unknown(rules):
                state = LinkState(PARTIAL, ("layer_order_unknown",))
        return rules[0], state

    def literal(self, field_: ed.Field) -> tuple[str | None, str]:
        """Строковое значение поля правила: литерал или единственная действующая ветка."""
        if field_.presence == "literal":
            return (field_.value or None), "resolved" if field_.value else "empty"
        if field_.presence == "absent":
            return None, "absent"
        if field_.presence != "ambiguous" or not field_.assignments:
            return None, "dynamic"
        values = []
        for item in field_.assignments:
            truth: bool | None = True
            for guard in item.guards:
                value = self.applicability.guard(guard, self.direction)
                truth = False if value is False or truth is False else truth and value
            if truth is None:
                return None, "opaque_condition"
            if truth:
                values.append(item)
        if len(values) == 1 and values[0].literal_type == "string":
            return str(values[0].literal_value), "resolved"
        return None, "ambiguous"

    def has_event(self, rule: ed.ObjectRule | ed.ProcessingRule, *events: str) -> bool:
        """Привязан обработчик, который может быть вызван (ветка не доказанно отсутствует)."""
        return any(
            b.event in events
            and b.target_name.strip()
            and b.resolution not in ("missing", "no_call")
            for b in rule.events
        )

    def pod_metadata(self, pod: ed.ProcessingRule) -> tuple[str | None, str]:
        field_ = pod.configuration_selection
        if field_.presence == "ambiguous":
            return None, "ambiguous_configuration_object"
        return metadata_name(field_.value)

    def pko_metadata(self, rule: ed.ObjectRule) -> tuple[str | None, str]:
        field_ = rule.configuration_object
        if field_.presence == "ambiguous":
            return None, "ambiguous_configuration_object"
        return metadata_name(field_.value)

    def type_state(self, rule: ed.ObjectRule) -> tuple[SchemaType | None, str]:
        """Тип формата ПКО в схеме; `missing` — исполнитель удаляет ПКО (XDTO:4146–4158)."""
        return self.profile.owner_type(rule, self.direction, self.applicability)

    def property_schema(
        self,
        rule: ed.ObjectRule,
        group: ed.PropertyGroup | None,
        prop: ed.PropertyRule,
    ) -> PropertySchema:
        """Свойство формата ПКС в схеме: разрешено ли и ключевое ли (XDTO:4884–4931)."""
        if not prop.format_property.strip():
            return PropertySchema("empty_format_side")
        owner, status = self.type_state(rule)
        if owner is None or status != "resolved":
            return PropertySchema("owner_type_unavailable", reason=status)
        if prop.namespace and rule.format_object.value:
            owner, status = self.profile.find_type(rule.format_object.value, prop.namespace)
            if owner is None or status != "resolved":
                return PropertySchema("owner_type_unavailable", reason=status)
        table = group.format_property if group and group.format_property else None
        resolved = self.profile.resolve(owner, prop.format_property, table)
        if resolved.status != "resolved":
            return PropertySchema(resolved.status, reason=resolved.reason)
        physical = resolved.physical_paths[0] if resolved.physical_paths else ()
        key = bool(group is None and len(physical) > 1 and physical[0].local == "КлючевыеСвойства")
        return PropertySchema("resolved", ".".join(resolved.effective_path), key)
