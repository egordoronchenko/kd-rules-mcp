"""Baseline/delta автора ED: существующие проверки неизменны, профили разделены заранее."""

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
from types import FunctionType
from typing import Any, cast

from kd2_rules_mcp.authoring.ed.candidates import compatibility, target_objects
from kd2_rules_mcp.authoring.ed.canonical import canonicalize_operations
from kd2_rules_mcp.authoring.ed.context import AuthoringContext
from kd2_rules_mcp.authoring.ed.handlers import HandlerOperationsPlan, canonical_operations_bytes
from kd2_rules_mcp.authoring.ed.hook import generate_hook
from kd2_rules_mcp.authoring.ed.model import (
    AddHeaderProperty,
    AuthoringInputs,
    AuthoringPreconditionError,
    ExtensionIdentity,
    Failure,
    Notice,
    Operation,
    PreparedAuthoring,
    ProfileComparison,
    ProfileReport,
    ValidationDelta,
    digest,
    runtime_verified,
)
from kd2_rules_mcp.authoring.ed.operations import (
    apply_header_properties,
    copy_structure_with_attributes,
    draft_property,
    manager_for_document,
    require_single_version,
    validate_preconditions,
)
from kd2_rules_mcp.ed.address import build_addresses, escape_segment
from kd2_rules_mcp.ed.model import EdDocument, ObjectRule
from kd2_rules_mcp.ed.refs import build_references
from kd2_rules_mcp.ed.schema.model import EdSchema
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.validation.ed_links import validate_links
from kd2_rules_mcp.validation.ed_schema import validate_schema
from kd2_rules_mcp.validation.ed_structure import validate_structure
from kd2_rules_mcp.validation.ed_structure_snapshot import CheckContext, StructureSnapshot
from kd2_rules_mcp.validation.report import Issue, Skipped, ValidationReport


def prepare_handler_operations(
    inputs: AuthoringInputs,
    operations: tuple[Operation, ...],
    identity: ExtensionIdentity,
    *,
    version_scope: str | None,
) -> HandlerOperationsPlan:
    """Запуск A: канонические решения и предусловия, ещё без проекции/отрисовки B."""
    from .ed_authoring_handlers import validate_handler_operations

    if not operations:
        raise ValueError("Нужна хотя бы одна операция")
    if len(operations) > 100:
        from kd2_rules_mcp.errors import EdAuthoringResourceLimitError

        raise EdAuthoringResourceLimitError("В одном вызове допускается не более 100 операций")
    context = AuthoringContext(inputs)
    canonical = canonicalize_operations(inputs, operations, context)
    notices = validate_preconditions(
        inputs, canonical, identity, version_scope=version_scope, context=context
    )
    _, _, bindings = validate_handler_operations(inputs, canonical, identity, context)
    return HandlerOperationsPlan(
        canonical,
        bindings,
        notices,
        canonical_operations_bytes(canonical),
        runtime_verified=bool(bindings) and all(binding.runtime_verified for binding in bindings),
        dispatcher_name=identity.prefix + "Диспетчер",
        dispatcher_order=tuple(sorted(binding.handler_name for binding in bindings)),
        manager_interface=inputs.document.manager_version or 0,
    )


def _cached_checker(
    function: Callable[..., ValidationReport],
    applicable: Applicability,
    addresses: tuple[str, ...] = (),
) -> Callable[..., ValidationReport]:
    """Локальная зависимость неизменного checker, без подмены глобалов и общей памяти.

    Существующий CheckContext не принимает готовую применимость. Копия функции
    с локальным пространством имён исполняет тот же код конструктора и проверки.
    """

    class CachedApplicability:
        @staticmethod
        def build(document: EdDocument, profile: ValidationProfile) -> Applicability:
            return applicable

    original_init = cast(Any, CheckContext.__init__)
    init = FunctionType(
        original_init.__code__, {**original_init.__globals__, "Applicability": CachedApplicability}
    )

    class CachedContext(CheckContext):
        def __init__(self, document: EdDocument, index: Any, profile: ValidationProfile):
            init(self, document, index, profile)

        def finish(self) -> ValidationReport:
            if addresses:
                self.report.issues = [
                    i for i in self.report.issues if _belongs(i.address, addresses)
                ]
                # Фильтруем до группировки: полный отчёт перечисляет только первые пять адресов.
                self.skips = {
                    key: {
                        ident: address
                        for ident, address in instances.items()
                        if _belongs(address, addresses)
                    }
                    for key, instances in self.skips.items()
                    if any(_belongs(address, addresses) for address in instances.values())
                }
            return super().finish()

    original = cast(Any, function)
    result = FunctionType(
        original.__code__,
        {**original.__globals__, "CheckContext": CachedContext},
        original.__name__,
        original.__defaults__,
        original.__closure__,
    )
    result.__kwdefaults__ = original.__kwdefaults__
    return result


def _belongs(address: str, prefixes: tuple[str, ...]) -> bool:
    return any(address == p or address.startswith(p + "/") for p in prefixes)


def check_profile(
    document: EdDocument,
    schema: EdSchema,
    snapshot: StructureSnapshot,
    version: str,
    direction: str,
    *,
    context: AuthoringContext | None = None,
    links: ValidationReport | None = None,
    addresses: tuple[str, ...] = (),
) -> ProfileReport:
    """Тот же порядок трёх проверок, что у ed_validate; связность всего модуля."""
    index = context.index(document) if context else build_addresses(document)
    profile = (
        context.profile(version, direction)
        if context
        else ValidationProfile.build(schema, version, direction)
    )
    applicable = (
        context.applicable(document, version, direction)
        if context
        else Applicability.build(document, profile)
    )
    report = ValidationReport()
    link_report = (
        links if links is not None else validate_links(document, index, build_references(document))
    )
    report.extend(
        ValidationReport(issues=[i for i in link_report.issues if _belongs(i.address, addresses)])
        if addresses
        else link_report
    )
    report.extend(
        _cached_checker(validate_schema, applicable, addresses)(
            document, schema, index, profile, snapshot
        )
    )
    report.extend(
        _cached_checker(validate_structure, applicable, addresses)(
            document, snapshot, index, profile
        )
    )
    return ProfileReport(version, direction, tuple(report.issues), tuple(report.skipped))


def _skip_instances(item: Skipped) -> tuple[tuple[str, str, str], ...]:
    """Адаптер точного протокола CheckContext.finish, не regex над сообщениями."""
    head, separator, addresses = item.reason.partition("; ")
    reason, colon, count = head.rpartition(": ")
    if separator and colon and count.isdecimal():
        names = addresses.split(", ") if addresses else []
        return tuple((item.check, reason, name) for name in names) + (
            (item.check, reason, "<неперечисленные>"),
        ) * max(0, int(count) - len(names))
    return ((item.check, item.reason, ""),)


def compare_reports(
    before: ProfileReport,
    after: ProfileReport,
    *,
    logical_addresses: Mapping[str, str] | None = None,
    relevant_addresses: tuple[str, ...] = (),
    proven_type_addresses: tuple[str, ...] = (),
) -> ValidationDelta:
    """Мультимножества (profile, level, check, logical_address, message), §6.2.4."""
    logical = logical_addresses or {}

    def key(report: ProfileReport, issue: Issue) -> tuple[str, ...]:
        return (
            report.version,
            report.direction,
            str(issue.level),
            issue.check,
            logical.get(issue.address, issue.address).casefold(),
            issue.message,
        )

    old = Counter(key(before, i) for i in before.issues)
    new = Counter(key(after, i) for i in after.issues)

    def difference(report: ProfileReport, counts: Counter) -> tuple[Issue, ...]:
        result = []
        for issue in report.issues:
            ident = key(report, issue)
            if counts[ident] > 0:
                result.append(issue)
                counts[ident] -= 1
        return tuple(result)

    def skip_counts(report: ProfileReport) -> Counter:
        return Counter(
            (c, r, logical.get(a, a).casefold())
            for item in report.skipped
            for c, r, a in _skip_instances(item)
        )

    new_skips = skip_counts(after) - skip_counts(before)
    skipped = []
    for (check, reason, address), count in sorted(new_skips.items()):
        relevant = (
            not relevant_addresses
            or not address
            or address == "<неперечисленные>"
            or any(
                address == a.casefold() or address.startswith(a.casefold() + "/")
                for a in relevant_addresses
            )
        )
        # Проверка типизации самого автора доказала совместимость узким белым списком.
        # Исходный skip сохраняется в ProfileReport. Бизнес-эффект handler остаётся unknown.
        proven = (
            (
                address in {a.casefold() for a in proven_type_addresses}
                or (address == "<неперечисленные>" and count <= len(proven_type_addresses))
            )
            and check == "ed.schema.type_incompatible"
            and reason in ("non_atomic_type", "handler_may_supply")
        )
        if relevant and not proven:
            canonical = next(
                (
                    a
                    for item in after.skipped
                    for c, r, a in _skip_instances(item)
                    if c == check and r == reason and logical.get(a, a).casefold() == address
                ),
                address,
            )
            skipped.extend(Skipped(check, f"{reason}: {canonical}") for _ in range(count))
    if (before.version, before.direction) != (after.version, after.direction):
        skipped.append(Skipped("ed.author.profile_mismatch", "Baseline другого профиля"))
    return ValidationDelta(
        difference(after, new - old), difference(before, old - new), tuple(skipped)
    )


def enforce_delta(
    delta: ValidationDelta,
    operations: tuple[Operation, ...],
    document: EdDocument,
    context: AuthoringContext | None = None,
) -> None:
    """Дефект новой операции не может быть принят как старое замечание (§6.2.6)."""
    if not delta.no_new_issues:
        index = context.index(document) if context else build_addresses(document)
        failures = []
        defects = tuple((i.check, i.address, i.message) for i in delta.new) + tuple(
            (s.check, "", s.reason) for s in delta.new_relevant_skipped
        )
        for check, address, message in defects:
            sources = (
                tuple(
                    op
                    for op in operations
                    if address.casefold() == op.target.pko_address.casefold()
                    or address.casefold().startswith(op.target.pko_address.casefold() + "/")
                )
                or operations
            )
            for op in sources:
                rule = index.find(op.target.pko_address)
                source = next(f for f in document.files if f.file_id == rule.span.file_id)
                failures.append(
                    Failure(
                        "ed.author.new_issues",
                        op.target.pko_address,
                        f"Новое замечание {check}; адрес «{address}»; {message}; "
                        f"операция {op.operation_id}",
                        source.path,
                        rule.span.line_start,
                    )
                )
        raise AuthoringPreconditionError(tuple(failures))


def _other_routes(inputs: AuthoringInputs, manager: str) -> tuple[list[str], dict[str, list[str]]]:
    """Неполная карта не превращается в доказательство отсутствия других версий."""
    versions: set[str] = set()
    unknown: dict[str, list[str]] = {}
    for plan in inputs.routes.plans:
        if plan.is_ed is True and plan.status == "partial" and not plan.entries:
            key = f"<неизвестный ключ: {plan.plan_name}>"
            place = (
                f"{plan.settings_source.relative_file}:{plan.settings_source.line_start}"
                if plan.settings_source
                else plan.metadata_path
            )
            versions.add(key)
            unknown[key] = [f"Неполная карта без прочитанных ключей ({place})"]
    groups = [(p.entries, p.status, p.settings_source) for p in inputs.routes.plans]
    groups.append((inputs.routes.without_node_entries, inputs.routes.without_node_status, None))
    for entries, status, source in groups:
        relevant = [
            e
            for e in entries
            if e.state in ("effective", "conditional")
            and (e.manager_name is None or e.manager_name.casefold() == manager.casefold())
        ]
        if not relevant:
            continue
        for entry in relevant:
            versions.add(entry.key)
            reasons = []
            place = f"{entry.source.relative_file}:{entry.source.line_start}"
            if status != "complete":
                reasons.append(f"Неполная карта ({place})")
            if entry.manager_name is None:
                reasons.append(f"Менеджер не определён ({place})")
            if entry.state == "conditional" or any(c.value != "true" for c in entry.conditions):
                reasons.append(f"Условный маршрут ({place})")
            if reasons:
                fallback_file = source.relative_file if source else entry.source.relative_file
                reasons.extend(
                    f"{s.reason} "
                    f"({s.relative_file or fallback_file}"
                    f":{s.line or entry.source.line_start})"
                    for s in inputs.routes.skipped
                    if "map" in s.code
                )
                unknown.setdefault(entry.key, []).extend(reasons)
    if inputs.routes.reading.unparsed_map_operations and not unknown:
        reasons = [
            f"{s.reason} ({s.relative_file}:{s.line})"
            for s in inputs.routes.skipped
            if "map" in s.code
        ]
        unknown["<неизвестный ключ>"] = reasons or [
            "Непрочитанные операции карты; место не установлено читателем маршрутов"
        ]
        versions.add("<неизвестный ключ>")
    return sorted(versions), unknown


def prepare_authoring(
    inputs: AuthoringInputs,
    operations: tuple[AddHeaderProperty, ...],
    identity: ExtensionIdentity,
    *,
    version_scope: str | None,
    context: AuthoringContext | None = None,
    other_rules_only: bool = False,
) -> PreparedAuthoring:
    """Подготовка в памяти: входы → baseline → предусловия → BSL → after/delta.

    Сервис включает other_rules_only. Прямые потребители ядра сохраняют полный
    отчёт остальных версий и побайтовый контракт существующих комплектов B.
    """
    context = context or AuthoringContext(inputs)
    context.other_rules_only = other_rules_only
    context.selected = frozenset(
        (op.target.format_version, op.target.direction) for op in operations
    )
    require_single_version(inputs, operations, context)
    operations = canonicalize_operations(inputs, operations, context)
    if not operations:
        raise ValueError("Нужна хотя бы одна операция")
    selected = sorted({(op.target.format_version, op.target.direction) for op in operations})
    before_index = context.index(inputs.document)
    context.target_ids = frozenset(
        before_index.find(op.target.pko_address).entity_id for op in operations
    )
    before_links = validate_links(
        inputs.document, before_index, context.reference_index(inputs.document, build_references)
    )
    baselines = {}
    for version, direction in selected:
        schema = inputs.schemas.get(version)
        if isinstance(schema, EdSchema):
            baselines[(version, direction)] = check_profile(
                inputs.document,
                schema,
                inputs.structure,
                version,
                direction,
                context=context,
                links=before_links,
            )
    notices = list(
        validate_preconditions(
            inputs, operations, identity, version_scope=version_scope, context=context
        )
    )
    manager = manager_for_document(inputs)
    assert manager is not None
    hook = generate_hook(
        inputs.document,
        operations,
        identity,
        path=f"modules/CommonModules/{manager.name}/Ext/Module.bsl",
        context=context,
    )
    projection = apply_header_properties(
        inputs.document, operations, hook, schemas=inputs.schemas, context=context
    )
    snapshot = copy_structure_with_attributes(inputs, operations, context)
    after_index = context.index(projection.document)
    after_links = validate_links(
        projection.document,
        after_index,
        context.reference_index(projection.document, build_references),
    )
    logical = {
        address: before_index.by_id[ident][0]
        for ident, addresses in after_index.by_id.items()
        if ident in before_index.by_id
        for address in addresses
    }
    generated_addresses = tuple(after_index.by_id[c.entity_id][0] for c in projection.changes)
    comparisons = []
    for version, direction in selected:
        schema = inputs.schemas[version]
        assert isinstance(schema, EdSchema)
        after = check_profile(
            projection.document,
            schema,
            snapshot,
            version,
            direction,
            context=context,
            links=after_links,
        )
        relevant_ops = tuple(
            op
            for op in operations
            if (op.target.format_version, op.target.direction) == (version, direction)
        )
        delta = compare_reports(
            baselines[(version, direction)],
            after,
            logical_addresses=logical,
            relevant_addresses=tuple(op.target.pko_address for op in relevant_ops),
            proven_type_addresses=generated_addresses,
        )
        enforce_delta(delta, relevant_ops, inputs.document, context)
        comparisons.append(ProfileComparison(baselines[(version, direction)], after, delta))
    versions, route_unknown = _other_routes(inputs, manager.name)
    other = []
    unverified = dict(route_unknown)
    for version in versions:
        schema = inputs.schemas.get(version)
        if not isinstance(schema, EdSchema) or schema.status != "complete":
            unverified.setdefault(version, []).append(
                schema if isinstance(schema, str) else "Схема не прочитана полностью"
            )
        if version in unverified:
            continue
        assert isinstance(schema, EdSchema)
        for direction in sorted({op.target.direction for op in operations}):
            if (version, direction) in selected:
                continue
            addresses = (
                tuple(op.target.pko_address for op in operations) if other_rules_only else ()
            )
            before = check_profile(
                context.scope(inputs.document) if other_rules_only else inputs.document,
                schema,
                inputs.structure,
                version,
                direction,
                context=context,
                links=before_links,
                addresses=addresses,
            )
            after = check_profile(
                context.scope(projection.document) if other_rules_only else projection.document,
                schema,
                snapshot,
                version,
                direction,
                context=context,
                links=after_links,
                addresses=addresses,
            )
            other.append(
                ProfileComparison(
                    before,
                    after,
                    compare_reports(
                        before,
                        after,
                        logical_addresses=logical,
                        relevant_addresses=tuple(op.target.pko_address for op in operations),
                    ),
                )
            )
    ranges: dict[tuple[str, str], set[str]] = {}
    for notice in notices:
        if notice.id == "ed.author.value_range":
            ranges.setdefault((notice.operation_id, notice.message), set()).update(
                notice.version_keys
            )
    notices = [n for n in notices if n.id != "ed.author.value_range"]
    for op in operations:
        missing, mismatched = [], []
        unknown = {
            key: reasons
            for key, reasons in route_unknown.items()
            if key == op.target.format_version
        }
        for version in versions:
            if version == op.target.format_version:
                continue
            if version in unverified:
                unknown[version] = unverified[version]
                continue
            rule, profile, applicable, typ, owner = target_objects(
                inputs, replace(op.target, format_version=version), context
            )
            state = applicable.evaluate(rule, op.target.direction)
            if state is False:
                continue
            if state is None:
                unknown[version] = ["Применимость ПКО неизвестна"]
                continue
            if profile.owner_type(rule, op.target.direction, applicable)[1] == "missing":
                missing.append(version)
                continue
            if typ is None:
                unknown[version] = ["Тип ПКО не разрешён"]
                continue
            resolved = profile.resolve(typ, op.format_property)
            if resolved.status == "missing":
                missing.append(version)
            elif resolved.status != "resolved" or len(resolved.property_ids) != 1:
                unknown[version] = ["Свойство формата не разрешено однозначно"]
            else:
                rows = owner.property(op.configuration_attribute) if owner else ()
                attribute = (
                    draft_property(op.new_attribute)
                    if op.new_attribute
                    else rows[0]
                    if len(rows) == 1
                    else None
                )
                if attribute is None:
                    unknown[version] = ["Реквизит конфигурации не разрешён"]
                else:
                    result = compatibility(
                        profile,
                        profile.properties[resolved.property_ids[0]],
                        attribute,
                        op.target.direction,
                    )
                    if not result.compatible:
                        mismatched.append(version)
                    elif result.value_range:
                        ranges.setdefault((op.operation_id, result.value_range), set()).add(version)
        if missing or mismatched:
            messages = []
            if missing:
                effect = (
                    "значение не передаётся"
                    if op.target.direction == "send"
                    else "реквизит найденного объекта может очищаться при каждом получении"
                    if op.configuration_attribute != op.format_property
                    else "свойство отсутствует в сообщении"
                )
                messages.append(
                    f"В версиях {', '.join(missing)} свойства «{op.format_property}» "
                    f"или типа ПКО нет: {effect}"
                )
            if mismatched:
                effect = "передача" if op.target.direction == "send" else "получение"
                messages.append(
                    f"В версиях {', '.join(mismatched)} тип свойства не совпадает: "
                    f"{effect} прямой ПКС несовместима со схемой"
                )
            notices.append(
                Notice(
                    "ed.author.other_version_incompatible",
                    op.operation_id,
                    op.target.pko_address,
                    "; ".join(messages),
                    tuple(sorted(set(missing + mismatched))),
                )
            )
        if unknown:
            notices.append(
                Notice(
                    "ed.author.other_version_unverified",
                    op.operation_id,
                    op.target.pko_address,
                    "Не проверены версии: "
                    + "; ".join(
                        f"{key}: {', '.join(dict.fromkeys(reasons))}"
                        for key, reasons in sorted(unknown.items())
                    ),
                    tuple(sorted(unknown)),
                )
            )
        rule = before_index.find(op.target.pko_address)
        assert isinstance(rule, ObjectRule)
        handler_names = {e.target_name.casefold() for e in rule.events if e.target_name}
        methods = tuple(
            sorted(
                {
                    address
                    for routine in inputs.document.routines
                    if routine.name.casefold() in handler_names
                    for address in before_index.by_id.get(routine.entity_id, ())
                    if address.startswith("Обработчик/")
                }
                | {
                    "Обработчик/" + escape_segment(e.target_name)
                    for e in rule.events
                    if e.target_name
                    and not any(
                        r.name.casefold() == e.target_name.casefold()
                        and any(
                            a.startswith("Обработчик/")
                            for a in before_index.by_id.get(r.entity_id, ())
                        )
                        for r in inputs.document.routines
                    )
                }
            )
        )
        if methods:
            notices.append(
                Notice(
                    "ed.author.handler_effect_unknown",
                    op.operation_id,
                    op.target.pko_address,
                    "Влияние обработчиков на данные не проверено: " + ", ".join(methods),
                    methods=methods,
                )
            )
    for (operation_id, message), keys in sorted(ranges.items()):
        op = next(op for op in operations if op.operation_id == operation_id)
        notices.append(
            Notice(
                "ed.author.value_range",
                operation_id,
                op.target.pko_address,
                f"В версиях {', '.join(sorted(keys))}: {message}",
                tuple(sorted(keys)),
                detail_key=digest(message)[:16],
            )
        )
    skipped = tuple(
        Skipped(check, reason)
        for check, reason in (
            ("runtime.hook", "Исполнение перехватчика в новом сеансе не проверено"),
            ("runtime.security", "Безопасный режим и права не проверены"),
            ("runtime.registration", "Регистрация изменений не проверена"),
            ("runtime.node_version", "Фактическая версия узла неизвестна"),
            ("runtime.messages", "Содержимое сообщений не проверено"),
            ("runtime.handlers", "Бизнес-логика opaque обработчиков не исполнялась"),
            ("runtime.object_conversion", "Путь объектной конвертации не проверен"),
        )
    )
    current = inputs.input_fingerprints
    if current != inputs.source_set:
        raise AuthoringPreconditionError(
            (
                Failure(
                    "ed.author.snapshot_mismatch",
                    operations[0].target.pko_address,
                    "Входы изменились во время подготовки",
                ),
            )
        )
    assert current is not None
    build_hash = digest(
        (
            tuple(op.operation_id for op in operations),
            identity,
            inputs.metadata_profile,
            current,
            version_scope,
        )
    )
    return PreparedAuthoring(
        operations,
        identity,
        inputs.document,
        projection,
        snapshot,
        current,
        hook,
        tuple(comparisons),
        tuple(other),
        tuple(notices),
        skipped,
        build_hash,
        runtime_verified(inputs.document.manager_version),
        preparation_inputs=inputs,
    )
