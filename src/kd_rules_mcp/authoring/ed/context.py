"""Локальные индексы одной подготовки; входные снимки остаются неизменными."""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import fields, is_dataclass, replace
from types import MappingProxyType
from typing import Any

from kd_rules_mcp.ed.address import AddressIndex, build_addresses
from kd_rules_mcp.ed.model import EdDocument
from kd_rules_mcp.ed.refs import ReferenceIndex
from kd_rules_mcp.ed.schema.model import EdSchema
from kd_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd_rules_mcp.ed.schema.resolver import effective_properties

from .model import AuthoringInputs


class _EffectiveProperties(Mapping):
    """Тот же индекс схемы, материализуемый лишь для затронутых типов и их зависимостей."""

    def __init__(self, schema: EdSchema):
        self.schema = schema
        self.cache: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        if key not in self.cache:
            self.cache[key] = effective_properties(self.schema, self.schema.by_id[key])
        return self.cache[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.schema.by_id)

    def __len__(self) -> int:
        return len(self.schema.by_id)


class AuthoringContext:
    """Профиль один на пару версия/направление, применимость — на документ и профиль."""

    def __init__(self, inputs: AuthoringInputs):
        self.inputs = inputs
        self.indices: dict[int, AddressIndex] = {}
        self.profiles: dict[tuple[str, str], ValidationProfile] = {}
        self.folded_profiles: dict[int, ValidationProfile] = {}
        self.applications: dict[tuple[int, str, str], Applicability] = {}
        self.selected: frozenset[tuple[str, str]] | None = None
        self.other_rules_only = False
        self.target_ids: frozenset[str] = frozenset()
        self.scopes: dict[int, EdDocument] = {}
        self.references: ReferenceIndex | None = None
        # Проверенное тело привязано к operation_id, который включает все его байты.
        self.handler_bodies: dict[str, Any] = {}

    def reference_index(
        self, document: EdDocument, build: Callable[[EdDocument], ReferenceIndex]
    ) -> ReferenceIndex:
        """Прямая ПКС не меняет тела обработчиков, привязки и направления ссылок."""
        base = self.inputs.document
        roles = {"handler", "algorithm", "event", "callback"}
        unchanged = (
            document.files[0] is base.files[0]
            and tuple(r for r in document.routines if roles & r.roles)
            == tuple(r for r in base.routines if roles & r.roles)
            and tuple((r.entity_id, r.events) for r in (*document.pko, *document.pod))
            == tuple((r.entity_id, r.events) for r in (*base.pko, *base.pod))
            and document.rule_uses == base.rule_uses
            and document.dispatcher_cases == base.dispatcher_cases
        )
        if not unchanged:
            return build(document)
        if self.references is None:
            self.references = build(base)
        return self.references

    def scope(self, document: EdDocument) -> EdDocument:
        """Целевые ПКО, зависимости конвертации, ПОД и полные цепочки их условий.

        Адреса остаются из полного документа: суффиксы дубликатов не перенумеровываются.
        Все методы сохранены, включая сверяемый helper сравнения версий.
        """
        if id(document) in self.scopes:
            return self.scopes[id(document)]
        selected = set(self.target_ids)
        names: dict[str, set[str]] = {}
        for rule in (*document.pko, *document.pkpd):
            names.setdefault((rule.declared_name or rule.name).casefold(), set()).add(
                rule.entity_id
            )
        pending = list(selected)
        by_id = {r.entity_id: r for r in document.pko}
        while pending:
            rule = by_id.get(pending.pop())
            if rule is None:
                continue
            properties = (*rule.properties, *(p for g in rule.groups for p in g.properties))
            references = [p.conversion_rule for p in properties]
            references.append(rule.declared_name or rule.name)
            for name in references:
                for ident in names.get(name.casefold(), ()):
                    if ident not in selected:
                        selected.add(ident)
                        pending.append(ident)
        pko = tuple(r for r in document.pko if r.entity_id in selected)
        pkpd = tuple(r for r in document.pkpd if r.entity_id in selected)
        pod = tuple(
            r
            for r in document.pod
            if any(
                ref.target_id in selected or names.get(ref.name.casefold(), set()) & selected
                for ref in r.used_pko
            )
        )
        uses = tuple(u for u in document.rule_uses if u.rule_id in selected)
        needed: set[str] = set()

        def collect(value: Any) -> None:
            if is_dataclass(value) and not isinstance(value, type):
                needed.update(getattr(value, "guards", ()))
                for field in fields(value):
                    collect(getattr(value, field.name))
            elif isinstance(value, tuple):
                for item in value:
                    collect(item)

        collect((pko, pkpd, pod, uses))
        previous: dict[str, tuple[str, ...]] = {}
        chains: dict[str | None, list[str]] = {}
        for guard in document.guards:
            chain = chains.setdefault(guard.parent_id, [])
            if guard.branch == "if":
                chain.clear()
            previous[guard.entity_id] = tuple(chain)
            chain.append(guard.entity_id)
        guards = {g.entity_id: g for g in document.guards}
        pending = list(needed)
        while pending:
            ident = pending.pop()
            if ident not in guards:
                continue
            parent = guards[ident].parent_id
            for dependency in (*previous[ident], *((parent,) if parent else ())):
                if dependency not in needed:
                    needed.add(dependency)
                    pending.append(dependency)
        result = replace(
            document,
            pko=pko,
            pkpd=pkpd,
            pod=pod,
            rule_uses=uses,
            guards=tuple(g for g in document.guards if g.entity_id in needed),
        )
        self.scopes[id(document)] = result
        self.scopes[id(result)] = result
        self.indices[id(result)] = self.index(document)
        return result

    def index(self, document: EdDocument) -> AddressIndex:
        key = id(document)
        if key not in self.indices:
            self.indices[key] = build_addresses(document)
        return self.indices[key]

    def profile(self, version: str, direction: str) -> ValidationProfile:
        key = (version, direction)
        if key not in self.profiles:
            schema = self.inputs.schemas.get(version)
            if (
                self.selected is not None
                and self.other_rules_only
                and key not in self.selected
                and isinstance(schema, EdSchema)
            ):
                self.profiles[key] = ValidationProfile(
                    schema,
                    version,
                    direction,
                    (schema.base_namespace, *schema.extension_namespaces),
                    _EffectiveProperties(schema),
                    MappingProxyType(
                        {p.id: p for t in schema.by_id.values() for p in t.properties}
                    ),
                )
            else:
                self.profiles[key] = ValidationProfile.build(
                    schema if isinstance(schema, EdSchema) else None, version, direction
                )
        return self.profiles[key]

    def applicable(self, document: EdDocument, version: str, direction: str) -> Applicability:
        if (
            self.other_rules_only
            and self.selected is not None
            and (version, direction) not in self.selected
        ):
            document = self.scope(document)
        key = (id(document), version, direction)
        if key not in self.applications:
            self.applications[key] = Applicability.build(document, self.profile(version, direction))
        return self.applications[key]
