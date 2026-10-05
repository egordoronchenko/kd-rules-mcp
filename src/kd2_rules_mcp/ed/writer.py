"""Детерминированный вывод раскладки W1, без исполнения BSL и обращений к базам."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from . import writer_forms as forms
from .canonical import model_addresses
from .writer_import import infer_module_styles
from .writer_model import (
    CodeUnit,
    Identification,
    ImportReport,
    LayoutContainer,
    LayoutElement,
    ManagerModel,
    ObjectRule,
    ProcessingRule,
    Property,
    RuleUse,
    SearchSet,
    SourceSlice,
    TextStyle,
    Value,
    leaf_fingerprint,
    validate_model,
)

RenderMode = Literal["preserve", "canonical"]


@dataclass(frozen=True, slots=True)
class RenderEntry:
    leaf_id: str
    kind: str
    address: str
    state: Literal["regenerated", "verbatim", "retained"]
    byte_start: int
    byte_end: int
    source_equal: bool | None
    form: str = ""


@dataclass(frozen=True, slots=True)
class RenderReport:
    """Смещения — байты результата с BOM.

    exchange_verified — проверенность формы интерфейса, не сертификация данного обмена.
    use_source_style сообщает об индивидуальных подсказках листа; мода модуля применяется всегда.
    """

    mode: RenderMode
    entries: tuple[RenderEntry, ...]
    exchange_verified: bool
    use_source_style: bool = True

    @property
    def counts(self) -> dict[str, dict[str, int]]:
        """Число листьев по виду и способу вывода."""
        result: dict[str, dict[str, int]] = {}
        for entry in self.entries:
            row = result.setdefault(entry.kind, {"regenerated": 0, "verbatim": 0, "retained": 0})
            row[entry.state] += 1
        return result


@dataclass(frozen=True, slots=True)
class RenderResult:
    """text не содержит BOM; data — окончательный UTF-8 файл, включая BOM."""

    text: str
    data: bytes
    report: RenderReport


def new_manager(
    *,
    project_id: str = "manager",
    manager_name: str = "Manager",
    interface_version: int = 2,
    style: TextStyle | None = None,
) -> ManagerModel:
    """Создаёт полный минимальный интерфейс из собственных шаблонов W1."""
    from .reader import read_manager_text
    from .writer_import import import_manager

    style = style or TextStyle()
    if style.encoding != "utf-8":
        raise ValueError("Писатель поддерживает UTF-8")
    if style.indent != "\t" or style.newline == "mixed":
        raise ValueError("Новый менеджер использует табуляцию и единые LF либо CRLF")
    if interface_version not in (1, 2, 3):
        raise ValueError("Поддержаны интерфейсы 1, 2 и 3")
    text = forms.empty_module(interface_version, manager_name)
    if "\n" in manager_name or "\r" in manager_name:
        raise ValueError("Имя менеджера должно занимать одну строку")
    text = text.replace("\n", "\r\n" if style.newline == "mixed" else style.newline)
    model, _ = import_manager(
        read_manager_text(("\ufeff" if style.bom else "") + text),
        project_id=project_id,
        manager_name=manager_name,
    )
    # Шаблонные сохранённые тела — часть модели, исходного файла у проекта нет.
    return replace(
        model,
        module_styles=(),
        source_files=(),
        source_map=(),
        import_report=ImportReport(()),
        code_units=tuple(
            replace(u, origin="authored", file_id="", body_start=0, body_end=0)
            for u in model.code_units
        ),
        retained_blocks=tuple(
            replace(b, file_id="", source_hash="", char_start=0, char_end=0)
            for b in model.retained_blocks
        ),
        layouts=tuple(
            replace(
                c,
                opening=None,
                closing=None,
                elements=tuple(replace(e, source=None) for e in c.elements),
            )
            for c in model.layouts
        ),
    ).with_revision()


def render(
    model: ManagerModel, mode: RenderMode = "preserve", *, use_source_style: bool = True
) -> RenderResult:
    """Выводит каждый лист ровно один раз; непрозрачный текст никогда не переписывает.

    use_source_style отключает индивидуальные подсказки листа. Сохранённый module_styles
    применяется и без них: новые сущности следуют моде своего вида и направления.
    """
    if mode not in ("preserve", "canonical"):
        raise ValueError("Режим отрисовки: preserve либо canonical")
    validate_model(model)
    if model.header.text_style.encoding != "utf-8":
        raise ValueError("Писатель поддерживает UTF-8")
    if not model.layouts:
        raise ValueError("У модели нет раскладки")
    if not model.source_files and not any(
        e.field == "header.interface_version" for c in model.layouts for e in c.elements
    ):
        raise ValueError("Новый менеджер создаётся функцией new_manager с полным интерфейсом")
    containers = {c.logical_id: c for c in model.layouts}
    members = {m.logical_id: m for m in model.members()}
    blocks = {b.logical_id: b for b in model.retained_blocks}
    sources = {s.file_id: s.text for s in model.source_files}
    addresses = model_addresses(model)
    newline = (
        "\r\n" if model.header.text_style.newline == "mixed" else model.header.text_style.newline
    )
    indent = model.header.text_style.indent
    module_styles = model.module_styles
    if not module_styles and model.source_files:
        module_styles = infer_module_styles(model)
    styles = {(s.kind, s.direction): s for s in module_styles}

    def entity_style(owner: LayoutContainer, kind: str | None = None):
        current = owner
        while current.owner_id and current.kind != "rule":
            current = containers[current.owner_id]
        rule = members.get(current.logical_id)
        if not isinstance(rule, ObjectRule | ProcessingRule):
            return None
        direction = "receive" if any(d in ("receive", "both") for d in rule.directions) else "send"
        return styles.get((kind or ("pko" if isinstance(rule, ObjectRule) else "pod"), direction))

    def width_for(element: LayoutElement, owner: LayoutContainer, default: int) -> int:
        if use_source_style and element.source and element.source.assignment_width:
            return element.source.assignment_width
        kind = "identification" if element.field == "mode" else None
        style = entity_style(owner, kind)
        if style:
            if element.field == "group_flag" and element.field not in dict(style.field_widths):
                return default
            return dict(style.field_widths).get(element.field, style.assignment_width or default)
        return default

    parts: list[str] = []
    entries: list[RenderEntry] = []
    position = 3 if model.header.text_style.bom else 0
    previous_span: SourceSlice | None = None

    def original(span: SourceSlice | None) -> str | None:
        if span and span.file_id in sources:
            return sources[span.file_id][span.char_start : span.char_end]
        return None

    def emit(key, kind, address, text, state, source, form="", span=None):
        nonlocal position, previous_span
        adjacent = (
            previous_span is not None
            and span is not None
            and previous_span.file_id == span.file_id
            and previous_span.char_end == span.char_start
        )
        if (
            parts
            and text
            and not parts[-1].endswith(("\n", "\r"))
            and not text.startswith(("\n", "\r"))
            and not adjacent
        ):
            parts[-1] += newline
            position += len(newline.encode("utf-8"))
            entries[-1] = replace(entries[-1], byte_end=position, source_equal=False)
        end = position + len(text.encode("utf-8"))
        entries.append(
            RenderEntry(
                key,
                kind,
                address,
                state,
                position,
                end,
                text == source if source is not None else None,
                form,
            )
        )
        parts.append(text)
        position = end
        previous_span = span

    def generated(text: str, depth: int, tail: str = "", unit: str | None = None) -> str:
        text = text.rstrip("\r\n")
        if tail:
            text += " " + tail
        return (
            newline.join(
                (unit or indent) * depth + line if line else "" for line in text.split("\n")
            )
            + newline
        )

    def field_text(element: LayoutElement, owner: LayoutContainer) -> str:
        # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/
        # ШаблоныТекстовМодулей/Ext/Template.txt:23–29,44–56,64–69.
        member = members.get(element.entity_id or "")
        name = element.field
        if name == "header.title":
            date = model.header.generated_at.value
            if any(c in str(model.header.title.value) + str(date) for c in "\r\n"):
                raise ValueError("Заголовок менеджера должен занимать одну строку")
            if model.header.title.state == "unset":
                if model.header.generated_at.state != "unset":
                    raise ValueError("Дата заголовка требует названия")
                return "// Менеджер обмена через универсальный формат"
            return (
                "// Менеджер обмена через универсальный формат ("
                + str(model.header.title.value)
                + (" от " + str(date) if date is not None else "")
                + ")"
            )
        if name == "header.interface_version":
            return forms.version(model.header.interface_version or 2)
        if name == "properties_start":
            return "СвойстваШапки = ПравилоКонвертации.Свойства;"
        if name == "mode":
            assert isinstance(member, Identification)
            width = width_for(element, owner, 53)
            return (
                "ПравилоКонвертации.ВариантИдентификации".ljust(width)
                + " = "
                + forms.literal(member.mode)
                + ";"
            )
        mapping = {
            "name": "ИмяПКО" if isinstance(member, ObjectRule) else "Имя",
            "configuration_object": "ОбъектДанных",
            "format_object": "ОбъектФормата",
            "group_flag": "ПравилоДляГруппыСправочника",
            "configuration_selection": "ОбъектВыборкиМетаданные",
            "format_selection": "ОбъектВыборкиФормат",
            "clear_data": "ОчисткаДанных",
        }
        if name == "used_pko":
            assert isinstance(member, ProcessingRule)
            index = [e.logical_id for e in owner.elements if e.field == name].index(
                element.logical_id
            )
            reference = member.used_pko[index]
            target = members.get(reference.target_id or "")
            return (
                "ПравилоОбработки.ИспользуемыеПКО.Добавить("
                + forms.literal(Value("string", target.name if target else reference.name))
                + ");"
            )
        if name == "extensions":
            assert isinstance(member, ObjectRule)
            index = [e.logical_id for e in owner.elements if e.field == name].index(
                element.logical_id
            )
            # Template.txt:57: табуляция после последнего расширения формы получения.
            style = entity_style(owner)
            suffix = (
                ""
                if use_source_style and element.source
                else dict(style.line_suffixes).get(name)
                if style
                else None
            )
            suffix = (
                suffix
                if suffix is not None
                else (
                    "\t"
                    if (
                        (not use_source_style or element.source is None)
                        and any(d in ("receive", "both") for d in member.directions)
                        and index == len(member.extensions) - 1
                    )
                    else ""
                )
            )
            return (
                "ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта("
                "ПравилоКонвертации, "
                + forms.literal(Value("string", member.extensions[index]))
                + ");"
                + suffix
            )
        prefix = "ПравилоКонвертации." if isinstance(member, ObjectRule) else "ПравилоОбработки."
        width = (
            forms.assignment_width(member.directions, name)
            if isinstance(member, ObjectRule)
            else 40
        )
        width = width_for(element, owner, width)
        assert isinstance(member, ObjectRule | ProcessingRule)
        value = Value("string", member.name) if name == "name" else getattr(member, name)
        return (prefix + mapping[name]).ljust(width) + " = " + forms.literal(value) + ";"

    def entity_text(element: LayoutElement, owner: LayoutContainer) -> str:
        if element.field:
            return field_text(element, owner)
        member = members[element.entity_id or ""]
        if isinstance(member, Property):
            arguments = ["СвойстваШапки"]
            for flag, value in zip(
                member.argument_presence[1:], member.argument_values, strict=True
            ):
                arguments.append(forms.literal(value) if flag else "")
            rule = members.get(owner.logical_id)
            if not isinstance(rule, ObjectRule):
                parent = owner
                while parent.owner_id and parent.kind != "rule":
                    parent = containers[parent.owner_id]
                rule = members.get(parent.logical_id)
            if isinstance(rule, ObjectRule) and len(arguments) >= 3:
                maximum = max((len(p.configuration_property) for p in rule.properties), default=0)
                arguments[2] = " " * (maximum - len(member.configuration_property)) + arguments[2]
            return forms.property_call(tuple(arguments))
        if isinstance(member, SearchSet):
            return forms.search(member.fields)
        if isinstance(member, RuleUse):
            rule = members[member.rule.target_id or ""]
            assert isinstance(rule, ObjectRule | ProcessingRule)
            return (
                rule.procedure_name
                + "("
                + ", ".join(p.name or "" for p in rule.signature.parameters)
                + ");"
            )
        if isinstance(member, CodeUnit) and member.state == "editable":
            return (
                forms.routine_open(member.name, member.signature)
                + "\n"
                + member.body.replace("\r\n", "\n").lstrip("\r\n")
                + forms.routine_close(member.signature)
            )
        raise ValueError("Нет формы W1 для листа " + addresses.get(member.logical_id, member.name))

    def frame(container: LayoutContainer, close: bool, span: SourceSlice | None) -> str:
        # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/
        # ШаблоныТекстовМодулей/Ext/Template.txt:21–22,41–43,62–63.
        if container.kind == "conditional":
            return (
                "КонецЕсли;"
                if close
                else forms.condition(container.direction or "receive", container.branch)
            )
        if close:
            return forms.routine_close(container.signature)
        result = forms.routine_open(container.name, container.signature)
        rule = members.get(container.logical_id)
        if isinstance(rule, ObjectRule):
            style = entity_style(container)
            gap = (
                span.opening_blank_lines
                if use_source_style and span
                else style.opening_blank_lines
                if style
                else 0
            )
            result += (
                "\n" * (gap + 1) + "\tПравилоКонвертации = ОбменДаннымиXDTOСервер."
                "ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);"
            )
        elif isinstance(rule, ProcessingRule):
            style = entity_style(container)
            gap = (
                span.opening_blank_lines
                if use_source_style and span
                else style.opening_blank_lines
                if style
                else 0
            )
            width = dict(style.field_widths).get("opening", 40) if style else 40
            result += (
                "\n" * (gap + 1)
                + "\t"
                + "ПравилоОбработки".ljust(width)
                + " = ПравилаОбработкиДанных.Добавить();"
            )
        style = entity_style(container)
        return result.replace("\t", style.indent) if style else result

    def walk(key: str, depth: int = 0, close_branch: bool = True):
        container = containers[key]
        framed = (
            container.kind in ("rule", "entrypoint", "conditional") and container.branch != "chain"
        )
        for close in (False, True):
            span = container.closing if close else container.opening
            # Промежуточная ветка цепочки не имеет своего КонецЕсли.
            has_frame = framed and (not close or close_branch)
            if has_frame:
                source = original(span)
                valid = span is not None and span.fingerprint == leaf_fingerprint(
                    model, container, members
                )
                container_style = entity_style(container)
                text = (
                    source
                    if mode == "preserve" and source is not None and valid
                    else generated(
                        frame(container, close, span),
                        depth,
                        unit=container_style.indent if container_style else None,
                    )
                )
                emit(
                    key + ("/closing" if close else "/opening"),
                    "pko"
                    if isinstance(members.get(key), ObjectRule)
                    else "pod"
                    if isinstance(members.get(key), ProcessingRule)
                    else container.kind,
                    addresses[key],
                    text,
                    "verbatim" if text is source else "regenerated",
                    source,
                    "closing" if close else "opening",
                    span,
                )
            if close:
                break
            child_depth = depth + (1 if framed else 0)
            for n, element in enumerate(container.elements):
                if element.container_id:
                    walk(
                        element.container_id,
                        child_depth,
                        container.branch != "chain" or n == len(container.elements) - 1,
                    )
                    continue
                source = original(element.source)
                if element.block_id:
                    block = blocks[element.block_id]
                    emit(
                        element.logical_id,
                        block.kind,
                        addresses[element.block_id],
                        block.text,
                        "retained",
                        source,
                        span=element.source,
                    )
                    continue
                valid = (
                    element.source is not None
                    and element.source.fingerprint == leaf_fingerprint(model, element, members)
                    and element.source.container_id in ("", container.logical_id)
                )
                if mode == "preserve" and source is not None and valid:
                    text, state = source, "verbatim"
                else:
                    local_depth = child_depth
                    style = entity_style(
                        container, "identification" if element.field == "mode" else None
                    )
                    unit = style.indent if style else indent
                    if source is not None and use_source_style:
                        first = source.splitlines()[0] if source else ""
                        prefix = first[: len(first) - len(first.lstrip(" \t"))]
                        if element.source and element.source.container_id not in (
                            "",
                            container.logical_id,
                        ):
                            prefix = unit * local_depth
                        text = entity_text(element, container).rstrip("\r\n")
                        if element.trailing_comment:
                            text += " " + element.trailing_comment
                        if element.source:
                            text += element.source.line_suffix
                        text = newline.join(prefix + line for line in text.split("\n")) + newline
                    else:
                        text = generated(
                            entity_text(element, container),
                            local_depth,
                            element.trailing_comment,
                            unit,
                        )
                    state = "regenerated"
                member = members.get(element.entity_id or "")
                kind = (
                    "header"
                    if element.field.startswith("header.")
                    else "pko"
                    if isinstance(member, ObjectRule)
                    else "pod"
                    if isinstance(member, ProcessingRule)
                    else "pks"
                    if isinstance(member, Property)
                    else "identification"
                    if isinstance(member, Identification)
                    else "search_set"
                    if isinstance(member, SearchSet)
                    else "rule_use"
                    if isinstance(member, RuleUse)
                    else "routine"
                )
                emit(
                    element.logical_id,
                    kind,
                    addresses.get(
                        element.entity_id or "", addresses.get(element.logical_id, "Конвертация")
                    ),
                    text,
                    state,
                    source,
                    element.field or type(member).__name__,
                    element.source,
                )

    for root in model.root_layouts:
        walk(root)
    text = "".join(parts)
    data = (b"\xef\xbb\xbf" if model.header.text_style.bom else b"") + text.encode("utf-8")
    return RenderResult(
        text,
        data,
        RenderReport(mode, tuple(entries), model.header.interface_version == 2, use_source_style),
    )
