"""Представления снимка слоя: контексты, история и адреса без исполнения BSL."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, cast

from lxml import etree

from kd2_rules_mcp import ed
from kd2_rules_mcp.ed.address import AddressIndex, build_addresses, escape_segment
from kd2_rules_mcp.ed.address import locate as locate_entities
from kd2_rules_mcp.ed.forms import EVENT_INVOCATIONS
from kd2_rules_mcp.ed.layer_address import (
    AmbiguousLayerAddress,
    LayerAddressHit,
    LayerAddressNotFound,
    build_layer_addresses,
)
from kd2_rules_mcp.ed.layer_model import EffectiveContext, EntityVersion, LayeredManager, Origin
from kd2_rules_mcp.ed.layer_reader import read_dump
from kd2_rules_mcp.ed.layers import compose_manager, read_layers
from kd2_rules_mcp.errors import AmbiguousAddressError, EdReadError, Kd2Error, RuleNotFoundError
from kd2_rules_mcp.service import ed_views as views
from kd2_rules_mcp.validation.ed_projection import (
    effective_document,
    event_direction,
    select_context,
)

KINDS = frozenset({"layer", "change", "hook", "layer_unknown"})


@dataclass(frozen=True)
class ContextView:
    context: EffectiveContext
    document: ed.EdDocument
    index: AddressIndex
    entities: dict[str, ed.Entity]
    versions: dict[str, EntityVersion]


@dataclass(frozen=True)
class ChangeEntry:
    version: EntityVersion
    address: str
    contexts: tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class LayerSnapshot:
    manager: LayeredManager
    key: tuple[str, ...]
    dependencies: tuple[tuple[Path, str | None], ...]
    contexts: tuple[ContextView, ...]
    changes: tuple[ChangeEntry, ...] = ()


def checked_dump(root: Path, required_module: str | None = None):
    """Отсутствующий файл — отказ сервиса; непонятная форма остаётся пропуском читателя."""
    dump = read_dump(root)
    expected = [root / "Configuration.xml"]
    for obj in dump.objects:
        folder = "CommonModules" if obj.kind == "CommonModule" else "ExchangePlans"
        expected.append(root / folder / f"{obj.name}.xml")
        if not obj.module_path and (
            obj.module_extended
            or (
                obj.kind == "CommonModule"
                and required_module is not None
                and obj.name.casefold() == required_module.casefold()
            )
        ):
            filename = "Module.bsl" if obj.kind == "CommonModule" else "ManagerModule.bsl"
            expected.append(root / folder / obj.name / "Ext" / filename)
    for path in expected:
        try:
            with path.open("rb"):
                pass
        except OSError as error:
            raise EdReadError(f"Файл выгрузки недоступен: {path}") from error
    return dump


def extension_paths(paths: list[str], resolve_path: Callable[[str], Path]) -> tuple[Path, ...]:
    """Недоступный вход называется расширением, а не неизвестным файлом проекта."""
    roots = []
    for path in paths:
        try:
            roots.append(resolve_path(path).resolve())
        except (Kd2Error, OSError) as error:
            raise EdReadError(
                f"Расширение недоступно: {path}; проверьте путь к выгрузке"
            ) from error
    return tuple(roots)


def checked_extensions(root: Path, extensions: tuple[Path, ...]) -> None:
    """Порядок явный, но повторные пути и одинаковые имена не задают два слоя."""
    paths = set()
    names = set()
    for folder in extensions:
        if folder == root:
            raise ValueError(
                "Основная выгрузка передана как расширение; исключите её из extensions"
            )
        if folder in paths:
            raise ValueError("Одно расширение перечислено дважды; оставьте каждый путь один раз")
        paths.add(folder)
        if not (folder / "Configuration.xml").is_file():
            raise EdReadError(f"Расширение недоступно: {folder}; нужен корень с Configuration.xml")
        dump = checked_dump(folder)
        if not dump.failed:
            configuration = etree.fromstring(
                (folder / "Configuration.xml").read_bytes(),
                etree.XMLParser(resolve_entities=False, no_network=True),
            )
            if (
                configuration.find(
                    ".//{*}Configuration/{*}Properties/{*}ConfigurationExtensionPurpose"
                )
                is None
            ):
                raise ValueError(
                    "Основная выгрузка передана как расширение; нужен корень выгрузки расширения"
                )
        name = (dump.name or folder.name).casefold()
        if name in names:
            raise ValueError(f"Два расширения имеют одинаковое имя: {dump.name}")
        names.add(name)


def portable_files(manager: LayeredManager) -> LayeredManager:
    """Пути остаются в SourceFile.path; ID и ссылки используют путь внутри слоя."""
    aliases = {}
    for source in manager.source_files:
        path = Path(source.path).resolve()
        for layer in manager.layers[1:]:
            root = Path(layer.root).resolve()
            if path.is_relative_to(root):
                aliases[source.file_id] = f"{layer.id}:{path.relative_to(root).as_posix()}"
                break
    if not aliases:
        return manager

    def rewrite(value: Any, field_name: str = "") -> Any:
        if isinstance(value, tuple):
            return tuple(rewrite(item, field_name) for item in value)
        if isinstance(value, str) and (
            field_name.endswith(("_id", "_ids"))
            or field_name
            in {"history", "addresses", "check_scope", "ref", "target_ref", "coverage", "taints"}
        ):
            for before, after in aliases.items():
                if escape_segment(before) != before:
                    value = value.replace(escape_segment(before), escape_segment(after))
                value = value.replace(before, after)
            return value
        if is_dataclass(value):
            return replace(
                cast(Any, value),
                **{
                    item.name: rewrite(getattr(value, item.name), item.name)
                    for item in fields(value)
                },
            )
        return value

    return rewrite(manager)


def entity_layer(manager: LayeredManager, entity: ed.Entity) -> str:
    """Принадлежность декларации определяется файлом, а не родительским ПКО."""
    return next(
        (
            layer.id
            for layer in manager.layers[1:]
            if entity.span.file_id.startswith(layer.id + ":")
        ),
        "base",
    )


def entity_signature(entity: ed.Entity) -> str:
    """Семантика строки без различающихся защит, владельца и результата разрешения."""
    data = views.entity_fields(entity)
    for name in ("owner_id", "target_id", "resolution"):
        data.pop(name, None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def member_version(
    manager: LayeredManager,
    entity: ed.Entity,
    parent: EntityVersion | None = None,
    originals: dict[str, ed.Entity] | None = None,
) -> EntityVersion:
    """Отдельная история члена: неизменённая ПКС не наследует правку своего ПКО."""
    source = manager.source_document or manager.base
    originals = originals if originals is not None else {e.entity_id: e for e in source.entities()}
    original = originals.get(entity.entity_id)
    if isinstance(entity, ed.HandlerBinding) and parent and parent.payload:
        owner = originals.get(entity.owner_id)
        original = (
            next((e for e in owner.events if e.event == entity.event), None)
            if isinstance(owner, (ed.ObjectRule, ed.ProcessingRule))
            else None
        )
    changes = []
    for change in parent.changes if parent else ():
        if isinstance(entity, ed.HandlerBinding) and change.path == (entity.event,):
            changes.append(change)
            continue
        before = change.before if isinstance(change.before, tuple) else ()
        after = change.after if isinstance(change.after, tuple) else ()
        old = next(
            (e for e in before if isinstance(e, ed.Entity) and e.entity_id == entity.entity_id),
            None,
        )
        new = next(
            (e for e in after if isinstance(e, ed.Entity) and e.entity_id == entity.entity_id), None
        )
        if new is not None and (old is None or entity_signature(old) != entity_signature(new)):
            changes.append(change)
    layer_id = changes[-1].origin.layer_id if changes else entity_layer(manager, entity)
    state = "base" if layer_id == "base" else "added" if original is None else "changed"
    if (
        original is not None
        and entity_signature(original) == entity_signature(entity)
        and not changes
    ):
        layer_id = entity_layer(manager, original)
        state = "base" if layer_id == "base" else "added"
    origin = Origin(
        entity_layer(manager, original or entity),
        "CommonModule",
        "",
        (original or entity).span.file_id,
        (original or entity).span,
        getattr(parent.payload, "procedure_name", None) if parent else None,
    )
    origins = tuple(dict.fromkeys((origin, *(change.origin for change in changes))))
    return EntityVersion(
        entity.entity_id,
        entity.entity_id,
        entity,
        state,
        tuple(changes),
        tuple(change.operation_id for change in changes),
        origins,
        parent.certainty if parent else "known",
        layer_id=layer_id,
        direction=parent.direction if parent else "",
        headers_only=parent.headers_only if parent else False,
    )


def resolve_binding(binding: ed.HandlerBinding, document: ed.EdDocument, context: EffectiveContext):
    """Привязка разрешается действующей цепочкой, исходная декларация не подменяется."""
    projected = next(
        (
            event
            for rule in (*document.pko, *document.pod)
            for event in rule.events
            if event.entity_id == binding.entity_id
        ),
        None,
    )
    if projected is not None:
        binding = projected
    if binding.resolution == "invalid_signature":
        return binding
    signature = EVENT_INVOCATIONS.get(binding.event)
    chain = next(
        (
            c
            for c in context.dispatch_chains
            if c.target_name == binding.target_name
            and c.kind == (signature.kind if signature else "procedure")
        ),
        None,
    )
    if chain is None:
        return binding
    if binding.target_id:
        return replace(
            binding,
            resolution=chain.resolution,
            target_id=binding.target_id if chain.resolution == "call" else None,
        )
    targets = [
        link.case.target.reference_parts[-1]
        for link in chain.links
        if link.case and link.case.target.reference_parts
    ]
    routine = next(
        (r for r in document.routines if r.name.casefold() in {t.casefold() for t in targets}), None
    )
    return replace(
        binding,
        resolution=chain.resolution,
        target_id=routine.entity_id if routine and chain.resolution == "call" else None,
    )


def module_path(root: Path, extensions: tuple[Path, ...], name: str) -> Path:
    """Точное имя объекта; собственный одноимённый модуль требует выбора пути."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Нужно точное имя общего модуля module")
    candidates = []
    for ordinal, folder in enumerate((root, *extensions)):
        for obj in checked_dump(folder).objects:
            if obj.kind != "CommonModule" or obj.name.casefold() != name.casefold():
                continue
            if ordinal and obj.belonging is not None:
                continue
            if obj.module_path:
                candidates.append(str(Path(obj.module_path).resolve()))
    if len(candidates) > 1:
        raise AmbiguousAddressError("Неоднозначный модуль менеджера", tuple(candidates))
    if not candidates:
        raise EdReadError(f"Модуль менеджера не найден: {name}")
    return Path(candidates[0])


def read_selected_layers(path: Path, root: Path, extensions: tuple[Path, ...]) -> LayeredManager:
    """Выбор по пути дополняет read_layers, который принимает только базовое имя."""
    dumps = [checked_dump(folder, path.parent.parent.name) for folder in (root, *extensions)]
    selected = [
        (ordinal, obj)
        for ordinal, dump in enumerate(dumps)
        for obj in dump.objects
        if obj.kind == "CommonModule"
        and obj.module_path
        and Path(obj.module_path).resolve() == path
    ]
    if len(selected) != 1:
        raise ValueError("path должен быть модулем выбранной конфигурации или расширения")
    ordinal, obj = selected[0]
    if ordinal and obj.belonging is not None:
        raise ValueError("Заимствованный файл не является полным менеджером; откройте основной")
    if not ordinal:
        return portable_files(read_layers(root, extensions, manager=obj.name))
    layered = read_layers(root, extensions)
    original = ed.read_manager(path).files[0]
    # read_manager даёт всем полным модулям file_id=module. У собственного менеджера
    # он должен отличаться от базового файла; повторно используем принятый читатель.
    source = ed.read_manager_text(
        ("\ufeff" if original.bom else "") + original.text, file_id=str(path), path=path
    )
    # Явный собственный менеджер не обязан быть единственным в карте версий.
    # Чужие операции заполнения к нему не применяются; маршрутные чтения сохраняются.
    route_readings = tuple(
        reading
        for reading in layered.readings
        if all(op.kind == "map_insert" for op in reading.operations)
        and all(
            hook.target_name.casefold()
            in {
                "приполучениинастроек",
                "приполучениидоступныхверсийформата",
                "приполучениидоступныхрасширенийформата",
            }
            for hook in reading.hooks
        )
    )
    result = compose_manager(
        layered.base,
        source,
        readings=route_readings,
        layers=layered.layers,
        map_entries=layered.map_entries,
        skips=tuple(skip for skip in layered.skipped if skip.reason != "ambiguous_manager"),
        manager_name=obj.name,
        manager_layer_id=layered.layers[ordinal].id,
    )
    owner = layered.layers[ordinal].id

    def owned(version: EntityVersion) -> EntityVersion:
        return replace(
            version,
            layer_id=owner if version.layer_id == "base" else version.layer_id,
            origins=tuple(
                replace(origin, layer_id=owner)
                if origin.layer_id == "base" and origin.file_id == str(path)
                else origin
                for origin in version.origins
            ),
        )

    return portable_files(
        replace(
            result,
            revisions=tuple(owned(v) for v in result.revisions),
            contexts=tuple(
                replace(c, entities=tuple(owned(v) for v in c.entities)) for c in result.contexts
            ),
        )
    )


def snapshot(manager: LayeredManager, key: tuple[str, ...]) -> LayerSnapshot:
    """UI сохраняет last_known payload; проверки используют только доказанные сущности."""
    contexts = []
    originals = {e.entity_id: e for e in (manager.source_document or manager.base).entities()}
    for context in manager.contexts:
        document = effective_document(manager, context)
        projected = document
        payloads = [v.payload for v in context.entities if v.state != "deleted"]
        document = replace(
            document,
            pko=tuple(p for p in payloads if isinstance(p, ed.ObjectRule)),
            pod=tuple(p for p in payloads if isinstance(p, ed.ProcessingRule)),
            pkpd=tuple(p for p in payloads if isinstance(p, ed.PredefinedRule)),
            parameters=tuple(p for p in payloads if isinstance(p, ed.Parameter)),
        )
        # Обзор хранит last_known привязки неизвестного ПКО. Перехват его тела
        # остаётся объявленным обработчиком слоя, даже когда ПКО исключён из проверок.
        body_origins = {
            change.origin
            for rule in (*document.pko, *document.pod)
            for event in rule.events
            if event_direction(event.event) in (None, context.direction)
            for change in getattr(event, "body_changes", ())
        }
        body_handler_ids = {
            hook.routine.entity_id for hook in manager.hooks if hook.origin in body_origins
        }
        document = replace(
            document,
            routines=tuple(
                replace(routine, roles=routine.roles | {"handler"})
                if routine.entity_id in body_handler_ids
                else routine
                for routine in document.routines
            ),
        )
        document = replace(
            document,
            pko=tuple(
                replace(r, events=tuple(resolve_binding(e, projected, context) for e in r.events))
                for r in document.pko
            ),
            pod=tuple(
                replace(r, events=tuple(resolve_binding(e, projected, context) for e in r.events))
                for r in document.pod
            ),
        )
        entities = {e.entity_id: e for e in document.entities()}
        versions = {}
        for version in context.entities:
            if version.payload is None or version.state == "deleted":
                continue
            versions[version.payload.entity_id] = version
            if isinstance(version.payload, ed.ObjectRule):
                for prop in version.payload.properties:
                    versions[prop.entity_id] = member_version(manager, prop, version, originals)
                for group in version.payload.groups:
                    versions[group.entity_id] = member_version(manager, group, version, originals)
                    for prop in group.properties:
                        versions[prop.entity_id] = member_version(manager, prop, version, originals)
            if isinstance(version.payload, (ed.ObjectRule, ed.ProcessingRule)):
                for binding in version.payload.events:
                    versions[binding.entity_id] = member_version(
                        manager, binding, version, originals
                    )
        for entity in entities.values():
            if entity.entity_id not in versions:
                versions[entity.entity_id] = replace(
                    member_version(manager, entity, originals=originals),
                    direction=context.direction,
                    headers_only=context.headers_only,
                )
        contexts.append(
            ContextView(context, document, build_addresses(document), entities, versions)
        )
    paths = {Path(f.path).resolve() for f in manager.source_files}
    for layer in manager.layers:
        root = Path(layer.root)
        paths.add(root / "Configuration.xml")
        for obj in checked_dump(root).objects:
            if obj.xml_path:
                paths.add(Path(obj.xml_path))
            if obj.module_path and (
                obj.kind == "ExchangePlan" or obj.name == "ОбменДаннымиПереопределяемый"
            ):
                paths.add(Path(obj.module_path))
    source_hashes = {Path(f.path).resolve(): f.sha256 for f in manager.source_files}
    dependencies = tuple(
        (p, source_hashes[p] if p in source_hashes else file_hash(p)) for p in sorted(paths)
    )
    return LayerSnapshot(
        manager, key, dependencies, tuple(contexts), change_index(manager, contexts)
    )


def change_index(manager: LayeredManager, contexts: list[ContextView]) -> tuple[ChangeEntry, ...]:
    """Индекс истории строится один раз; адрес и контексты не ищутся для каждой страницы."""
    root_ids = {v.logical_id for v in manager.revisions}
    revisions = (
        *manager.revisions,
        *(v for view in contexts for v in view.versions.values() if v.logical_id not in root_ids),
    )
    addresses = {}
    for hit in build_layer_addresses(manager).by_address.values():
        if hit.entity is not None and hit.address.startswith("Слой/"):
            addresses.setdefault(
                (hit.address.split("/")[1], hit.entity.entity_id, entity_signature(hit.entity)),
                hit.address,
            )
    source_index = build_addresses(manager.source_document or manager.base)
    grouped: dict[tuple, tuple[EntityVersion, set[tuple[str, bool]]]] = {}
    for version in revisions:
        if version.state == "base":
            continue
        key = (
            version.revision_id,
            version.payload,
            version.changes,
            version.state,
            version.certainty,
        )
        if version.logical_id not in root_ids and version.payload is not None:
            key = (
                version.logical_id,
                entity_signature(version.payload),
                version.state,
                version.certainty,
                tuple(c.operation_id for c in version.changes),
            )
        if key not in grouped:
            grouped[key] = (version, set())
        grouped[key][1].add((version.direction, version.headers_only))
    entries = []
    for version, visible in grouped.values():
        payload = version.payload
        address = (
            addresses.get(
                (escape_segment(version.layer_id), payload.entity_id, entity_signature(payload))
            )
            if payload
            else None
        )
        if address is None:
            address = f"Слой/{escape_segment(version.layer_id)}/" + (
                views.address_of(payload, source_index)
                if payload
                else f"Ревизия/{escape_segment(version.revision_id)}"
            )
        entries.append(ChangeEntry(version, address, tuple(sorted(visible))))
    return tuple(entries)


def change_page(
    snap: LayerSnapshot,
    direction: str | None,
    headers_only: bool,
    layer: str | None,
    entity_id: str | None,
    text: str | None,
    offset: int,
    limit: int,
    format_object: str | None = None,
    metadata_object: str | None = None,
) -> dict[str, Any]:
    """Линейный отбор по индексу; дорогие строки ответа создаются только для страницы."""
    selected = select_views(snap, direction, headers_only)
    if layer is not None and layer not in {item.id for item in snap.manager.layers}:
        raise ValueError(f"Неизвестный слой: {layer}; выберите id из ed_list(kind=layer)")
    if entity_id is not None and not any(
        entity_id in (v.logical_id, v.revision_id, getattr(v.payload, "entity_id", None))
        for v in (*snap.manager.revisions, *(v for c in snap.contexts for v in c.versions.values()))
    ):
        raise RuleNotFoundError(f"Сущность истории не найдена: {entity_id}")
    visible = {(v.context.direction, v.context.headers_only) for v in selected}
    matches = []
    for entry in snap.changes:
        version = entry.version
        if not visible.intersection(entry.contexts):
            continue
        if layer is not None and version.layer_id != layer:
            continue
        if entity_id is not None and entity_id not in (
            version.logical_id,
            version.revision_id,
            getattr(version.payload, "entity_id", None),
        ):
            continue
        if format_object is not None or metadata_object is not None:
            continue  # У строки истории нет полей сторон, как и в прежнем ответе.
        if text is not None and not any(
            text.casefold() in value.casefold()
            for value in (getattr(version.payload, "name", ""), entry.address)
        ):
            continue
        matches.append(entry)
    result = views.page(matches, offset, limit)
    result["items"] = [
        {
            "address": entry.address,
            "kind": "change",
            "name": getattr(entry.version.payload, "name", ""),
            "entity_id": entry.version.logical_id,
            "revision_id": entry.version.revision_id,
            "state": entry.version.state,
            "layer_id": entry.version.layer_id,
            "certainty": entry.version.certainty,
            "changed_fields": sorted({".".join(c.path) for c in entry.version.changes}),
            "contexts": [
                context_row(v.context)
                for v in selected
                if (v.context.direction, v.context.headers_only) in entry.contexts
            ],
        }
        for entry in result["items"]
    ]
    return result


def file_hash(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise EdReadError(f"Файл выгрузки недоступен: {path}") from error


def source_changed(snap: LayerSnapshot) -> bool:
    changed = False
    for path, digest in snap.dependencies:
        current = file_hash(path)
        if digest is not None and current is None:
            raise EdReadError(f"Файл выгрузки недоступен: {path}")
        changed |= current != digest
    return changed


def context_row(context: EffectiveContext) -> dict[str, Any]:
    return {"direction": context.direction, "headers_only": context.headers_only}


def select_views(
    snap: LayerSnapshot, direction: str | None, headers_only: bool
) -> tuple[ContextView, ...]:
    validate_context(direction, headers_only)
    directions = ("send", "receive") if direction is None else (direction,)
    contexts = [select_context(snap.manager, item, headers_only) for item in directions]
    return tuple(view for view in snap.contexts if view.context in contexts)


def validate_context(direction: str | None, headers_only: bool) -> None:
    if direction not in (None, "send", "receive"):
        raise ValueError("Направление: send, receive или null")
    if type(headers_only) is not bool:
        raise ValueError("headers_only должен быть логическим значением")


def layer_row(layer) -> dict[str, Any]:
    return {
        "address": f"Слой/{escape_segment(layer.id)}",
        "kind": "layer",
        "id": layer.id,
        "ordinal": layer.ordinal,
        "name": layer.name,
        "fingerprint": layer.fingerprint,
    }


def composition(snap: LayerSnapshot, *, overview: bool = False) -> dict[str, Any]:
    manager = snap.manager
    effective = []
    for view in snap.contexts:
        counts = {k: v for k, v in view.document.counts.items() if k != "values"}
        effective.append({**context_row(view.context), "counts": counts})
    changed = {(v.logical_id, v.state) for v in manager.revisions if v.state != "base"}
    changed.update(
        (v.logical_id, v.state)
        for view in snap.contexts
        for v in view.versions.values()
        if v.state != "base"
    )
    changes = {
        state: sum(s == state for _, s in changed) for state in ("added", "changed", "deleted")
    }
    unknown = sum(op.kind == "unknown" or op.resolution == "unknown" for op in manager.operations)
    executor_warning = (
        {
            "executor_note": (
                "Выводы проверок по типовому исполнителю для этой базы "
                "не гарантированы; тела перехватов не разбирались."
            )
        }
        if manager.executor_hooks
        else {}
    )
    result = {
        **executor_warning,
        "composition_status": manager.status,
        "layers": {
            "total": len(manager.layers),
            "items": [layer_row(layer) for layer in manager.layers[:16]],
            "has_more": len(manager.layers) > 16,
        },
        "contexts": [context_row(view.context) for view in snap.contexts],
        "effective_counts": effective,
        "changes_summary": changes,
        "skipped_summary": {
            "total": len(manager.skipped),
            "by_reason": dict(Counter(s.reason for s in manager.skipped)),
        },
        "unselected_managers": [
            {
                "name": name,
                "layer_id": layer_id,
                "file_id": source.file_id,
                "reason": "Собственный модуль менеджера расширения, маршрутом не выбран",
            }
            for name, layer_id, source in manager.unselected_managers
        ],
        "executor_overrides": [
            {"module": module, "procedure": target, "kind": kind, "origin": origin_row(origin)}
            for module, target, kind, origin in manager.executor_hooks
        ],
    }
    if not overview:
        return result
    return {
        **executor_warning,
        "status": manager.status,
        "base_parse_status": manager.base.parse_status.value,
        "unselected_managers": result["unselected_managers"],
        "executor_overrides": result["executor_overrides"],
        "effective_counts": effective,
        "changes_summary": changes,
        "known_operations": len(manager.operations) - unknown,
        "unknown_operations": unknown,
        "coverage": {
            "base": coverage_row(manager.base.coverage, manager.base.files[0].file_id)
            if manager.base.files
            else None,
            "layers": {
                "total": len(manager.coverage),
                "items": [coverage_row(c, file_id) for file_id, c in manager.coverage[:16]],
                "has_more": len(manager.coverage) > 16,
            },
        },
    }


def coverage_row(coverage: ed.Coverage, file_id: str) -> dict[str, Any]:
    return {
        "file_id": file_id,
        "counts": dict(coverage.counts),
        "entity_covered_lines": coverage.entity_covered_lines,
        "coverage_ratio": round(coverage.coverage_ratio, 6),
        "classified_ratio": round(coverage.classified_ratio, 6),
    }


def origin_row(origin) -> dict[str, Any]:
    return {
        "layer_id": origin.layer_id,
        "file_id": origin.file_id,
        "span": views.span_view(origin.span),
        "procedure": origin.procedure,
        "hook_id": origin.hook_id,
        "call_chain": views.page([views.span_view(s) for s in origin.call_chain]),
    }


def provenance(
    version: EntityVersion, offset: int, limit: int, manager: LayeredManager | None = None
) -> dict[str, Any]:
    operations = {op.id: op for op in manager.operations} if manager else {}

    def after_value(change):
        op = operations.get(change.operation_id)
        if change.after is None and op and op.kind == "set" and isinstance(op.value, ed.Expr):
            return views.scalar(
                ed.Field(
                    "literal" if op.value.literal_type else "expression",
                    op.value.literal_value if op.value.literal_type else op.value,
                )
            )
        return change_value(change.after)

    return {
        "state": version.state,
        "layer_id": version.layer_id,
        "logical_id": version.logical_id,
        "certainty": version.certainty,
        "origins": views.page([origin_row(o) for o in version.origins], offset, limit),
        "changed_fields": views.page(
            [
                {
                    "path": list(c.path),
                    "before": change_value(c.before),
                    "after": after_value(c),
                    "operation_id": c.operation_id,
                    "origin": origin_row(c.origin),
                }
                for c in version.changes
            ],
            offset,
            limit,
        ),
    }


def change_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return {"count": len(value)}
    return views.scalar(value)


def acting_address(entity: ed.Entity, view: ContextView) -> str:
    address = views.address_of(entity, view.index)
    return "Действующее/" + address if entity.entity_id in view.versions else address


def list_rows(
    snap: LayerSnapshot,
    kind: str,
    direction: str | None,
    headers_only: bool,
    layer: str | None,
    entity_id: str | None,
    format_object: str | None = None,
    metadata_object: str | None = None,
    text: str | None = None,
) -> list[dict[str, Any]]:
    if kind == "change":
        return change_page(
            snap,
            direction,
            headers_only,
            layer,
            entity_id,
            text,
            0,
            max(len(snap.changes), 1),
            format_object,
            metadata_object,
        )["items"]
    selected = select_views(snap, direction, headers_only)
    manager = snap.manager
    if layer is not None and layer not in {item.id for item in manager.layers}:
        raise ValueError(f"Неизвестный слой: {layer}; выберите id из ed_list(kind=layer)")
    root_ids = {v.logical_id for v in manager.revisions}
    revisions = (
        *manager.revisions,
        *(
            v
            for view in snap.contexts
            for v in view.versions.values()
            if v.logical_id not in root_ids
        ),
    )
    if entity_id is not None and not any(
        entity_id in (v.logical_id, v.revision_id, getattr(v.payload, "entity_id", None))
        for v in revisions
    ):
        raise RuleNotFoundError(f"Сущность истории не найдена: {entity_id}")
    if kind == "layer":
        return [layer_row(item) for item in manager.layers if layer is None or item.id == layer]
    if kind == "hook":
        return [
            {
                "address": (
                    f"Слой/{escape_segment(h.origin.layer_id)}/Перехват/"
                    f"{escape_segment(h.routine.name)}"
                ),
                "kind": "hook",
                "name": h.routine.name,
                "id": h.id,
                "layer_id": h.origin.layer_id,
                "target_name": h.target_name,
                "target_class": h.target_class,
                "continuation": h.continuation,
                "applicability": h.applicability,
                "file_id": h.origin.file_id,
                "line_start": h.routine.span.line_start,
            }
            for h in manager.hooks
            if layer is None or h.origin.layer_id == layer
        ]
    if kind == "layer_unknown":
        return [
            {
                "address": f"Слой/{escape_segment(s.origin.layer_id)}/Неизвестное/{n}",
                "kind": kind,
                "name": s.reason,
                "layer_id": s.origin.layer_id,
                "file_id": s.origin.file_id,
                "line_start": s.origin.span.line_start,
                "affected_ids": views.page(list(s.affected_ids)),
                "check_scope": views.page(list(s.check_scope)),
                "certainty": s.certainty,
                "detail": views.short(s.raw)
                if s.reason in {"unmodeled_hook", "unsupported_event"}
                else None,
            }
            for n, s in enumerate(manager.skipped, 1)
            if layer is None or s.origin.layer_id == layer
        ]
    rows = []
    grouped: dict[str, dict[str, Any]] = {}
    for view in selected:
        for entity in view.entities.values():
            if kind in views.ROLES:
                if not isinstance(entity, ed.Routine) or kind not in entity.roles:
                    continue
            elif entity.kind != kind:
                continue
            version = view.versions.get(entity.entity_id)
            layer_id = (
                version.layer_id
                if version
                else next(
                    (i for i, r in manager.routines if r.entity_id == entity.entity_id), "base"
                )
            )
            if layer is not None and layer_id != layer:
                continue
            configuration, format_name = views.sides(entity, view.entities)
            if any(
                value is not None
                and (actual is None or actual.strip().casefold() != value.strip().casefold())
                for actual, value in (
                    (configuration, metadata_object),
                    (format_name, format_object),
                )
            ):
                continue
            row = views.row(entity, view.index, view.entities, kind)
            row["address"] = acting_address(entity, view) if version else row["address"]
            if text is not None and not any(
                text.casefold() in value.casefold()
                for value in (entity.name, row["address"], configuration or "", format_name or "")
            ):
                continue
            if (
                not version
                and layer_id != "base"
                and isinstance(entity, ed.Routine)
                and kind == "handler"
            ):
                row["address"] = (
                    f"Слой/{escape_segment(layer_id)}/Обработчик/{escape_segment(entity.name)}"
                )
            row.update(
                layer_id=layer_id,
                state=version.state if version else "base",
                certainty=version.certainty if version else "known",
            )
            if version:
                row["entity_id"] = version.logical_id
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key in grouped:
                grouped[key]["contexts"].append(context_row(view.context))
            else:
                row["contexts"] = [context_row(view.context)]
                grouped[key] = row
                rows.append(row)
    file_order = {source.file_id: n for n, source in enumerate(manager.source_files)}
    rows.sort(
        key=lambda row: (file_order.get(row["file_id"], 0), row["line_start"], row["address"])
    )
    return rows


def _ambiguous(address: str, candidates: tuple[str, ...], offset: int, limit: int):
    error = AmbiguousAddressError(f"Неоднозначный адрес: {address}", candidates)
    error.candidate_page = views.page(list(candidates), offset, limit)
    raise error


def source_entity_active(entity: ed.Entity, view: ContextView, short_address: str) -> bool:
    """Исходная декларация обработчика видна в контекстах его действующей роли."""
    current = view.entities.get(entity.entity_id)
    if current is None:
        return False
    if isinstance(current, ed.Routine):
        role = {
            "обработчик": "handler",
            "алгоритм": "algorithm",
            "диспетчер": "dispatcher",
            "служебный": "support",
        }.get(short_address.split("/", 1)[0].casefold())
        if role is not None:
            return role in current.roles
    return True


def get_hits(
    snap: LayerSnapshot,
    address: str,
    direction: str | None,
    headers_only: bool,
    offset: int,
    limit: int,
) -> list[tuple[LayerAddressHit, ContextView]]:
    selected = select_views(snap, direction, headers_only)
    if address.casefold().startswith("слой/"):
        index = build_layer_addresses(snap.manager)
        try:
            hit = index.find(address)
        except AmbiguousLayerAddress as error:
            candidates = tuple(
                a
                for a in error.candidates
                if direction is None
                or not index.by_address[a].version
                or getattr(index.by_address[a].version, "direction", "") in ("", direction)
            )
            if len(candidates) != 1:
                _ambiguous(address, candidates, offset, limit)
            hit = index.find(candidates[0])
        except LayerAddressNotFound as error:
            # Индекс A1 хранит ревизии правил и обработчики, но не служебные сущности
            # базового файла и не использованные декларации заменённого менеджера.
            prefix = "Слой/base/"
            if address.casefold().startswith(prefix.casefold()):
                short = address[len(prefix) :]
                base_index = build_addresses(snap.manager.base)
                if short.casefold() in base_index.conflicts:
                    candidates = tuple(prefix + a for a in base_index.conflicts[short.casefold()])
                    _ambiguous(address, candidates, offset, limit)
                entity = next(
                    (
                        e
                        for a, e in base_index.by_address.items()
                        if a.casefold() == short.casefold()
                    ),
                    None,
                )
                if entity is None:
                    entity = next(
                        (
                            e
                            for e in snap.manager.base.entities()
                            if views.address_of(e, base_index).casefold() == short.casefold()
                        ),
                        None,
                    )
                if entity is not None:
                    return [
                        (
                            LayerAddressHit(
                                prefix + views.address_of(entity, base_index), entity=entity
                            ),
                            view,
                        )
                        for view in (
                            tuple(v for v in selected if source_entity_active(entity, v, short))
                            if isinstance(entity, ed.Routine)
                            else selected[:1]
                        )
                    ]
            parts = address.split("/", 2)
            if len(parts) == 3:
                for view in selected:
                    entity = next(
                        (
                            e
                            for e in view.entities.values()
                            if views.address_of(e, view.index).casefold() == parts[2].casefold()
                            and entity_layer(snap.manager, e) == parts[1]
                        ),
                        None,
                    )
                    if entity:
                        return [
                            (
                                LayerAddressHit(
                                    address, view.versions.get(entity.entity_id), entity
                                ),
                                v,
                            )
                            for v in selected
                            if source_entity_active(entity, v, parts[2])
                        ]
            raise RuleNotFoundError(f"Сущность слоя не найдена: {address}") from error
        if (
            hit.entity is not None
            and hit.version
            and hit.entity.entity_id != getattr(hit.version.payload, "entity_id", None)
        ):
            hit = replace(hit, version=member_version(snap.manager, hit.entity, hit.version))
        if hit.entity is not None and hit.version is None:
            return [
                (
                    replace(
                        hit,
                        entity=v.entities.get(hit.entity.entity_id, hit.entity),
                        version=v.versions.get(hit.entity.entity_id),
                    ),
                    v,
                )
                for v in selected
                if source_entity_active(hit.entity, v, address.split("/", 2)[2])
            ]
        return [(hit, selected[0])]
    short = (
        address[len("Действующее/") :] if address.casefold().startswith("действующее/") else address
    )
    hits = []
    for view in selected:
        if short.casefold() in view.index.conflicts:
            _ambiguous(address, view.index.conflicts[short.casefold()], offset, limit)
        entity = next(
            (e for a, e in view.index.by_address.items() if a.casefold() == short.casefold()), None
        )
        if entity is None:
            entity = next(
                (
                    e
                    for e in view.entities.values()
                    if views.address_of(e, view.index).casefold() == short.casefold()
                ),
                None,
            )
        if entity is not None and source_entity_active(entity, view, short):
            hits.append(
                (
                    LayerAddressHit(
                        acting_address(entity, view)
                        if address.casefold().startswith("действующее/")
                        else views.address_of(entity, view.index),
                        view.versions.get(entity.entity_id),
                        entity,
                    ),
                    view,
                )
            )
    if not hits:
        raise RuleNotFoundError(
            f"Сущность ED не найдена: {address}; "
            f"контекст direction={direction or 'send/receive'}, headers_only={headers_only}"
        )
    return hits


def get_view(
    snap: LayerSnapshot,
    address: str,
    direction: str | None,
    headers_only: bool,
    children_kind: str | None,
    offset: int,
    limit: int,
    include_text: bool,
    text_offset: int,
    text_limit: int,
) -> dict[str, Any]:
    select_views(snap, direction, headers_only)
    descriptor = next(
        (
            item
            for item in snap.manager.layers
            if f"Слой/{escape_segment(item.id)}".casefold() == address.casefold()
        ),
        None,
    )
    if descriptor is not None:
        files = [
            f
            for f in snap.manager.source_files
            if Path(f.path).is_relative_to(Path(descriptor.root))
        ]
        return {
            **layer_row(descriptor),
            "source_files": views.page(
                [
                    {"file_id": f.file_id, "path": f.path, "sha256": f.sha256, "lines": f.lines}
                    for f in files
                ],
                offset,
                limit,
            ),
        }
    # Пропуск не имеет адреса в индексе A1, но доступен по адресу страницы layer_unknown.
    unknowns = list_rows(snap, "layer_unknown", direction, headers_only, None, None)
    unknown = next((r for r in unknowns if r["address"].casefold() == address.casefold()), None)
    if unknown:
        result = dict(unknown)
        if include_text:
            skip = snap.manager.skipped[unknowns.index(unknown)]
            result["text"] = text_page(skip.raw, text_offset, text_limit)
        return result
    hits = get_hits(snap, address, direction, headers_only, offset, limit)
    if not hits:
        raise RuleNotFoundError(
            f"Сущность ED не действует: {address}; "
            f"контекст direction={direction or 'send/receive'}, headers_only={headers_only}"
        )
    signatures = {
        (
            entity_signature(h.entity) if h.entity is not None else None,
            h.version.state if h.version else None,
            h.version.certainty if h.version else None,
        )
        for h, _ in hits
    }
    if len(signatures) > 1:
        return {
            "address": address,
            "requires_context": True,
            "message": "Выберите direction и headers_only",
            "variants": views.page(
                [
                    {
                        "address": h.address,
                        **context_row(v.context),
                        "state": h.version.state if h.version else "base",
                        "certainty": h.version.certainty if h.version else "known",
                        "fields": views.entity_fields(h.entity) if h.entity else {},
                    }
                    for h, v in hits
                ],
                offset,
                limit,
            ),
        }
    hit, view = hits[0]
    entity = hit.entity
    if hit.operation is not None:
        op = hit.operation
        result = {
            "address": hit.address,
            "kind": "operation",
            "operation_id": op.id,
            "operation_kind": op.kind,
            "target_ref": op.target_ref,
            "field_path": list(op.field_path),
            "layer_id": op.origin.layer_id,
            "origin": origin_row(op.origin),
            "resolution": op.resolution,
        }
        if include_text:
            source = next(f for f in snap.manager.source_files if f.file_id == op.origin.file_id)
            raw = source.text[op.origin.span.char_start : op.origin.span.char_end]
            result["text"] = text_page(raw, text_offset, text_limit)
        return result
    if entity is None:
        raise RuleNotFoundError(f"Сущность ED не найдена: {address}")
    children, kinds = views.direct_children(entity, view.entities)
    if children_kind == "reference" and views.accepts_code_references(entity):
        refs = ed.build_references(view.document)
        selected = views.page(
            [views.reference_row(r) for r in refs.entries if r.owner_id == entity.entity_id],
            offset,
            limit,
        )
    else:
        if children_kind is not None:
            if children_kind not in kinds:
                raise ValueError(f"Вид детей {children_kind} недоступен для {entity.kind}")
            children = [pair for pair in children if pair[0] == children_kind]
        rows = []
        for kind, item in children:
            row = views.child_view(kind, item, view.index, view.entities)
            if isinstance(item, ed.Entity):
                if hit.address.startswith("Слой/"):
                    parent = views.address_of(entity, view.index)
                    row["address"] = (
                        row["address"].replace(parent, hit.address, 1)
                        if row["address"].startswith(parent + "/")
                        else "/".join(hit.address.split("/")[:2]) + "/" + row["address"]
                    )
                elif hit.address.startswith("Действующее/"):
                    row["address"] = "Действующее/" + row["address"]
            rows.append(row)
        selected = views.page(rows, offset, limit)
    result = {
        "address": hit.address,
        "kind": "hook" if hit.hook else entity.kind,
        "fields": views.entity_fields(entity),
        "span": views.span_view(entity.span),
        "contexts": [context_row(v.context) for _, v in hits],
        "children": selected,
    }
    if hit.version:
        result.update(provenance(hit.version, offset, limit, snap.manager))
    else:
        layer_id = (
            hit.hook.origin.layer_id
            if hit.hook
            else next(
                (
                    layer_id
                    for layer_id, r in snap.manager.routines
                    if r.entity_id == entity.entity_id
                ),
                "base",
            )
        )
        result.update(
            state="base",
            layer_id=layer_id,
            logical_id=entity.entity_id,
            certainty="known",
            origins=views.page(
                [origin_row(hit.hook.origin)]
                if hit.hook
                else [
                    origin_row(
                        Origin(
                            layer_id,
                            "CommonModule",
                            "",
                            entity.span.file_id,
                            entity.span,
                            entity.name
                            if isinstance(entity, ed.Routine)
                            else getattr(entity, "procedure_name", None),
                        )
                    )
                ],
                offset,
                limit,
            ),
            changed_fields=views.page([], offset, limit),
        )
    if include_text:
        result["text"] = text_page(entity.raw_text, text_offset, text_limit)
        if not hit.address.startswith("Слой/"):
            result["source_declaration"] = True
    return result


def text_page(raw: str, offset: int, limit: int) -> dict[str, Any]:
    return {
        "text": raw[offset : offset + limit],
        "total_chars": len(raw),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(raw),
    }


def locate(
    snap: LayerSnapshot, file_id: str | None, line: int, offset: int, limit: int
) -> dict[str, Any]:
    manager = snap.manager
    source = (
        next((f for f in manager.source_files if f.file_id == file_id), None)
        if file_id
        else manager.base.files[0]
    )
    if source is None:
        raise ValueError("Неизвестный file_id; выберите файл из source_files")
    if type(line) is not int or not 1 <= line <= source.lines:
        raise ValueError("Номер строки вне файла")
    if manager.base.files and source.file_id == manager.base.files[0].file_id:
        doc = manager.base
        base_index = build_addresses(doc)
        found = tuple(
            e
            for e in locate_entities(doc, line)
            if not (isinstance(e, ed.Routine) and e.roles <= {"rule"})
        )
        rows = [
            {
                "address": "Слой/base/" + views.address_of(e, base_index),
                "kind": e.kind,
                "span": views.span_view(e.span),
                "relation": "innermost"
                if n == 0
                or not (
                    getattr(found[0], "group_id", None) == e.entity_id
                    or (
                        e.span.char_start <= found[0].span.char_start
                        and e.span.char_end >= found[0].span.char_end
                    )
                )
                else "ancestor",
            }
            for n, e in enumerate(found)
        ]
        entities = {e.entity_id: e for e in doc.entities()}
        routine_ids = {e.entity_id for e in found if isinstance(e, ed.Routine)}
        for binding in entities.values():
            if isinstance(binding, ed.HandlerBinding) and binding.target_id in routine_ids:
                owner = (
                    doc.conversion
                    if binding.owner_id == "conversion"
                    else entities.get(binding.owner_id)
                )
                if (
                    owner is not None
                    and owner not in found
                    and not any(
                        r["address"] == "Слой/base/" + views.address_of(owner, base_index)
                        for r in rows
                    )
                ):
                    rows.append(
                        {
                            "address": "Слой/base/" + views.address_of(owner, base_index),
                            "kind": owner.kind,
                            "span": views.span_view(owner.span),
                            "relation": "associated",
                        }
                    )
        return {
            "file_id": source.file_id,
            "line": line,
            "classification": doc.coverage.classify_line(line).value,
            "matches": views.page(rows, offset, limit),
        }
    index = build_layer_addresses(manager)
    found = []
    seen = set()
    for hit in index.by_address.values():
        span = (
            hit.hook.routine.span
            if hit.hook
            else hit.operation.origin.span
            if hit.operation
            else hit.entity.span
            if hit.entity
            else None
        )
        if span and span.file_id == source.file_id and span.line_start <= line <= span.line_end:
            if not hit.address.startswith("Слой/") or hit.address in seen:
                continue
            seen.add(hit.address)
            found.append(
                {
                    "address": hit.address,
                    "kind": "hook"
                    if hit.hook
                    else "operation"
                    if hit.operation
                    else hit.entity.kind
                    if hit.entity
                    else "unknown",
                    "span": views.span_view(span),
                    "relation": "innermost",
                }
            )
    found.sort(
        key=lambda r: (
            r["kind"] != "hook",
            r["span"]["char_end"] - r["span"]["char_start"],
            r["address"],
        )
    )
    for n, row in enumerate(found):
        if (
            n
            and row["span"]["char_start"] <= found[0]["span"]["char_start"]
            and row["span"]["char_end"] >= found[0]["span"]["char_end"]
        ):
            row["relation"] = "ancestor"
    coverage = next(
        (c for ident, c in manager.coverage if ident == source.file_id), manager.base.coverage
    )
    return {
        "file_id": source.file_id,
        "line": line,
        "classification": coverage.classify_line(line).value,
        "matches": views.page(found, offset, limit),
    }
