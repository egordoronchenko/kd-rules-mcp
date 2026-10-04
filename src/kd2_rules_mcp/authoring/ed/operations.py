"""Предусловия и наложение известных решений; I/O и состояние сервиса отсутствуют."""

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from itertools import pairwise
from types import MappingProxyType
from typing import Any, cast

from lxml import etree

from kd2_rules_mcp.ed.address import (
    AmbiguousAddressError,
    EntityNotFoundError,
    build_addresses,
)
from kd2_rules_mcp.ed.forms import helper_forms
from kd2_rules_mcp.ed.lexer import normalized, tokenize
from kd2_rules_mcp.ed.model import EdDocument, Guard, ObjectRule, PropertyRule
from kd2_rules_mcp.ed.route_model import ManagerInfo
from kd2_rules_mcp.ed.schema.model import EdSchema
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.validation.ed_structure_snapshot import (
    StructureProperty,
    StructureSnapshot,
    metadata_key,
)

from .candidates import compatibility, occupied_properties, target_objects
from .canonical import canonicalize_operations
from .context import AuthoringContext
from .hook import identifier_failures, valid_identifier
from .model import (
    FILLER,
    AddHeaderProperty,
    AttributeDraft,
    AuthoringInputs,
    AuthoringPreconditionError,
    Change,
    ExtensionIdentity,
    Failure,
    GeneratedHook,
    Notice,
    Projection,
    digest,
    order_operations,
)

PRIMITIVES = {"string": "Строка", "boolean": "Булево", "number": "Число", "date": "Дата"}


def unsupported_qualifiers(draft: AttributeDraft) -> tuple[str, ...]:
    """Закрытые обязательные квалификаторы черновика, без неявных значений (§1.2)."""
    required = {
        "string": {"string_length": int},
        "number": {"number_length": int, "number_precision": int, "number_nonnegative": bool},
        "date": {"date_parts": str},
        "boolean": {},
    }.get(draft.primitive)
    if required is None:
        return ("primitive",)
    qualifiers = draft.qualifiers
    invalid = set(qualifiers) - set(required)
    for key, kind in required.items():
        if key not in qualifiers or type(qualifiers[key]) is not kind:
            invalid.add(key)
    if (
        "string_length" in qualifiers
        and type(qualifiers["string_length"]) is int
        and qualifiers["string_length"] < 0
    ):
        invalid.add("string_length")
    if draft.primitive == "number" and not invalid:
        digits, precision = qualifiers["number_length"], qualifiers["number_precision"]
        if not isinstance(digits, int) or not 1 <= digits <= 38:
            invalid.add("number_length")
        if (
            not isinstance(precision, int)
            or not isinstance(digits, int)
            or not 0 <= precision < digits
        ):
            invalid.add("number_precision")
    if draft.primitive == "date" and qualifiers.get("date_parts") not in (
        "Дата",
        "Время",
        "ДатаВремя",
        "Date",
        "Time",
        "DateTime",
    ):
        invalid.add("date_parts")
    return tuple(sorted(invalid))


def draft_property(draft: AttributeDraft, ident: int = -1) -> StructureProperty:
    return StructureProperty(
        ident,
        draft.name,
        "Реквизит",
        "",
        (PRIMITIVES.get(draft.primitive, draft.primitive),),
        (),
        draft.qualifiers,
    )


def validate_owned_content(
    expected_hashes: Mapping[str, str],
    actual_texts: Mapping[str, str],
    address: str,
) -> None:
    """Проверка переданных свидетельств владения, без manifest и файловой записи (C)."""
    if set(expected_hashes) != set(actual_texts) or any(
        hashlib.sha256(actual_texts[name].encode("utf-8")).hexdigest() != expected
        for name, expected in expected_hashes.items()
        if name in actual_texts
    ):
        raise AuthoringPreconditionError(
            (
                Failure(
                    "ed.author.owned_content_changed",
                    address,
                    "Содержимое прежнего результата изменено или содержит неизвестные файлы",
                ),
            )
        )


def extension_conflicts(
    inputs: AuthoringInputs, op: AddHeaderProperty, context: AuthoringContext | None = None
) -> tuple[tuple[str, int, str], ...]:
    """Только переданные тексты: аннотации lexer и описания новых реквизитов."""
    selected_manager = manager_for_document(inputs)
    manager = selected_manager.name if selected_manager else ""
    context = context or AuthoringContext(inputs)
    rule = context.index(inputs.document).find(op.target.pko_address)
    assert isinstance(rule, ObjectRule)
    key, _ = metadata_key(rule.configuration_object.value)
    result = []
    for file, text in sorted(inputs.extension_sources.items()):
        if file.casefold().endswith(".bsl"):
            # Процедуры с такими именами в другом общем модуле независимы.
            if manager.casefold() not in _path(file).split("/"):
                continue
            for token in tokenize(text):
                if token.kind != "directive" or not token.value.startswith("&"):
                    continue
                annotation = tokenize(token.value[1:])
                intercepts = (
                    "перед",
                    "после",
                    "вместо",
                    "изменениеиконтроль",
                    "before",
                    "after",
                    "around",
                    "changeandvalidate",
                )
                if not annotation or annotation[0].folded not in intercepts:
                    continue
                if (
                    len(annotation) != 4
                    or annotation[1].value != "("
                    or annotation[2].kind != "string"
                    or annotation[3].value != ")"
                ) or not valid_identifier(annotation[2].value.strip(" ")):
                    result.append(
                        (
                            file,
                            text.count("\n", 0, token.start) + 1,
                            "Неизвестная аннотация менеджера",
                        )
                    )
                elif annotation[2].value.strip(" ").casefold() in (
                    FILLER.casefold(),
                    "добавитьпкс",
                    rule.procedure_name.casefold(),
                ):
                    result.append((file, text.count("\n", 0, token.start) + 1, annotation[2].value))
        elif file.casefold().endswith(".xml") and key:
            parser = etree.XMLParser(
                resolve_entities=False, no_network=True, load_dtd=False, encoding="utf-8"
            )
            try:
                root = etree.fromstring(text.encode("utf-8"), parser)
                # В lxml-stubs атрибут doctype отсутствует; сам lxml его предоставляет.
                if cast(Any, root.getroottree().docinfo).doctype:
                    result.append((file, 1, "DTD и сущности в описании расширения запрещены"))
                    continue
            except (etree.XMLSyntaxError, ValueError) as error:
                line = error.position[0] if isinstance(error, etree.XMLSyntaxError) else 1
                result.append((file, line, "Описание расширения не является корректным XML"))
                continue
            for owner in cast(
                list[etree._Element],
                root.xpath("//*[local-name()='Catalog' or local-name()='Document']"),
            ):
                names = cast(
                    list[str],
                    owner.xpath("./*[local-name()='Properties']/*[local-name()='Name']/text()"),
                )
                kind = "справочник" if etree.QName(owner).localname == "Catalog" else "документ"
                if not names or (kind, names[0].casefold()) != key:
                    continue
                for attr in cast(
                    list[etree._Element],
                    owner.xpath("./*[local-name()='ChildObjects']/*[local-name()='Attribute']"),
                ):
                    names = cast(
                        list[str],
                        attr.xpath("./*[local-name()='Properties']/*[local-name()='Name']/text()"),
                    )
                    if names and names[0].casefold() == op.configuration_attribute.casefold():
                        result.append((file, attr.sourceline or 1, names[0]))
    return tuple(result)


def _path(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()


def manager_for_document(inputs: AuthoringInputs) -> ManagerInfo | None:
    """RouteProfile хранит путь относительно root; сравнение не обращается к диску."""
    source = _path(inputs.document.files[0].path)
    found = [
        m
        for m in inputs.routes.managers
        if m.path
        and source
        in (
            _path(m.path),
            _path(inputs.routes.root) + "/" + _path(m.path),
        )
    ]
    return found[0] if len(found) == 1 else None


def _target_partial(document: EdDocument, rule: ObjectRule) -> bool:
    """Неизвестные воздействия локализуются по владельцу и достижимым вызовам."""
    names = {FILLER.casefold(), "добавитьпкс", rule.procedure_name.casefold()}
    routines = {r.name.casefold(): r for r in document.routines}
    foreign_collections: dict[str, set[str]] = {}
    pending = list(names)
    while pending:
        routine = routines.get(pending.pop())
        if routine is None:
            continue
        tokens = tokenize(routine.raw_text)
        for first, second in pairwise(tokens):
            if (
                first.kind == "identifier"
                and second.value == "("
                and first.folded in routines
                and first.folded not in names
            ):
                # Локальные свойства чужого ПКО независимы, общая таблица ПКО — нет.
                if any(
                    r.procedure_name.casefold() == first.folded and r.entity_id != rule.entity_id
                    for r in document.pko
                ):
                    foreign = routines[first.folded]
                    aliases = {
                        foreign.parameters[0].name.casefold()
                        if foreign.parameters and foreign.parameters[0].name
                        else "правилаконвертации"
                    }
                    foreign_tokens = tokenize(foreign.raw_text)
                    changed = True
                    while changed:
                        changed = False
                        for left, assign, right in zip(
                            foreign_tokens, foreign_tokens[1:], foreign_tokens[2:], strict=False
                        ):
                            if (
                                left.kind == right.kind == "identifier"
                                and assign.value == "="
                                and right.folded in aliases
                                and left.folded not in aliases
                            ):
                                aliases.add(left.folded)
                                changed = True
                    foreign_collections[first.folded] = aliases
                    continue
                names.add(first.folded)
                pending.append(first.folded)
    ids = {
        rule.entity_id,
        *(r.entity_id for name, r in routines.items() if name in names),
        *(p.entity_id for p in rule.properties),
        *(g.entity_id for g in rule.groups),
    }
    for item in (*document.unknown, *document.diagnostics):
        hosts = {
            name
            for name, routine in routines.items()
            if routine.span.file_id == item.span.file_id
            and routine.span.char_start <= item.span.char_start < routine.span.char_end
        }
        if item.owner_id in ids or hosts & names or (item.owner_id is None and not hosts):
            return True
        if any(
            token.kind == "identifier" and token.folded in foreign_collections.get(host, set())
            for host in hosts
            for token in tokenize(item.raw_text)
        ):
            return True
    return False


def validate_preconditions(
    inputs: AuthoringInputs,
    operations: tuple[AddHeaderProperty, ...],
    identity: ExtensionIdentity,
    *,
    version_scope: str | None,
    context: AuthoringContext | None = None,
) -> tuple[Notice, ...]:
    """Каждый отказ §2.3 имеет отдельный id; ошибки возвращаются вместе."""
    failures: list[Failure] = []
    notices = []
    doc = inputs.document
    context = context or AuthoringContext(inputs)
    require_single_version(inputs, operations, context)
    operations = canonicalize_operations(inputs, operations, context)
    sources = inputs.input_fingerprints
    index = context.index(doc)
    staged_format: set[tuple[str, str, str]] = set()
    staged_config: set[tuple[str, str, str]] = set()
    declarations: dict[tuple[tuple[str, str], str], AttributeDraft] = {}
    for op in order_operations(operations):
        target = op.target
        rule: ObjectRule | None = None
        rule_box: list[ObjectRule | None] = [None]

        def fail(
            check: str,
            message: str,
            file: str | None = None,
            line: int | None = None,
            *,
            _address: str = target.pko_address,
            _rule_box: list[ObjectRule | None] = rule_box,
        ) -> None:
            current_rule = _rule_box[0]
            source = next(
                (f for f in doc.files if current_rule and f.file_id == current_rule.span.file_id),
                doc.files[0],
            )
            failures.append(
                Failure(
                    "ed.author." + check,
                    _address,
                    message,
                    file if file is not None else source.path,
                    line
                    if line is not None
                    else current_rule.span.line_start
                    if current_rule
                    else 1,
                )
            )

        if version_scope != "manager":
            fail("scope_required", "Требуется явная область действия version_scope=manager")
        if (
            sources != inputs.source_set
            or (
                inputs.source_set.project is not None
                and target.project.casefold() != inputs.source_set.project.casefold()
            )
            or (
                inputs.source_set.configuration is not None
                and target.configuration.casefold() != inputs.source_set.configuration.casefold()
            )
        ):
            fail(
                "snapshot_mismatch",
                "Снимки ED, схемы, структуры и маршрутов не совпадают с SourceSet",
            )
        plans = [p for p in inputs.routes.plans if p.plan_name.casefold() == target.plan.casefold()]
        entries = [
            e
            for p in plans
            for e in p.entries
            if e.key == target.format_version and e.state in ("effective", "conditional")
        ]
        managers = [
            m
            for m in inputs.routes.managers
            if entries
            and entries[0].manager_name
            and m.name.casefold() == entries[0].manager_name.casefold()
        ]
        route_ok = (
            len(plans) == 1
            and plans[0].is_ed is True
            and plans[0].status == "complete"
            and len(entries) == 1
            and entries[0].state == "effective"
            and all(c.value == "true" for c in entries[0].conditions)
            and len(managers) == 1
            and managers[0].path is not None
            and managers[0] == manager_for_document(inputs)
            and managers[0].metadata_exists
            and managers[0].source_exists
        )
        if inputs.routes.reading.unparsed_map_operations:
            map_skips = [s for s in inputs.routes.skipped if "map" in s.code]
            selected_sources = {
                p.settings_source.relative_file.casefold() for p in plans if p.settings_source
            }
            located_partial = (
                any(p.status == "partial" for p in inputs.routes.plans)
                or inputs.routes.without_node_status == "partial"
            )
            if any(
                s.relative_file.casefold() in selected_sources for s in map_skips if s.relative_file
            ) or (
                not located_partial
                and (not map_skips or any(not s.relative_file for s in map_skips))
            ):
                route_ok = False
        if target.variant is not None:
            variants = [v for p in plans for v in p.variants if v.id == target.variant]
            route_ok &= len(variants) == 1 and all(
                c.value == "true" for v in variants for c in (*v.conditions, *v.metadata_predicates)
            )
        if not route_ok:
            fail("route_unresolved", "Маршрут ED не разрешён однозначно на прочитанный менеджер")
        if managers and managers[0].interface_version != doc.manager_version:
            fail(
                "snapshot_mismatch",
                "Версия интерфейса маршрута не совпадает с прочитанным документом",
            )
        try:
            found = index.find(target.pko_address)
            if not isinstance(found, ObjectRule):
                raise EntityNotFoundError(target.pko_address)
            rule = found
            rule_box[0] = rule
        except EntityNotFoundError:
            fail("pko_missing", f"ПКО «{target.pko_address}» отсутствует")
            continue
        except AmbiguousAddressError:
            fail("pko_ambiguous", f"Поиск «{target.pko_address.removeprefix('ПКО/')}» неоднозначен")
            continue
        profile = context.profile(target.format_version, target.direction)
        applicable = context.applicable(doc, target.format_version, target.direction)
        active = [
            r
            for r in doc.pko
            if (r.declared_name or r.name).casefold()
            == (rule.declared_name or rule.name).casefold()
            and applicable.evaluate(r, target.direction) is not False
        ]
        if len(active) > 1:
            fail("pko_ambiguous", f"Поиск «{rule.declared_name or rule.name}» неоднозначен")
        state = applicable.evaluate(rule, target.direction)
        if state is False:
            fail("pko_inactive", "ПКО не применим в выбранном направлении и версии")
        elif state is None or any(
            applicable.field(f, target.direction) is not True
            for f in (rule.configuration_object, rule.format_object, rule.group_flag)
        ):
            fail("applicability_unknown", "Применимость ПКО, его полей или новой ПКС не доказана")
        typ, status = profile.owner_type(rule, target.direction, applicable)
        if status == "missing":
            fail(
                "pko_removed",
                f"Тип «{rule.format_object.value}» отсутствует; исполнитель удалит ПКО",
            )
        elif status != "resolved":
            fail("applicability_unknown", "Применимость ПКО, его полей или новой ПКС не доказана")
        key, _ = metadata_key(rule.configuration_object.value)
        owner = inputs.structure.objects.get(key) if key else None
        if (
            owner is None
            or owner.kind not in ("Справочник", "Документ")
            or not valid_identifier(owner.name)
        ):
            fail("header_ineffective", "Владелец не обычный объект с применимой шапкой")
        attr_rows = owner.property(op.configuration_attribute) if owner else ()
        if op.new_attribute:
            bad_qualifiers = unsupported_qualifiers(op.new_attribute)
            for qualifier in bad_qualifiers:
                fail(
                    "metadata_profile_unsupported",
                    f"Квалификатор «{qualifier}» нового реквизита не поддержан, "
                    "отсутствует или имеет неверный тип",
                )
            if attr_rows:
                fail(
                    "configuration_attribute_occupied",
                    f"Реквизит «{op.configuration_attribute}» уже занят",
                )
            attr = draft_property(op.new_attribute) if not bad_qualifiers else None
            declaration = (key, op.configuration_attribute.casefold()) if key else None
            if (
                declaration
                and declaration in declarations
                and declarations[declaration] != op.new_attribute
            ):
                fail(
                    "configuration_attribute_occupied",
                    f"Реквизит «{op.configuration_attribute}» уже занят",
                )
            if declaration:
                declarations[declaration] = op.new_attribute
        elif len(attr_rows) != 1 or attr_rows[0].kind != "Реквизит" or attr_rows[0].parent_kind:
            fail(
                "configuration_attribute_missing",
                f"Обычный записываемый реквизит «{op.configuration_attribute}» отсутствует",
            )
            attr = None
        else:
            attr = attr_rows[0]
        if (
            attr
            and rule.group_flag.value
            and str(attr.qualifiers.get("usage", "ForItem")).casefold()
            in ("foritem", "дляэлемента")
        ):
            fail("header_ineffective", "Владелец не обычный объект с применимой шапкой")
        metadata = inputs.metadata_profile
        if (
            metadata.dump_version != "2.20"
            or metadata.run_mode != "ManagedApplication"
            or metadata.script_variant != "Russian"
            or metadata.use_purposes != ("PlatformApplication",)
            or (
                op.new_attribute
                and (
                    op.new_attribute.primitive not in PRIMITIVES
                    or owner is None
                    or owner.kind not in ("Справочник", "Документ")
                )
            )
        ):
            fail(
                "metadata_profile_unsupported",
                "Профиль метаданных или нового реквизита не поддержан",
            )
        if _target_partial(doc, rule):
            fail("target_partial", "Unknown влияет на целевой ПКО, helper или путь заполнения")
        helpers = [r for r in doc.routines if r.name.casefold() == "добавитьпкс"]
        expected = [
            normalized(tokenize(text)) for text in helper_forms("ДобавитьПКС", doc.manager_version)
        ]
        if (
            len(helpers) != 1
            or normalized(tuple(t for t in tokenize(helpers[0].raw_text) if t.kind != "comment"))
            not in expected
        ):
            fail("helper_unverified", "Семантика ДобавитьПКС не подтверждена эталоном")
        fillers = [r for r in doc.routines if r.name.casefold() == FILLER.casefold()]
        names = (
            ("КомпонентыОбмена", "ПравилаКонвертации", "ТолькоЗаголовки")
            if doc.manager_version == 3
            else ("НаправлениеОбмена", "ПравилаКонвертации")
        )
        if (
            doc.manager_version not in (1, 2, 3)
            or len(fillers) != 1
            or fillers[0].routine_kind != "procedure"
            or tuple(p.name.casefold() if p.name else "" for p in fillers[0].parameters)
            != tuple(n.casefold() for n in names)
            or any(p.by_value for p in fillers[0].parameters)
        ):
            fail("manager_signature", "Сигнатура заполнителя не соответствует интерфейсу 1/2/3")
        for file, line, reason in extension_conflicts(inputs, op, context):
            fail("extension_conflict", f"Чужое расширение влияет на операцию: {reason}", file, line)
            if reason.casefold() == "добавитьпкс":
                fail(
                    "helper_unverified",
                    "Семантика ДобавитьПКС не подтверждена эталоном",
                    file,
                    line,
                )
        if typ is None or status != "resolved":
            continue
        resolved = profile.resolve(typ, op.format_property)
        if resolved.status != "resolved" or len(resolved.property_ids) != 1:
            fail(
                "schema_property_missing",
                f"Свойство формата «{op.format_property}» не разрешено однозначно",
            )
            continue
        ids, attributes, conditional = occupied_properties(
            rule, profile, applicable, typ, target.direction
        )
        if conditional:
            fail("applicability_unknown", "Применимость ПКО, его полей или новой ПКС не доказана")
        prop_id = resolved.property_ids[0]
        # Перехватчик общий для версий: package-specific id не разделяет его решения.
        physical_key = ".".join(q.local.casefold() for q in resolved.physical_paths[0])
        format_key = (rule.entity_id, target.direction, physical_key)
        config_key = (rule.entity_id, target.direction, op.configuration_attribute.casefold())
        if prop_id in ids or format_key in staged_format:
            fail("format_property_occupied", f"Свойство формата «{op.format_property}» уже занято")
        if op.configuration_attribute.casefold() in attributes or config_key in staged_config:
            fail(
                "configuration_attribute_occupied",
                f"Реквизит «{op.configuration_attribute}» уже занят",
            )
        staged_format.add(format_key)
        staged_config.add(config_key)
        if attr:
            result = compatibility(profile, profile.properties[prop_id], attr, target.direction)
            if not result.compatible:
                fail(result.refusal, result.reason)
            elif result.value_range:
                notices.append(
                    Notice(
                        "ed.author.value_range",
                        op.operation_id,
                        target.pko_address,
                        result.value_range,
                        (target.format_version,),
                        detail_key=digest(result.value_range)[:16],
                    )
                )
        if target.direction == "receive" and op.configuration_attribute != op.format_property:
            reason = (
                "Для полного пути с точкой исполнитель не отмечает отсутствие свойства. "
                if "." in op.format_property
                else "Защита сравнивает имена разных сторон. "
            )
            notices.append(
                Notice(
                    "ed.author.missing_value_clears",
                    op.operation_id,
                    target.pko_address,
                    f"ПКС «{op.configuration_attribute} ← {op.format_property}»: "
                    f"в обычном пути получения отсутствие «{op.format_property}» "
                    f"в сообщении может очистить «{op.configuration_attribute}» "
                    f"найденного объекта. {reason}"
                    "Сохранение прежнего значения не обеспечено",
                )
            )
    failures.extend(identifier_failures(doc, operations, identity))
    if failures:
        raise AuthoringPreconditionError(tuple(dict.fromkeys(failures)))
    return tuple(notices)


def require_single_version(
    inputs: AuthoringInputs, operations: tuple[AddHeaderProperty, ...], context: AuthoringContext
) -> None:
    """Один baseline менеджера, независимо от дальнейшей разрешимости полей."""
    if len({op.target.format_version for op in operations}) <= 1:
        return
    index = context.index(inputs.document)
    failures = []
    for op in operations:
        try:
            rule = index.find(op.target.pko_address)
            address = index.by_id[rule.entity_id][0]
            source = next(f for f in inputs.document.files if f.file_id == rule.span.file_id)
            file, line = source.path, rule.span.line_start
        except (EntityNotFoundError, AmbiguousAddressError):
            address, file, line = op.target.pko_address, inputs.document.files[0].path, 1
        failures.append(
            Failure(
                "ed.author.scope_required",
                address,
                "операции одного менеджера проверяются по одной выбранной версии; "
                "остальные версии показываются как другие",
                file,
                line,
            )
        )
    raise AuthoringPreconditionError(tuple(dict.fromkeys(failures)))


def copy_structure_with_attributes(
    inputs: AuthoringInputs,
    operations: tuple[AddHeaderProperty, ...],
    context: AuthoringContext | None = None,
) -> StructureSnapshot:
    """Детерминированные отрицательные id; исходный DTO и SQLite не изменяются (§6.1)."""
    objects = dict(inputs.structure.objects)
    minimum = min(
        (
            ident
            for o in objects.values()
            for ident in (o.id, *(p.id for rows in o.properties.values() for p in rows))
        ),
        default=0,
    )
    ident = min(minimum, 0) - 1
    drafts = {}
    for op in operations:
        if op.new_attribute:
            rule, *_ = target_objects(inputs, op.target, context)
            key, _ = metadata_key(rule.configuration_object.value)
            assert key is not None
            drafts[(key, op.new_attribute.name.casefold())] = op.new_attribute
    for (key, _), draft in sorted(drafts.items()):
        owner = objects[key]
        props = dict(owner.properties)
        if owner.property(draft.name):
            raise AuthoringPreconditionError(
                (
                    Failure(
                        "ed.author.configuration_attribute_occupied",
                        "",
                        f"Реквизит «{draft.name}» уже занят",
                    ),
                )
            )
        props[(draft.name.casefold(), "")] = (draft_property(draft, ident),)
        ident -= 1
        objects[key] = replace(owner, properties=MappingProxyType(props))
    return StructureSnapshot(
        MappingProxyType(objects),
        MappingProxyType({o.type_name.casefold(): o for o in objects.values()}),
    )


def apply_header_properties(
    base: EdDocument,
    operations: tuple[AddHeaderProperty, ...],
    generated_sources: GeneratedHook,
    *,
    schemas: Mapping[str, EdSchema | str],
    headers_only: bool = False,
    context: AuthoringContext | None = None,
) -> Projection:
    """Сохраняет чтение base; новые диапазоны относятся только к реальному BSL (§6.1)."""
    if headers_only and base.manager_version == 3:
        return Projection(base, (), str(base.parse_status), 0, True)

    def decision(op: AddHeaderProperty) -> tuple:
        return (
            op.target.format_version,
            op.target.direction,
            op.target.pko_address.casefold(),
            op.configuration_attribute.casefold(),
            op.format_property.strip(" ").casefold(),
            digest(op.new_attribute),
        )

    if generated_sources.operations:
        if {decision(op) for op in operations} != {
            decision(op) for op in generated_sources.operations
        }:
            raise ValueError("Решения проекции не совпадают с порождённым перехватчиком")
        operations = generated_sources.operations
    index = context.index(base) if context else build_addresses(base)
    profiles = {}
    applications = {}
    rules = {rule.entity_id: rule for rule in base.pko}
    occupied = {}
    guards = list(base.guards)
    changes = []
    for op in order_operations(operations):
        found = index.find(op.target.pko_address)
        assert isinstance(found, ObjectRule)
        rule = rules[found.entity_id]
        schema = schemas[op.target.format_version]
        assert isinstance(schema, EdSchema)
        key = (op.target.format_version, op.target.direction)
        if key not in profiles:
            profiles[key] = (
                context.profile(*key) if context else ValidationProfile.build(schema, *key)
            )
            applications[key] = (
                context.applicable(base, *key)
                if context
                else Applicability.build(base, profiles[key])
            )
        profile, applicable = profiles[key], applications[key]
        typ, status = profile.owner_type(rule, op.target.direction, applicable)
        assert typ is not None and status == "resolved"
        resolved = profile.resolve(typ, op.format_property)
        occupied_key = (found.entity_id, *key)
        if occupied_key not in occupied:
            occupied[occupied_key] = occupied_properties(
                found, profile, applicable, typ, op.target.direction
            )
        ids, attrs, unknown = occupied[occupied_key]
        if (
            resolved.status != "resolved"
            or unknown
            or any(p in ids for p in resolved.property_ids)
            or op.configuration_attribute.casefold() in attrs
        ):
            raise AuthoringPreconditionError(
                (
                    Failure(
                        "ed.author.format_property_occupied",
                        op.target.pko_address,
                        "Проекция столкнулась с занятой или неопределённой ПКС",
                    ),
                )
            )
        ids.update(resolved.property_ids)
        attrs.add(op.configuration_attribute.casefold())
        span = generated_sources.calls[op.operation_id]
        source = generated_sources.source
        guard_id = "generated-direction-" + op.operation_id
        direction_name = "Отправка" if op.target.direction == "send" else "Получение"
        direction_span = generated_sources.direction_spans[op.target.direction]
        guard = Guard(
            entity_id=guard_id,
            kind="guard",
            name=guard_id,
            span=direction_span,
            raw_text=source.text[direction_span.char_start : direction_span.char_end],
            # Нормализация известного предиката для текущего Applicability; raw/span — BSL.
            expression_raw=f'НаправлениеОбмена = "{direction_name}"',
            branch="if",
            known_direction=op.target.direction,
            guard_kind="direction",
        )
        prop = PropertyRule(
            entity_id="generated-pks-" + op.operation_id,
            kind="pks",
            name=op.format_property,
            span=span,
            raw_text=source.text[span.char_start : span.char_end],
            owner_id=rule.entity_id,
            group_id=None,
            configuration_property=op.configuration_attribute,
            format_property=op.format_property,
            guards=(guard_id,),
            argument_presence=(True, True, True),
            raw_arguments=generated_sources.arguments[op.operation_id],
        )
        updated = replace(rule, properties=(*rule.properties, prop))
        rules[rule.entity_id] = updated
        guards.append(guard)
        changes.append(
            Change(op.operation_id, rule.entity_id, prop.entity_id, ("properties",), span)
        )
    document = replace(base, pko=tuple(rules[r.entity_id] for r in base.pko), guards=tuple(guards))
    if changes:
        if any(f.file_id == generated_sources.source.file_id for f in base.files):
            raise ValueError("Идентификатор порождённого файла уже занят")
        document = replace(document, files=(*base.files, generated_sources.source))
    return Projection(document, tuple(changes), str(base.parse_status), len(changes), headers_only)
