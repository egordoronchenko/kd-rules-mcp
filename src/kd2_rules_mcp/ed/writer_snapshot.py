"""Миграция собственных рамок W1/W2 после проверки хеша старого снимка."""

from dataclasses import replace

from .forms import DISPATCHERS, helper_forms
from .lexer import tokenize
from .writer_model import (
    LayoutContainer,
    LayoutElement,
    ManagerModel,
    RetainedBlock,
    logical_id,
    text_hash,
)


def _tokens(text):
    # Комментарии тоже сравниваются: миграция не должна терять чужой текст.
    return tuple((t.kind, t.value if t.kind == "string" else t.folded) for t in tokenize(text))


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
