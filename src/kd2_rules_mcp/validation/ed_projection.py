"""Общая проекция авторинга и адаптер действующего контекста к EdDocument.

Сохраняет исходные ID, span и raw. Новая грамматика BSL здесь не вводится.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any, cast

from kd2_rules_mcp.ed.forms import EVENT_INVOCATIONS, accepts_arguments, invocation_keys_match
from kd2_rules_mcp.ed.layer_model import (
    Certainty,
    EffectiveContext,
    EffectiveDocument,
    EntityState,
    LayeredManager,
)
from kd2_rules_mcp.ed.lexer import tokenize
from kd2_rules_mcp.ed.model import (
    EdDocument,
    Guard,
    ObjectRule,
    Parameter,
    PredefinedRule,
    ProcessingRule,
    RuleUse,
    SourceFile,
)


@dataclass(frozen=True, slots=True)
class ContextDocument(EffectiveDocument):
    """Роли для вызовов контекста и счётчик деклараций исходного файла раздельны.

    Исходные процедуры остаются в документе во всех направлениях. Их счётчик
    сохраняет классификацию читателя базы; собственные обработчики слоя добавляются
    только там, где действуют. Это не расширяет индекс тел неактивных обработчиков.
    """

    source_handler_ids: frozenset[str] = frozenset()

    @property
    def counts(self) -> dict[str, int]:
        result = super(ContextDocument, self).counts
        result["handlers"] = len(
            self.source_handler_ids
            | {routine.entity_id for routine in self.routines if "handler" in routine.roles}
        )
        return result


def event_direction(event: str) -> str | None:
    """Направление события по исполнителю; неизвестные события не угадываются."""
    if event in {"ПриОтправкеДанных", "ВыборкаДанных", "ПередОбработкой"}:
        return "send"
    if event in {
        "ПриКонвертацииДанныхXDTO",
        "ПередЗаписьюПолученныхДанных",
        "ПослеЗагрузкиВсехДанных",
        "АлгоритмПоиска",
    }:
        return "receive"
    return None


def project_document(
    base: EdDocument,
    *,
    pko: tuple[ObjectRule, ...],
    guards: tuple[Guard, ...],
    additional_files: tuple[SourceFile, ...] = (),
) -> EdDocument:
    """Общая сборка проекции автора и слоя без изменения исходного документа."""
    identifiers = {source.file_id for source in base.files}
    for source in additional_files:
        if source.file_id in identifiers:
            raise ValueError("Идентификатор порождённого файла уже занят")
        identifiers.add(source.file_id)
    return replace(base, pko=pko, guards=guards, files=(*base.files, *additional_files))


def select_context(
    layered: LayeredManager, direction: str = "send", headers_only: bool = False
) -> EffectiveContext:
    """Явный контекст: оба направления нельзя незаметно слить в один документ."""
    for context in layered.contexts:
        if context.direction == direction and context.headers_only == headers_only:
            return context
    raise ValueError(f"Контекст направления {direction!r}, headers_only={headers_only} отсутствует")


def _clean[T](value: T, base_guards: set[str]) -> T:
    """Доказанные предикаты слоя не выдаются за непрозрачные условия исходного BSL."""
    if isinstance(value, tuple):
        return cast(T, tuple(_clean(item, base_guards) for item in value))
    if not is_dataclass(value) or isinstance(value, (SourceFile,)):
        return value
    changes = {}
    for item in fields(value):
        current = getattr(value, item.name)
        if item.name == "guards":
            changes[item.name] = tuple(guard for guard in current if guard in base_guards)
        elif is_dataclass(current) or isinstance(current, tuple):
            changes[item.name] = _clean(current, base_guards)
    return cast(T, replace(cast(Any, value), **changes)) if changes else value


def effective_document(layered: LayeredManager, context: EffectiveContext) -> EdDocument:
    """Адаптер A1 → EdDocument; исходные span/raw и ID сущностей сохраняются.

    Неизвестные правила не подаются проверкам; их зависимости обрабатывает обвязка.
    Документ всегда содержит правила выбранного направления, включая контекст без слоя.
    """
    source = layered.source_document or layered.base
    if not source.files and not layered.readings:
        return source
    known = [
        version.payload
        for version in context.entities
        if version.certainty == Certainty.KNOWN
        and version.state != EntityState.DELETED
        and version.payload is not None
    ]
    guard_ids = {guard.entity_id for guard in source.guards}
    payloads = tuple(_clean(entity, guard_ids) for entity in known)
    pko = tuple(
        replace(
            entity,
            events=tuple(replace(event, owner_id=entity.entity_id) for event in entity.events),
            properties=tuple(
                replace(prop, owner_id=entity.entity_id) for prop in entity.properties
            ),
            groups=tuple(
                replace(
                    group,
                    owner_id=entity.entity_id,
                    properties=tuple(
                        replace(prop, owner_id=entity.entity_id) for prop in group.properties
                    ),
                )
                for group in entity.groups
            ),
        )
        for entity in payloads
        if isinstance(entity, ObjectRule)
    )
    pod = tuple(
        replace(
            entity,
            events=tuple(replace(event, owner_id=entity.entity_id) for event in entity.events),
        )
        for entity in payloads
        if isinstance(entity, ProcessingRule)
    )
    pkpd = tuple(entity for entity in payloads if isinstance(entity, PredefinedRule))
    parameters = tuple(entity for entity in payloads if isinstance(entity, Parameter))
    unchanged_ids = {
        version.payload.entity_id
        for version in context.entities
        if version.payload is not None and version.state == EntityState.BASE
    }
    unscoped_ids = {
        use.rule_id
        for use in source.rule_uses
        if use.direction is None and use.rule_id in unchanged_ids
    }
    uses = tuple(
        use for use in source.rule_uses if use.rule_id is None or use.rule_id in unscoped_ids
    ) + tuple(
        RuleUse(
            entity_id="effective-use:" + entity.entity_id,
            kind="rule_use",
            name=entity.name,
            span=entity.span,
            raw_text=entity.raw_text,
            rule_id=entity.entity_id,
            target_name=getattr(entity, "procedure_name", entity.name),
            direction=context.direction,
        )
        for entity in (*pko, *pod)
        if entity.entity_id not in unscoped_ids
    )
    routines = {routine.entity_id: routine for routine in source.routines}
    routines.update({routine.entity_id: routine for _, routine in layered.routines})
    # Новые привязки содержат имя; связываем его с процедурой своего файла.
    # Неоднозначность имён не разрешается выбором последнего элемента.

    dispatch_targets: dict[tuple[str, str], str | None] = {}
    dispatch_cases = {}
    dispatch_resolution = {
        (chain.kind, chain.target_name): chain.resolution for chain in context.dispatch_chains
    }
    base_unknown_paths = {
        (chain.kind, chain.target_name)
        for chain in context.dispatch_chains
        if chain.resolution == "unknown" and not any(link.hook for link in chain.links)
    }
    original_bindings = {
        (rule.entity_id, event.event): event.target_name
        for rule in (*source.pko, *source.pod)
        for event in rule.events
    }

    def bind_events(rule):
        events = []
        for event in rule.events:
            signature = EVENT_INVOCATIONS.get(event.event)
            key = (signature.kind if signature else "procedure", event.target_name)
            unchanged_base_path = (
                key in base_unknown_paths
                and original_bindings.get((rule.entity_id, event.event)) == event.target_name
            )
            if key in dispatch_targets:
                target_id = dispatch_targets[key]
            elif layered.readings and not unchanged_base_path:
                target_id = None
            else:
                target_id = event.target_id
            case = dispatch_cases.get(key)
            callee = routines.get(target_id or "")
            invalid = bool(
                layered.readings
                and case
                and signature
                and original_bindings.get((rule.entity_id, event.event)) != event.target_name
                and (
                    case.returns != (signature.kind == "function")
                    or not invocation_keys_match(signature, case.arguments)
                    or (
                        callee is not None
                        and not accepts_arguments(callee.parameters, len(case.arguments))
                    )
                )
            )
            events.append(
                replace(
                    event,
                    target_id=None if invalid else target_id,
                    resolution="invalid_signature"
                    if invalid
                    else "unknown"
                    if layered.readings
                    and not unchanged_base_path
                    and (signature is None or dispatch_resolution.get(key) == "unknown")
                    else event.resolution,
                )
            )
        return replace(rule, events=tuple(events))

    dispatcher_ids = {routine.name.casefold(): routine.entity_id for routine in source.routines}
    cases = {}
    for chain in context.dispatch_chains:
        if chain.resolution != "call":
            continue
        arounds = [link for link in chain.links if link.hook and link.hook.kind == "around"]
        selected = []
        for link in reversed(arounds):
            if link.case is not None:
                selected.append(link)
                break
            if not link.continues:
                break
        else:
            selected.extend(link for link in chain.links if link.hook is None)
        selected.extend(
            link
            for link in chain.links
            if link.hook and link.hook.kind in {"before", "after"} and link.case
        )
        for link in selected:
            if link.case is None:
                continue
            case = _clean(link.case, guard_ids)
            target = link.hook.target_name.casefold() if link.hook else None
            if target:
                case = replace(case, dispatcher_id=dispatcher_ids.get(target, case.dispatcher_id))
            cases[case.entity_id] = case
        # Строка ключа точная; идентификатор процедуры сравнивается без регистра.
        # Источник выбранной ветки различает одноимённые методы разных расширений.
        if selected and selected[0].case is not None:
            case = selected[0].case
            candidates = [
                routine.entity_id
                for routine in routines.values()
                if case.target.reference_parts
                and routine.name.casefold() == case.target.reference_parts[0].casefold()
                and routine.span.file_id == case.span.file_id
            ]
            dispatch_targets[chain.kind, chain.target_name] = (
                candidates[0] if len(candidates) == 1 else None
            )
            dispatch_cases[chain.kind, chain.target_name] = case
    pko = tuple(bind_events(rule) for rule in pko)
    pod = tuple(bind_events(rule) for rule in pod)
    source_ids = {item.entity_id for item in source.routines}
    active_ids = {
        event.target_id
        for rule in (*pko, *pod)
        for event in rule.events
        if event_direction(event.event) in (None, context.direction)
    }
    active_ids.update(
        hook.routine.entity_id
        for rule in (*pko, *pod)
        for event in rule.events
        if event_direction(event.event) in (None, context.direction)
        for change in getattr(event, "body_changes", ())
        for hook in layered.hooks
        if hook.origin == change.origin
    )
    previous_calls = tuple(
        call
        for call in layered.previous_calls
        if call.rule_id in {rule.entity_id for rule in (*pko, *pod)}
        and event_direction(call.event) in (None, context.direction)
    )
    helpers = {
        (routine.span.file_id, routine.name.casefold()): routine
        for routine in routines.values()
        if "handler_helper" in routine.roles
    }
    helper_files = {file_id for file_id, _ in helpers}
    changed = bool(helpers or previous_calls)
    while changed:
        before = len(active_ids)
        for call in previous_calls:
            if call.routine_id in active_ids:
                active_ids.add(call.target_id)
        for routine in routines.values():
            if routine.entity_id not in active_ids or routine.span.file_id not in helper_files:
                continue
            tokens = tokenize(routine.raw_text)
            for index, token in enumerate(tokens):
                target = helpers.get((routine.span.file_id, token.folded))
                if (
                    target
                    and index + 1 < len(tokens)
                    and tokens[index + 1].value == "("
                    and (not index or tokens[index - 1].value != ".")
                ):
                    active_ids.add(target.entity_id)
        changed = len(active_ids) != before
    originally_bound = {
        event.target_id for rule in (*source.pko, *source.pod) for event in rule.events
    }
    all_routines = tuple(
        replace(routine, roles=routine.roles - {"handler", "handler_helper"})
        if routine.entity_id not in active_ids
        and (routine.entity_id not in source_ids or routine.entity_id in originally_bound)
        else routine
        for routine in routines.values()
    )
    existing_files = {file.file_id for file in source.files}
    document = project_document(
        source,
        pko=pko,
        guards=source.guards,
        additional_files=tuple(
            file for file in layered.source_files if file.file_id not in existing_files
        ),
    )
    document = replace(
        document,
        pod=pod,
        pkpd=pkpd,
        parameters=parameters,
        routines=all_routines,
        rule_uses=uses,
        dispatcher_cases=tuple(cases.values()),
    )
    return ContextDocument(
        **{f.name: getattr(document, f.name) for f in fields(EdDocument)},
        previous_calls=previous_calls,
        source_handler_ids=frozenset(
            routine.entity_id for routine in source.routines if "handler" in routine.roles
        ),
    )
