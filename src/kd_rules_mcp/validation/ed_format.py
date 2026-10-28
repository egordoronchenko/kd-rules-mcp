"""Контракт правил с пакетами формата; основания — строки исполнителя XDTO."""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from kd_rules_mcp.ed.schema.model import EdSchema, QName
from kd_rules_mcp.ed.writer_import import property_directions
from kd_rules_mcp.ed.writer_model import ManagerModel, Property, model_addresses
from kd_rules_mcp.validation.report import ValidationReport

PREFIX = "ed.format."


def validate_format(
    model: ManagerModel, schema: EdSchema | None, packages: Sequence[Any] = ()
) -> ValidationReport:
    """Литералы проверяются статически, недоступное условие имеет явный skipped.

    packages — пакеты сборки из файлов (FormatPackage): свои URI наравне с пакетами модели.
    Пустой объект формата исполнитель не ищет (XDTO:4146), такие правила пропускаются.
    """
    report = ValidationReport()
    addresses = model_addresses(model)
    own_types = {}

    def collect(typ):
        own_types[typ.name] = typ
        for prop in typ.properties:
            if prop.inline_type:
                collect(prop.inline_type)

    for package in (*model.format_packages, *packages):
        for typ in package.types:
            collect(typ)
    own_uris = {p.namespace for p in (*model.format_packages, *packages)}
    if schema:
        own_uris.update(schema.extension_namespaces)
        own_uris.update(
            p.namespace for p in schema.packages if p.namespace != schema.base_namespace
        )
    known_uris = own_uris | ({p.namespace for p in schema.packages} if schema else set())
    base_uri = schema.base_namespace if schema else ""
    schema_types = schema.types if schema else {}
    guards = {g.logical_id: g for g in model.guards}

    def skip(check, address, reason):
        report.skip(PREFIX + check, reason + ": " + address)

    def error(check, address, text):
        report.error(PREFIX + check, address, text)

    def literal(value, check, address):
        if value.state != "string":
            skip(check, address, "not_literal")
            return None
        return str(value.value)

    def has_type(uri, name):
        return QName(uri, name) in own_types or QName(uri, name) in schema_types

    def own_type(name, uris):
        return next(
            (QName(uri, name) for uri in sorted(uris & own_uris) if has_type(uri, name)), None
        )

    def need_schema(check, address):
        if schema is None:
            skip(check, address, "schema_unavailable")
            return False
        return True

    def properties(name):
        typ = own_types.get(name)
        if typ:
            return {p.name.local: p.type for p in typ.properties}
        typ = schema_types.get(name)
        if typ and schema is not None:
            return {p.name.local: p.type_ref for p in schema.inherited.get(typ.id, typ.properties)}
        return {}

    for package in model.format_packages:
        address = addresses[package.logical_id]
        if len(model.format_bindings) != 1:
            skip("version_mismatch", address, "format_binding_unavailable")
        else:
            binding = model.format_bindings[0]
            if (package.base_version, package.base_namespace) != (binding.key, binding.uri):
                error(
                    "version_mismatch",
                    address,
                    "Базовые версия/URI пакета не равны привязке; XDTO:4636–4659",
                )

    for rule in model.pko:
        address = addresses[rule.logical_id]
        if rule.format_object.state == "string" and not str(rule.format_object.value).strip():
            continue
        namespaced = any(
            m.namespace
            for m in (
                *rule.properties,
                *rule.groups,
                *(x for g in rule.groups for x in g.properties),
            )
        )
        if not (own_uris or rule.extensions or namespaced):
            continue  # правило без расширений формата: контракту нечего проверять
        if rule.state != "editable":
            # Непрочитанный init (не литерал) не попадает в extensions: проверки по init молчат.
            for check in ("init_missing", "type_missing", "init_type_missing"):
                skip(check, address, "rule_not_editable")
        for uri in rule.extensions:
            if uri not in known_uris and need_schema("uri_unknown", address):
                error(
                    "uri_unknown",
                    address,
                    "URI init отсутствует в пакетах: " + uri + "; XDTO:9903–9949",
                )
        name = literal(rule.format_object, "type_missing", address)
        base = bool(name is not None and has_type(base_uri, name))
        own = own_type(name, set(rule.extensions)) if name else None
        type_uri = base_uri if base else own.namespace if own else ""
        if (
            name is not None
            and rule.state == "editable"
            # Без init и без своего типа с этим именем это вопрос версии базовой схемы
            # (многоверсионный модуль), а не пакета: тот случай показывает ed_trace.
            and (rule.extensions or own_type(name, own_uris) is not None)
            and not base
            and own is None
            and need_schema("type_missing", address)
        ):
            error(
                "type_missing",
                address,
                "Тип ПКО не найден в базе и URI init; XDTO:4146–4158,4838–4853",
            )
        if name is None:
            for check in ("init_type_missing", "pod_missing", "deletion_route_unresolved"):
                skip(check, address, "not_literal")
        elif rule.state == "editable" and rule.extensions and schema is None:
            skip("init_type_missing", address, "schema_unavailable")
        elif rule.state == "editable" and base:
            for uri in rule.extensions:
                if uri in own_uris and not has_type(uri, name):
                    error(
                        "init_type_missing",
                        address,
                        "Init не содержит одноимённый тип: " + uri + "; XDTO:4164–4165,4855–4881",
                    )

        for group in (None, *rule.groups):
            members = rule.properties if group is None else group.properties
            collisions = defaultdict(list)
            for member in ((group,) if group else ()) + members:
                member_address = addresses[member.logical_id]
                namespace = member.namespace
                if (
                    isinstance(member, Property)
                    and len(member.argument_values) > 4
                    and member.argument_values[4].state not in ("string", "unset")
                ) or (
                    not isinstance(member, Property)
                    and member.state != "editable"
                    and not namespace
                    and len(member.argument_presence) > 3
                    and member.argument_presence[3]
                ):
                    for check in ("uri_unknown", "init_missing", "name_collision"):
                        skip(check, member_address, "not_literal")
                    continue
                if namespace:
                    if namespace not in known_uris:
                        if need_schema("uri_unknown", member_address):
                            error(
                                "uri_unknown",
                                member_address,
                                "URI свойства отсутствует в пакетах: "
                                + namespace
                                + "; XDTO:9903–9949",
                            )
                        else:
                            skip("init_missing", member_address, "schema_unavailable")
                    elif (
                        namespace in own_uris
                        and namespace not in rule.extensions
                        and rule.state == "editable"
                    ):
                        error(
                            "init_missing",
                            member_address,
                            "Нет init ПКО для URI " + namespace + "; XDTO:4808–4817,9903–9949",
                        )
                if member is group:
                    continue
                if not namespace and not type_uri:
                    if not any(m.namespace for m in members):
                        continue  # на уровне нет своих URI — коллизии быть не может
                    skip(
                        "name_collision",
                        member_address,
                        "not_literal"
                        if name is None
                        else "schema_unavailable"
                        if schema is None
                        else "owner_type_unavailable",
                    )
                    continue
                if not member.format_property:
                    continue  # свойство только конфигурации: в структуру формата не попадает
                directions = property_directions(
                    rule, member, guards, group.guards if group else ()
                )
                collisions[member.format_property.casefold()].append(
                    (namespace or type_uri, directions, member_address)
                )
            for rows in collisions.values():
                for uri, directions, member_address in rows:
                    if any(
                        uri != other_uri and directions & other_directions
                        for other_uri, other_directions, _ in rows
                    ):
                        error(
                            "name_collision",
                            member_address,
                            "Локальное имя совпадает в разных URI одного уровня и направления; "
                            "XDTO:945–954",
                        )

        if (
            name is not None
            and own
            and any(d in ("receive", "both") for d in rule.directions)
            and need_schema("pod_missing", address)
        ):
            receiving = [
                p for p in model.pod if any(d in ("receive", "both") for d in p.directions)
            ]
            if not any(
                p.format_selection.state == "string" and p.format_selection.value == name
                for p in receiving
            ):
                if any(p.format_selection.state != "string" for p in receiving):
                    skip("pod_missing", address, "not_literal")
                else:
                    report.warning(
                        PREFIX + "pod_missing",
                        address,
                        "Нет ПОД получения своего типа; XDTO:7433–7437",
                    )
        if (
            name is not None
            and own
            and any(d in ("send", "both") for d in rule.directions)
            and need_schema("deletion_route_unresolved", address)
        ):
            key = properties(own).get("КлючевыеСвойства")
            if key and "Ссылка" in properties(key):
                report.warning(
                    PREFIX + "deletion_route_unresolved",
                    address,
                    "Удаление своей ссылки не маршрутизируется базовым ЛюбаяСсылка; XDTO:5488–5509",
                )

    # ПОД и ПКПД проверяются, только если свои URI вообще есть.
    for rule in model.pod if own_uris else ():
        if not any(d in ("receive", "both") for d in rule.directions):
            continue
        address = addresses[rule.logical_id]
        name = literal(rule.format_selection, "type_missing", address)
        if (
            name is not None
            and name.strip()
            and own_uris
            and need_schema("type_missing", address)
            and not has_type(base_uri, name)
            and own_type(name, own_uris) is None
        ):
            error(
                "type_missing",
                address,
                "Тип ПОД получения не найден в базе и расширениях; XDTO:3939–3950,4820–4834",
            )
    for rule in model.pkpd if own_uris else ():
        address = addresses[rule.logical_id]
        name = literal(rule.format_type, "pkpd_unsupported", address)
        if (
            name is not None
            and name.strip()
            and own_type(name, own_uris)
            and need_schema("pkpd_unsupported", address)
            and not has_type(base_uri, name)
        ):
            error(
                "pkpd_unsupported", address, "ПКПД ищет тип только в базовом пакете; XDTO:4703–4706"
            )
    report.issues = list(dict.fromkeys(report.issues))
    report.skipped = list(dict.fromkeys(report.skipped))
    return report
