"""Наложение слоёв на неизменяемый документ менеджера.

Порядок расширений задаёт вызывающий. Базовый документ не изменяется.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from .errors import EdFormatError, EdReadError
from .forms import DISPATCHERS, EVENT_INVOCATIONS, EVENT_SIGNATURES
from .layer_model import (
    Certainty,
    DispatchChain,
    DispatchLink,
    EffectiveContext,
    EntityState,
    EntityVersion,
    ExtensionReading,
    FieldChange,
    Footprint,
    HandlerBodyChange,
    LayerDescriptor,
    LayeredManager,
    LayerHandlerBinding,
    LayerOperation,
    LayerSkip,
    LayerStatus,
    MapEntry,
    OperationKind,
    Origin,
    Pred,
)
from .layer_reader import (
    DumpInfo,
    DumpObject,
    base_map,
    dispatcher_throws,
    dispatcher_unknown,
    filler_calls,
    handled_literals,
    layer_key,
    load_source,
    module_routines,
    parse_annotation,
    property_conditions,
    read_dump,
    read_extension_file,
    recover_parameters,
    resolve_handler_bindings,
    source_from_text,
)
from .lexer import lex
from .model import (
    Conversion,
    Coverage,
    DispatcherCase,
    EdDocument,
    Expr,
    Field,
    HandlerBinding,
    ObjectRule,
    Parameter,
    ParseStatus,
    PredefinedRule,
    ProcessingRule,
    PropertyGroup,
    PropertyRule,
    RuleUse,
    SourceSpan,
)
from .reader import read_manager, read_manager_text

MAX_LAYERS = 16
_UNIT = "\x1f"
_DISPATCH = {name.casefold() for name in DISPATCHERS}
_EVENT_ATTR = {name.casefold(): name for name in EVENT_SIGNATURES}
_ROUTE = {
    "приполучениинастроек",
    "приполучениидоступныхверсийформата",
    "приполучениидоступныхрасширенийформата",
}
_PKO_ATTR = {
    "ОбъектДанных": "configuration_object",
    "ОбъектФормата": "format_object",
    "ПравилоДляГруппыСправочника": "group_flag",
    "ВариантИдентификации": "identification",
}
_POD_ATTR = {
    "ОбъектВыборкиМетаданные": "configuration_selection",
    "ОбъектВыборкиФормат": "format_selection",
    "ОчисткаДанных": "clear_data",
}
_PKPD_ATTR = {"ТипДанных": "configuration_type", "ТипXDTO": "format_type"}
_PROP_ATTR = frozenset(
    {
        "configuration_property",
        "format_property",
        "algorithm_flag",
        "conversion_rule",
        "namespace",
        "condition_name",
    }
)


def read_layers(
    base_root: str | Path,
    extension_roots: tuple[str | Path, ...] | list[str | Path] = (),
    manager: str | None = None,
    version_key: str | None = None,
) -> LayeredManager:
    """Читает основную выгрузку и явно перечисленные расширения в заданном порядке.

    ``manager`` — имя модуля менеджера. Без него модуль берётся, только если он
    один и карта версий его не развивает на несколько имён. Имена сравниваются
    без учёта регистра. Перехват чужого заимствованного модуля к этому менеджеру
    не относится.
    """
    base_path = Path(base_root)
    roots = tuple(Path(path) for path in extension_roots)
    base_dump = read_dump(base_path)
    if base_dump.failed:
        raise EdFormatError("Нет читаемого Configuration.xml основной выгрузки")
    base_layer = _layer(0, base_dump, base_path)
    if len(roots) > MAX_LAYERS:
        origin = _dump_origin(base_layer, base_path)
        return _failed(
            base_layer,
            (LayerSkip("resource_limit", origin, ("manager",), str(len(roots)), ("manager",)),),
        )
    ext_layers: list[LayerDescriptor] = []
    ext_dumps: list[DumpInfo] = []
    skips: list[LayerSkip] = list(base_dump.skips)
    for ordinal, path in enumerate(roots, 1):
        dump = read_dump(path)
        ext_dumps.append(dump)
        ext_layers.append(_layer(ordinal, dump, path))
        skips.extend(dump.skips)
    base_objects = {(item.kind, item.name.casefold()): item for item in base_dump.objects}
    map_entries = _base_maps(base_dump, base_layer)
    if manager:
        base_module = _find_object(base_dump, "CommonModule", manager)
        if (
            not base_module
            or not base_module.module_path
            or not _looks_like_manager(Path(base_module.module_path))
        ):
            raise EdReadError(
                f"Модуль менеджера обмена отсутствует или не объявляет заполнитель: {manager}"
            )
        manager_name = base_module.name
    else:
        manager_name, base_module = _select_base_manager(map_entries, ext_dumps, base_dump)
    if base_module is None or not base_module.module_path:
        base_document = _empty_document()
        manager_name = None
    else:
        try:
            base_document = read_manager(base_module.module_path)
        except (EdFormatError, EdReadError) as error:
            if not roots:
                raise
            skips.append(
                LayerSkip(
                    "manager_unreadable",
                    _dump_origin(base_layer, base_module.module_path),
                    (base_module.name,),
                    str(error),
                    ("manager",),
                )
            )
            base_document = _empty_document()
            manager_name = None
    route_readings: list[ExtensionReading] = []
    own: dict[str, tuple[EdDocument, LayerDescriptor]] = {}
    executor_hooks = []
    executor_files = []
    adopted: list[tuple[DumpObject, LayerDescriptor]] = []
    for dump, layer in zip(ext_dumps, ext_layers, strict=True):
        if dump.failed:
            skips.append(
                LayerSkip(
                    "metadata_xml",
                    _dump_origin(layer, dump.root),
                    ("manager",),
                    dump.root,
                    ("manager",),
                )
            )
            continue
        for obj in dump.objects:
            if not _usable(obj, base_objects, layer, skips) or not obj.module_path:
                continue
            if obj.kind == "ExchangePlan" or (
                obj.kind == "CommonModule" and obj.name.casefold() == "обменданнымипереопределяемый"
            ):
                base_target = base_objects.get((obj.kind, obj.name.casefold()))
                route_targets = {}
                if base_target and base_target.module_path:
                    route_source = load_source(
                        Path(base_target.module_path), base_target.module_path
                    )
                    route_targets = {
                        item.name.casefold(): item for item in module_routines(route_source)
                    }
                route_readings.append(
                    _read_obj(
                        obj,
                        layer,
                        base_document.manager_version,
                        _trusted(base_document),
                        route_targets,
                        obj.name if obj.kind == "ExchangePlan" else None,
                    )
                )
                continue
            if obj.kind != "CommonModule":
                continue
            if (
                obj.name.casefold()
                in {
                    "обменданнымиxdtoсервер",
                    "обменданнымисервер",
                    "обменданнымисобытия",
                    "обменданнымиповтисп",
                }
                and obj.belonging
                and obj.belonging.casefold() == "adopted"
            ):
                executor_source = load_source(Path(obj.module_path), obj.module_path)
                pending = None
                for statement in lex(executor_source).statements:
                    if statement.tokens[0].kind == "directive":
                        annotation = parse_annotation(statement.tokens[0].value)
                        if annotation:
                            pending = annotation
                    elif statement.head in {"процедура", "функция"}:
                        if pending:
                            kind, target = pending
                            origin = Origin(
                                layer.id,
                                obj.kind,
                                obj.name,
                                executor_source.file_id,
                                statement.span,
                                statement.tokens[1].value,
                                path=executor_source.path,
                            )
                            executor_hooks.append((obj.name, target, kind, origin))
                        pending = None
                    else:
                        pending = None
                executor_files.append(executor_source)
                continue
            if obj.belonging is None:
                if _looks_like_manager(Path(obj.module_path)):
                    try:
                        own_source = load_source(Path(obj.module_path), obj.module_path)
                        own[obj.name] = (
                            read_manager_text(
                                ("\ufeff" if own_source.bom else "") + own_source.text,
                                file_id=own_source.file_id,
                                path=own_source.path,
                            ),
                            layer,
                        )
                    except (EdFormatError, EdReadError) as error:
                        skips.append(
                            LayerSkip(
                                "manager_unreadable",
                                _dump_origin(layer, obj.module_path),
                                (obj.name,),
                                str(error),
                                ("manager",),
                            )
                        )
                continue
            if obj.belonging.casefold() == "adopted":
                adopted.append((obj, layer))
    map_entries = _apply_maps(map_entries, route_readings)
    selected, candidates = _choose_manager(manager, version_key, map_entries, base_dump, list(own))
    if selected is None:
        listed = ", ".join(candidates) or (base_dump.name or str(base_path))
        skips.append(
            LayerSkip(
                "ambiguous_manager",
                _dump_origin(base_layer, base_path),
                ("manager",),
                listed,
                ("manager",),
            )
        )
        if not roots and candidates:
            raise EdReadError(f"Модуль менеджера обмена не определён: {listed}")
        selected = ""
        base_document = _empty_document()
        manager_name = None
    elif selected.casefold() != (manager_name or "").casefold():
        chosen = _find_object(base_dump, "CommonModule", selected)
        if chosen and chosen.module_path:
            try:
                base_document = read_manager(chosen.module_path)
                manager_name = chosen.name
            except (EdFormatError, EdReadError) as error:
                skips.append(
                    LayerSkip(
                        "manager_unreadable",
                        _dump_origin(base_layer, chosen.module_path),
                        (chosen.name,),
                        str(error),
                        ("manager",),
                    )
                )
                base_document = _empty_document()
                manager_name = None
        else:
            manager_name = selected
    else:
        chosen = _find_object(base_dump, "CommonModule", selected)
        if chosen:
            manager_name = chosen.name
            selected = chosen.name
    helpers = _trusted(base_document)
    targets = {routine.name.casefold(): routine for routine in base_document.routines}
    readings = list(route_readings)
    for obj, layer in adopted:
        if not selected or obj.name.casefold() != selected.casefold():
            continue
        readings.append(
            _read_obj(obj, layer, base_document.manager_version, helpers, targets, None)
        )
    source: EdDocument = base_document
    source_layer = "base"
    rule_readings = list(readings)
    tainted = any(dump.failed for dump in ext_dumps)
    own_key = next((key for key in own if key.casefold() == (selected or "").casefold()), None)
    if own_key:
        source, own_layer = own[own_key]
        source_layer = own_layer.id
        selected = own_key
        rule_readings = [reading for reading in readings if _route_only(reading)]
    layered = compose_manager(
        base_document,
        source,
        readings=rule_readings,
        layers=(base_layer, *ext_layers),
        map_entries=tuple(map_entries),
        skips=tuple(skips),
        version=source.manager_version,
        manager_name=selected,
        manager_layer_id=source_layer,
        own=own,
    )
    layered = replace(
        layered,
        executor_hooks=tuple(executor_hooks),
        source_files=_unique_files((*layered.source_files, *executor_files)),
    )
    if not tainted:
        return layered
    return replace(
        layered,
        contexts=tuple(
            replace(context, taints=tuple(sorted({*context.taints, "manager"})))
            for context in layered.contexts
        ),
    )


def compose_manager(
    base: EdDocument,
    source: EdDocument | None = None,
    *,
    readings: list[ExtensionReading] | tuple[ExtensionReading, ...] = (),
    layers: tuple[LayerDescriptor, ...] = (),
    map_entries: tuple[MapEntry, ...] = (),
    skips: tuple[LayerSkip, ...] = (),
    version: int | None = None,
    manager_name: str = "",
    manager_layer_id: str = "base",
    own: dict[str, tuple[EdDocument, LayerDescriptor]] | None = None,
) -> LayeredManager:
    """Собирает действующие контексты. Объект ``base`` не подменяется."""
    source = base if source is None else source
    version = source.manager_version if version is None else version
    calls = filler_calls(source.files[0]) if source.files else ()
    parameters = _parameters(source)
    throws = dispatcher_throws(source.files[0]) if source.files else False
    header_flags = (False, True) if version == 3 else (False,)
    contexts: list[EffectiveContext] = []
    revisions: list[EntityVersion] = []
    fault_skips: list[LayerSkip] = []
    active: set[tuple[int, str]] = set()
    live_skips: set[tuple[int, str, bool]] = set()
    conditions = property_conditions(source.files[0]) if source.files else {}
    if any(
        hook.target_name.casefold() in _DISPATCH for reading in readings for hook in reading.hooks
    ):
        preliminary = []
        for direction in ("send", "receive"):
            for headers_only in header_flags:
                tips, *_ = _context_state(
                    source,
                    calls,
                    parameters,
                    readings,
                    direction,
                    headers_only,
                    conditions,
                    set(),
                    set(),
                )
                preliminary.append(
                    EffectiveContext(
                        direction,
                        headers_only,
                        None,
                        manager_name,
                        manager_layer_id,
                        tuple(tips.values()),
                        (),
                        _dispatch(source, readings, tips, throws),
                        (),
                        (),
                    )
                )
        descriptors = {layer.id: layer for layer in layers}
        resolved = []
        prior_contexts = []
        for index, reading in enumerate(readings):
            previous_contexts = []
            if index:
                for context in preliminary:
                    tips, *_ = _context_state(
                        source,
                        calls,
                        parameters,
                        readings[:index],
                        context.direction,
                        context.headers_only,
                        conditions,
                        set(),
                        set(),
                    )
                    previous_contexts.append(
                        replace(
                            context,
                            entities=tuple(tips.values()),
                            dispatch_chains=_dispatch(source, readings[:index], tips, throws),
                        )
                    )
            resolved.append(
                resolve_handler_bindings(
                    reading,
                    source,
                    preliminary,
                    descriptors.get(
                        reading.layer_id,
                        LayerDescriptor(
                            reading.layer_id, 1, reading.layer_id, reading.source.path, None, ""
                        ),
                    ),
                    _trusted(source),
                    previous_contexts,
                    tuple(routine for earlier in readings[:index] for routine in earlier.routines),
                )
            )
            prior_contexts.append(previous_contexts)
        # Прежний обработчик нижнего слоя остаётся действующим через доказанный вызов.
        # Разбираем его на пути вызывающего события; каскад обёрток конечен по слоям.
        followed = set()
        for _ in readings:
            calls_to_previous = tuple(call for item in resolved for call in item.previous_calls)
            pending = {call.target_id for call in calls_to_previous} - followed
            affected = {
                index
                for index, item in enumerate(resolved)
                if any(r.entity_id in pending for r in item.routines)
            }
            if not affected:
                break
            followed.update(pending)
            for index in sorted(affected):
                reading = resolved[index]
                resolved[index] = resolve_handler_bindings(
                    reading,
                    source,
                    preliminary,
                    descriptors.get(
                        reading.layer_id,
                        LayerDescriptor(
                            reading.layer_id,
                            index + 1,
                            reading.layer_id,
                            reading.source.path,
                            None,
                            "",
                        ),
                    ),
                    _trusted(source),
                    prior_contexts[index],
                    tuple(routine for earlier in readings[:index] for routine in earlier.routines),
                    calls_to_previous,
                )
        readings = tuple(resolved)
    for direction in ("send", "receive"):
        for headers_only in header_flags:
            tips, history, taints, references, faults = _context_state(
                source,
                calls,
                parameters,
                readings,
                direction,
                headers_only,
                conditions,
                active,
                live_skips,
            )
            tips = {
                key: replace(
                    value,
                    payload=_with_handler_hooks(value.payload, source, readings),
                    direction=direction,
                    headers_only=headers_only,
                )
                for key, value in tips.items()
            }
            history = [
                replace(item, direction=direction, headers_only=headers_only) for item in history
            ]
            fault_skips.extend(faults)
            revisions.extend(history)
            contexts.append(
                EffectiveContext(
                    direction,
                    headers_only,
                    None,
                    manager_name,
                    manager_layer_id,
                    tuple(tips.values()),
                    (),
                    _dispatch(source, readings, tips, throws),
                    references,
                    tuple(sorted(taints)),
                )
            )
    operations = tuple(
        op
        for reading in readings
        for op in reading.operations
        if op.resolution != "observe"
        and (
            (id(reading), op.id) in active
            or op.kind in (OperationKind.MAP_INSERT, OperationKind.DISPATCH)
        )
    )
    hooks = tuple(hook for reading in readings for hook in reading.hooks)
    all_skips = (
        tuple(skips)
        + tuple(
            skip
            for reading in readings
            for skip in reading.skips
            if skip.operation_offset is None
            or any(
                (id(skip), context.direction, context.headers_only) in live_skips
                for context in contexts
            )
        )
        + tuple(_unique_faults(fault_skips))
    )
    _taint_from_skips(contexts, all_skips, live_skips)
    unknown = any(
        op.kind == OperationKind.UNKNOWN or op.resolution == "unknown" for op in operations
    )
    uncertain = any(
        version.certainty == Certainty.UNKNOWN
        for context in contexts
        for version in context.entities
    )
    tainted = any(context.taints for context in contexts)
    status = (
        LayerStatus.PARTIAL
        if all_skips or unknown or uncertain or tainted
        else LayerStatus.COMPLETE
    )
    extra = tuple(reading.source for reading in readings)
    own_files = tuple(doc.files[0] for doc, _item in (own or {}).values() if doc.files)
    files = _unique_files((*base.files, *source.files, *extra, *own_files))
    routines = tuple(
        (reading.layer_id, routine) for reading in readings for routine in reading.routines
    )
    coverage = tuple((reading.source.file_id, reading.coverage) for reading in readings)
    from .layer_address import build_layer_addresses

    layered = LayeredManager(
        base,
        layers,
        hooks,
        operations,
        tuple(revisions),
        tuple(contexts),
        map_entries,
        files,
        coverage,
        routines,
        all_skips,
        status,
        source,
        tuple(readings),
        tuple(
            (name, layer.id, doc.files[0])
            for name, (doc, layer) in (own or {}).items()
            if doc.files
            and name.casefold() != manager_name.casefold()
            and not any(
                entry.state == "effective"
                and (entry.manager_name or "").casefold() == name.casefold()
                for entry in map_entries
            )
        ),
    )
    index = build_layer_addresses(layered)
    return replace(
        layered,
        contexts=tuple(
            replace(context, addresses=tuple(index.addresses_for(context)))
            for context in layered.contexts
        ),
    )


def _route_only(reading: ExtensionReading) -> bool:
    hooks_ok = all(hook.target_name.casefold() in _ROUTE for hook in reading.hooks)
    ops_ok = all(op.kind == OperationKind.MAP_INSERT for op in reading.operations)
    return hooks_ok and ops_ok


def _with_handler_hooks(payload, source, readings):
    """Перехват типового тела — факт привязки, не изменение состава правил."""
    if not isinstance(payload, (ObjectRule, ProcessingRule)):
        return payload
    events = []
    routines = {r.entity_id: r for r in source.routines}
    for binding in payload.events:
        target = routines.get(binding.target_id or "")
        if target is None:
            case = next(
                (c for c in source.dispatcher_cases if c.literal_name == binding.target_name), None
            )
            name = (
                case.target.reference_parts[0]
                if case and case.target.reference_parts
                else binding.target_name
            )
            target = next(
                (r for r in source.routines if r.name.casefold() == name.casefold()), None
            )
        reached = {target.entity_id} if target else set()
        reached.update(
            call.target_id
            for reading in readings
            for call in reading.previous_calls
            if call.rule_id == payload.entity_id and call.event == binding.event
        )
        changes = tuple(
            HandlerBodyChange(hook.kind, hook.target_name, hook.origin)
            for reading in readings
            for hook in reading.hooks
            if hook.target_class == "handler"
            and hook.applicability == "known"
            and any(
                routines[rid].name.casefold() == hook.target_name.casefold()
                for rid in reached
                if rid in routines
            )
        )
        if changes:
            from dataclasses import fields

            binding = LayerHandlerBinding(
                **{f.name: getattr(binding, f.name) for f in fields(HandlerBinding)},
                body_changes=changes,
            )
        events.append(binding)
    return replace(payload, events=tuple(events))


def _context_state(
    source, calls, parameters, readings, direction, headers_only, conditions, active, live_skips
):
    tips: dict[str, EntityVersion] = {}
    history: list[EntityVersion] = []
    index: dict[str, list[str]] = defaultdict(list)
    taints: set[str] = set()
    faults: list[LayerSkip] = []
    snapshots: dict[str, bool | None] = {}
    references: list[RuleUse] = []
    for collection, rules in (("pko", source.pko), ("pod", source.pod)):
        for rule in rules:
            matched = [
                call
                for call in calls
                if call.collection == collection
                and call.name.casefold() == rule.procedure_name.casefold()
            ]
            path = _file_path(source, rule.span.file_id)
            if not matched:
                _seed(rule, collection, Certainty.UNKNOWN, tips, history, index, path)
                faults.append(
                    LayerSkip(
                        "call_not_found",
                        Origin(
                            "base",
                            "CommonModule",
                            "",
                            rule.span.file_id,
                            rule.span,
                            None,
                            (),
                            None,
                            path,
                        ),
                        (rule.entity_id,),
                        rule.procedure_name,
                        (collection,),
                        (
                            Pred.atom("direction_is", direction),
                            Pred.atom("headers" if headers_only else "not_headers"),
                        ),
                    )
                )
                continue
            decision = holds(
                Pred("or", kids=tuple(call.pred for call in matched)),
                direction,
                headers_only,
                index,
                tips,
                snapshots,
            )
            if decision is False:
                continue
            certainty = Certainty.KNOWN if decision is True else Certainty.UNKNOWN
            payload = rule
            if collection == "pko":
                payload, _uncertain = _filter_properties(
                    rule, direction, headers_only, conditions, snapshots
                )
                # Непрозрачное условие исходной ПКС не меняет определённость
                # самого ПКО. Проверки членов учитывают исходные guards отдельно.
            if certainty != Certainty.KNOWN:
                faults.append(
                    LayerSkip(
                        "conditional_call",
                        Origin(
                            "base",
                            "CommonModule",
                            "",
                            rule.span.file_id,
                            rule.span,
                            None,
                            (),
                            None,
                            path,
                        ),
                        (rule.entity_id,),
                        rule.procedure_name,
                        (collection,),
                        (
                            Pred.atom("direction_is", direction),
                            Pred.atom("headers" if headers_only else "not_headers"),
                        ),
                    )
                )
            _seed(payload, collection, certainty, tips, history, index, path)
            references.append(
                RuleUse(
                    entity_id=f"use:{rule.entity_id}:{direction}:{int(headers_only)}",
                    kind="use",
                    name=rule.name,
                    span=rule.span,
                    raw_text=rule.raw_text,
                    rule_id=rule.entity_id,
                    target_name=rule.procedure_name,
                    direction=direction if decision is True else None,
                )
            )
    for rule in source.pkpd:
        _seed(
            rule,
            "pkpd",
            Certainty.KNOWN,
            tips,
            history,
            index,
            _file_path(source, rule.span.file_id),
        )
    for parameter in parameters:
        _seed(
            parameter,
            "parameters",
            Certainty.KNOWN,
            tips,
            history,
            index,
            _file_path(source, parameter.span.file_id),
        )
    for call in calls:
        if (
            call.effect == "call"
            or holds(call.pred, direction, headers_only, index, tips, snapshots) is False
        ):
            continue
        if call.effect == "unknown_call" and any(
            item.reason == "external_rule_call" and item.span.char_start == call.span.char_start
            for item in source.unknown
        ):
            # Непрочитанный вызов уже учтён базовым читателем. Слой не обещает
            # определённость его действий и не загрязняет повторно базовые правила.
            continue
        origin = Origin(
            "base",
            "CommonModule",
            "",
            call.span.file_id,
            call.span,
            None,
            path=_file_path(source, call.span.file_id),
        )
        ref = "filler" if call.effect == "filler_error" else call.collection
        faults.append(
            LayerSkip(
                call.effect,
                origin,
                (ref,),
                source.files[0].text[call.span.char_start : call.span.char_end],
                (ref,),
                (
                    Pred.atom("direction_is", direction),
                    Pred.atom("headers" if headers_only else "not_headers"),
                ),
            )
        )
        _unknown(
            LayerOperation(
                "base:unknown",
                OperationKind.UNKNOWN,
                ref,
                (),
                None,
                origin,
                (),
                Footprint("filler" if ref == "filler" else "collection", ref),
                "unknown",
            ),
            tips,
            taints,
            history,
            index,
        )
    for reading in readings:
        duplicated = {
            hook.id
            for hook in reading.hooks
            if sum(
                item.target_name.casefold() == hook.target_name.casefold() for item in reading.hooks
            )
            > 1
        }
        skip_events = defaultdict(list)
        for skip in reading.skips:
            if skip.operation_offset is not None:
                skip_events[skip.operation_offset].append(skip)
        for offset, operation in enumerate(reading.operations):
            _note_skip_paths(
                skip_events.get(offset, ()),
                direction,
                headers_only,
                index,
                tips,
                snapshots,
                live_skips,
            )
            if operation.resolution == "observe":
                _observe(operation, direction, headers_only, index, tips, snapshots)
                continue
            if operation.kind in (OperationKind.MAP_INSERT, OperationKind.DISPATCH):
                continue
            decision = holds(
                _join(operation.preds), direction, headers_only, index, tips, snapshots
            )
            certainty = (
                Certainty.CONDITIONAL if operation.hook_id in duplicated else Certainty.KNOWN
            )
            if decision is False:
                continue
            active.add((id(reading), operation.id))
            if operation.resolution == "filler_error":
                _filler_fault(operation, tips, taints, faults, direction, headers_only)
                continue
            if (
                decision is None
                or operation.resolution == "unknown"
                or operation.kind == OperationKind.UNKNOWN
            ):
                ref = operation.footprint.ref if operation.footprint else operation.target_ref
                explained = any(
                    holds(_join(skip.preds), direction, headers_only, index, tips, snapshots)
                    is not False
                    and any(
                        scope in (ref, ref.split(_UNIT)[0], "filler", "manager")
                        for scope in (*skip.affected_ids, *skip.check_scope)
                    )
                    for skip in reading.skips
                )
                if not explained:
                    faults.append(
                        LayerSkip(
                            "conditional_operation" if decision is None else "unknown_statement",
                            operation.origin,
                            (ref or "manager",),
                            "",
                            (ref or "manager",),
                            (
                                Pred.atom("direction_is", direction),
                                Pred.atom("headers" if headers_only else "not_headers"),
                            ),
                        )
                    )
                _unknown(operation, tips, taints, history, index)
                continue
            if (
                operation.kind == OperationKind.DELETE
                and operation.field_path[:1] == ("properties",)
                and len(operation.field_path) >= 4
            ):
                label, column, snapshot = operation.field_path[1:4]
                present = _property_state(
                    _UNIT.join((operation.target_ref, column, label)) + "\x1e" + snapshot,
                    tips,
                    False,
                    snapshots,
                )
                if present is None:
                    _unknown(operation, tips, taints, history, index)
                    continue
                if present is False:
                    _filler_fault(operation, tips, taints, faults, direction, headers_only)
                    continue
            if not _apply(operation, tips, history, index, certainty, snapshots):
                _filler_fault(operation, tips, taints, faults, direction, headers_only)
        _note_skip_paths(
            skip_events.get(len(reading.operations), ()),
            direction,
            headers_only,
            index,
            tips,
            snapshots,
            live_skips,
        )
    return tips, history, taints, tuple(references), tuple(faults)


def _note_skip_paths(skips, direction, headers_only, index, tips, snapshots, live_skips):
    for skip in skips:
        if holds(_join(skip.preds), direction, headers_only, index, tips, snapshots) is not False:
            live_skips.add((id(skip), direction, headers_only))


def _file_path(source: EdDocument, file_id: str) -> str:
    for item in source.files:
        if item.file_id == file_id:
            return item.path
    return source.files[0].path if source.files else ""


def _seed(entity, collection, certainty, tips, history, index, path: str) -> None:
    logical = entity.entity_id
    origin = Origin(
        "base", "CommonModule", "", entity.span.file_id, entity.span, None, (), None, path
    )
    version = EntityVersion(
        logical,
        f"base:{logical}",
        entity,
        EntityState.BASE,
        (),
        (f"base:{logical}",),
        (origin,),
        certainty,
        collection,
        "base",
    )
    tips[logical] = version
    history.append(version)
    _index_add(index, collection, entity.name, logical)


def _apply(operation, tips, history, index, certainty, snapshots) -> bool:
    if operation.kind == OperationKind.ADD and isinstance(
        operation.value, (ObjectRule, ProcessingRule, PredefinedRule)
    ):
        _add_rule(operation, tips, history, index, certainty)
    elif operation.kind == OperationKind.ADD and isinstance(
        operation.value, (PropertyRule, PropertyGroup)
    ):
        return _add_property(operation, tips, history, certainty)
    elif operation.kind == OperationKind.SET and operation.target_ref.startswith(
        "parameters" + _UNIT
    ):
        _set_parameter(operation, tips, history, index, certainty)
    elif operation.kind == OperationKind.SET:
        return _set_field(operation, tips, history, certainty, snapshots)
    elif operation.kind == OperationKind.DELETE and operation.field_path[:1] == ("properties",):
        return _delete_property(operation, tips, history, certainty, snapshots)
    elif operation.kind == OperationKind.DELETE:
        return _delete_named(operation, tips, history, index, certainty)
    elif operation.kind == OperationKind.INIT_EXTENSION and isinstance(operation.value, Expr):
        _extend(operation, tips, history, certainty)
    return True


def _add_rule(operation, tips, history, index, certainty) -> None:
    entity = operation.value
    collection, _column, name = _split(operation.target_ref)
    # ID декларации уже различает файлы слоёв; второй префикс не нужен.
    logical = entity.entity_id
    named = replace(entity, name=name or entity.name)
    _commit(
        logical,
        named,
        EntityState.ADDED,
        FieldChange((), None, (named,), operation.origin, operation.id),
        operation,
        tips,
        history,
        collection,
        certainty,
    )
    _index_add(index, collection, named.name, logical)


def _add_property(operation, tips, history, certainty) -> bool:
    found = _tip_ref(tips, operation.target_ref)
    if found is None:
        return False
    tip = tips[found]
    if not isinstance(tip.payload, ObjectRule):
        return False
    prop = replace(operation.value, owner_id=tip.payload.entity_id)
    if isinstance(prop, PropertyGroup):
        previous = tip.payload.groups
        current = (*previous, prop)
        updated = replace(tip.payload, groups=current)
        member = "groups"
    else:
        previous = tip.payload.properties
        current = (*previous, prop)
        updated = replace(tip.payload, properties=current)
        member = "properties"
    label = prop.format_property or prop.configuration_property
    _commit(
        found,
        updated,
        EntityState.CHANGED if tip.state != EntityState.ADDED else EntityState.ADDED,
        FieldChange(
            (member, label),
            previous,
            current,
            operation.origin,
            operation.id,
        ),
        operation,
        tips,
        history,
        "pko",
        certainty,
    )
    return True


def _set_field(operation, tips, history, certainty, snapshots=None) -> bool:
    found = _tip_ref(tips, operation.target_ref)
    if found is None or not isinstance(operation.value, Expr) or not operation.field_path:
        return False
    tip = tips[found]
    payload = tip.payload
    path = operation.field_path
    updated = None
    if path[0] == "properties" and isinstance(payload, ObjectRule) and len(path) >= 3:
        row_id = (
            snapshots.get("row:" + path[4]) if snapshots is not None and len(path) >= 5 else None
        )
        updated = _set_property_attr(payload, path[1], path[2], operation.value, row_id)
    elif isinstance(payload, (ObjectRule, ProcessingRule, PredefinedRule)):
        updated = _set_rule_attr(payload, path[0], operation.value)
    if updated is None:
        return False
    state = EntityState.CHANGED if tip.state != EntityState.ADDED else EntityState.ADDED
    rank = Certainty.CONDITIONAL if operation.resolution == "unknown" else certainty
    _commit(
        found,
        updated,
        state,
        FieldChange(
            path, _snapshot(payload, path), _snapshot(updated, path), operation.origin, operation.id
        ),
        operation,
        tips,
        history,
        tip.collection,
        rank,
    )
    return True


def _set_parameter(operation, tips, history, index, certainty) -> None:
    if not isinstance(operation.value, Expr):
        return
    _collection, _column, name = _split(operation.target_ref)
    found = [
        item
        for item in index.get(operation.target_ref, [])
        if tips[item].state != EntityState.DELETED
    ]
    if len(found) == 1:
        tip = tips[found[0]]
        assert isinstance(tip.payload, Parameter)
        updated = replace(tip.payload, default=operation.value, default_source="explicit")
        _commit(
            found[0],
            updated,
            EntityState.CHANGED,
            FieldChange(
                ("default",),
                Field("literal", _literal(tip.payload.default), ()),
                Field("literal", _literal(operation.value), (operation.value,)),
                operation.origin,
                operation.id,
            ),
            operation,
            tips,
            history,
            "parameters",
            certainty,
        )
        return
    if found:
        return
    span = operation.origin.span
    created = Parameter(
        entity_id=f"{operation.origin.file_id}:parameter:{span.char_start}",
        kind="parameter",
        name=name,
        span=span,
        raw_text=operation.value.raw,
        default=operation.value,
        default_source="explicit",
    )
    logical = f"{operation.origin.layer_id}:{created.entity_id}"
    _commit(
        logical,
        created,
        EntityState.ADDED,
        FieldChange(
            ("default",),
            None,
            Field("literal", _literal(operation.value), (operation.value,)),
            operation.origin,
            operation.id,
        ),
        operation,
        tips,
        history,
        "parameters",
        certainty,
    )
    index[operation.target_ref].append(logical)


def _delete_named(operation, tips, history, index, certainty) -> bool:
    found = [
        item
        for item in index.get(operation.target_ref, [])
        if tips[item].state != EntityState.DELETED
    ]
    if len(found) != 1:
        return not found and operation.target_ref.startswith("parameters" + _UNIT)
    tip = tips[found[0]]
    _commit(
        found[0],
        tip.payload,
        EntityState.DELETED,
        FieldChange((), tip.payload, None, operation.origin, operation.id),
        operation,
        tips,
        history,
        tip.collection,
        certainty,
    )
    return True


def _delete_property(operation, tips, history, certainty, snapshots) -> bool:
    found = _tip_ref(tips, operation.target_ref)
    if found is None or len(operation.field_path) < 2:
        return False
    tip = tips[found]
    if not isinstance(tip.payload, ObjectRule):
        return False
    label = operation.field_path[1]
    row_id = (
        snapshots.get("row:" + operation.field_path[3]) if len(operation.field_path) >= 4 else None
    )
    matched = (
        [prop for prop in tip.payload.properties if prop.entity_id == row_id]
        if row_id
        else [
            prop
            for prop in tip.payload.properties
            if (prop.format_property or prop.configuration_property) == label
        ]
    )
    if len(matched) != 1:
        return False
    label = matched[0].format_property or matched[0].configuration_property
    kept = tuple(prop for prop in tip.payload.properties if prop.entity_id != matched[0].entity_id)
    updated = replace(tip.payload, properties=kept)
    _commit(
        found,
        updated,
        EntityState.CHANGED,
        FieldChange(
            ("properties", label), tip.payload.properties, kept, operation.origin, operation.id
        ),
        operation,
        tips,
        history,
        "pko",
        certainty,
    )
    return True


def _extend(operation, tips, history, certainty) -> None:
    found = _tip_ref(tips, operation.target_ref)
    if found is None or not isinstance(operation.value, Expr):
        return
    tip = tips[found]
    if not isinstance(tip.payload, ObjectRule) or operation.value.literal_type != "string":
        return
    uri = str(operation.value.literal_value)
    updated = replace(tip.payload, extensions=(*tip.payload.extensions, uri))
    _commit(
        found,
        updated,
        EntityState.CHANGED,
        FieldChange(
            ("extensions",),
            tip.payload.extensions,
            updated.extensions,
            operation.origin,
            operation.id,
        ),
        operation,
        tips,
        history,
        "pko",
        certainty,
    )


def _set_rule_attr(payload, field_name: str, expression: Expr):
    if field_name.casefold() in _EVENT_ATTR and isinstance(payload, (ObjectRule, ProcessingRule)):
        canonical = _EVENT_ATTR[field_name.casefold()]
        if expression.literal_type != "string":
            return None
        binding = HandlerBinding(
            entity_id=f"{expression.span.file_id}:binding:{expression.span.char_start}",
            kind="binding",
            name=canonical,
            span=expression.span,
            raw_text=expression.raw,
            owner_id=payload.entity_id,
            event=canonical,
            target_name=str(expression.literal_value),
        )
        events = tuple(
            item for item in payload.events if item.event.casefold() != canonical.casefold()
        )
        return replace(payload, events=(*events, binding))
    table = (
        _PKO_ATTR
        if isinstance(payload, ObjectRule)
        else _POD_ATTR
        if isinstance(payload, ProcessingRule)
        else _PKPD_ATTR
    )
    attr = table.get(field_name)
    if attr is None:
        return None
    value = expression.literal_value if expression.literal_type else expression
    presence = "literal" if expression.literal_type else "expression"
    return replace(
        payload,
        **{attr: Field(presence, None if presence == "expression" else value, (expression,))},
    )


def _set_property_attr(
    payload: ObjectRule, label: str, attr: str, expression: Expr, row_id: str | None = None
) -> ObjectRule | None:
    if attr not in _PROP_ATTR:
        return None
    props: list[PropertyRule] = []
    found = False
    for prop in payload.properties:
        if (
            prop.entity_id != row_id
            if row_id
            else (prop.format_property or prop.configuration_property) != label
        ):
            props.append(prop)
            continue
        found = True
        if attr == "algorithm_flag" and expression.literal_type == "number":
            props.append(replace(prop, algorithm_flag=int(expression.literal_value or 0)))
        elif expression.literal_type == "string":
            props.append(replace(prop, **{attr: expression.literal_value}))
        else:
            props.append(prop)
    if not found:
        return None
    return replace(payload, properties=tuple(props))


def _snapshot(payload, path: tuple[str, ...]):
    if path and path[0] == "properties" and isinstance(payload, ObjectRule):
        return payload.properties
    table = (
        _PKO_ATTR
        if isinstance(payload, ObjectRule)
        else _POD_ATTR
        if isinstance(payload, ProcessingRule)
        else _PKPD_ATTR
    )
    attr = table.get(path[0]) if path else None
    if attr and hasattr(payload, attr):
        value = getattr(payload, attr)
        return value if isinstance(value, Field) else None
    return None


def _commit(
    logical, payload, state, change, operation, tips, history, collection, certainty
) -> None:
    prev = tips.get(logical)
    layer_id = operation.origin.layer_id
    if prev and prev.layer_id == layer_id and prev.state != EntityState.BASE:
        merged = replace(
            prev,
            payload=payload,
            state=state,
            changes=(*prev.changes, change),
            origins=(*prev.origins, operation.origin),
            certainty=_worse(prev.certainty, certainty),
        )
        tips[logical] = merged
        for index, item in enumerate(history):
            if item.revision_id == merged.revision_id:
                history[index] = merged
        return
    revision = f"{logical}:revision:{layer_id}"
    past = () if prev is None else prev.history
    origins = (operation.origin,) if prev is None else (*prev.origins, operation.origin)
    version = EntityVersion(
        logical,
        revision,
        payload,
        state,
        (change,),
        (*past, revision),
        origins,
        _worse(prev.certainty, certainty) if prev is not None else certainty,
        collection,
        layer_id,
    )
    tips[logical] = version
    history.append(version)


def _unknown(operation, tips, taints, history, index) -> None:
    footprint = operation.footprint or Footprint(
        "entity" if _UNIT in operation.target_ref else "collection",
        operation.target_ref or "manager",
    )
    if footprint.scope == "field":
        _set_field(operation, tips, history, Certainty.CONDITIONAL)
        return
    ref = footprint.ref or footprint.scope
    taints.add(ref)
    if footprint.scope in ("manager", "filler") or ref in ("manager", "filler"):
        for logical, tip in list(tips.items()):
            tips[logical] = replace(tip, certainty=Certainty.UNKNOWN)
        return
    if footprint.scope == "procedure":
        for logical, tip in list(tips.items()):
            payload = tip.payload
            procedure = getattr(payload, "procedure_name", "")
            if procedure.casefold() == ref.casefold():
                tips[logical] = replace(tip, certainty=Certainty.UNKNOWN)
        return
    if footprint.scope == "entity":
        for logical, tip in list(tips.items()):
            if _entity_ref(tip) == ref:
                tips[logical] = replace(tip, certainty=Certainty.UNKNOWN)
        return
    for logical, tip in list(tips.items()):
        if tip.collection == ref:
            tips[logical] = replace(tip, certainty=Certainty.UNKNOWN)
    _ = index


def _tip_ref(tips: dict[str, EntityVersion], ref: str) -> str | None:
    found = [
        logical
        for logical, tip in tips.items()
        if tip.state != EntityState.DELETED and _entity_ref(tip) == ref
    ]
    if len(found) == 1:
        return found[0]
    return None


def _entity_ref(tip: EntityVersion) -> str:
    payload = tip.payload
    if isinstance(payload, ObjectRule):
        return _ref("pko", "ИмяПКО", payload.name)
    if isinstance(payload, ProcessingRule):
        return _ref("pod", "Имя", payload.name)
    if isinstance(payload, PredefinedRule):
        return _ref("pkpd", "ИмяПКПД", payload.name)
    if isinstance(payload, Parameter):
        return _ref("parameters", "Имя", payload.name)
    return ""


def _index_add(index, collection: str, name: str, logical: str) -> None:
    column = {"pko": "ИмяПКО", "pod": "Имя", "pkpd": "ИмяПКПД", "parameters": "Имя"}.get(collection)
    if column:
        index[_ref(collection, column, name)].append(logical)


def _ref(collection: str, column: str, name: str) -> str:
    return _UNIT.join((collection, column, name))


def _split(ref: str) -> tuple[str, str, str]:
    collection, column, name = ref.split(_UNIT, 2)
    return collection, column, name


def _join(preds: tuple[Pred, ...]) -> Pred:
    return Pred("and", kids=preds)


def holds(
    pred: Pred, direction: str, headers_only: bool, index, tips, snapshots=None
) -> bool | None:
    if pred.op == "uncertain":
        return holds(pred.kids[0], direction, headers_only, index, tips, snapshots) is None
    if pred.op == "opaque":
        return None
    if pred.op == "and":
        values = [holds(kid, direction, headers_only, index, tips, snapshots) for kid in pred.kids]
        if any(value is False for value in values):
            return False
        if any(value is None for value in values):
            return None
        return True
    if pred.op == "or":
        values = [holds(kid, direction, headers_only, index, tips, snapshots) for kid in pred.kids]
        if any(value is True for value in values):
            return True
        if any(value is None for value in values):
            return None
        return False
    if pred.op == "not" and pred.kids:
        inner = holds(pred.kids[0], direction, headers_only, index, tips, snapshots)
        return None if inner is None else not inner
    if pred.kind == "direction_is":
        return direction == pred.arg
    if pred.kind == "headers":
        return headers_only
    if pred.kind == "not_headers":
        return not headers_only
    if pred.kind == "collection_nonempty":
        items = [
            item
            for item in tips.values()
            if item.collection == pred.arg and item.state != EntityState.DELETED
        ]
        return None if any(item.certainty == Certainty.UNKNOWN for item in items) else bool(items)
    if pred.kind in ("rule_found", "rule_missing"):
        count = len(
            [
                item
                for item in index.get(pred.arg, [])
                if tips.get(item) is None or tips[item].state != EntityState.DELETED
            ]
        )
        if not tips:
            count = len(index.get(pred.arg, []))
        if count > 1:
            return None
        return count == 1 if pred.kind == "rule_found" else count == 0
    if pred.kind in ("property_absent", "property_present"):
        return _property_state(pred.arg, tips, pred.kind == "property_absent", snapshots)
    return None


def _property_state(
    arg: str, tips: dict[str, EntityVersion], absent: bool, snapshots=None
) -> bool | None:
    snapshot = ""
    if "\x1e" in arg:
        arg, snapshot = arg.rsplit("\x1e", 1)
    if snapshots is not None and snapshot and snapshot in snapshots:
        stored = snapshots[snapshot]
        if stored is None:
            return None
        return stored if absent else not stored
    rule_key, column, literal = arg.rsplit(_UNIT, 2)
    found = _tip_ref(tips, rule_key)
    if found is None:
        return absent
    payload = tips[found].payload
    if not isinstance(payload, ObjectRule):
        return None
    matched = [
        prop
        for prop in payload.properties
        if (column == "СвойствоФормата" and prop.format_property == literal)
        or (column == "СвойствоКонфигурации" and prop.configuration_property == literal)
    ]
    if len(matched) > 1:
        return None
    present = len(matched) == 1
    return (not present) if absent else present


def _observe(operation, direction, headers_only, index, tips, snapshots) -> None:
    decision = holds(_join(operation.preds), direction, headers_only, index, tips, snapshots)
    if decision is False:
        return
    arg = operation.target_ref
    if "\x1e" not in arg:
        return
    body, snapshot = arg.rsplit("\x1e", 1)
    if decision is None:
        snapshots[snapshot] = None
        return
    snapshots[snapshot] = _property_state(body, tips, True, None)
    parts = body.split(_UNIT)
    if len(parts) == 5:
        ref, column, literal = _UNIT.join(parts[:3]), parts[3], parts[4]
        owner = _tip_ref(tips, ref)
        payload = tips[owner].payload if owner is not None else None
        if isinstance(payload, ObjectRule):
            matched = [
                prop
                for prop in payload.properties
                if (
                    prop.format_property
                    if column == "СвойствоФормата"
                    else prop.configuration_property
                )
                == literal
            ]
            snapshots["row:" + snapshot] = matched[0].entity_id if len(matched) == 1 else None


def _filter_properties(rule, direction, headers_only, conditions, snapshots):
    kept = []
    uncertain = False
    for prop in rule.properties:
        pred = conditions.get(prop.span.char_start)
        if pred is None:
            kept.append(prop)
            continue
        decision = holds(pred, direction, headers_only, {}, {}, snapshots)
        if decision is False:
            continue
        kept.append(prop)
        if decision is None:
            uncertain = True
    if len(kept) == len(rule.properties):
        return rule, uncertain
    return replace(rule, properties=tuple(kept)), uncertain


def _filler_fault(operation, tips, taints, faults, direction, headers_only) -> None:
    faults.append(
        LayerSkip(
            "filler_error",
            operation.origin,
            ("filler",),
            operation.origin.path,
            ("filler",),
            (
                Pred.atom("direction_is", direction),
                Pred.atom("headers" if headers_only else "not_headers"),
            ),
        )
    )
    _unknown(
        replace(operation, footprint=Footprint("filler", "filler"), kind=OperationKind.UNKNOWN),
        tips,
        taints,
        [],
        {},
    )


def _unique_faults(faults: list[LayerSkip]) -> list[LayerSkip]:
    seen: set[tuple[str, str, int, tuple[Pred, ...]]] = set()
    result: list[LayerSkip] = []
    for skip in faults:
        key = (skip.reason, skip.origin.span.file_id, skip.origin.span.char_start, skip.preds)
        if key in seen:
            continue
        seen.add(key)
        result.append(skip)
    return result


def _taint_from_skips(
    contexts: list[EffectiveContext],
    skips: tuple[LayerSkip, ...],
    live_skips: set[tuple[int, str, bool]] | None = None,
) -> None:
    for skip in skips:
        refs = skip.affected_ids
        for context in contexts:
            tips = {item.logical_id: item for item in context.entities}
            scopes = (
                refs
                if any(
                    "\x1f" in ref
                    or ref in tips
                    or any(
                        getattr(tip.payload, "procedure_name", "").casefold() == ref.casefold()
                        for tip in tips.values()
                    )
                    for ref in refs
                )
                else skip.check_scope
            )
            index = defaultdict(list)
            for tip in tips.values():
                index[_entity_ref(tip)].append(tip.logical_id)
            if skip.operation_offset is not None and live_skips is not None:
                if (id(skip), context.direction, context.headers_only) not in live_skips:
                    continue
            elif (
                holds(_join(skip.preds), context.direction, context.headers_only, index, tips)
                is False
            ):
                continue
            taints = set(context.taints)
            for scope in scopes:
                if skip.certainty == Certainty.UNKNOWN:
                    taints.add(scope)
                if scope in ("manager", "filler", "hook", "dispatcher"):
                    tips = {
                        key: replace(tip, certainty=_worse(tip.certainty, skip.certainty))
                        for key, tip in tips.items()
                    }
                elif scope in ("pko", "pod", "pkpd", "parameters"):
                    tips = {
                        key: replace(tip, certainty=_worse(tip.certainty, skip.certainty))
                        if tip.collection == scope
                        else tip
                        for key, tip in tips.items()
                    }
                else:
                    tips = {
                        key: replace(tip, certainty=_worse(tip.certainty, skip.certainty))
                        if key == scope
                        or _entity_ref(tip) == scope
                        or getattr(tip.payload, "procedure_name", "").casefold() == scope.casefold()
                        else tip
                        for key, tip in tips.items()
                    }
            context_index = contexts.index(context)
            contexts[context_index] = replace(
                context,
                entities=tuple(tips[item.logical_id] for item in context.entities),
                taints=tuple(sorted(taints)),
            )


def _worse(left: str, right: str) -> str:
    order: dict[str, int] = {"known": 0, "conditional": 1, "unknown": 2}
    return left if order[left] >= order[right] else right


def _literal(expression: Expr | None):
    if expression is None or expression.literal_type == "undefined":
        return None
    return expression.literal_value


def _parameters(document: EdDocument) -> tuple[Parameter, ...]:
    recovered = recover_parameters(document.files[0]) if document.files else ()
    names = {parameter.name for parameter in document.parameters}
    return tuple(document.parameters) + tuple(item for item in recovered if item.name not in names)


def _dispatch(source, readings, tips, throws: bool) -> tuple[DispatchChain, ...]:
    hooks = [
        hook
        for reading in readings
        for hook in reading.hooks
        if hook.target_name.casefold() in _DISPATCH
        and (hook.applicability == "known" or hook.target_class == "function_dispatcher")
    ]
    names = {
        (case.literal_name, "function" if case.returns else "procedure")
        for case in source.dispatcher_cases
    }
    for reading in readings:
        for hook in reading.hooks:
            names.update(
                (name, hook.routine.routine_kind) for name in handled_literals(reading, hook.id)
            )
    for tip in tips.values():
        if isinstance(tip.payload, (ObjectRule, ProcessingRule)):
            names.update(
                (
                    binding.target_name,
                    EVENT_INVOCATIONS[binding.event].kind
                    if binding.event in EVENT_INVOCATIONS
                    else "procedure",
                )
                for binding in tip.payload.events
            )
    chains: list[DispatchChain] = []
    unknown_by_kind = {
        kind: dispatcher_unknown(source, kind=kind) for kind in ("procedure", "function")
    }
    function_throws = False
    if any(kind == "function" for _, kind in names):
        routine = next(
            (r for r in source.routines if r.name.casefold() == "выполнитьфункциюмодуляменеджера"),
            None,
        )
        if routine:
            function_throws = dispatcher_throws(
                source_from_text(
                    routine.raw_text,
                    routine.span.file_id,
                    "dispatcher",
                    False,
                    routine.raw_text.encode("utf-8"),
                )
            )
    for name, kind in sorted(names):
        active_hooks = [hook for hook in hooks if hook.routine.routine_kind == kind]
        arounds = [hook for hook in active_hooks if hook.kind == "around"]
        afters = [hook for hook in active_hooks if hook.kind == "after"]
        befores = [hook for hook in active_hooks if hook.kind == "before"]
        base_unknown = unknown_by_kind[kind]
        if any(
            hook.kind == "change_control" or hook.continuation == "unknown" for hook in active_hooks
        ):
            resolution = "unknown"
        else:
            resolution = _resolve(
                name,
                arounds,
                afters,
                source,
                readings,
                throws if kind == "procedure" else function_throws,
                base_unknown,
                kind,
            )
        links = [DispatchLink(hook, None, hook.origin.layer_id, True) for hook in befores]
        links.extend(
            DispatchLink(
                hook,
                _case_for(readings, hook.id, name),
                hook.origin.layer_id,
                hook.continuation == "once",
            )
            for hook in arounds
        )
        links.extend(
            DispatchLink(None, case, "base", False)
            for case in source.dispatcher_cases
            if case.literal_name == name and case.returns == (kind == "function")
        )
        links.extend(
            DispatchLink(hook, _case_for(readings, hook.id, name), hook.origin.layer_id, True)
            for hook in afters
        )
        chains.append(DispatchChain(name, kind, tuple(links), resolution))
    return tuple(chains)


def _resolve(
    name, arounds, afters, source, readings, throws: bool, base_unknown: bool, kind: str
) -> str:
    def base_outcome() -> str:
        if any(
            case.literal_name == name and case.returns == (kind == "function")
            for case in source.dispatcher_cases
        ):
            return "call"
        if base_unknown:
            return "unknown"
        return "throws" if throws else "no_call"

    def around_outcome(index: int) -> str:
        if index < 0:
            return base_outcome()
        hook = arounds[index]
        handled = set()
        for reading in readings:
            handled.update(handled_literals(reading, hook.id))
        if name in handled:
            operation = next(
                (
                    op
                    for reading in readings
                    for op in reading.operations
                    if op.hook_id == hook.id
                    and op.kind == OperationKind.DISPATCH
                    and op.target_ref == name
                ),
                None,
            )
            return "unknown" if operation and operation.resolution != "applied" else "call"
        if hook.continuation == "once":
            return around_outcome(index - 1)
        if hook.continuation == "none":
            return "no_call"
        return "unknown"

    result = around_outcome(len(arounds) - 1)
    if result in {"throws", "unknown"}:
        return result
    for hook in afters:
        handled = set()
        for reading in readings:
            handled.update(handled_literals(reading, hook.id))
        if name in handled:
            return "call"
    return result


def _case_for(readings, hook_id: str, name: str):
    for reading in readings:
        for operation in reading.operations:
            if (
                operation.hook_id == hook_id
                and operation.kind == OperationKind.DISPATCH
                and operation.target_ref == name
                and isinstance(operation.value, DispatcherCase)
            ):
                return operation.value
    return None


def _layer(ordinal: int, dump: DumpInfo, path: Path) -> LayerDescriptor:
    name = dump.name or path.name
    identifier = "base" if ordinal == 0 else layer_key(ordinal, name)
    return LayerDescriptor(
        identifier, ordinal, name, str(path), dump.configuration_uuid, dump.fingerprint
    )


def _dump_origin(layer: LayerDescriptor, path: str | Path) -> Origin:
    return Origin(
        layer.id,
        "Configuration",
        layer.name,
        "configuration",
        SourceSpan("configuration", 1, 1, 0, 0),
        None,
        (),
        None,
        str(path),
    )


def _base_maps(dump: DumpInfo, layer: LayerDescriptor) -> list[MapEntry]:
    entries: list[MapEntry] = []
    for obj in dump.objects:
        if not obj.module_path:
            continue
        if obj.kind == "ExchangePlan":
            entries.extend(
                base_map(
                    load_source(Path(obj.module_path), obj.module_path),
                    "ПриПолученииНастроек",
                    "plan",
                    obj.name,
                    layer,
                    obj.kind,
                    obj.name,
                )
            )
        if obj.kind == "CommonModule" and obj.name.casefold() == "обменданнымипереопределяемый":
            entries.extend(
                base_map(
                    load_source(Path(obj.module_path), obj.module_path),
                    "ПриПолученииДоступныхВерсийФормата",
                    "without_node",
                    None,
                    layer,
                    obj.kind,
                    obj.name,
                )
            )
    return entries


def _choose_manager(
    explicit: str | None,
    version_key: str | None,
    entries: list[MapEntry],
    base: DumpInfo,
    own_names: list[str],
) -> tuple[str | None, tuple[str, ...]]:
    """Явное имя имеет приоритет перед картой версий."""

    def unique(names: list[str]) -> list[str]:
        folded: dict[str, str] = {}
        for name in names:
            folded.setdefault(name.casefold(), name)
        return list(folded.values())

    effective = [
        entry.manager_name for entry in entries if entry.state == "effective" and entry.manager_name
    ]
    if explicit:
        found = _find_object(base, "CommonModule", explicit)
        if found and found.module_path and _looks_like_manager(Path(found.module_path)):
            return found.name, ()
        return None, ()
    fillers = [
        obj.name
        for obj in base.objects
        if obj.kind == "CommonModule"
        and obj.module_path
        and _looks_like_manager(Path(obj.module_path))
    ]
    if version_key:
        matching = [
            entry
            for entry in entries
            if entry.state == "effective" and entry.manager_name and entry.key == version_key
        ]
        plan_entries = [entry for entry in matching if entry.role == "plan"]
        keyed = [entry.manager_name for entry in (plan_entries or matching)]
        chosen = unique([name for name in keyed if name])
        if len(chosen) == 1:
            return chosen[0], ()
        return None, tuple(unique([name for name in effective if name]) or fillers or own_names)
    chosen = unique([name for name in effective if name])
    if len(chosen) == 1:
        return chosen[0], ()
    pool = chosen or fillers or own_names
    if not chosen and len(fillers) == 1:
        return fillers[0], ()
    return None, tuple(pool)


def _select_base_manager(
    entries: list[MapEntry], dumps: list[DumpInfo], base: DumpInfo
) -> tuple[str | None, DumpObject | None]:
    """Первый кандидат, чей модуль содержит заполнитель правил.

    Согласованное имя в карте версий имеет приоритет. Модуль без точки входа
    менеджера обмена не выбирается: дальше смотрится заимствованный менеджер
    расширения и единственный такой модуль базы.
    """
    seen: set[str] = set()
    for candidate in (
        _manager_from_maps(entries),
        _adopted_ed_manager(dumps, base),
        _sole_manager(base),
    ):
        if not candidate or candidate.casefold() in seen:
            continue
        seen.add(candidate.casefold())
        module = _find_object(base, "CommonModule", candidate)
        if module and module.module_path and _looks_like_manager(Path(module.module_path)):
            return candidate, module
    return None, None


def _manager_from_maps(entries: list[MapEntry] | tuple[MapEntry, ...]) -> str | None:
    names = {
        entry.manager_name for entry in entries if entry.state == "effective" and entry.manager_name
    }
    if len(names) == 1:
        return next(iter(names))
    return None


def _adopted_ed_manager(dumps: list[DumpInfo], base: DumpInfo) -> str | None:
    names: list[str] = []
    for dump in dumps:
        for obj in dump.objects:
            if (
                obj.kind != "CommonModule"
                or not obj.belonging
                or obj.belonging.casefold() != "adopted"
            ):
                continue
            found = _find_object(base, "CommonModule", obj.name)
            if found and found.module_path and _looks_like_manager(Path(found.module_path)):
                names.append(found.name)
    unique = set(names)
    if len(unique) == 1:
        return next(iter(unique))
    return None


def _sole_manager(dump: DumpInfo) -> str | None:
    found = [
        obj.name
        for obj in dump.objects
        if obj.kind == "CommonModule"
        and obj.module_path
        and _looks_like_manager(Path(obj.module_path))
    ]
    if len(found) == 1:
        return found[0]
    return None


def _looks_like_manager(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return False
    if "заполнитьправилаконвертацииобъектов" not in text.casefold():
        return False
    try:
        statements = lex(load_source(path, str(path))).statements
    except EdFormatError:
        return False
    return any(
        statement.head == "процедура"
        and len(statement.tokens) > 1
        and statement.tokens[1].folded == "заполнитьправилаконвертацииобъектов"
        for statement in statements
    )


def _find_object(dump: DumpInfo, kind: str, name: str | None) -> DumpObject | None:
    if not name:
        return None
    for obj in dump.objects:
        if obj.kind == kind and obj.name.casefold() == name.casefold():
            return obj
    return None


def _usable(obj: DumpObject, base_objects, layer: LayerDescriptor, skips: list[LayerSkip]) -> bool:
    if obj.belonging and obj.belonging.casefold() == "adopted":
        base = base_objects.get((obj.kind, obj.name.casefold()))
        extended = obj.extended_uuid
        base_uuid = base.uuid if base is not None else None
        mismatch = base is None or (
            extended is not None
            and base_uuid is not None
            and extended.casefold() != base_uuid.casefold()
        )
        if mismatch:
            skips.append(
                LayerSkip(
                    "identity_mismatch",
                    _dump_origin(layer, obj.xml_path or obj.name),
                    (f"{obj.kind}/{obj.name}", obj.extended_uuid or ""),
                    f"{obj.name}:{obj.extended_uuid}",
                    (obj.kind,),
                )
            )
            return False
    if obj.module_extended and not obj.module_path:
        skips.append(
            LayerSkip(
                "missing_module",
                _dump_origin(layer, obj.xml_path or obj.name),
                (f"{obj.kind}/{obj.name}",),
                obj.name,
                (obj.kind,),
            )
        )
        return False
    return True


def _read_obj(obj, layer, version, helpers, targets, plan) -> ExtensionReading:
    return read_extension_file(
        Path(obj.module_path),
        layer=layer,
        metadata_kind=obj.kind,
        metadata_name=obj.name,
        version=version,
        helpers=helpers,
        targets=targets,
        plan_name=plan,
        file_id=str(Path(obj.module_path)),
    )


def _trusted(document: EdDocument) -> frozenset[str]:
    flagged = {
        (item.span.file_id, item.span.char_start)
        for item in document.diagnostics
        if item.code == "helper_semantics_unverified"
    }
    return frozenset(
        routine.name.casefold()
        for routine in document.routines
        if routine.name.casefold() in ("добавитьпкс", "добавитьпктч")
        and (routine.span.file_id, routine.span.char_start) not in flagged
    )


def _apply_maps(entries: list[MapEntry], readings: list[ExtensionReading]) -> list[MapEntry]:
    current = list(entries)
    for reading in readings:
        for operation in reading.operations:
            if operation.kind != OperationKind.MAP_INSERT or not isinstance(operation.value, Expr):
                continue
            if not operation.field_path or not operation.value.reference_parts:
                continue
            key = operation.field_path[0]
            role = "without_node" if operation.target_ref == "without_node" else "plan"
            plan = None if role == "without_node" else operation.target_ref.split("/", 1)[-1]
            module = operation.value.reference_parts[0]
            for index, entry in enumerate(current):
                same = entry.state == "effective" and entry.role == role and entry.key == key
                if same and (role == "without_node" or entry.plan_name == plan):
                    current[index] = replace(entry, state="overwritten")
            current.append(MapEntry(role, plan, key, module, "effective", operation.origin))
    return current


def _unique_files(files) -> tuple:
    seen: set[str] = set()
    result = []
    for item in files:
        if item.path in seen:
            continue
        seen.add(item.path)
        result.append(item)
    return tuple(result)


def _empty_document() -> EdDocument:
    span = SourceSpan("empty", 1, 1, 0, 0)
    conversion = Conversion(
        entity_id="empty:conversion", kind="conversion", name="", span=span, raw_text=""
    )
    coverage = Coverage((), (), 0, 0.0, 0.0)
    return EdDocument(
        (),
        conversion,
        None,
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        coverage,
        ParseStatus.PARTIAL,
    )


def _failed(layer: LayerDescriptor, skips: tuple[LayerSkip, ...]) -> LayeredManager:
    return LayeredManager(
        _empty_document(), (layer,), (), (), (), (), (), (), (), (), skips, LayerStatus.PARTIAL
    )
