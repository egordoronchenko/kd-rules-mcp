"""Смысловой дифф двух документов правил КД 2 по адресам правил.

Правила сопоставляются по адресу (`validation/address.py`). Значения перед сравнением
приводятся правилами канонической формы К3–К7: переводы строк, пустое и значение по
умолчанию не отличаются от отсутствия. Порядок правил в разделе не учитывается;
перестановку ПКС и ПКЗ включает параметр `order`.
"""

import difflib
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from lxml import etree

from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.kd2.canonical import (
    FALSE_DEFAULT_FLAG_ATTRS,
    is_default_value,
    normalize_newlines,
)
from kd2_rules_mcp.kd2.model import (
    ExchangeRules,
    Node,
    RegistrationRules,
    RulesDocument,
)
from kd2_rules_mcp.kd2.schema import Child, Scalar, kind
from kd2_rules_mcp.validation.address import pks_address, pks_segment, pkz_address, rule_address
from kd2_rules_mcp.validation.handlers import ALGORITHM_EVENT, EVENT_AREAS

# Поля заголовка, которые писатель КД заполняет заново при каждой выгрузке.
HEADER_VOLATILE = ("ДатаВремяСоздания", "Ид")

# Имена разделов в ответе инструментов.
SECTIONS = (
    "header",
    "pko",
    "pks",
    "pkz",
    "pvd",
    "pod",
    "algorithms",
    "queries",
    "parameters",
    "registration",
)

# Последняя строка усечённого построчного диффа обработчика.
_TRUNCATED = "… усечено"

# Виды, у которых поле `Текст` — код алгоритма или текст запроса, а не подпись.
_TEXT_HANDLERS = frozenset({"algorithm", "query"})

# Вложенные списки, чей адрес сам по себе не адрес правила: к нему добавляется адрес владельца.
_NESTED = frozenset({"search", "filter", "property"})

# Дочерний список узла → (режим сопоставления, раздел или None — раздел владельца).
_CHILD_LISTS: dict[str, tuple[str, str | None]] = {
    "Свойства": ("pks", "pks"),
    "Значения": ("pkz", "pkz"),
    "НастройкаВариантовПоискаОбъектов": ("search", None),
    "ОтборПоСвойствамПланаОбмена": ("filter", None),
    "ОтборПоСвойствамОбъекта": ("filter", None),
    "ТаблицаСвойствОбъекта": ("property", None),
    "ТаблицаСвойствПланаОбмена": ("property", None),
}

# Списки верхнего уровня правил обмена: тег → (раздел, режим).
_EXCHANGE_LISTS: tuple[tuple[str, str, str], ...] = (
    ("ПравилаКонвертацииОбъектов", "pko", "plain"),
    ("ПравилаВыгрузкиДанных", "pvd", "plain"),
    ("ПравилаОчисткиДанных", "pod", "plain"),
    ("Алгоритмы", "algorithms", "plain"),
    ("Запросы", "queries", "plain"),
    ("Параметры", "parameters", "plain"),
    ("Обработки", "header", "plain"),
)

ChangeKind = Literal["added", "removed", "changed"]


@dataclass(frozen=True, slots=True)
class RuleChange:
    """Одно изменение правила: добавлено, удалено или изменено поле."""

    section: str
    address: str
    change: ChangeKind
    field: str | None = None
    old: str | None = None
    new: str | None = None
    handler_diff: list[str] | None = None


@dataclass(frozen=True, slots=True)
class _Field:
    """Нормализованное значение поля; `handler` — отдавать построчный дифф, а не текст."""

    text: str
    handler: bool


def ignored_header_fields(include_header: bool) -> list[str]:
    """Поля заголовка, исключённые из сравнения."""
    return [] if include_header else list(HEADER_VOLATILE)


def diff_rules(
    left: RulesDocument,
    right: RulesDocument,
    *,
    include_header: bool = False,
    order: bool = False,
) -> list[RuleChange]:
    """Изменения правого документа относительно левого.

    Разные виды (правила обмена и правила регистрации) сравнивать нельзя.
    """
    left_kind, right_kind = _kind_title(left), _kind_title(right)
    if left_kind != right_kind:
        raise Kd2Error(f"Разные виды документов: слева {left_kind}, справа {right_kind}")
    changes: list[RuleChange] = []
    _diff_header(left.root, right.root, include_header=include_header, out=changes)
    if isinstance(left, ExchangeRules) and isinstance(right, ExchangeRules):
        _diff_exchange(left, right, order=order, out=changes)
    elif isinstance(left, RegistrationRules) and isinstance(right, RegistrationRules):
        _diff_registration(left, right, out=changes)
    return changes


def _kind_title(document: RulesDocument) -> str:
    if isinstance(document, ExchangeRules):
        return "правила обмена"
    if isinstance(document, RegistrationRules):
        return "правила регистрации"
    return document.root_tag


def _diff_header(left: Node, right: Node, *, include_header: bool, out: list[RuleChange]) -> None:
    _diff_maps(
        _field_map(left, skip_volatile=not include_header),
        _field_map(right, skip_volatile=not include_header),
        "header",
        "",
        out,
    )


def _diff_exchange(
    left: ExchangeRules, right: ExchangeRules, *, order: bool, out: list[RuleChange]
) -> None:
    for tag, section, mode in _EXCHANGE_LISTS:
        _diff_list(
            left.root.child(tag),
            right.root.child(tag),
            section=section,
            mode=mode,
            pko_code="",
            prefix="",
            parent_address="",
            order=order,
            compare_shell=True,
            out=out,
        )


def _diff_registration(
    left: RegistrationRules, right: RegistrationRules, *, out: list[RuleChange]
) -> None:
    _diff_list(
        left.root.child("СоставПланаОбмена"),
        right.root.child("СоставПланаОбмена"),
        section="header",
        mode="plan",
        pko_code="",
        prefix="",
        parent_address="",
        order=False,
        compare_shell=True,
        out=out,
    )
    _diff_list(
        left.root.child("ПравилаРегистрацииОбъектов"),
        right.root.child("ПравилаРегистрацииОбъектов"),
        section="registration",
        mode="pro",
        pko_code="",
        prefix="",
        parent_address="",
        order=False,
        compare_shell=True,
        out=out,
    )


def _diff_list(
    left: Node | None,
    right: Node | None,
    *,
    section: str,
    mode: str,
    pko_code: str,
    prefix: str,
    parent_address: str,
    order: bool,
    compare_shell: bool,
    out: list[RuleChange],
) -> None:
    """Сопоставляет элементы списка по адресу. Добавленный узел не раскрывается."""
    if compare_shell:
        _diff_maps(
            _field_map(left) if left is not None else {},
            _field_map(right) if right is not None else {},
            section,
            parent_address,
            out,
        )
    left_rows = _rows(_items(left), mode, pko_code, prefix, parent_address)
    right_rows = _rows(_items(right), mode, pko_code, prefix, parent_address)
    right_by_key = {address: (node, path) for address, node, path in right_rows}
    left_keys = {address for address, _, _ in left_rows}
    for address, node, path in left_rows:
        other = right_by_key.get(address)
        if other is None:
            out.append(RuleChange(section, address, "removed"))
            continue
        _diff_matched(
            node,
            other[0],
            section=section,
            address=address,
            mode=mode,
            pko_code=pko_code,
            path=path,
            order=order,
            out=out,
        )
    for address, _, _ in right_rows:
        if address not in left_keys:
            out.append(RuleChange(section, address, "added"))
    if order and mode in ("pks", "pkz"):
        _diff_order(left_rows, right_rows, section=section, address=parent_address, out=out)


def _diff_matched(
    left: Node,
    right: Node,
    *,
    section: str,
    address: str,
    mode: str,
    pko_code: str,
    path: str,
    order: bool,
    out: list[RuleChange],
) -> None:
    _diff_maps(_field_map(left), _field_map(right), section, address, out)
    if left.kind.name == "data_processor" or right.kind.name == "data_processor":
        _diff_processor_size(left, right, section, address, out)
    owner_code = left.code if left.kind.name == "pko" else pko_code
    for tag in _list_tags(left, right):
        child_mode, child_section = _CHILD_LISTS.get(tag, ("plain", None))
        _diff_list(
            left.child(tag),
            right.child(tag),
            section=child_section or section,
            mode=child_mode,
            pko_code=owner_code,
            prefix="",
            parent_address=address,
            order=order,
            compare_shell=True,
            out=out,
        )
    if left.kind.items or right.kind.items:
        _diff_list(
            left,
            right,
            section=section,
            mode=mode,
            pko_code=owner_code,
            prefix=f"{path}/" if mode == "pks" else "",
            parent_address=address,
            order=order,
            compare_shell=False,
            out=out,
        )


def _diff_order(
    left_rows: list[tuple[str, Node, str]],
    right_rows: list[tuple[str, Node, str]],
    *,
    section: str,
    address: str,
    out: list[RuleChange],
) -> None:
    """Перестановка тех же правил — одно изменение `#порядок`, не пара удалено/добавлено."""
    left_keys = [key for key, _, _ in left_rows]
    right_keys = [key for key, _, _ in right_rows]
    if left_keys == right_keys or Counter(left_keys) != Counter(right_keys):
        return
    out.append(
        RuleChange(
            section,
            address,
            "changed",
            field="#порядок",
            old=clip("\n".join(left_keys)),
            new=clip("\n".join(right_keys)),
        )
    )


def _diff_processor_size(
    left: Node, right: Node, section: str, address: str, out: list[RuleChange]
) -> None:
    """Тело обработки не сравнивается по содержимому: только размер после К3."""
    left_body = normalize_newlines(left.text or "")
    right_body = normalize_newlines(right.text or "")
    if left_body == right_body:
        return
    out.append(
        RuleChange(
            section,
            address,
            "changed",
            field="#размер",
            old=str(len(left_body)),
            new=str(len(right_body)),
        )
    )


def _rows(
    items: list[Node], mode: str, pko_code: str, prefix: str, parent_address: str
) -> list[tuple[str, Node, str]]:
    raw = [(_local_key(item, mode, pko_code, index), item) for index, item in enumerate(items)]
    rows: list[tuple[str, Node, str]] = []
    for key, node in _with_suffixes(raw):
        if mode == "pks":
            path = f"{prefix}{key}"
            address = pks_address(pko_code, path)
        elif mode in _NESTED and parent_address:
            path = ""
            address = f"{parent_address} / {key}"
        else:
            path = ""
            address = key
        rows.append((address, node, path))
    return rows


def _local_key(node: Node, mode: str, pko_code: str, index: int) -> str:
    if mode == "pks":
        return pks_segment(node, index)
    if mode == "pkz":
        if node.kind.name == "pkz":
            return pkz_address(pko_code, str(node.values.get("Источник", "")))
        return f"ПКО «{pko_code}» / {rule_address(node)}"
    if mode == "pro" and node.kind.name == "pro":
        meta = str(node.values.get("ОбъектМетаданныхИмя", "")).strip()
        base = rule_address(node)
        return f"{base} / {meta}" if meta else base
    if mode == "plan":
        return f"состав «{node.values.get('Тип', '')}»"
    if mode == "filter":
        return _filter_key(node)
    if mode == "property":
        return f"свойство «{node.values.get('Наименование', '')}»"
    return rule_address(node)


def _filter_key(node: Node) -> str:
    if node.is_group:
        return rule_address(node)
    bits = [
        str(node.values.get("СвойствоПланаОбмена", "")),
        str(node.values.get("СвойствоОбъекта", "")),
        str(node.values.get("ВидСравнения", "")),
        str(node.values.get("ЗначениеКонстанты", "")),
    ]
    label = " ".join(bit for bit in bits if bit).strip()
    return f"{node.kind.title} «{label}»"


def _with_suffixes(pairs: list[tuple[str, Node]]) -> list[tuple[str, Node]]:
    """Повтор адреса: первый ключ без номера, следующие — ` #2`, ` #3`, …"""
    seen: dict[str, int] = {}
    result: list[tuple[str, Node]] = []
    for key, node in pairs:
        seen[key] = seen.get(key, 0) + 1
        number = seen[key]
        result.append((key if number == 1 else f"{key} #{number}", node))
    return result


def _items(node: Node | None) -> list[Node]:
    return list(node.items) if node is not None else []


def _list_tags(left: Node, right: Node) -> list[str]:
    tags = _list_child_tags(left)
    for tag in _list_child_tags(right):
        if tag not in tags:
            tags.append(tag)
    return tags


def _list_child_tags(node: Node) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for slot in node.kind.slots:
        if isinstance(slot, Child) and kind(slot.kind).items:
            tags.append(slot.tag)
            seen.add(slot.tag)
    for tag, child in node.children.items():
        if tag not in seen and bool(child.kind.items):
            tags.append(tag)
    return tags


def _diff_maps(
    left: dict[str, _Field],
    right: dict[str, _Field],
    section: str,
    address: str,
    out: list[RuleChange],
) -> None:
    names = list(left)
    names.extend(name for name in right if name not in left)
    for name in names:
        before = left.get(name)
        after = right.get(name)
        if before == after:
            continue
        old = before.text if before is not None else ""
        new = after.text if after is not None else ""
        handler = (before.handler if before is not None else False) or (
            after.handler if after is not None else False
        )
        if handler:
            lines = _handler_lines(old, new)
            if lines is None:
                continue
            out.append(RuleChange(section, address, "changed", field=name, handler_diff=lines))
            continue
        out.append(
            RuleChange(section, address, "changed", field=name, old=clip(old), new=clip(new))
        )


def _field_map(node: Node, *, skip_volatile: bool = False) -> dict[str, _Field]:
    """Поля узла без списков правил: атрибуты, простые значения, одиночные вложенные узлы."""
    fields: dict[str, _Field] = {}
    _collect(node, fields, "", skip_volatile=skip_volatile)
    return fields


def _collect(node: Node, fields: dict[str, _Field], prefix: str, *, skip_volatile: bool) -> None:
    known_attrs = {attr.name for attr in node.kind.attrs}
    for attr in node.kind.attrs:
        if attr.name in node.attrs:
            _put_attr(fields, node, prefix, attr.name, node.attrs[attr.name])
    for name, value in node.attrs.items():
        if name not in known_attrs:
            _put_attr(fields, node, prefix, name, value)
    known_leaves = set(node.kind.leaves)
    for leaf in node.kind.leaves.values():
        if skip_volatile and not prefix and leaf.tag in HEADER_VOLATILE:
            continue
        if leaf.tag in node.values:
            _put_value(fields, node, prefix, leaf.tag, node.values[leaf.tag])
    for tag, value in node.values.items():
        if tag not in known_leaves:
            _put_value(fields, node, prefix, tag, value)
    if node.kind.name != "data_processor" and node.text:
        text = _norm_leaf(node.tag, node.text)
        if text is not None:
            fields[prefix[:-1] if prefix else "#значение"] = _Field(text, False)
    marker = _marker(node)
    if marker is not None:
        fields[f"{prefix}#текст"] = _Field(marker, False)
    unknown = _unknown_text(node)
    if unknown is not None:
        fields[f"{prefix}#неизвестное"] = _Field(unknown, False)
    seen: set[str] = set()
    for slot in node.kind.slots:
        if isinstance(slot, Child) and not kind(slot.kind).items:
            child = node.children.get(slot.tag)
            if child is not None:
                _collect(child, fields, f"{prefix}{slot.tag}.", skip_volatile=False)
            seen.add(slot.tag)
    for tag, child in node.children.items():
        if tag not in seen and not child.kind.items:
            _collect(child, fields, f"{prefix}{tag}.", skip_volatile=False)


def _put_attr(fields: dict[str, _Field], node: Node, prefix: str, name: str, value: Scalar) -> None:
    text = _norm_attr(name, value)
    if text is None:
        return
    fields[f"{prefix}{name}"] = _Field(text, _is_handler(node.kind.name, name))


def _put_value(fields: dict[str, _Field], node: Node, prefix: str, tag: str, value: Scalar) -> None:
    text = _norm_leaf(tag, value)
    if text is None:
        return
    fields[f"{prefix}{tag}"] = _Field(text, _is_handler(node.kind.name, tag))


def _norm_leaf(tag: str, value: Scalar) -> str | None:
    text = _scalar_text(value)
    if is_default_value(tag, text):
        return None
    return text


def _norm_attr(name: str, value: Scalar) -> str | None:
    """К6: ложь у флага-атрибута равна отсутствию. Пустая строка атрибута значима."""
    text = _scalar_text(value)
    if name in FALSE_DEFAULT_FLAG_ATTRS and text == "false":
        return None
    return text


def _scalar_text(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return normalize_newlines(str(value))


def _is_handler(kind_name: str, tag: str) -> bool:
    """Событие из `EVENT_AREAS` или текст алгоритма и запроса. Новый список событий не заводится."""
    if (kind_name, tag) in EVENT_AREAS:
        return True
    return kind_name in _TEXT_HANDLERS and tag == ALGORITHM_EVENT


def _marker(node: Node) -> str | None:
    parts: list[str] = []
    for value in (node.leading_text, node.trailing_text):
        if not value:
            continue
        stripped = normalize_newlines(value).strip()
        if stripped:
            parts.append(stripped)
    return "\n".join(parts) if parts else None


def _unknown_text(node: Node) -> str | None:
    if not node.unknown:
        return None
    parts: list[str] = []
    for element in node.unknown:
        raw = etree.tostring(element, encoding="unicode", with_tail=False)
        parts.append(normalize_newlines(str(raw)).strip())
    text = "\n".join(part for part in parts if part)
    return text or None


# Длина текста поля или обработчика в ответах инструментов; полный код — через handlers_export.
# Живёт здесь, а не в `service/`: слой формата не зависит от сервиса, сервис импортирует отсюда.
TEXT_LIMIT = 2000


def clip(text: str) -> str:
    """Обрезка длинного текста до `TEXT_LIMIT` с пометкой."""
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + " …[обрезано]"


def _handler_lines(old: str, new: str) -> list[str] | None:
    """Unified diff внутри обработчика, без заголовков файлов, не длиннее предела ответа."""
    if old.splitlines() == new.splitlines():
        return None
    lines = [
        line
        for line in difflib.unified_diff(old.splitlines(), new.splitlines(), n=1, lineterm="")
        if not line.startswith("---") and not line.startswith("+++")
    ]
    text = "\n".join(lines)
    if len(text) <= TEXT_LIMIT:
        return lines
    kept = text[:TEXT_LIMIT].splitlines()
    kept.append(_TRUNCATED)
    return kept
