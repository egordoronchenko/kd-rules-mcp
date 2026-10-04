"""Проверки статического слоя ED и адаптер к принятым проверкам документа.

Условия §4 проверяются по ревизиям, областям и цепочкам A1, а не по повторному
разбору BSL. XDTO:3618 (карты), 3719–3731 (вызов обработчика), 4147–4155
(расширения формата). Неизвестная зависимость всегда получает skipped.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from itertools import combinations

from kd2_rules_mcp.ed.address import build_addresses, escape_segment
from kd2_rules_mcp.ed.layer_address import LayerAddressIndex, build_layer_addresses
from kd2_rules_mcp.ed.layer_model import (
    Certainty,
    EffectiveContext,
    EntityState,
    EntityVersion,
    Hook,
    HookKind,
    LayeredManager,
    OperationKind,
    Origin,
    Pred,
)
from kd2_rules_mcp.ed.layers import holds
from kd2_rules_mcp.ed.model import (
    ObjectRule,
    PredefinedRule,
    ProcessingRule,
    PropertyGroup,
    PropertyRule,
)
from kd2_rules_mcp.ed.refs import build_references, norm_name
from kd2_rules_mcp.ed.route_model import RouteProfile
from kd2_rules_mcp.ed.schema.model import EdSchema
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
from kd2_rules_mcp.validation.report import Issue, Level, Skipped, ValidationReport

from .ed_projection import effective_document as effective_document
from .ed_projection import event_direction
from .ed_projection import project_document as project_document
from .ed_projection import select_context as select_context

LAYER_CHECKS = (
    "ed.layer.route.single_version",
    "ed.layer.route.context_mismatch",
    "ed.layer.handler.unreachable",
    "ed.layer.target.missing",
    "ed.layer.rule.conflict",
    "ed.layer.format.undeclared",
    "ed.layer.rule.duplicate",
    "ed.layer.hook.signature",
    "ed.layer.hook.target_missing",
    "ed.layer.dispatcher.suppressed",
    "ed.layer.hook.control_conflict",
)
_PREFIX = {"pko": "ПКО", "pod": "ПОД", "pkpd": "ПКПД", "parameters": "Параметр"}
_PROCEDURE = "выполнитьпроцедурумодуляменеджера"
_FUNCTION = "выполнитьфункциюмодуляменеджера"
_UNIT = "\x1f"
_LINK_CHECKS = (
    "ed.handler.missing",
    "ed.handler.ambiguous",
    "ed.reference.property_rule_missing",
    "ed.extension.uninitialized",
    "ed.extension.unused",
    "ed.group.empty_property",
    "ed.algorithm.handler_missing",
    "ed.identity.uid_without_pod",
    "ed.reference.pod_pko_missing",
    "ed.reference.code_rule_missing",
    "ed.reference.pod_usage_key_missing",
    "ed.rule.duplicate",
)
_SCHEMA_CHECKS = (
    "ed.schema.type_missing",
    "ed.schema.table_missing",
    "ed.schema.property_missing",
    "ed.schema.pko_unavailable",
    "ed.schema.type_incompatible",
    "ed.schema.required_source",
    "ed.schema.search_source",
    "ed.schema.pkpd_type_missing",
    "ed.schema.pkpd_value_missing",
)
_STRUCTURE_CHECKS = (
    "ed.structure.object_missing",
    "ed.structure.property_missing",
    "ed.structure.search_missing",
    "ed.structure.pkpd_type_missing",
    "ed.structure.pkpd_value_missing",
)


class _LayerReport(ValidationReport):
    # problem, logical_id, контекст, имя/URI — ключ §4, независимый от текста.
    __slots__ = ("direction", "explained", "index")

    def __init__(self) -> None:
        super().__init__()
        self.explained: set[tuple[str, str, tuple[str, bool, str | None], str, str]] = set()
        self.index: LayerAddressIndex | None = None
        self.direction: str | None = None


def _key(check: str, version: EntityVersion, context: EffectiveContext, name: str, event: str = ""):
    return (
        check,
        version.logical_id,
        (context.direction, context.headers_only, context.version_key),
        name,
        event,
    )


def _address(version: EntityVersion, *, layer: str | None = None) -> str:
    name = version.payload.name if version.payload else version.logical_id
    prefix = f"Слой/{escape_segment(layer or version.layer_id)}" if layer else "Действующее"
    return f"{prefix}/{_PREFIX.get(version.collection, version.collection)}/{escape_segment(name)}"


def _place(origin: Origin) -> str:
    return f"{origin.path or origin.file_id}:{origin.span.line_start}"


def _hook_address(hook: Hook) -> str:
    return (
        f"Слой/{escape_segment(hook.origin.layer_id)}/Перехват/{escape_segment(hook.routine.name)}"
    )


def _skip(report: ValidationReport, check: str, address: str, reason: str) -> None:
    item = Skipped(check, f"{address}: {reason}")
    if item not in report.skipped:
        report.skipped.append(item)


def _issue(
    report: ValidationReport,
    check: str,
    address: str,
    message: str,
    origin: Origin,
    *,
    error: bool = False,
    key: tuple[str, str, tuple[str, bool, str | None], str, str] | None = None,
    entity_id: str | None = None,
) -> None:
    if isinstance(report, _LayerReport) and report.index is not None:
        prefix = "/".join(address.split("/")[:2]) + "/"
        candidates = [
            hit.address
            for hit in report.index.by_address.values()
            if hit.address.startswith(prefix)
            and (
                not hit.version
                or not hit.version.direction
                or hit.version.direction == report.direction
            )
            and (
                (
                    entity_id is not None
                    and hit.entity is not None
                    and hit.entity.entity_id == entity_id
                )
                or (
                    hit.address in report.index.conflicts.get(address.casefold(), ())
                    and (
                        (hit.hook and hit.hook.origin == origin)
                        or (hit.version and origin in hit.version.origins)
                    )
                )
            )
        ]
        if len(candidates) == 1:
            address = candidates[0]
    item = Issue(
        Level.ERROR if error else Level.WARNING, check, address, f"{message}; {_place(origin)}"
    )
    if item not in report.issues:
        report.issues.append(item)
    if key is not None and isinstance(report, _LayerReport):
        report.explained.add(key)


def _unknown(context: EffectiveContext, collection: str) -> bool:
    return (
        "manager" in context.taints
        or collection in context.taints
        or any(
            version.collection == collection and version.certainty != Certainty.KNOWN
            for version in context.entities
        )
    )


def _hook_checks(layered: LayeredManager, report: ValidationReport) -> None:
    groups: dict[tuple[str, str], list[Hook]] = defaultdict(list)
    for hook in layered.hooks:
        address = _hook_address(hook)
        target = next(
            (
                routine
                for routine in (layered.source_document or layered.base).routines
                if hook.target_origin
                and routine.span.file_id == hook.target_origin.span.file_id
                and routine.name.casefold() == hook.target_name.casefold()
            ),
            None,
        )
        if target is None and hook.target_origin is not None:
            # Перехват маршрута заимствует свой модуль, а не выбранный менеджер.
            target = next(
                (
                    routine
                    for reading in layered.readings
                    for routine in reading.routines
                    if routine.span.file_id == hook.target_origin.span.file_id
                    and routine.name.casefold() == hook.target_name.casefold()
                ),
                None,
            )
        if hook.target_origin is None:
            missing = any(
                skip.reason == "missing_target" and skip.origin.hook_id == hook.id
                for skip in layered.skipped
            )
            if missing:
                _issue(
                    report,
                    "ed.layer.hook.target_missing",
                    address,
                    f"Цель перехвата {hook.target_name} отсутствует в {hook.origin.metadata_name}",
                    hook.origin,
                    error=True,
                )
            else:
                _skip(
                    report,
                    "ed.layer.hook.target_missing",
                    address,
                    "Полнота целевого модуля не доказана",
                )
        elif target is not None:
            differences = []
            if target.routine_kind != hook.routine.routine_kind:
                differences.append("вид метода")
            if len(target.parameters) != len(hook.routine.parameters):
                differences.append("число параметров")
            elif any(
                a.by_value != b.by_value
                for a, b in zip(target.parameters, hook.routine.parameters, strict=True)
            ):
                differences.append("позиции Знач")
            if differences:
                _issue(
                    report,
                    "ed.layer.hook.signature",
                    address,
                    f"Сигнатура перехвата {hook.routine.name} не соответствует "
                    f"{hook.target_name}: {', '.join(differences)}",
                    hook.origin,
                    error=True,
                )
        elif any(
            skip.reason == "manager_signature" and skip.origin.hook_id == hook.id
            for skip in layered.skipped
        ):
            _issue(
                report,
                "ed.layer.hook.signature",
                address,
                f"Сигнатура перехвата {hook.routine.name} не соответствует {hook.target_name}",
                hook.origin,
                error=True,
            )
        elif hook.applicability != "known":
            _skip(report, "ed.layer.hook.signature", address, "Сигнатура цели неизвестна")
        if (
            hook.kind == HookKind.CHANGE_CONTROL
            and hook.target_origin is not None
            and hook.applicability != "invalid"
        ):
            groups[(hook.target_origin.span.file_id, hook.target_name.casefold())].append(hook)
    for hooks in groups.values():
        for first, second in combinations(hooks, 2):
            if first.origin.layer_id == second.origin.layer_id:
                continue
            _issue(
                report,
                "ed.layer.hook.control_conflict",
                _hook_address(second),
                f"Для {second.target_name} заданы несовместимые перехваты ИзменениеИКонтроль: "
                f"{first.origin.layer_id}, {second.origin.layer_id}",
                second.origin,
                error=True,
            )


def _route_checks(
    layered: LayeredManager, routes: RouteProfile | None, report: ValidationReport
) -> None:
    changes = [entry for entry in layered.map_entries if entry.origin.layer_id != "base"]
    route_hooks = [
        hook
        for hook in layered.hooks
        if hook.target_name.casefold()
        in {"приполучениинастроек", "приполучениидоступныхверсийформата"}
    ]
    if routes is None:
        if changes or route_hooks:
            for check in LAYER_CHECKS[:2]:
                _skip(report, check, "Маршруты", "Профиль маршрутов не передан")
        return
    for hook in route_hooks:
        incomplete = (
            routes.without_node_status != "complete"
            if hook.target_name.casefold() == "приполучениидоступныхверсийформата"
            else any(
                plan.plan_name.casefold() == hook.origin.metadata_name.casefold()
                and plan.status != "complete"
                for plan in routes.plans
            )
        )
        if (
            hook.applicability != "known"
            or incomplete
            or hook.kind not in {"after", "before"}
            or any(skip.origin.hook_id == hook.id for skip in layered.skipped)
        ):
            for check in LAYER_CHECKS[:2]:
                _skip(report, check, _hook_address(hook), "Область карты перехвата неизвестна")
    if not changes:
        return
    # История принятого читателя полнее карты A1 (алиасы и вызовы помощников).
    # Проверяем приращение каждого слоя, а не приписываем ему асимметрию позднего слоя.
    states: dict[tuple[str, str | None], dict[str, str | None]] = {
        ("without_node", None): {
            entry.key: entry.manager_name
            for entry in routes.without_node_entries
            if entry.source.layer == "base" and entry.state in {"effective", "overwritten"}
        }
    }
    states.update(
        {
            ("plan", plan.plan_name): {
                entry.key: entry.manager_name
                for entry in plan.entries
                if entry.source.layer == "base" and entry.state in {"effective", "overwritten"}
            }
            for plan in routes.plans
        }
    )
    before_ids = {hook.id for hook in layered.hooks if hook.kind == "before"}
    changes = [
        entry
        for entry in changes
        if not (
            entry.origin.hook_id in before_ids
            and entry.key.strip() in states.get((entry.role, entry.plan_name), {})
        )
    ]
    for layer in dict.fromkeys(entry.origin.layer_id for entry in changes):
        own = [entry for entry in changes if entry.origin.layer_id == layer]
        previous = {key: dict(value) for key, value in states.items()}
        for entry in own:
            key = (entry.role, entry.plan_name)
            states.setdefault(key, {})[entry.key.strip()] = entry.manager_name
        for role, plan in dict.fromkeys((entry.role, entry.plan_name) for entry in own):
            selected = [entry for entry in own if (entry.role, entry.plan_name) == (role, plan)]
            keys = {entry.key.strip() for entry in selected}
            origin = selected[0].origin
            address = f"Слой/{escape_segment(layer)}/Маршрут/{escape_segment(plan or 'БезУзла')}"
            lower = previous.get((role, plan), {})
            lower_manager = lower.get(selected[0].key.strip())
            others = [
                key
                for key, manager in lower.items()
                if key not in keys and norm_name(manager or "") == norm_name(lower_manager or "")
            ]
            full = (
                routes.without_node_status == "complete"
                if role == "without_node"
                else any(
                    item.plan_name == plan and item.status == "complete" for item in routes.plans
                )
            )
            if not full:
                _skip(report, LAYER_CHECKS[0], address, "Карта версий имеет неизвестную область")
            elif (
                len(keys) == 1
                and lower_manager is not None
                and others
                and norm_name(selected[0].manager_name or "") != norm_name(lower_manager)
            ):
                key = selected[0].key.strip()
                _issue(
                    report,
                    LAYER_CHECKS[0],
                    address,
                    f"Подмена менеджера {selected[0].manager_name} покрывает только {key}; "
                    f"при выборе {', '.join(others)} действует {lower_manager}",
                    origin,
                )
        plans = [plan for plan in routes.plans if plan.is_ed is True]
        for key in dict.fromkeys(entry.key.strip() for entry in own):
            origin = next(entry.origin for entry in own if entry.key.strip() == key)
            for plan in plans:
                plan_touched = any(
                    entry.key.strip() == key
                    and entry.role == "plan"
                    and entry.plan_name == plan.plan_name
                    for entry in own
                )
                global_touched = any(
                    entry.key.strip() == key and entry.role == "without_node" for entry in own
                )
                if not (plan_touched or global_touched):
                    continue
                address = (
                    f"Слой/{escape_segment(layer)}/Маршрут/"
                    f"{escape_segment(plan.plan_name)}/{escape_segment(key)}"
                )
                if plan.status != "complete" or routes.without_node_status != "complete":
                    _skip(report, LAYER_CHECKS[1], address, "Сопоставление карт не доказано")
                    continue
                local = states.get(("plan", plan.plan_name), {}).get(key)
                global_name = states.get(("without_node", None), {}).get(key)
                if (norm_name(local or ""), norm_name(global_name or "")) == (
                    norm_name(previous.get(("plan", plan.plan_name), {}).get(key) or ""),
                    norm_name(previous.get(("without_node", None), {}).get(key) or ""),
                ):
                    continue
                if plan_touched != global_touched or norm_name(local or "") != norm_name(
                    global_name or ""
                ):
                    _issue(
                        report,
                        LAYER_CHECKS[1],
                        address,
                        f"Подмена ключа {key} различается с узлом ({local}) "
                        f"и без узла ({global_name})",
                        origin,
                    )


def _without_target(pred: Pred, ref: str) -> Pred:
    if pred.op == "not" and len(pred.kids) == 1 and pred.kids[0].arg == ref:
        return Pred("and")
    if pred.kind in ("rule_found", "rule_missing") and pred.arg == ref:
        return Pred("and")
    return replace(pred, kids=tuple(_without_target(kid, ref) for kid in pred.kids))


def _target_checks(
    layered: LayeredManager, context: EffectiveContext, report: ValidationReport
) -> None:
    revisions = [
        version
        for version in layered.revisions
        if version.direction == context.direction and version.headers_only == context.headers_only
    ]
    applied = {change.operation_id: version for version in revisions for change in version.changes}
    live = {
        version.logical_id: version.payload
        for version in revisions
        if version.state == EntityState.BASE
    }
    for reading in layered.readings:
        for op in reading.operations:
            version = applied.get(op.id)
            if version is not None:
                if op.kind == OperationKind.DELETE and not op.field_path:
                    live.pop(version.logical_id, None)
                elif op.kind == OperationKind.ADD and isinstance(
                    op.value, (ObjectRule, ProcessingRule, PredefinedRule)
                ):
                    live[version.logical_id] = op.value
                continue  # наличие цели доказано применённой операцией, даже если позже удалено
            parts = op.target_ref.split(_UNIT)
            if len(parts) != 3 or op.kind not in (
                OperationKind.ADD,
                OperationKind.SET,
                OperationKind.DELETE,
                OperationKind.INIT_EXTENSION,
            ):
                continue
            collection, _, name = parts
            if collection not in _PREFIX or collection == "parameters":
                continue
            kinds = {"pko": ObjectRule, "pod": ProcessingRule, "pkpd": PredefinedRule}
            candidates = [
                entity
                for entity in live.values()
                if entity is not None
                and isinstance(entity, kinds[collection])
                and entity.name == name
            ]
            if candidates:
                continue
            pred = Pred(
                "and", kids=tuple(_without_target(item, op.target_ref) for item in op.preds)
            )
            active = holds(pred, context.direction, context.headers_only, {}, {})
            if active is False:
                continue
            hook = next((hook for hook in layered.hooks if hook.id == op.hook_id), None)
            address = (
                _hook_address(hook)
                if hook
                else (
                    f"Слой/{escape_segment(op.origin.layer_id)}/{_PREFIX[collection]}/"
                    f"{escape_segment(name)}"
                )
            )
            check = "ed.layer.target.missing"
            if active is None or _unknown(context, collection):
                _skip(report, check, address, "Цель правки неизвестна в коллекции или условии пути")
            else:
                _issue(
                    report,
                    check,
                    address,
                    f"Правка {'.'.join(op.field_path)} не имеет цели {name} "
                    f"в контексте {context.direction}",
                    op.origin,
                )


def _rule_checks(
    layered: LayeredManager, context: EffectiveContext, report: ValidationReport
) -> None:
    active = [
        version
        for version in context.entities
        if version.payload and version.state != EntityState.DELETED
    ]
    names: dict[tuple[str, str], list[EntityVersion]] = defaultdict(list)
    for version in active:
        assert version.payload is not None
        names[(version.collection, norm_name(version.payload.name))].append(version)
        if version.certainty != Certainty.KNOWN:
            for check in (
                "ed.layer.rule.duplicate",
                "ed.layer.rule.conflict",
                "ed.layer.handler.unreachable",
                "ed.layer.format.undeclared",
            ):
                _skip(report, check, _address(version), f"Определённость {version.certainty}")
    for group in names.values():
        added = [
            version
            for version in group
            if version.state == EntityState.ADDED and version.layer_id != "base"
        ]
        if len(group) > 1 and added:
            version = added[-1]
            assert version.payload is not None
            if _unknown(context, version.collection):
                _skip(
                    report,
                    "ed.layer.rule.duplicate",
                    _address(version),
                    "Полнота коллекции не доказана",
                )
            else:
                _issue(
                    report,
                    "ed.layer.rule.duplicate",
                    _address(version, layer=version.layer_id),
                    f"Добавленное имя {version.payload.name} уже используется: "
                    f"{', '.join(_address(item, layer=item.layer_id) for item in group)}",
                    version.origins[-1],
                )
                if isinstance(report, _LayerReport):
                    report.explained.update(
                        _key("ed.rule.duplicate", item, context, norm_name(item.payload.name))
                        for item in group
                        if item.payload
                    )
    for tip in context.entities:
        if tip.payload is None:
            continue
        assert tip.payload is not None
        versions = [
            version
            for version in layered.revisions
            if version.logical_id == tip.logical_id
            and version.direction == context.direction
            and version.headers_only == context.headers_only
            and version.layer_id != "base"
            and version.changes
        ]
        for first, second in combinations(versions, 2):
            if first.layer_id == second.layer_id:
                continue
            check = "ed.layer.rule.conflict"
            if tip.certainty != Certainty.KNOWN:
                _skip(report, check, _address(tip), "Итог совместных правок неизвестен")
                continue
            common = sorted(
                {
                    ".".join(a.path if len(a.path) <= len(b.path) else b.path) or "*"
                    for a in first.changes
                    for b in second.changes
                    if a.path[: len(b.path)] == b.path or b.path[: len(a.path)] == a.path
                }
            )
            _issue(
                report,
                check,
                _address(second, layer=second.layer_id),
                f"Слои {first.layer_id}, {second.layer_id} изменяют {tip.payload.name}; "
                f"пересекающиеся поля: {', '.join(common) or 'нет'}; "
                f"итог в этом порядке: {tip.layer_id}",
                second.origins[-1],
            )


def _handler_checks(
    layered: LayeredManager, context: EffectiveContext, report: ValidationReport
) -> None:
    source = layered.source_document or layered.base
    base_names = {
        (rule.entity_id, binding.event): binding.target_name
        for rule in (*source.pko, *source.pod)
        for binding in rule.events
    }
    chains = {norm_name(chain.target_name): chain for chain in context.dispatch_chains}
    for version in context.entities:
        rule = version.payload
        if version.state == EntityState.DELETED or not isinstance(
            rule, (ObjectRule, ProcessingRule)
        ):
            continue
        for binding in rule.events:
            if not binding.target_name:
                continue
            new = norm_name(base_names.get((rule.entity_id, binding.event), "")) != norm_name(
                binding.target_name
            )
            check = "ed.layer.handler.unreachable" if new else "ed.layer.dispatcher.suppressed"
            chain = chains.get(norm_name(binding.target_name))
            direction = event_direction(binding.event)
            active_event = context.direction == direction if direction else None
            if active_event is False:
                continue
            hooks = [link.hook for link in chain.links if link.hook] if chain else []
            assignment = next(
                (
                    change.origin
                    for revision in reversed(layered.revisions)
                    if revision.logical_id == version.logical_id
                    and revision.direction == context.direction
                    and revision.headers_only == context.headers_only
                    for change in reversed(revision.changes)
                    if not change.path or norm_name(change.path[0]) == norm_name(binding.event)
                ),
                None,
            )
            origin = (
                assignment
                if new
                else next((hook.origin for hook in reversed(hooks) if hook.kind == "around"), None)
            )
            if origin is None or origin.layer_id == "base":
                continue  # прежнее замечание базы остаётся под прежним идентификатором
            if (
                version.certainty != Certainty.KNOWN
                or active_event is None
                or (chain and chain.resolution == "unknown")
            ):
                _skip(
                    report,
                    check,
                    _hook_address(hooks[-1])
                    if not new and hooks
                    else _address(version, layer=origin.layer_id),
                    f"Область правила или путь вызова обработчика неизвестны; {_place(origin)}",
                )
                continue
            if chain and chain.resolution == "call":
                continue
            if new:
                _issue(
                    report,
                    check,
                    _address(version, layer=origin.layer_id),
                    f"Обработчик {binding.target_name} назначен правилу {rule.name}, "
                    "но цепочка диспетчера его не вызывает",
                    origin,
                    error=True,
                    key=_key(
                        "ed.handler.missing",
                        version,
                        context,
                        norm_name(binding.target_name),
                        norm_name(binding.event),
                    ),
                )
            else:
                hook = next(
                    (
                        hook
                        for hook in reversed(hooks)
                        if hook.kind == "around" and hook.continuation == "none"
                    ),
                    None,
                )
                if hook is not None:
                    _issue(
                        report,
                        check,
                        _hook_address(hook),
                        f"Перехват {hook.routine.name} отсекает назначенный обработчик "
                        f"{binding.target_name}; нет ветки или ПродолжитьВызов",
                        hook.origin,
                        error=True,
                    )


def _format_checks(
    layered: LayeredManager,
    context: EffectiveContext,
    routes: RouteProfile | None,
    report: ValidationReport,
) -> None:
    for version in context.entities:
        rule = version.payload
        if (
            version.certainty != Certainty.KNOWN
            or not isinstance(rule, ObjectRule)
            or version.state == EntityState.DELETED
        ):
            continue
        for entity in (
            *rule.properties,
            *rule.groups,
            *(prop for group in rule.groups for prop in group.properties),
        ):
            if not entity.namespace:
                continue
            origin = next(
                (
                    op.origin
                    for op in reversed(layered.operations)
                    if (
                        isinstance(op.value, (PropertyRule, PropertyGroup))
                        and (
                            op.value.entity_id == entity.entity_id
                            or (
                                isinstance(op.value, PropertyGroup)
                                and any(
                                    prop.entity_id == entity.entity_id
                                    for prop in op.value.properties
                                )
                            )
                        )
                    )
                    or (
                        isinstance(op.value, ObjectRule)
                        and any(
                            member.entity_id == entity.entity_id
                            for member in (
                                *op.value.properties,
                                *op.value.groups,
                                *(prop for group in op.value.groups for prop in group.properties),
                            )
                        )
                    )
                    or (
                        op.kind == OperationKind.SET
                        and op.field_path[:1] == ("properties",)
                        and len(op.field_path) >= 3
                        and op.field_path[1]
                        in {entity.format_property, entity.configuration_property}
                        and op.field_path[2] == "namespace"
                        and any(change.operation_id == op.id for change in version.changes)
                    )
                ),
                None,
            )
            if origin is None or origin.layer_id == "base":
                continue
            address = (
                _address(version, layer=origin.layer_id)
                + ("/ПКТЧ/" if isinstance(entity, PropertyGroup) else "/ПКС/")
                + escape_segment(entity.format_property or entity.configuration_property)
            )
            missing = []
            if entity.namespace not in rule.extensions:
                missing.append("инициализация ПКО")
            if routes is None or context.version_key is None:
                _skip(
                    report,
                    "ed.layer.format.undeclared",
                    address,
                    "Глобальное объявление URI для версии неизвестно",
                )
            elif routes.without_node_status != "complete" or any(
                skip.code == "ed.route.format_extensions" for skip in routes.skipped
            ):
                _skip(
                    report,
                    "ed.layer.format.undeclared",
                    address,
                    "Карта расширений формата неполна",
                )
            elif not any(
                entry.uri == entity.namespace
                and entry.version == context.version_key
                and entry.state == "effective"
                for entry in routes.format_extensions
            ):
                missing.append("глобальное объявление URI")
            if missing:
                _issue(
                    report,
                    "ed.layer.format.undeclared",
                    address,
                    f"Пространство {entity.namespace} свойства {entity.format_property} "
                    f"не объявлено: {', '.join(missing)}; "
                    f"проверьте расширение формата для {context.version_key}",
                    origin,
                    key=_key("ed.extension.uninitialized", version, context, entity.namespace),
                    entity_id=entity.entity_id,
                )


def validate_layers(
    layered: LayeredManager,
    routes: RouteProfile | None = None,
    *,
    context: EffectiveContext | None = None,
) -> ValidationReport:
    """Одиннадцать условий §4. Без контекста проверяются все прочитанные направления."""
    report = _LayerReport()
    report.index = build_layer_addresses(layered)
    for skip in layered.skipped:
        _skip(
            report,
            "ed.layer.reading",
            _place(skip.origin),
            f"{skip.reason}; affected={', '.join(skip.check_scope)}",
        )
    report.skipped.append(
        Skipped("ed.layer.runtime", "Активность и порядок подключения в базе не проверены")
    )
    _hook_checks(layered, report)
    _route_checks(layered, routes, report)
    for current in (context,) if context is not None else layered.contexts:
        report.direction = current.direction
        _target_checks(layered, current, report)
        _rule_checks(layered, current, report)
        _handler_checks(layered, current, report)
        _format_checks(layered, current, routes, report)
    return report


def _uncertain_report(
    layered: LayeredManager, context: EffectiveContext, report: ValidationReport, family: str
) -> None:
    checks = (
        _LINK_CHECKS
        if family == "links"
        else _SCHEMA_CHECKS
        if family == "schema"
        else _STRUCTURE_CHECKS
    )
    for version in context.entities:
        if version.certainty == Certainty.KNOWN:
            continue
        applicable = checks
        if family != "links":
            applicable = (
                tuple(
                    check for check in checks if ("pkpd" in check) == (version.collection == "pkpd")
                )
                if version.collection in {"pko", "pkpd"}
                else ()
            )
        for check in applicable:
            _skip(
                report,
                check,
                _address(version),
                f"Определённость {version.certainty}; правило не оценивалось",
            )
    for scope in context.taints:
        for check in checks:
            _skip(
                report,
                check,
                f"Действующее/{_PREFIX.get(scope, scope)}",
                "Полнота области не доказана",
            )
    # Отсутствие зависимой цели нельзя доказать по отфильтрованной коллекции.
    dependent = {
        "ed.reference.property_rule_missing": "pko",
        "ed.reference.pod_pko_missing": "pko",
        "ed.reference.code_rule_missing": "pko",
        "ed.schema.pko_unavailable": "pko",
        "ed.identity.uid_without_pod": "pod",
    }
    kept = []
    for issue in report.issues:
        if "manager" in context.taints:
            _skip(
                report, issue.check, issue.address, "Менеджер не определён; проверка не оценивалась"
            )
            continue
        collection = dependent.get(issue.check)
        if collection and _unknown(context, collection):
            _skip(
                report, issue.check, issue.address, f"Зависимая коллекция {collection} неизвестна"
            )
        else:
            kept.append(issue)
    report.issues[:] = kept


def validate_effective_links(
    layered: LayeredManager, context: EffectiveContext, routes: RouteProfile | None = None
) -> ValidationReport:
    from .ed_links import validate_links

    document = effective_document(layered, context)
    index = build_addresses(document)
    source = layered.source_document or layered.base
    uncertain = set()
    if document is not source:
        applicable = Applicability.build(
            document, ValidationProfile.build(None, context.version_key, context.direction)
        )
        for rule in document.pko:
            for binding in rule.events:
                if applicable.evaluate(binding, context.direction, rule) is None:
                    uncertain.update(
                        (check, binding.entity_id)
                        for check in ("ed.handler.missing", "ed.handler.ambiguous")
                    )
            props = (
                *rule.properties,
                *(prop for group in rule.groups for prop in group.properties),
            )
            for prop in props:
                if applicable.evaluate(prop, context.direction, rule) is None:
                    uncertain.update(
                        (check, prop.entity_id)
                        for check in (
                            "ed.reference.property_rule_missing",
                            "ed.extension.uninitialized",
                            "ed.algorithm.handler_missing",
                        )
                    )
                    uncertain.add(("ed.extension.unused", rule.entity_id))
        pko = tuple(
            replace(
                rule,
                events=tuple(
                    binding
                    for binding in rule.events
                    if applicable.evaluate(binding, context.direction, rule) is not False
                ),
                properties=tuple(
                    prop
                    for prop in rule.properties
                    if applicable.evaluate(prop, context.direction, rule) is not False
                ),
                groups=tuple(
                    replace(
                        group,
                        properties=tuple(
                            prop
                            for prop in group.properties
                            if applicable.evaluate(prop, context.direction, group) is not False
                        ),
                    )
                    for group in rule.groups
                    if applicable.evaluate(group, context.direction, rule) is not False
                ),
            )
            for rule in document.pko
        )
        document = replace(document, pko=pko)
    layer_report = validate_layers(layered, routes, context=context)
    logical = {version.logical_id: version for version in context.entities}
    explained = set()
    preserved = set()
    original_properties = {
        prop.entity_id: prop
        for rule in source.pko
        for prop in (
            *rule.properties,
            *(prop for group in rule.groups for prop in group.properties),
        )
    }
    for rule in document.pko:
        for prop in (
            *rule.properties,
            *(prop for group in rule.groups for prop in group.properties),
        ):
            original = original_properties.get(prop.entity_id)
            if original is not None and original.namespace == prop.namespace:
                # Объяснение новой ПКС с тем же URI не поглощает прежнее замечание базы.
                preserved.add(("ed.extension.uninitialized", prop.entity_id))
    if isinstance(layer_report, _LayerReport):
        for check, identifier, _, name, event in layer_report.explained:
            version = logical.get(identifier)
            if version and version.payload:
                if check == "ed.rule.duplicate" and version.layer_id == "base":
                    # Дубликаты, уже бывшие в базе, не исчезают при добавлении третьей строки.
                    original = layered.source_document or layered.base
                    if (
                        sum(norm_name(rule.name) == name for rule in (*original.pko, *original.pod))
                        > 1
                    ):
                        continue
                explained.add((check, version.payload.entity_id, name, event))
    references = build_references(document)
    # Тело обработчика исключённого по направлению правила не является действующим.
    # Свободные методы без привязки остаются входом прежней проверки связности.
    bound_names = {
        norm_name(binding.target_name)
        for rule in (*source.pko, *source.pod)
        for binding in rule.events
    }
    active_handlers = {
        norm_name(binding.target_name)
        for rule in (*document.pko, *document.pod)
        for binding in rule.events
        if event_direction(binding.event) in (None, context.direction)
    }
    inactive_handlers = {
        routine.entity_id
        for routine in document.routines
        if norm_name(routine.name) in bound_names - active_handlers
    }
    references = replace(
        references,
        entries=tuple(ref for ref in references.entries if ref.owner_id not in inactive_handlers),
    )
    active_ids = {rule.entity_id for rule in (*document.pko, *document.pod)}
    deleted_ids = {
        version.payload.entity_id
        for version in context.entities
        if version.state == EntityState.DELETED and version.payload is not None
    }
    inactive_names = {
        norm_name(rule.name)
        for rule in (*source.pko, *source.pod)
        if rule.entity_id not in active_ids | deleted_ids
    }
    for rule in document.pko:
        for prop in (
            *rule.properties,
            *(prop for group in rule.groups for prop in group.properties),
        ):
            if norm_name(prop.conversion_rule) in inactive_names:
                uncertain.add(("ed.reference.property_rule_missing", prop.entity_id))
    for rule in document.pod:
        if any(norm_name(ref.name) in inactive_names for ref in rule.used_pko):
            uncertain.add(("ed.reference.pod_pko_missing", rule.entity_id))
    unproven = [
        reference
        for reference in references.entries
        if reference.name is not None and norm_name(reference.name) in inactive_names
    ]
    unproven_ordinals = {reference.ordinal for reference in unproven}
    references = replace(
        references,
        entries=tuple(
            reference
            for reference in references.entries
            if reference.ordinal not in unproven_ordinals
        ),
    )
    report = validate_links(
        document,
        index,
        references,
        explained=frozenset(explained),
        uncertain=frozenset(uncertain),
        preserved=frozenset(preserved),
        context=context,
    )
    for reference in unproven:
        _skip(
            report,
            "ed.reference.code_rule_missing",
            index.by_id[reference.owner_id][0],
            "Условие упоминания правила другого направления в теле метода неизвестно",
        )
    _uncertain_report(layered, context, report, "links")
    unknown_handlers = {
        norm_name(chain.target_name)
        for chain in context.dispatch_chains
        if chain.resolution == "unknown"
    }
    for name in unknown_handlers:
        for check in ("ed.dispatcher.target_missing", "ed.deferred.argument"):
            _skip(
                report, check, "Диспетчер/" + escape_segment(name), "Цепочка диспетчера неизвестна"
            )
    suppressed = set()
    for version in context.entities:
        rule = version.payload
        if isinstance(rule, (ObjectRule, ProcessingRule)):
            for binding in rule.events:
                if event_direction(binding.event) not in (None, context.direction):
                    continue
                if norm_name(binding.target_name) in unknown_handlers:
                    for address in index.by_id.get(rule.entity_id, ()):
                        suppressed.add(("ed.handler.missing", address))
                        _skip(
                            report, "ed.handler.missing", address, "Цепочка диспетчера неизвестна"
                        )
    report.issues[:] = [
        issue for issue in report.issues if (issue.check, issue.address) not in suppressed
    ]
    return report


def validate_effective_schema(
    layered: LayeredManager,
    context: EffectiveContext,
    schema: EdSchema,
    profile: ValidationProfile,
    snapshot: StructureSnapshot | None = None,
    coverage: Counter[str] | None = None,
) -> ValidationReport:
    from .ed_schema import validate_schema

    document = effective_document(layered, context)
    report = validate_schema(
        document, schema, build_addresses(document), profile, snapshot, coverage
    )
    _uncertain_report(layered, context, report, "schema")
    return report


def validate_effective_structure(
    layered: LayeredManager,
    context: EffectiveContext,
    snapshot: StructureSnapshot,
    profile: ValidationProfile,
    coverage: Counter[str] | None = None,
) -> ValidationReport:
    from .ed_structure import validate_structure

    document = effective_document(layered, context)
    report = validate_structure(document, snapshot, build_addresses(document), profile, coverage)
    _uncertain_report(layered, context, report, "structure")
    return report
