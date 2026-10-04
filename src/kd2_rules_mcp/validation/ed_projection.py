"""Общая проекция авторинга и адаптер действующего контекста к EdDocument.

Сохраняет исходные ID, span и raw. Новая грамматика BSL здесь не вводится.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Any, cast

from kd2_rules_mcp.ed.layer_model import (
    Certainty,
    EffectiveContext,
    EntityState,
    LayeredManager,
)
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
    uses = tuple(use for use in source.rule_uses if use.rule_id is None) + tuple(
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
    )
    routines = {routine.entity_id: routine for routine in source.routines}
    routines.update({routine.entity_id: routine for _, routine in layered.routines})
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
    targets = {binding.target_name.casefold() for rule in (*pko, *pod) for binding in rule.events}
    callees = {
        case.target.reference_parts[0].casefold()
        for case in cases.values()
        if case.target.reference_parts
    }
    all_routines = tuple(
        replace(routine, roles=routine.roles | {"handler"})
        if routine.name.casefold() in targets | callees
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
    return replace(
        document,
        pod=pod,
        pkpd=pkpd,
        parameters=parameters,
        routines=all_routines,
        rule_uses=uses,
        dispatcher_cases=tuple(cases.values()),
    )
