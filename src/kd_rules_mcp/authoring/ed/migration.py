"""Детерминированный черновик КД 2 → операции собственного менеджера.

Тела BSL не интерпретируются. Семантика тегов: reference/kd2-cfg/DataProcessors/
ВыгрузкаКонвертации/Ext/ObjectModule.bsl:563–738,762–880,896–1000.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any

from kd_rules_mcp.authoring.ed.hook import valid_identifier
from kd_rules_mcp.authoring.ed.manager_candidates import _classify, _enumeration_values
from kd_rules_mcp.authoring.ed.migration_candidates import CandidateContext, unique
from kd_rules_mcp.authoring.ed.migration_inventory import (
    DUPLICATE_CODE,
    EVENT_SIDES,
    Entity,
    Inventory,
    ManualTask,
    active_rules,
    enabled,
    flag_side,
    implicit_rule,
    inventory,
    items,
    reachable,
    snapshot,
)
from kd_rules_mcp.authoring.ed.migration_packets import Packet, operation, packets
from kd_rules_mcp.ed.model import EdDocument
from kd_rules_mcp.ed.schema.model import EdSchema, SchemaType
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.ed.schema.resolver import property_type
from kd_rules_mcp.ed.writer_model import ManagerModel, digest, model_addresses
from kd_rules_mcp.kd2.model import ExchangeRules, Node, rule_code
from kd_rules_mcp.structures.xmldump import KINDS
from kd_rules_mcp.validation.address import side_name
from kd_rules_mcp.validation.ed_schema import required_properties
from kd_rules_mcp.validation.ed_structure_snapshot import StructureObject
from kd_rules_mcp.validation.handlers import EVENT_AREAS

STATES = ("ready", "needs_decision", "manual", "excluded", "blocked")
COLLECTIONS = {item[1]: item[2] for item in KINDS.values()}


def string(value: str) -> dict:
    return {"state": "string", "value": value}


def reference(host: StructureObject, value: str | None = None) -> dict:
    parts = ["Метаданные", COLLECTIONS[host.kind], host.name]
    if value is not None:
        parts = [COLLECTIONS[host.kind], host.name, value]
    return {"state": "reference", "reference_parts": parts}


@dataclass(frozen=True, slots=True)
class MigrationDraft:
    revision: str
    input_hash: str
    decision_hash: str
    coverage: list[Entity]
    manual: list[ManualTask]
    packets: list[Packet]
    accepted: list[dict]
    decisions: list[dict]
    notices: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "draft": True,
            "runtime_verified": False,
            "revision": self.revision,
            "input_hash": self.input_hash,
            "decision_hash": self.decision_hash,
            "entities": len(self.coverage),
            "states": {s: sum(e.state == s for e in self.coverage) for s in STATES},
            "kinds": dict(sorted(Counter(e.kind for e in self.coverage).items())),
            "packet_count": len(self.packets),
            "operation_count": sum(len(p.operations) for p in self.packets),
            "operation_kinds": dict(
                sorted(Counter(o["kind"] for p in self.packets for o in p.operations).items())
            ),
            "accepted_single": self.accepted,
            "decisions": self.decisions,
            "notices": self.notices,
            "registration_hint": "Правила регистрации переносит registration_retarget",
        }

    def page(self, section: str, offset: int, limit: int) -> dict:
        if section == "summary":
            return self.summary()
        rows = {
            "coverage": [asdict(e) for e in self.coverage],
            "manual": [asdict(t) for t in self.manual],
            "decisions": [
                asdict(e) for e in self.coverage if e.state in ("needs_decision", "blocked")
            ],
            "operations": [asdict(p) for p in self.packets],
        }[section]
        selected = rows[offset : offset + limit]
        return {
            "draft": True,
            "revision": self.revision,
            "input_hash": self.input_hash,
            "decision_hash": self.decision_hash,
            "items": selected,
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(selected) < len(rows),
            "next_offset": offset + len(selected),
        }


@dataclass(slots=True)
class Resolved:
    entity: Entity
    host: StructureObject
    typ: SchemaType
    name: str
    address: str
    kind: str
    existing: Any = None
    identification: dict | None = None


@dataclass(slots=True)
class Planned:
    """План полей операции: сериализация только после решения всех зависимостей."""

    entity: Entity
    kind: str
    patch: dict
    address: str
    options: dict


class _Draft:
    def __init__(
        self, inputs, structure, schema, manager, reference_document, decisions, accept_single
    ):
        self.inputs = inputs
        self.manager = manager
        self.context = CandidateContext(structure, schema, reference_document)
        self.inventory: Inventory = inventory(inputs)
        self.by_origin = {e.origin: e for e in self.inventory.entities}
        self.decisions = self.check_decisions(decisions)
        self.accept_single = accept_single
        self.accepted: list[dict] = []
        self.resolved: dict[str, Resolved] = {}
        self.planned: list[Planned] = []
        self.dependencies: dict[str, set[str]] = {}
        self.receive_groups: dict[str, list[Resolved]] = {}
        self.targets: dict[tuple[str, str], Entity] = {}
        self.implicit_targets: dict[str, str] = {}
        self.pko_nodes = {
            role: active_rules(items(rules.root, "ПравилаКонвертацииОбъектов"))
            for role, rules in inputs
        }
        reach = {
            role: reachable(
                rules,
                tuple(
                    self.inventory.nodes[e.origin].code
                    for e in self.inventory.entities
                    if e.kind == "pko"
                    and e.role == role
                    and self.decisions.get(e.origin, {}).get("format_type")
                    and not self.decisions[e.origin].get("exclude")
                ),
                structure=self.context.snapshot,
                role=role,
                inv=self.inventory,
            )
            for role, rules in inputs
        }
        self.reachable = {role: value.codes for role, value in reach.items()}
        self.code_only = {role: value.code_only for role, value in reach.items()}
        self.used_names: dict[str, Entity] = {}
        self.default_names: dict[tuple[str, str], set[str]] = {}

    def check_decisions(self, rows) -> dict[str, dict]:
        if not isinstance(rows, list):
            raise ValueError("decisions должен быть списком")
        result = {}
        allowed = {
            "origin",
            "format_type",
            "format_property",
            "format_value",
            "name",
            "exclude",
            "reason",
        }
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) - allowed
                or not isinstance(row.get("origin"), str)
            ):
                raise ValueError("Некорректная форма decisions")
            origin = row["origin"]
            if origin not in self.by_origin and not origin.startswith("receive:ПОД «"):
                raise ValueError(f"Неизвестный origin: {origin}")
            if origin in result:
                raise ValueError(f"Повтор решения: {origin}")
            if any(not isinstance(v, str) for k, v in row.items() if k != "exclude") or (
                "exclude" in row and type(row["exclude"]) is not bool
            ):
                raise ValueError(f"Неверные типы решения: {origin}")
            result[origin] = row
        return result

    def manual(
        self, entity: Entity, message: str, event: str = "unsupported", body: str = ""
    ) -> None:
        entity.set("manual", "unsupported", message)
        self.inventory.task(entity, event, body)

    def blocked(self, entity: Entity, parent: Entity) -> None:
        entity.set("blocked", "dependency", "Зависит от нерешённой сущности", parent.origin)

    @staticmethod
    def available(entity: Entity) -> bool:
        return entity.state == "ready" or entity.reason == "already_in_manager"

    def decision_valid(self, entity: Entity, fields: set[str]) -> bool:
        row = self.decisions.get(entity.origin, {})
        if set(row) - {"origin", "exclude", "reason", *fields}:
            entity.set(
                "needs_decision", "decision_invalid", "Решение не относится к этому виду сущности"
            )
            return False
        return True

    def choose(self, entity: Entity, key: str, rows: list[dict]) -> str | None:
        entity.candidates = rows[:3]
        entity.candidates_total = len(rows)
        decision = self.decisions.get(entity.origin, {})
        if key in decision:
            return decision[key]
        if len(rows) == 1 and self.accept_single:
            selected = rows[0][key]
            self.accepted.append({"origin": entity.origin, key: selected})
            return selected
        reason = (
            "single_candidate"
            if len(rows) == 1
            else "several_candidates"
            if rows
            else "no_candidate"
        )
        entity.set("needs_decision", reason, "Сопоставление требует решения агента")
        return None

    def name(
        self, entity: Entity, code: str, *, shared: bool = False, pool: str = "rule"
    ) -> str | None:
        row = self.decisions.get(entity.origin, {})
        default_name = (
            code + ("_Отправка" if entity.role == "send" else "_Получение") if shared else code
        )
        existing = (
            self.manager.parameters
            if pool == "parameter"
            else self.manager.pod
            if pool.startswith("pod:")
            else (*self.manager.pko, *self.manager.pkpd)
        )
        occupied_default = pool + ":" + default_name.casefold() in self.used_names or any(
            r.name.casefold() == default_name.casefold() for r in existing
        )
        if (
            "name" in row
            and valid_identifier(default_name)
            and (not occupied_default or pool == "parameter")
        ):
            entity.set(
                "needs_decision",
                "decision_invalid",
                "name допустимо для invalid_name или name_conflict",
            )
            return None
        name = row.get("name", code)
        if "name" not in row and shared:
            name += "_Отправка" if entity.role == "send" else "_Получение"
        if not valid_identifier(name):
            entity.set(
                "needs_decision",
                "invalid_name",
                "Код не является идентификатором BSL; задайте name",
            )
            return None
        if "name" in row and self.default_names.get((pool, name.casefold()), set()) - {
            entity.origin
        }:
            entity.set(
                "needs_decision", "name_conflict", "Имя по умолчанию принадлежит другой сущности"
            )
            return None
        key = pool + ":" + name.casefold()
        other = self.used_names.get(key)
        if other and other.origin != entity.origin:
            if pool == "parameter":
                return name
            entity.set("needs_decision", "name_conflict", "Имя занято другой сущностью черновика")
            return None
        self.used_names[key] = entity
        return name

    def prepare(self) -> None:
        for entity in self.inventory.entities:
            if entity.reason in (DUPLICATE_CODE, "parent_excluded"):
                continue
            if entity.kind == "pko":
                node = self.inventory.nodes[entity.origin]
                key = (entity.role, node.code.casefold())
                self.targets[key] = entity
            fields = {
                "pko": {"format_type", "name"},
                "pks": {"format_property"},
                "pks_group": {"format_property"},
                "pkz": {"format_value"},
                "pvd": {"name"},
                "parameter": {"name"},
            }.get(entity.kind, set())
            if not self.decision_valid(entity, fields):
                continue
            decision = self.decisions.get(entity.origin, {})
            if decision.get("exclude"):
                entity.set(
                    "excluded", "excluded_by_agent", decision.get("reason") or "Исключено агентом"
                )
        pko = [e for e in self.inventory.entities if e.kind == "pko"]
        roles: dict[str, set[str]] = {}
        for entity in pko:
            roles.setdefault(self.inventory.nodes[entity.origin].code.casefold(), set()).add(
                entity.role
            )
        for entity in self.inventory.entities:
            node = self.inventory.nodes[entity.origin]
            if entity.kind in ("pko", "parameter"):
                name = node.code
                if entity.kind == "pko" and len(roles[node.code.casefold()]) > 1:
                    name += "_Отправка" if entity.role == "send" else "_Получение"
                self.reserve_name(
                    entity, name, "parameter" if entity.kind == "parameter" else "rule"
                )
        for entity in pko:
            if entity.state in ("manual", "excluded") or entity.reason == "decision_invalid":
                continue
            node = self.inventory.nodes[entity.origin]
            if not self.decision_valid(entity, {"format_type", "name"}):
                continue
            side = "Источник" if entity.role == "send" else "Приемник"
            host_name = str(node.get(side))
            if not host_name:
                self.manual(entity, "Пустая сторона объекта")
                continue
            host = self.context.host_object(host_name)
            if host is None:
                entity.set(
                    "excluded", "host_object_missing", "Объект стороны отсутствует в структуре"
                )
                continue
            enumeration = host.kind == "Перечисление"
            rows = self.context.object_rows(host, entity.role, enumeration)
            if host.kind == "Справочник" and len(rows) == 2:
                ordered = sorted(rows, key=lambda r: len(r["format_type"]))
                if ordered[1]["format_type"] == ordered[0]["format_type"] + "Группа":
                    self.policy_task(
                        entity,
                        "Группы",
                        "Группы справочника — отдельное ПКО на " + ordered[1]["format_type"],
                    )
                    rows = ordered[:1]
            if node.code.casefold() not in self.reachable[entity.role]:
                entity.candidates, entity.candidates_total = rows[:3], len(rows)
                entity.set(
                    "excluded", "unreachable", "ПКО не вызывается включённым ПВД или ссылкой ПКС"
                )
                continue
            # QName канонизирован для типов расширений; исходные кандидаты сохраняют класс.
            selected = self.choose(entity, "format_type", rows)
            if selected is None:
                continue
            typ = self.context.resolve_type(selected, enumeration)
            if typ is None:
                entity.set(
                    "needs_decision",
                    "decision_invalid",
                    "Тип отсутствует в схеме или не подходит виду правила",
                )
                continue
            kind = "pkpd" if enumeration else "pko"
            collection = self.manager.pkpd if enumeration else self.manager.pko
            matches = [
                r
                for r in collection
                if (entity.role in r.directions or "both" in r.directions)
                and tuple(
                    p.casefold()
                    for p in (
                        r.configuration_type if enumeration else r.configuration_object
                    ).reference_parts
                )
                == tuple(p.casefold() for p in reference(host)["reference_parts"])
                and (r.format_type if enumeration else r.format_object).value
                == self.context.type_name(typ)
            ]
            if matches:
                rule = sorted(matches, key=lambda r: model_addresses(self.manager)[r.logical_id])[0]
                self.resolved[entity.origin] = Resolved(
                    entity,
                    host,
                    typ,
                    rule.name,
                    model_addresses(self.manager)[rule.logical_id],
                    kind,
                    rule,
                )
                entity.set("excluded", "already_in_manager", "Правило уже есть в менеджере")
                continue
            shared = len(roles[node.code.casefold()]) > 1 or any(
                r.name.casefold() == node.code.casefold() and entity.role not in r.directions
                for r in (*self.manager.pko, *self.manager.pkpd)
            )
            name = self.name(entity, node.code, shared=shared)
            if name is None:
                continue
            resolved = Resolved(
                entity,
                host,
                typ,
                name,
                ("ПКПД/" if enumeration else "ПКО/") + name,
                kind,
            )
            occupied = [
                r
                for r in (*self.manager.pko, *self.manager.pkpd)
                if r.name.casefold() == name.casefold()
                and (entity.role in r.directions or "both" in r.directions)
            ]
            if occupied:
                rule = occupied[0]
                cfg = (
                    rule.configuration_type
                    if enumeration and rule in self.manager.pkpd
                    else getattr(rule, "configuration_object", None)
                )
                fmt = (
                    rule.format_type
                    if enumeration and rule in self.manager.pkpd
                    else getattr(rule, "format_object", None)
                )
                if (
                    cfg is None
                    or fmt is None
                    or tuple(p.casefold() for p in cfg.reference_parts)
                    != tuple(p.casefold() for p in reference(host)["reference_parts"])
                    or fmt.value != self.context.type_name(typ)
                ):
                    entity.set(
                        "needs_decision", "name_conflict", "Имя занято правилом с другим содержимым"
                    )
                    continue
                resolved.name = rule.name
                resolved.address = model_addresses(self.manager)[rule.logical_id]
                resolved.existing = rule
                entity.set("excluded", "already_in_manager", "Правило уже есть в менеджере")
            else:
                entity.set("ready", "resolved", "Каркас правила решён")
            self.resolved[entity.origin] = resolved
        # ПОД — отдельная сущность; неоднозначный выбор ПКО не блокирует сами ПКО.
        for resolved in self.resolved.values():
            if resolved.kind == "pko" and resolved.entity.role == "receive":
                self.receive_groups.setdefault(self.context.type_name(resolved.typ), []).append(
                    resolved
                )
        for typ, group in self.receive_groups.items():
            entity = self.inventory.add(
                "receive", f"ПОД «{typ}»", self.inventory.nodes[group[0].entity.origin]
            )
            entity.kind = "pod_receive"
            self.by_origin[entity.origin] = entity
            self.decision_valid(entity, {"name"})
            if len(group) == 1:
                self.reserve_name(entity, group[0].name + "_Получение", "pod:")
        for entity in self.inventory.entities:
            if entity.kind == "pvd":
                target = self.targets.get(
                    (
                        entity.role,
                        rule_code(
                            self.inventory.nodes[entity.origin].get("КодПравилаКонвертации")
                        ).casefold(),
                    )
                )
                if target and (resolved := self.resolved.get(target.origin)):
                    self.reserve_name(entity, resolved.name + "_Отправка", "pod:")
        for origin in self.decisions:
            if origin not in self.by_origin:
                raise ValueError(f"Неизвестный origin: {origin}")

    def reserve_name(self, entity: Entity, name: str, pool: str) -> None:
        if entity.reason == DUPLICATE_CODE or self.decisions.get(entity.origin, {}).get("exclude"):
            return
        self.default_names.setdefault((pool, name.casefold()), set()).add(entity.origin)

    def policy_task(
        self, parent: Entity, suffix: str, message: str, event: str = "unsupported"
    ) -> None:
        entity = self.inventory.add(
            parent.role,
            parent.origin.split(":", 1)[1] + "/" + suffix,
            self.inventory.nodes[parent.origin],
            parent.origin,
        )
        entity.kind = "policy"
        self.by_origin[entity.origin] = entity
        self.manual(entity, message, event)

    def identification(self, resolved: Resolved) -> dict | None:
        node = self.inventory.nodes[resolved.entity.origin]
        uid = node.get("СинхронизироватьПоИдентификатору") is True
        fallback = node.get("ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли") is True
        header = node.child("Свойства")
        search = []
        uid_reference = False
        non_reference_fields = False
        for entity in self.inventory.entities:
            if entity.parent != resolved.entity.origin or entity.kind != "pks":
                continue
            prop = self.inventory.nodes[entity.origin]
            if (
                header is None
                or not any(prop is item for item in header.items)
                or not (prop.attrs.get("Поиск") is True or prop.attrs.get("Обязательное") is True)
            ):
                continue
            name = side_name(prop, "Приемник")
            if name.casefold() != "ссылка":
                non_reference_fields = True
            if entity.state == "excluded" and entity.reason != "already_in_manager":
                uid_reference |= name.casefold() == "ссылка" and entity.reason == "identification"
                continue
            if name.casefold() == "ссылка":
                if uid:
                    uid_reference = True
                    continue
                # Без СинхронизироватьПоИдентификатору поле «Ссылка» ищется общим запросом
                # (БСП:3934, НайтиЭлементЗапросом), а не по УИД: режим не угадываем.
                self.manual(
                    entity, "Поиск по полю «Ссылка» без синхронизации по УИД (БСП:8402–8445)"
                )
                self.blocked(resolved.entity, entity)
                return None
            search.append((entity, name))
        # VK:821–822; БСП:810–811,8856–8865; XDTO:1896–1897:
        # без продолжения поиска режим по УИД не обращается к полям поиска.
        if uid and (not fallback or (uid_reference and not non_reference_fields)):
            return {"mode": string("ПоУникальномуИдентификатору"), "search_sets": []}
        fields = []
        for entity, name in search:
            if not self.available(entity):
                self.blocked(resolved.entity, entity)
                return None
            found = resolved.host.property(name)
            if len(found) != 1:
                self.blocked(resolved.entity, entity)
                return None
            canonical = found[0].path
            if canonical not in fields:
                fields.append(canonical)
        mode = (
            "СначалаПоУникальномуИдентификаторуПотомПоПолямПоиска"
            if uid and fallback
            else "ПоУникальномуИдентификатору"
            if uid
            else "ПоПолямПоиска"
        )
        if mode != "ПоУникальномуИдентификатору" and not fields:
            self.manual(
                resolved.entity,
                "Запись регистра: нужен отбор по измерениям (БСП:11205–11213)"
                if resolved.host.kind.startswith("Регистр")
                else "Поиск без полей: набор полей поиска пуст",
                "Идентификация",
            )
            return None
        return {"mode": string(mode), "search_sets": [fields] if fields else []}

    def add(self, entity: Entity, kind: str, patch: dict, address: str, **kwargs) -> None:
        self.planned.append(Planned(entity, kind, patch, address, kwargs))
        entity.set("ready", "resolved", "Операция решена")

    def create_rules(self) -> None:
        # ПКПД и значения первыми, затем все ПКО до любых ссылочных ПКС (циклы допустимы).
        for kind in ("pkpd", "pko"):
            for resolved in list(self.resolved.values()):
                if resolved.kind != kind:
                    continue
                entity = resolved.entity
                if not self.available(entity):
                    continue
                if resolved.existing is None:
                    patch = {"name": resolved.name, "directions": [entity.role]}
                    if kind == "pkpd":
                        patch.update(
                            configuration_type=reference(resolved.host),
                            format_type=string(self.context.type_name(resolved.typ)),
                            data_kind="enumeration",
                        )
                    else:
                        patch.update(
                            configuration_object=reference(resolved.host),
                            format_object=string(self.context.type_name(resolved.typ)),
                        )
                        if entity.role == "receive":
                            patch["identification"] = resolved.identification
                    self.add(entity, kind, patch, resolved.address, group=entity.origin)

    def enum_values(self, resolved: Resolved) -> None:
        literals = _enumeration_values(self.context.schema, resolved.typ) or ()
        used: set[str] = set()
        for entity in self.inventory.entities:
            if (
                entity.parent != resolved.entity.origin
                or entity.kind != "pkz"
                or entity.state in ("manual", "excluded")
            ):
                continue
            node = self.inventory.nodes[entity.origin]
            if not self.decision_valid(entity, {"format_value"}):
                continue
            if not node.get("Источник") or not node.get("Приемник"):
                self.manual(entity, "Пустая сторона значения ПКЗ")
                continue
            value = str(node.get("Источник" if entity.role == "send" else "Приемник"))
            found = [
                v[0]
                for v in self.context.structure.execute(
                    "SELECT name FROM object_values WHERE object_id=? ORDER BY name",
                    (resolved.host.id,),
                )
                if v[0].casefold() == value.casefold()
            ]
            if len(found) != 1:
                self.manual(entity, "Значение перечисления отсутствует в структуре")
                continue
            value = found[0]
            other = str(node.get("Приемник" if entity.role == "send" else "Источник"))
            rows = unique(
                self.context.enum_rows(resolved.host, resolved.typ, value, entity.role)
                + [
                    {
                        "format_value": literal,
                        "auto": False,
                        "reason": "по значению другой стороны КД 2",
                    }
                    for literal in literals
                    if literal.casefold() == other.casefold()
                ],
                "format_value",
            )
            literal = self.choose(entity, "format_value", rows)
            if literal is None:
                continue
            # Литералы XDTO чувствительны к регистру; решение канонизируется только
            # при однозначном равенстве без учёта регистра имени 1С.
            matches = (
                [literal]
                if literal in literals
                else [f for f in literals if f.casefold() == literal.casefold()]
            )
            if len(matches) != 1:
                entity.set(
                    "needs_decision",
                    "decision_invalid",
                    "Литерал не найден в выбранном типе формата",
                )
                continue
            literal = matches[0]
            unique_key = value.casefold() if entity.role == "send" else literal
            if unique_key in used:
                entity.set("needs_decision", "name_conflict", "Повтор ключа карты значений")
                continue
            used.add(unique_key)
            existing = resolved.existing.mappings if resolved.existing else ()
            matched = [
                m
                for m in existing
                if m.direction == entity.role
                and (
                    m.configuration_value.reference_parts[-1].casefold() == value.casefold()
                    if entity.role == "send"
                    else m.format_value.value == literal
                )
            ]
            if matched:
                if (
                    matched[0].configuration_value.reference_parts[-1].casefold()
                    == value.casefold()
                    and matched[0].format_value.value == literal
                ):
                    entity.set("excluded", "already_in_manager", "Пара значений уже есть")
                else:
                    entity.set(
                        "needs_decision", "name_conflict", "Ключ уже занят другой парой значений"
                    )
                continue
            patch = {
                "direction": entity.role,
                "configuration_value": reference(resolved.host, value),
                "format_value": string(literal),
            }
            self.add(
                entity,
                "value_mapping",
                patch,
                resolved.address + "/Значение/" + value,
                owner_id={"address": resolved.address},
                group=resolved.entity.origin,
            )

    @staticmethod
    def has_handlers(node: Node, role: str) -> bool:
        return any(
            kind == node.kind.name
            and (event in node.values or event in node.attrs)
            and EVENT_SIDES.get((kind, event), role) == role
            for kind, event in EVENT_AREAS
        )

    def properties(self) -> None:
        created: set[tuple[str, str, str]] = set()
        for resolved in self.resolved.values():
            if resolved.kind != "pko":
                continue
            groups: dict[str, tuple[Entity, str, Any, str, SchemaType | None]] = {}
            rows = [
                e
                for e in self.inventory.entities
                if e.parent == resolved.entity.origin and e.kind in ("pks", "pks_group")
            ]
            for entity in rows:
                parent_group = next(
                    (
                        v
                        for k, v in sorted(groups.items(), key=lambda x: -len(x[0]))
                        if entity.origin.startswith(k + "/")
                    ),
                    None,
                )
                node = self.inventory.nodes[entity.origin]
                group = entity.kind == "pks_group"
                address = resolved.address
                existing = resolved.existing
                table, row_type = "", None
                if parent_group:
                    owner, address, existing, table, row_type = parent_group
                    self.dependencies.setdefault(entity.origin, set()).add(owner.origin)
                    if owner.state == "excluded" and owner.reason != "already_in_manager":
                        entity.set(
                            "excluded", "parent_excluded", "Группа исключена: " + owner.reason
                        )
                        continue
                    if owner.state not in ("ready", "excluded") or owner.reason not in (
                        "resolved",
                        "already_in_manager",
                    ):
                        if entity.state not in ("excluded", "manual"):
                            self.blocked(entity, owner)
                        continue
                if group:
                    groups[entity.origin] = (entity, "", None, "", None)
                if entity.state in ("manual", "excluded") or entity.reason == "decision_invalid":
                    continue
                if not enabled(node) or entity.origin in self.inventory.disabled:
                    entity.set("excluded", "disabled", "Правило отключено")
                    continue
                if not self.decision_valid(entity, {"format_property"}):
                    continue
                if (
                    entity.role == "receive"
                    and not group
                    and parent_group is None
                    and side_name(node, "Приемник").casefold() == "ссылка"
                    and (node.attrs.get("Поиск") is True or node.attrs.get("Обязательное") is True)
                    and self.inventory.nodes[resolved.entity.origin].get(
                        "СинхронизироватьПоИдентификатору"
                    )
                    is True
                ):
                    entity.set("excluded", "identification", "Ссылка учтена в идентификации по УИД")
                    continue
                if self.has_handlers(node, entity.role):
                    self.manual(entity, "ПКС с обработчиком переносится вручную")
                    continue
                # Получатель читает Имя свойства сообщения, источник может вычислять значение.
                # БСП:9465–9480,12075–12093.
                sides = [
                    node.child(s)
                    for s in (("Источник", "Приемник") if entity.role == "send" else ("Приемник",))
                ]
                allowed = {
                    "",
                    "Реквизит",
                    "Свойство",
                    "Измерение",
                    "Ресурс",
                    "ТабличнаяЧасть",
                    "СтандартныйРеквизит",
                }
                if any(
                    s is None
                    or not s.attrs.get("Имя")
                    or str(s.attrs.get("Вид", "")) not in allowed
                    for s in sides
                ):
                    self.manual(entity, "Пустая сторона или неподдержанный вид ПКС")
                    continue
                if group and parent_group:
                    self.manual(entity, "Вложенные группы требуют ручного разбора")
                    continue
                side = node.child("Источник" if entity.role == "send" else "Приемник")
                if group and (
                    side is None or str(side.attrs.get("Вид", "")).casefold() != "табличнаячасть"
                ):
                    self.manual(entity, "Группа не является табличной частью стороны")
                    continue
                if any(
                    node.get(k) and flag_side(node.kind.name, k) in (None, entity.role)
                    for k in (
                        "НеЗамещать",
                        "ПолучитьИзВходящихДанных",
                        "ИмяПараметраДляПередачи",
                        "ПриводитьКДлине",
                        "ПоискПоДатеНаРавенство",
                        "ВыгружатьГруппуЧерезФайл",
                    )
                ):
                    self.manual(entity, "Дополнительная политика ПКС требует ручного переноса")
                    continue
                cfg = side_name(node, "Источник" if entity.role == "send" else "Приемник")
                full = f"{table}.{cfg}" if table else cfg
                found = resolved.host.property(full, table=bool(table))
                if group:
                    found = tuple(p for p in found if p.kind == "ТабличнаяЧасть")
                if len(found) != 1:
                    self.manual(entity, "Свойство стороны отсутствует или неоднозначно в структуре")
                    continue
                cfg = found[0].path[len(table) + 1 :] if table else found[0].path
                conversion = None
                self.dependencies.setdefault(entity.origin, set()).add(resolved.entity.origin)
                code = rule_code(node.get("КодПравилаКонвертации"))
                implicit = (
                    implicit_rule(self.pko_nodes[entity.role], found[0].types, entity.role)
                    if not code and not group
                    else None
                )
                if implicit is not None:
                    code = implicit.code
                if not code and not group and any("Ссылка." in t for t in found[0].types):
                    self.manual(
                        entity, "Без кода ПКО ссылочный тип требует действующего ПКО этой роли"
                    )
                    continue
                if code:
                    target = self.targets.get((entity.role, code.casefold()))
                    if target is None:
                        self.manual(entity, "КодПравилаКонвертации ссылается на отсутствующее ПКО")
                        continue
                    if implicit is not None:
                        self.implicit_targets[entity.origin] = target.origin
                    conversion = self.resolved.get(target.origin)
                    self.dependencies[entity.origin].add(target.origin)
                    if conversion is None:
                        self.blocked(entity, target)
                        continue
                    if group:
                        self.manual(entity, "Конвертация группы через отдельное ПКО не поддержана")
                        continue
                candidates = self.context.property_rows(
                    resolved.host,
                    resolved.typ,
                    entity.role,
                    table=table,
                    row=row_type,
                    groups=group,
                )
                candidates = unique(
                    [
                        {**r, "format_property": r.get("format_path", "")}
                        for r in candidates
                        if r["configuration"].casefold() == cfg.casefold()
                        and r.get("class") not in ("mismatch", "algorithm", "unresolved")
                    ],
                    "format_path",
                    default_namespace=self.context.schema.base_namespace,
                )
                selected = self.choose(entity, "format_property", candidates)
                if selected is None:
                    continue
                fields = self.context.format_fields(row_type or resolved.typ, groups=group)
                fields = [
                    f
                    for f in fields
                    if selected.casefold() in (f.path.casefold(), f.leaf.casefold())
                ]
                if len(fields) != 1:
                    entity.set(
                        "needs_decision",
                        "decision_invalid",
                        "Свойство не принадлежит выбранному типу или строке",
                    )
                    continue
                fmt = fields[0]
                classified = (
                    _classify(
                        self.context.schema,
                        ValidationProfile.build(self.context.schema, None, entity.role),
                        fmt.prop,
                        found[0],
                        entity.role,
                    )
                    if not group
                    else None
                )
                # Исполнитель хранит имя листа, физический путь остаётся доказательством кандидата.
                patch = {"configuration_property": cfg, "format_property": fmt.leaf}
                if fmt.namespace != self.context.schema.base_namespace:
                    patch["namespace"] = fmt.namespace
                if conversion is not None:
                    assert classified is not None
                    assert conversion.typ.qname is not None
                    expected = conversion.typ.qname.local
                    compatible = (
                        classified.match == "needs_pkpd" and classified.type_name == expected
                        if conversion.kind == "pkpd"
                        else classified.match == "reference"
                        and classified.format_object == expected
                    )
                    if not compatible:
                        self.manual(
                            entity, "Тип свойства формата не соответствует целевому правилу"
                        )
                        continue
                    patch.update(
                        property_kind="pkpd" if conversion.kind == "pkpd" else "reference",
                        conversion={"address": conversion.address},
                    )
                elif not group:
                    if classified is None or classified.match != "direct":
                        self.manual(entity, "Без кода ПКО допустима только совместимая прямая ПКС")
                        continue
                    patch["property_kind"] = "direct"
                collection = (
                    existing.groups
                    if existing and group
                    else existing.properties
                    if existing
                    else ()
                )
                property_key = (
                    address,
                    "table_part" if group else "property",
                    (fmt.leaf if group or entity.role == "send" else cfg).casefold(),
                )
                if property_key in created:
                    entity.set(
                        "needs_decision", "name_conflict", "Имя свойства уже занято в черновике"
                    )
                    continue
                occupied = [
                    p
                    for p in collection
                    if (
                        p.format_property
                        if group or entity.role == "send"
                        else p.configuration_property
                    ).casefold()
                    == (fmt.leaf if group or entity.role == "send" else cfg).casefold()
                ]
                if occupied:
                    prop = occupied[0]
                    same = (
                        prop.configuration_property.casefold() == cfg.casefold()
                        and prop.format_property == fmt.leaf
                        and prop.namespace == patch.get("namespace", "")
                        and (
                            group
                            or (
                                prop.property_kind == patch["property_kind"]
                                and (not conversion or prop.conversion.name == conversion.name)
                            )
                        )
                    )
                    entity.set(
                        "excluded" if same else "needs_decision",
                        "already_in_manager" if same else "name_conflict",
                        "Свойство уже есть" if same else "Имя свойства занято другой конвертацией",
                    )
                else:
                    suffix = "/ПКТЧ/" if group else "/ПКС/"
                    self.add(
                        entity,
                        "table_part" if group else "property",
                        patch,
                        address + suffix + (fmt.leaf if group else cfg),
                        owner_id={"address": address},
                        group=resolved.entity.origin,
                    )
                    created.add(property_key)
                if group:
                    groups[entity.origin] = (
                        entity,
                        model_addresses(self.manager)[occupied[0].logical_id]
                        if occupied
                        else address + "/ПКТЧ/" + fmt.leaf,
                        occupied[0] if occupied else None,
                        cfg,
                        self.context.row_type(resolved.typ, fmt.path),
                    )

    @staticmethod
    def pod_uses(pod, resolved: Resolved) -> bool:
        return any(
            (resolved.existing is not None and r.target_id == resolved.existing.logical_id)
            or r.name == resolved.address
            for r in pod.used_pko
        )

    def pod(self, entity: Entity, resolved: Resolved, name: str, selection: dict) -> None:
        # Р5: совпадение по содержимому важнее имени, которое зависит от состава входов.
        def same(p):
            if entity.role not in p.directions and "both" not in p.directions:
                return False
            if entity.role == "receive":
                return p.format_selection.value == selection["format_selection"][
                    "value"
                ] and self.pod_uses(p, resolved)
            return tuple(v.casefold() for v in p.configuration_selection.reference_parts) == tuple(
                v.casefold() for v in selection["configuration_selection"]["reference_parts"]
            ) and any(
                r.target_id == resolved.existing.logical_id
                if resolved.existing is not None
                else r.name.casefold() == resolved.name.casefold()
                for r in p.used_pko
            )

        if any(same(p) for p in self.manager.pod):
            entity.set("excluded", "already_in_manager", "ПОД уже есть в менеджере")
            return
        # Имя ПОД одно на модуль: генератор КД 3 пишет процедуру «ДобавитьПОД_<Наименование>»
        # без направления (reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/
        # ObjectModule.bsl:1587), поэтому ПОД отправки и получения с одним именем — конфликт.
        selected_name = self.name(entity, name, pool="pod:")
        if selected_name is None:
            return
        name = selected_name
        patch = {
            "name": name,
            "directions": [entity.role],
            **selection,
            "used_pko": [{"address": resolved.address}],
        }
        if any(p.name.casefold() == name.casefold() for p in self.manager.pod):
            entity.set("needs_decision", "name_conflict", "Имя ПОД занято другим правилом")
            return
        self.dependencies.setdefault(entity.origin, set()).add(resolved.entity.origin)
        self.add(entity, "pod", patch, "ПОД/" + name + "~" + entity.role)

    def receive_pods(self) -> None:
        for typ, group in self.receive_groups.items():
            entity = self.by_origin[f"receive:ПОД «{typ}»"]
            decision = self.decisions.get(entity.origin, {})
            if entity.reason == "decision_invalid":
                continue
            if decision.get("exclude"):
                entity.set(
                    "excluded", "excluded_by_agent", decision.get("reason") or "Исключено агентом"
                )
                continue
            unresolved = [r.entity for r in group if not self.available(r.entity)]
            if unresolved:
                self.blocked(entity, unresolved[0])
                continue
            existing = [
                p
                for p in self.manager.pod
                if ("receive" in p.directions or "both" in p.directions)
                and p.format_selection.value == typ
            ]
            if existing:
                matched = next(
                    (p for p in existing if all(self.pod_uses(p, r) for r in group)), None
                )
                if matched is not None:
                    entity.set(
                        "excluded", "already_in_manager", "ПОД этого типа уже использует все ПКО"
                    )
                else:
                    addresses = model_addresses(self.manager)
                    pod = min(existing, key=lambda p: addresses[p.logical_id])
                    self.manual(
                        entity,
                        "Нужно добавить ПКО в ПОД " + addresses[pod.logical_id],
                        "ИспользуемыеПКО",
                    )
            elif len(group) > 1:
                self.manual(
                    entity, "Выбор одного из нескольких ПКО по данным требует кода", "ПриОбработке"
                )
            else:
                resolved = group[0]
                self.pod(
                    entity,
                    resolved,
                    resolved.name + "_Получение",
                    {"format_selection": string(typ)},
                )
                if entity.state == "needs_decision":
                    self.blocked(resolved.entity, entity)

    def settle(self) -> None:
        """Неподдержанные поля поиска и ссылки распространяют блокировку до устойчивости."""
        changed = True
        while changed:
            before = [(e.state, tuple(e.blocked_by)) for e in self.inventory.entities]
            for resolved in self.resolved.values():
                if (
                    resolved.kind == "pko"
                    and resolved.entity.role == "receive"
                    and (
                        resolved.entity.state == "ready"
                        or resolved.entity.reason == "already_in_manager"
                    )
                ):
                    resolved.identification = self.identification(resolved)
                    if resolved.existing is not None and resolved.identification is not None:
                        actual = resolved.existing.identification
                        project = {
                            "mode": string(actual.mode.value),
                            "search_sets": [list(s.fields) for s in actual.search_sets],
                        }

                        def normalized(value):
                            return (
                                value["mode"]["value"],
                                [sorted(f.casefold() for f in s) for s in value["search_sets"]],
                            )

                        if normalized(project) != normalized(resolved.identification):
                            self.manual(
                                resolved.entity,
                                "Идентификация отличается: КД 2 "
                                + json.dumps(resolved.identification, ensure_ascii=False)
                                + " / проект "
                                + json.dumps(project, ensure_ascii=False),
                                "Идентификация",
                            )
            for origin, dependencies in self.dependencies.items():
                entity = self.by_origin[origin]
                if entity.state != "ready":
                    continue
                for parent in sorted(dependencies):
                    if not self.available(self.by_origin[parent]):
                        self.blocked(entity, self.by_origin[parent])
                        break
            changed = before != [(e.state, tuple(e.blocked_by)) for e in self.inventory.entities]

    def child_state(self, entity: Entity) -> None:
        if entity.reason == "decision_invalid":
            return
        if not entity.parent or entity.kind not in ("pks", "pks_group", "pkz", "pkz_group"):
            return
        parent = self.by_origin[entity.parent]
        if parent.state == "excluded" and parent.reason != "already_in_manager":
            entity.set("excluded", "parent_excluded", "ПКО исключено: " + parent.reason)
        elif (
            entity.kind in ("pks", "pks_group")
            and (resolved := self.resolved.get(parent.origin))
            and resolved.kind == "pkpd"
        ):
            entity.set("excluded", "not_applicable", "ПКС у ПКО перечисления не переносится")
        elif (
            parent.origin not in self.resolved
            and entity.state == "needs_decision"
            and entity.reason != "decision_invalid"
        ):
            self.blocked(entity, parent)
        elif (
            entity.kind == "pkz"
            and (resolved := self.resolved.get(parent.origin))
            and resolved.kind != "pkpd"
            and entity.state != "excluded"
            and entity.reason != "decision_invalid"
        ):
            self.manual(entity, "ПКЗ предопределённого элемента переносится вручную", "Значения")

    def remainder(self) -> None:
        for entity in self.inventory.entities:
            node = self.inventory.nodes[entity.origin]
            if entity.kind in (
                "handler",
                "exchange_rules",
                "pko",
                "pks",
                "pks_group",
                "pkz",
                "pkz_group",
                "pod_receive",
            ):
                self.child_state(entity)
                continue
            if entity.state in ("manual", "excluded") or entity.reason == "decision_invalid":
                continue
            if entity.kind == "pvd":
                if not enabled(node) or entity.origin in self.inventory.disabled:
                    entity.set("excluded", "disabled", "ПВД отключено")
                    continue
                if entity.role != "send":
                    entity.set(
                        "excluded", "opposite_role", "ПВД относится к отправке другой конфигурации"
                    )
                    continue
                if (
                    self.has_handlers(node, entity.role)
                    or str(node.get("СпособОтбораДанных")).casefold() != "стандартнаявыборка"
                ):
                    event = next(
                        (
                            event
                            for kind, event in EVENT_AREAS
                            if kind == "pvd" and event in node.values
                        ),
                        "СпособОтбораДанных",
                    )
                    self.manual(
                        entity,
                        "Обработчики или нестандартная выборка ПВД",
                        event,
                        str(node.get(event)),
                    )
                    continue
                target = self.targets.get(
                    (entity.role, rule_code(node.get("КодПравилаКонвертации")).casefold())
                )
                if target is None:
                    self.manual(entity, "ПВД ссылается на отсутствующее ПКО")
                    continue
                resolved = self.resolved.get(target.origin)
                if resolved is None or not self.available(target):
                    self.blocked(entity, target)
                    continue
                host = self.context.host_object(str(node.get("ОбъектВыборки")))
                if host is None or resolved.kind != "pko":
                    self.manual(entity, "Объект выборки не найден или ПКО не является объектным")
                    continue
                if host.id != resolved.host.id:
                    self.manual(entity, "Объект выборки отличается от объекта ПКО", "ОбъектВыборки")
                    continue
                if not self.decision_valid(entity, {"name"}):
                    continue
                self.pod(
                    entity,
                    resolved,
                    resolved.name + "_Отправка",
                    {"configuration_selection": reference(host)},
                )
            elif entity.kind == "parameter":
                if not self.decision_valid(entity, {"name"}):
                    continue
                name = self.name(entity, node.code, pool="parameter")
                if name:
                    if any(
                        p.name.casefold() == name.casefold() for p in self.manager.parameters
                    ) or any(
                        p.kind == "parameter" and p.patch["name"].casefold() == name.casefold()
                        for p in self.planned
                    ):
                        entity.set("excluded", "already_in_manager", "Параметр уже объявлен")
                    else:
                        self.add(entity, "parameter", {"name": name}, "Параметр/" + name)
            else:
                self.manual(
                    entity,
                    "Сущность переносится вручную",
                    "Текст" if entity.kind in ("algorithm", "query") else "unsupported",
                    str(node.get("Текст")),
                )
        # Отключённые ПКС и группы сохраняют disabled даже при нерешённом владельце.
        for entity in self.inventory.entities:
            node = self.inventory.nodes[entity.origin]
            if (
                entity.kind in ("pks", "pks_group", "pvd")
                and (not enabled(node) or entity.origin in self.inventory.disabled)
                and self.decisions.get(entity.origin, {}).get("exclude") is not True
                and entity.reason not in ("decision_invalid", "parent_excluded", "not_applicable")
            ):
                entity.set("excluded", "disabled", "Правило отключено")

    def excluded_children(self) -> None:
        for entity in self.inventory.entities:
            if entity.kind not in ("handler", "policy") or not entity.parent:
                continue
            parent = self.by_origin[entity.parent]
            if parent.kind not in ("pko", "pks", "pks_group", "pvd") or parent.state != "excluded":
                continue
            if entity.reason == "decision_invalid":
                continue
            if entity.reason == "opposite_role":
                continue
            if parent.reason == "already_in_manager":
                if entity.state == "manual":
                    entity.message = (
                        "Правило уже в проекте — проверить, перенесено ли: " + entity.message
                    )
                    self.inventory.task(entity, "unsupported")
                continue
            reason = "disabled" if parent.reason == "disabled" else "parent_excluded"
            if parent.reason == "opposite_role":
                reason = "opposite_role"
            entity.set("excluded", reason, "Родительское правило исключено: " + parent.reason)

    def required_keys(self) -> None:
        # ed_required.check_required_unfilled: ключ ссылки проверяется и у optional-оболочки.
        # Расчёт обязательных листьев переиспользует ed_schema.required_properties (XDTO:981–993).
        schema = self.context.schema
        profile = ValidationProfile.build(schema, None, "send")
        for resolved in self.resolved.values():
            if (
                resolved.kind != "pko"
                or resolved.entity.role != "send"
                or not self.available(resolved.entity)
            ):
                continue
            wrapper = profile.resolve(resolved.typ, "КлючевыеСвойства")
            if wrapper.status != "resolved":
                continue
            typ = property_type(schema, profile.properties[wrapper.property_ids[0]])
            if typ is None:
                continue
            needed = {path for _, path in required_properties(profile, typ, ("КлючевыеСвойства",))}
            supplied = {
                p.patch["format_property"]
                for p in self.planned
                if p.kind == "property"
                and p.entity.parent == resolved.entity.origin
                and p.entity.state == "ready"
                and not p.options.get("owner_id", {}).get("address", "").count("/ПКТЧ/")
            }
            if resolved.existing is not None:
                supplied.update(p.format_property for p in resolved.existing.properties)
            covered = set()
            for path in supplied:
                match = profile.resolve(resolved.typ, path)
                covered.update(".".join(q.local for q in parts) for parts in match.physical_paths)
            missing = sorted(needed - covered)
            if missing:
                self.policy_task(
                    resolved.entity,
                    "КлючевыеСвойства",
                    "Ключевые свойства формата не заполнены: " + ", ".join(missing),
                )


def build_migration_draft(
    rules: list[tuple[str, ExchangeRules]],
    structure: sqlite3.Connection,
    schema: EdSchema,
    manager: ManagerModel,
    *,
    reference_document: EdDocument | dict[str, EdDocument] | None = None,
    decisions: list[dict] | None = None,
    accept_single: bool = False,
) -> MigrationDraft:
    """Только чтение входных моделей; результат не создаёт проект или файлы."""
    if (
        not 1 <= len(rules) <= 2
        or any(r not in ("send", "receive") for r, _ in rules)
        or len({r for r, _ in rules}) != len(rules)
    ):
        raise ValueError("Нужны 1–2 файла с разными ролями send/receive")
    if type(accept_single) is not bool:
        raise ValueError("accept_single должен быть булевым")
    draft = _Draft(
        rules,
        structure,
        schema,
        manager,
        reference_document,
        decisions if decisions is not None else [],
        accept_single,
    )
    draft.prepare()
    for resolved in draft.resolved.values():
        if resolved.kind == "pkpd":
            draft.enum_values(resolved)
    draft.properties()
    draft.settle()
    draft.receive_pods()
    draft.settle()
    draft.remainder()
    draft.settle()
    draft.required_keys()
    draft.excluded_children()
    draft.create_rules()
    for entity in draft.inventory.entities:
        if entity.origin in draft.implicit_targets:
            entity.message += (
                "; ПКО выбрано неявно по типу: " + draft.implicit_targets[entity.origin]
            )
        if entity.kind == "pko":
            code = draft.inventory.nodes[entity.origin].code.casefold()
            if entity.reason != DUPLICATE_CODE and code in draft.code_only[entity.role]:
                entity.message += "; Вызывается из кода: " + draft.code_only[entity.role][code]
    operations = [
        operation(p.entity.origin, p.kind, p.patch, p.address, **p.options)
        for p in draft.planned
        if p.entity.state == "ready"
    ]
    normalized = [draft.decisions[k] for k in sorted(draft.decisions)]
    decision_hash = digest((normalized, accept_single))
    input_hash = digest(
        (
            [(role, snapshot(document.root)) for role, document in sorted(rules)],
            sha256(structure.serialize()).hexdigest(),
            schema.schema_id,
            manager.revision,
            {
                role: [f.sha256 for f in doc.files]
                for role, doc in sorted(reference_document.items())
            }
            if isinstance(reference_document, dict)
            else [f.sha256 for f in reference_document.files]
            if reference_document
            else [],
            decision_hash,
        )
    )
    return MigrationDraft(
        manager.revision,
        input_hash,
        decision_hash,
        draft.inventory.entities,
        [t for t in draft.inventory.manual if draft.by_origin[t.origin].state == "manual"],
        packets(operations, manager.revision),
        draft.accepted,
        normalized,
    )
