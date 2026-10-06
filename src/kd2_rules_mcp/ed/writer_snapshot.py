"""Миграция импортов и собственных рамок после проверки хеша старого снимка."""

from dataclasses import fields, is_dataclass, replace
from typing import Any

from .canonical import model_addresses
from .forms import DISPATCHERS, helper_forms
from .lexer import tokenize
from .writer_model import (
    LayoutContainer,
    LayoutElement,
    ManagerModel,
    RetainedBlock,
    leaf_fingerprint,
    logical_id,
    text_hash,
)


def _tokens(text):
    # Комментарии тоже сравниваются: миграция не должна терять чужой текст.
    return tuple((t.kind, t.value if t.kind == "string" else t.folded) for t in tokenize(text))


def migrate_imported(model: ManagerModel) -> ManagerModel:
    """Перечитывает прежнюю классификацию, сохраняя текст, ID и историю решений."""
    from .reader import read_manager_text
    from .writer import render
    from .writer_import import import_manager

    action = (
        "Проект создан прежней версией сервера: миграция снимка невозможна. "
        'Используйте ed_create(mode="rebind") после проверки входов; '
        "повторный импорт создаст новый проект и потеряет прежние решения. Причина: "
    )
    try:
        rendered = render(model, "preserve")
        back, _ = import_manager(
            read_manager_text(
                rendered.data.decode("utf-8"), path=model.source_files[0].source_name
            ),
            project_id=model.project_id,
            manager_name=model.header.manager_name,
            host=model.host,
            format_bindings=model.format_bindings,
            executor_profile=model.executor_profile,
        )
        old_addresses, new_addresses = model_addresses(model), model_addresses(back)
        remap = {}
        used = set()

        def pair(old_nodes, new_nodes, tag):
            previous = {(tag(n), old_addresses.get(n.logical_id)): n.logical_id for n in old_nodes}
            for node in new_nodes:
                old_id = previous.get((tag(node), new_addresses.get(node.logical_id)))
                if old_id is not None and node.logical_id not in remap and old_id not in used:
                    remap[node.logical_id] = old_id
                    used.add(old_id)

        pair(model.members(), back.members(), type)
        pair(model.layouts, back.layouts, lambda c: c.kind)
        pair(
            (e for c in model.layouts for e in c.elements),
            (e for c in back.layouts for e in c.elements),
            lambda e: (e.kind, e.field),
        )
        # У шапки нет Member; её исходные идентификаторы тоже используются в раскладке.
        old_by_address = {}
        for key, address in old_addresses.items():
            old_by_address.setdefault(address, []).append(key)
        for key, address in new_addresses.items():
            candidates = old_by_address.get(address, ())
            if key not in remap and len(candidates) == 1 and candidates[0] not in used:
                remap[key] = candidates[0]
                used.add(candidates[0])
        old_members = {m.logical_id for m in model.members()}
        referenced = {key for d in model.decisions for key in d.result_ids}
        if (referenced & old_members) - used:
            raise ValueError("Не найдено однозначное соответствие сущности из истории решений")
        # Позиционный ID свежего листа мог совпасть со старым ID другой сущности.
        for key in new_addresses:
            if key not in remap and key in used:
                remap[key] = logical_id(model.project_id, "migration/new/" + key)
        scalar_ids = {
            "logical_id",
            "target_id",
            "owner_id",
            "parent_id",
            "inside_leaf_id",
            "entity_id",
            "block_id",
            "container_id",
        }

        def rewrite(value: Any, field: str = "") -> Any:
            if is_dataclass(value) and not isinstance(value, type):
                return replace(
                    value,
                    **{
                        f.name: rewrite(getattr(value, f.name), f.name)
                        for f in fields(value)
                        if f.init and not f.name.startswith("_")
                    },
                )
            if isinstance(value, tuple):
                return tuple(
                    rewrite(v, "target_id" if field in ("guards", "root_layouts") else "")
                    for v in value
                )
            if isinstance(value, str) and field in scalar_ids:
                return remap.get(value, value)
            return value

        back = rewrite(back)
        back = replace(back, decisions=model.decisions, confirmations=model.confirmations)
        members = {m.logical_id: m for m in back.members()}

        def stamped(source, item):
            return (
                replace(source, fingerprint=leaf_fingerprint(back, item, members))
                if source
                else None
            )

        back = replace(
            back,
            layouts=tuple(
                replace(
                    c,
                    opening=stamped(c.opening, c),
                    closing=stamped(c.closing, c),
                    elements=tuple(replace(e, source=stamped(e.source, e)) for e in c.elements),
                )
                for c in back.layouts
            ),
        ).with_revision()
        if render(back, "preserve").data != rendered.data:
            raise ValueError("Перенос идентификаторов изменил текст модуля")
        return back
    except Exception as error:
        raise ValueError(action + str(error)) from error


def reconcile_authored(model: ManagerModel) -> ManagerModel:
    """Восстанавливает производные поля опубликованных собственных снимков W2/W3."""
    if model.source_files or not any(u.origin == "authored" for u in model.code_units):
        return model
    from .writer_import import refresh_code_dependencies

    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2690–2692.
    expected = (
        'Если ПравилаОбработкиДанных.Колонки.Найти("ОчисткаДанных") = Неопределено Тогда\n'
        '\tПравилаОбработкиДанных.Колонки.Добавить("ОчисткаДанных");\nКонецЕсли;'
    )
    blocks = {b.logical_id: b for b in model.retained_blocks}
    containers = {c.logical_id: c for c in model.layouts}
    removed = set()

    def entry(container):
        while container.kind == "conditional" and container.owner_id in containers:
            container = containers[container.owner_id]
        return container.name.casefold() == "заполнитьправилаобработкиданных"

    for key, container in tuple(containers.items()):
        elements = []
        for element in container.elements:
            child = containers.get(element.container_id or "")
            if child is not None and child.kind in ("code", "dispatcher"):
                # Старый мигратор давал рамке отдельный ID; импорт использует ID метода.
                element = replace(element, logical_id=child.logical_id)
            block = blocks.get(element.block_id or "")
            if (
                entry(container)
                and block is not None
                and block.kind == "scaffold"
                and block.reason == "outside_w1"
                and _tokens(block.text) == _tokens(expected)
            ):
                removed.add(block.logical_id)
                element = replace(
                    element,
                    kind="entity",
                    entity_id=logical_id(model.project_id, "header"),
                    block_id=None,
                    field="header.clear_data_column",
                )
            elements.append(element)
        containers[key] = replace(container, elements=tuple(elements))
    result = replace(
        model,
        header=replace(model.header, clear_data_column=True) if removed else model.header,
        layouts=tuple(containers.values()),
        retained_blocks=tuple(b for b in model.retained_blocks if b.logical_id not in removed),
        pkpd=tuple(
            replace(r, directions=("both",)) if set(r.directions) == {"send", "receive"} else r
            for r in model.pkpd
        ),
    )
    result = refresh_code_dependencies(replace(result, code_units=()), result)
    return result.with_revision() if result != model else model


def migrate_v3(model: ManagerModel) -> ManagerModel:
    """Подтверждает происхождение и форму, сохраняя ID правил и решений."""
    if model.source_files:
        return model
    from .writer import new_manager
    from .writer_forms import routine_close, routine_open

    # В опубликованных new_manager исходники сняты, а CodeUnit помечены authored.
    if not any(u.origin == "authored" for u in model.code_units):
        return model
    template = new_manager(
        project_id=model.project_id,
        manager_name=model.header.manager_name,
        interface_version=model.header.interface_version or 2,
        style=model.header.text_style,
    )
    prototypes = {u.name: u for u in template.code_units}
    units = []
    containers = {c.logical_id: c for c in model.layouts}
    blocks = {b.logical_id: b for b in model.retained_blocks}
    removed = set()
    for unit in model.code_units:
        prototype = prototypes.get(unit.name)
        if (
            unit.origin != "authored"
            or unit.logical_id in containers
            or prototype is None
            or unit.signature != prototype.signature
        ):
            units.append(unit)
            continue
        pair = next(
            (
                (c, e)
                for c in containers.values()
                for e in c.elements
                if e.entity_id == unit.logical_id and not e.field
            ),
            None,
        )
        if pair is None:
            units.append(unit)
            continue
        parent, element = pair
        dispatcher = unit.name in DISPATCHERS
        if dispatcher:
            block = blocks.get(element.block_id or "")
            expected = (
                routine_open(unit.name, unit.signature) + "\n" + routine_close(unit.signature)
            )
            if unit.body.strip() or block is None or _tokens(block.text) != _tokens(expected):
                units.append(unit)
                continue
            removed.add(block.logical_id)
            unit = replace(
                unit, state="editable", body="", sha256=text_hash(""), inside_leaf_id=None
            )
        elif unit.state != "editable" or "event" not in unit.roles or element.block_id:
            units.append(unit)
            continue
        unit = replace(unit, parameters_text=prototype.parameters_text)
        containers[parent.logical_id] = replace(
            parent,
            elements=tuple(
                replace(
                    e, kind="container", entity_id=None, block_id=None, container_id=unit.logical_id
                )
                if e.logical_id == element.logical_id
                else e
                for e in parent.elements
            ),
        )
        prototype_layout = next(c for c in template.layouts if c.logical_id == prototype.logical_id)
        elements = []
        if dispatcher:
            for e in prototype_layout.elements:
                if e.block_id:
                    old = next(b for b in template.retained_blocks if b.logical_id == e.block_id)
                    key = logical_id(
                        model.project_id, "snapshot/trivia/" + unit.logical_id + "/" + e.logical_id
                    )
                    blocks[key] = replace(old, logical_id=key, owner_id=unit.logical_id)
                    elements.append(replace(e, logical_id=key, block_id=key))
        else:
            elements.append(
                LayoutElement(
                    logical_id(model.project_id, "snapshot/body/" + unit.logical_id),
                    "entity",
                    entity_id=unit.logical_id,
                    field="body",
                )
            )
        containers[unit.logical_id] = LayoutContainer(
            unit.logical_id,
            "dispatcher" if dispatcher else "code",
            unit.name,
            owner_id=prototype_layout.owner_id,
            signature=unit.signature,
            elements=tuple(elements),
        )
        units.append(unit)

    pks = next(
        (u for u in units if u.name.casefold() == "добавитьпкс" and u.origin == "authored"), None
    )
    pks_leaf = next(
        (
            e
            for c in containers.values()
            for e in c.elements
            if pks and e.entity_id == pks.logical_id
        ),
        None,
    )
    pks_block = blocks.get(pks_leaf.block_id or "") if pks_leaf else None
    verified = pks_block is not None and any(
        _tokens(pks_block.text) == _tokens(form)
        for form in helper_forms("ДобавитьПКС", model.header.interface_version)
    )
    if verified and not any(u.name.casefold() == "добавитьпктч" for u in units):
        prototype = prototypes["ДобавитьПКТЧ"]
        key = logical_id(model.project_id, "snapshot/helper/ДобавитьПКТЧ")
        block_key = logical_id(model.project_id, "snapshot/helper-leaf/ДобавитьПКТЧ")
        template_leaf = next(
            e for c in template.layouts for e in c.elements if e.entity_id == prototype.logical_id
        )
        template_block = next(
            b for b in template.retained_blocks if b.logical_id == template_leaf.block_id
        )
        parent = next(c for c in containers.values() if pks_leaf in c.elements)
        text = template_block.text
        blocks[block_key] = RetainedBlock(
            logical_id=block_key,
            name="Текст",
            state="retained",
            kind="routine",
            text=text,
            sha256=text_hash(text),
            file_id="",
            source_hash="",
            owner_id=parent.logical_id,
            reason=template_block.reason,
        )
        units.append(replace(prototype, logical_id=key, inside_leaf_id=None))
        position = parent.elements.index(pks_leaf)
        containers[parent.logical_id] = replace(
            parent,
            elements=(
                *parent.elements[:position],
                LayoutElement(block_key, "entity", entity_id=key, block_id=block_key),
                *parent.elements[position:],
            ),
        )
        model = replace(model, header=replace(model.header, helper_variant="modern"))
    result = replace(
        model,
        code_units=tuple(units),
        layouts=tuple(containers.values()),
        retained_blocks=tuple(b for key, b in blocks.items() if key not in removed),
    )
    return result.with_revision()
