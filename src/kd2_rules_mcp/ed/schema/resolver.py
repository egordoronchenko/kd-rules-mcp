"""Замыкание импортов, наследование и эффективные пути EnterpriseData (вывод)."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from .errors import (
    EdSchemaConflictError,
    EdSchemaProfileMismatchError,
    EdSchemaResourceLimitError,
)
from .model import (
    EdSchema,
    OriginStep,
    QName,
    ResolvedProperty,
    SchemaDiagnostic,
    SchemaPackage,
    SchemaProperty,
    SchemaSource,
    SchemaType,
)
from .xdto import XS, metadata, read_package

READER_VERSION = "xdto-1"
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 128
MAX_TYPES = 200000
MAX_PROPERTIES = 2000000
MAX_RESOLUTION_DEPTH = 128


def load_schema(
    path: Path,
    *,
    extensions: tuple[Path, ...] = (),
    locate_import: Callable[[str], Path | None] | None = None,
) -> EdSchema:
    """Регистрирует весь граф до разрешения ссылок; URI никогда не скачивается."""
    packages: list[SchemaPackage] = []
    files: dict[Path, SchemaPackage] = {}
    sources: dict[str, SchemaSource] = {}
    diagnostics: list[SchemaDiagnostic] = []

    def account(source: SchemaSource) -> None:
        sources[source.source_id] = source
        if len(sources) > MAX_FILES or sum(s.bytes for s in sources.values()) > MAX_TOTAL_BYTES:
            raise EdSchemaResourceLimitError("Превышен лимит файлов или суммарных байтов схемы")

    def read(input_path: Path, role: str) -> SchemaPackage:
        input_path = input_path.resolve()
        if input_path in files:
            package = files[input_path]
            if role != "dependency" and package.origin_role == "dependency":
                # Явно выбранное расширение активно независимо от порядка обхода импортов.
                package = replace(package, origin_role=role)
                files[input_path] = package
                packages[
                    packages.index(next(p for p in packages if p.sources == package.sources))
                ] = package
            return package
        meta = None
        binary = input_path
        if input_path.suffix.lower() == ".xml":
            meta = metadata(input_path)
            binary = input_path.parent / meta[0] / "Ext" / "Package.bin"
            account(meta[3])
        prior = next((p for p in packages if p.sources[0].path == str(binary.resolve())), None)
        if prior is not None:
            if meta:
                if meta[1] != prior.namespace:
                    raise EdSchemaProfileMismatchError("Namespace описания не совпадает с пакетом")
                if meta[3].source_id not in {s.source_id for s in prior.sources}:
                    updated = replace(
                        prior,
                        metadata_name=meta[0],
                        revision=meta[2],
                        sources=(*prior.sources, meta[3]),
                    )
                    packages[packages.index(prior)] = updated
                    prior = updated
            files[input_path] = prior
            return prior
        package = read_package(binary, role)
        account(package.sources[0])
        if meta:
            if meta[1] != package.namespace:
                raise EdSchemaProfileMismatchError("Namespace описания не совпадает с пакетом")
            package = replace(
                package,
                metadata_name=meta[0],
                revision=meta[2],
                sources=(*package.sources, meta[3]),
            )
        files[input_path] = package
        packages.append(package)
        diagnostics.extend(package.diagnostics)
        return package

    base = read(path, "base")
    extension_packages = tuple(read(p, "extension") for p in extensions)
    if len({p.namespace for p in extension_packages}) != len(extension_packages):
        raise EdSchemaProfileMismatchError("Расширения должны иметь разные URI")
    for package in extension_packages:
        if package.namespace == base.namespace or not any(
            i.namespace == base.namespace for i in package.imports
        ):
            raise EdSchemaProfileMismatchError("Расширение должно импортировать выбранную базу")
    cursor = 0
    while cursor < len(packages):
        package = packages[cursor]
        imports = []
        for item in package.imports:
            target = locate_import(item.namespace) if locate_import else None
            dependency = (
                read(target, "dependency")
                if target is not None
                else next((p for p in packages if p.namespace == item.namespace), None)
            )
            if dependency is not None:
                if dependency.namespace != item.namespace:
                    raise EdSchemaProfileMismatchError("URI импорта не совпадает с пакетом")
                imports.append(
                    replace(
                        item, status="resolved", resolved_source_id=dependency.sources[0].source_id
                    )
                )
            else:
                imports.append(item)
                diagnostics.append(
                    SchemaDiagnostic("unresolved_import", "Импорт не найден", item.span)
                )
        packages[cursor] = replace(package, imports=tuple(imports))
        cursor += 1

    # Граф происхождения хранит выбранные корни и реальные рёбра import, включая циклы.
    roots = [packages[0], *(p for p in packages if p.origin_role == "extension")]
    package_origins: dict[str, tuple[OriginStep, ...]] = {}
    for package in roots:
        head = (
            package.types[0].origin[0]
            if package.types
            else OriginStep(
                "base_package" if package.origin_role == "base" else "extension",
                package.namespace,
                package.imports[0].span,
            )
            if package.imports
            else None
        )
        if head:
            package_origins[package.namespace] = (
                *(
                    (package_origins.get(base.namespace, ()))
                    if package.origin_role == "extension"
                    else ()
                ),
                head,
            )
    changed = True
    while changed:
        changed = False
        for package in packages:
            for item in package.imports:
                if item.status == "resolved" and item.namespace not in package_origins:
                    package_origins[item.namespace] = (
                        *package_origins.get(package.namespace, ()),
                        OriginStep("import", item.namespace, item.span),
                    )
                    changed = True
    for index, package in enumerate(packages):
        incoming = tuple(
            OriginStep("import", item.namespace, item.span)
            for parent in packages
            for item in parent.imports
            if item.namespace == package.namespace and item.status == "resolved"
        )
        graph = tuple(dict.fromkeys((*package_origins.get(package.namespace, ()), *incoming)))
        packages[index] = replace(
            package,
            types=tuple(
                replace(
                    t,
                    origin=tuple(dict.fromkeys((*graph, *t.origin))),
                    properties=tuple(
                        replace(p, origin=tuple(dict.fromkeys((*graph, *p.origin))))
                        for p in t.properties
                    ),
                )
                for t in package.types
            ),
        )

    if (
        sum(len(p.types) for p in packages) > MAX_TYPES
        or sum(len(t.properties) for p in packages for t in p.types) > MAX_PROPERTIES
    ):
        raise EdSchemaResourceLimitError("Превышен лимит типов или свойств схемы")
    named: dict[QName, SchemaType] = {}
    by_id: dict[str, SchemaType] = {}

    def signature(typ: SchemaType) -> object:
        def property_signature(prop: SchemaProperty) -> object:
            return (
                prop.name,
                prop.type_ref,
                signature(by_id[prop.type_id]) if prop.type_id else None,
                prop.lower,
                prop.upper,
                prop.nillable,
                prop.form,
                prop.explicit_attributes,
                prop.status,
            )

        return (
            typ.kind,
            typ.base,
            typ.members,
            typ.variety,
            tuple(property_signature(p) for p in typ.properties),
            tuple((f.kind, f.lexical, f.value_type, f.fixed) for f in typ.facets),
            typ.open,
            typ.abstract,
            typ.ordered,
            typ.sequenced,
            typ.explicit_attributes,
            typ.status,
        )

    for package in packages:
        by_id.update((t.id, t) for t in package.types)
    for typ in by_id.values():
        if typ.qname is not None:
            existing = named.get(typ.qname)
            if existing and signature(existing) != signature(typ):
                raise EdSchemaConflictError(f"Разные определения типа {typ.qname}")
            named.setdefault(typ.qname, typ)

    for ident, typ in tuple(by_id.items()):
        missing = tuple(
            q
            for q in (typ.base, *typ.members, *(p.type_ref for p in typ.properties))
            if q and q.namespace != XS and q not in named
        )
        if missing:
            diagnostics.append(
                SchemaDiagnostic(
                    "unresolved_type", "Не найден тип зависимости", typ.origin[-1].span, (ident,)
                )
            )
            by_id[ident] = replace(typ, status="partial")
    named = {q: by_id[t.id] for q, t in named.items()}
    inherited: dict[str, tuple[SchemaProperty, ...]] = {}

    def properties(typ: SchemaType, chain: tuple[str, ...] = ()) -> tuple[SchemaProperty, ...]:
        if len(chain) >= MAX_RESOLUTION_DEPTH:
            raise EdSchemaResourceLimitError("Глубина разрешения превышает 128")
        if typ.id in chain:
            for ident in chain[chain.index(typ.id) :]:
                by_id[ident] = replace(by_id[ident], status="partial")
            diagnostics.append(
                SchemaDiagnostic(
                    "inheritance_cycle", "Циклическое наследование", typ.origin[-1].span, chain
                )
            )
            return ()
        if typ.id in inherited:
            return inherited[typ.id]
        parent = named.get(typ.base) if typ.base else None
        rows = ()
        if parent:
            step = OriginStep(
                "inheritance",
                typ.qname.namespace if typ.qname else "",
                typ.origin[-1].span,
                parent.qname,
            )
            rows = tuple(
                replace(p, origin=(*typ.origin, step, *p.origin))
                for p in properties(parent, (*chain, typ.id))
            )
            if by_id[parent.id].status == "partial":
                by_id[typ.id] = replace(by_id[typ.id], status="partial")
        inherited[typ.id] = (*rows, *typ.properties)
        return inherited[typ.id]

    for typ in tuple(by_id.values()):
        properties(typ)
    named = {q: by_id[t.id] for q, t in named.items()}
    packages = [replace(p, types=tuple(by_id[t.id] for t in p.types)) for p in packages]
    digest = hashlib.sha256(
        json.dumps(
            [
                READER_VERSION,
                base.namespace,
                [p.namespace for p in extension_packages],
                sorted((s.source_id, s.sha256) for s in sources.values()),
            ],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return EdSchema(
        "schema-" + digest[:24],
        tuple(packages),
        base.namespace,
        tuple(p.namespace for p in extension_packages),
        tuple(diagnostics),
        READER_VERSION,
        "partial" if diagnostics else "complete",
        MappingProxyType(named),
        MappingProxyType(by_id),
        MappingProxyType(inherited),
    )


def property_type(schema: EdSchema, prop: SchemaProperty) -> SchemaType | None:
    if prop.status != "complete":
        return None
    return (
        schema.by_id.get(prop.type_id)
        if prop.type_id
        else schema.types.get(prop.type_ref)
        if prop.type_ref
        else None
    )


def is_reference(schema: EdSchema, typ: SchemaType) -> bool:
    """Ссылка определяется цепочкой происхождения от импортированного Ref, а не своим именем."""
    seen: set[str] = set()
    while typ.base and typ.id not in seen:
        seen.add(typ.id)
        parent = schema.types.get(typ.base)
        if parent is None:
            return False
        if (
            parent.qname
            and parent.qname.local == "Ref"
            and any(
                p.namespace == parent.qname.namespace and p.origin_role == "dependency"
                for p in schema.packages
            )
        ):
            return True
        typ = parent
    return False


def table_row(schema: EdSchema, prop: SchemaProperty) -> SchemaType | None:
    """Критерий исполнителя: ровно одно свойство контейнера, с upper != 1."""
    typ = property_type(schema, prop)
    rows = schema.inherited.get(typ.id, ()) if typ and typ.status == "complete" else ()
    return property_type(schema, rows[0]) if len(rows) == 1 and rows[0].upper != 1 else None


def effective_properties(
    schema: EdSchema,
    typ: SchemaType,
    prefix: tuple[QName, ...] = (),
    chain: tuple[str, ...] = (),
) -> tuple[tuple[SchemaProperty, tuple[QName, ...]], ...]:
    """Плоские ключевые/общие свойства с сохранением всех физических кандидатов."""
    if len(chain) >= MAX_RESOLUTION_DEPTH:
        raise EdSchemaResourceLimitError("Глубина разрешения превышает 128")
    if typ.id in chain:
        return ()
    result = []
    for prop in schema.inherited[typ.id]:
        physical = (*prefix, prop.name)
        result.append((prop, physical))
        target = property_type(schema, prop)
        # Оболочки исполнителя: свойство ключа — XDTO:3107, XDTO:4892; общие свойства — по
        # вхождению в имя типа, как при конвертации (XDTO:1723, XDTO:6400), а не по началу имени
        # (XDTO:9550 — только список свойств объекта).
        if target and (
            prop.name.local == "КлючевыеСвойства"
            or (target.qname and "ОбщиеСвойства" in target.qname.local)
        ):
            result.extend(
                (replace(child, origin=tuple(dict.fromkeys((*prop.origin, *child.origin)))), path)
                for child, path in effective_properties(schema, target, physical, (*chain, typ.id))
            )
    return tuple(result)


def resolve_property(
    schema: EdSchema,
    owner: QName | str,
    path: str,
    *,
    table: str | None = None,
) -> ResolvedProperty:
    """Разрешает ПКС относительно шапки или строки указанной ПКТЧ."""
    typ = schema.types.get(owner) if isinstance(owner, QName) else schema.by_id.get(owner)
    parts = tuple(p.strip() for p in path.split("."))
    if len(parts) > MAX_RESOLUTION_DEPTH:
        raise EdSchemaResourceLimitError("Глубина пути превышает 128")
    if not path.strip() or not all(parts) or typ is None:
        return ResolvedProperty((), (), parts, "unknown", "invalid_context", ())
    initial_path: tuple[QName, ...] = ()
    initial_origin: tuple[OriginStep, ...] = ()
    if table is not None:
        group = resolve_property(schema, owner, table)
        if group.status != "resolved":
            return replace(group, effective_path=parts)
        prop = next(
            p for t in schema.by_id.values() for p in t.properties if p.id == group.property_ids[0]
        )
        container = property_type(schema, prop)
        typ = table_row(schema, prop)
        if typ is None:
            return ResolvedProperty((), (), parts, "unknown", "not_table", group.origin)
        if container:
            repeated = schema.inherited[container.id][0]
            initial_path = (*group.physical_paths[0], repeated.name)
            initial_origin = (*group.origin, *repeated.origin)
    states = [(typ, initial_path, initial_origin)]
    found: list[tuple[SchemaProperty, tuple[QName, ...], tuple[OriginStep, ...]]] = []
    uncertain = False
    for index, part in enumerate(parts):
        following = []
        found = []
        for current, prefix, origin in states:
            candidates = [
                (p, physical)
                for p, physical in effective_properties(schema, current)
                if p.name.local == part
            ]
            if not candidates and current.status == "partial":
                uncertain = True
            # В шапке приоритет ключа подтверждён исполнителем; прочие коллизии остаются.
            keys = [
                (p, physical)
                for p, physical in candidates
                if len(physical) > 1 and physical[0].local == "КлючевыеСвойства"
            ]
            if keys:
                candidates = keys
            for prop, physical in candidates:
                coordinate = (*prefix, *physical)
                graph = (*origin, *prop.origin)
                found.append((prop, coordinate, graph))
                if prop.status == "partial" or (
                    prop.type_ref is not None
                    and prop.type_ref.namespace != XS
                    and prop.type_ref not in schema.types
                ):
                    uncertain = True
                target = property_type(schema, prop)
                if target:
                    following.append((target, coordinate, graph))
                elif index + 1 < len(parts):
                    uncertain = True
        if not found:
            break
        states = following
        if index + 1 < len(parts) and not states:
            found = []
            break
    status = (
        "ambiguous"
        if len(found) > 1
        else "unknown"
        if uncertain
        else "resolved"
        if found
        else "missing"
    )
    return ResolvedProperty(
        tuple(p.id for p, _, _ in found),
        tuple(x for _, x, _ in found),
        parts,
        status,
        "partial_schema" if uncertain else None,
        tuple(dict.fromkeys(step for _, _, graph in found for step in graph)),
    )
