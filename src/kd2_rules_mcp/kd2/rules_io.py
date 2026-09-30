"""Импорт XML правил КД 2 в модель и экспорт модели в XML по схеме `schema.py`."""

import re
from pathlib import Path

from lxml import etree

from kd2_rules_mcp.errors import RulesFormatError
from kd2_rules_mcp.kd2.canonical import parse_xml
from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules, RulesDocument
from kd2_rules_mcp.kd2.schema import (
    ROOT_KINDS,
    Child,
    Items,
    Kind,
    Leaf,
    Policy,
    Scalar,
    ValueType,
    kind,
)
from kd2_rules_mcp.kd2.xmlstyle import XmlStyle, XmlWriter, detect_style

SUPPORTED_FORMAT_VERSION = "2.01"
_INT = re.compile(r"-?\d+")

# --- Разбор ------------------------------------------------------------------------------------


def _parse_scalar(value: str, value_type: ValueType, where: str) -> Scalar:
    if value_type is ValueType.STR:
        return value
    if value_type is ValueType.BOOL:
        if value in ("true", "false"):
            return value == "true"
        raise RulesFormatError(f"{where}: ожидается true или false, найдено {value!r}")
    if _INT.fullmatch(value):
        return int(value)
    raise RulesFormatError(f"{where}: ожидается целое число, найдено {value!r}")


def _describe(element: etree._Element) -> str:
    for child in element:
        if child.tag == "Код" and child.text:
            return f"{element.tag}[{child.text.strip()}]"
    name = element.get("Имя")
    return f"{element.tag}[{name.strip()}]" if name else str(element.tag)


def _is_plain_leaf(element: etree._Element) -> bool:
    return not element.attrib and not any(isinstance(c.tag, str) for c in element)


def _stripped(value: str | None) -> str | None:
    """Непробельный текст между элементами без пробелов по краям, иначе None."""
    value = (value or "").strip()
    return value or None


def _parse_child(node: Node, child: etree._Element, path: str) -> Node | None:
    """Разбирает дочерний элемент в узел; возвращает узел, если это не простое значение."""
    node_kind = node.kind
    tag = str(child.tag)
    child_path = f"{path}/{_describe(child)}"
    leaf = node_kind.leaves.get(tag)
    if leaf is not None and _is_plain_leaf(child):
        if tag in node.values:
            raise RulesFormatError(f"{child_path}: тег повторяется")
        node.values[tag] = _parse_scalar(child.text or "", leaf.type, child_path)
        return None
    if tag in node_kind.children:
        if tag in node.children:
            raise RulesFormatError(f"{child_path}: тег повторяется")
        parsed = _parse_node(child, kind(node_kind.children[tag].kind), child_path)
        node.children[tag] = parsed
        return parsed
    if tag in node_kind.items:
        parsed = _parse_node(child, kind(node_kind.items[tag]), child_path)
        node.items.append(parsed)
        return parsed
    node.unknown.append(child)
    return None


def _parse_node(element: etree._Element, node_kind: Kind, path: str) -> Node:
    node = Node(node_kind, str(element.tag))
    for name, raw in element.attrib.items():
        attr = node_kind.attr_map.get(str(name))
        value_type = attr.type if attr else ValueType.STR
        node.attrs[str(name)] = _parse_scalar(str(raw), value_type, f"{path}@{name}")

    if node_kind.text is not None and not any(isinstance(c.tag, str) for c in element):
        node.text = element.text or ""
        return node

    pending = _stripped(element.text)
    for child in element:
        if isinstance(child.tag, str):
            parsed = _parse_child(node, child, path)
            if pending is not None:
                if parsed is None:
                    raise RulesFormatError(
                        f"{path}: текст «{pending}» перед простым тегом «{child.tag}»"
                        " не поддерживается"
                    )
                parsed.leading_text = pending
                pending = None
        pending = _join(pending, _stripped(child.tail))
    node.trailing_text = pending
    return node


def _join(first: str | None, second: str | None) -> str | None:
    if first is None:
        return second
    return first if second is None else f"{first}\n{second}"


def _read(source: bytes | Path) -> bytes:
    return source.read_bytes() if isinstance(source, Path) else source


def load_rules(source: bytes | Path) -> ExchangeRules | RegistrationRules:
    """Импорт файла правил обмена или регистрации (вид — по корневому элементу)."""
    raw = _read(source)
    root = parse_xml(raw)
    root_tag = str(root.tag)
    root_kind = ROOT_KINDS.get(root_tag)
    version_element = root.find("ВерсияФормата")
    version = (version_element.text or "") if version_element is not None else ""
    if root_kind is None or version.strip() != SUPPORTED_FORMAT_VERSION:
        raise RulesFormatError(
            f"Неподдерживаемый формат правил: корень «{root_tag}», версия формата «{version}»;"
            f" поддерживаются «ПравилаОбмена» и «ПравилаРегистрации» версии"
            f" {SUPPORTED_FORMAT_VERSION}"
        )
    node = _parse_node(root, root_kind, root_tag)
    style = detect_style(raw)
    if root_tag == "ПравилаОбмена":
        return ExchangeRules(node, style)
    return RegistrationRules(node, style)


def load_exchange_rules(source: bytes | Path) -> ExchangeRules:
    """Импорт «ПравилаОбмена» 2.01; другой корень или версия — ошибка формата."""
    document = load_rules(source)
    if not isinstance(document, ExchangeRules):
        raise RulesFormatError(
            f"Ожидались правила обмена «ПравилаОбмена», найден корень «{document.root_tag}»"
        )
    return document


def load_registration_rules(source: bytes | Path) -> RegistrationRules:
    """Импорт «ПравилаРегистрации» 2.01; другой корень или версия — ошибка формата."""
    document = load_rules(source)
    if not isinstance(document, RegistrationRules):
        raise RulesFormatError(
            "Ожидались правила регистрации «ПравилаРегистрации»,"
            f" найден корень «{document.root_tag}»"
        )
    return document


# --- Вывод -------------------------------------------------------------------------------------


def format_scalar(value: Scalar) -> str:
    """Значение в текстовом виде XML (`XMLСтрока`)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _leaf_is_written(leaf: Leaf, value: Scalar) -> bool:
    if leaf.policy is Policy.IF_TRUE:
        return value is True
    if isinstance(value, bool):
        return True
    return value not in ("", 0)


def _attrs(node: Node) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for attr in node.kind.attrs:
        if attr.name not in node.attrs:
            continue
        value = node.attrs[attr.name]
        if attr.policy is Policy.IF_TRUE and value is not True:
            continue
        result.append((attr.name, format_scalar(value)))
    for name, value in node.attrs.items():
        if name not in node.kind.attr_map:
            result.append((name, format_scalar(value)))
    return result


type _Part = tuple[str, Node | etree._Element | str]


def _parts(node: Node) -> list[_Part]:
    parts: list[_Part] = []
    for slot in node.kind.slots:
        if isinstance(slot, Leaf):
            value = node.values.get(slot.tag)
            if value is not None and _leaf_is_written(slot, value):
                parts.append((slot.tag, format_scalar(value)))
        elif isinstance(slot, Child):
            child = node.children.get(slot.tag)
            if child is None:
                if slot.policy is Policy.ALWAYS:
                    parts.append((slot.tag, ""))
            elif not (slot.policy is Policy.IF_NOT_EMPTY and child.is_empty()):
                parts.append((slot.tag, child))
        elif isinstance(slot, Items):
            parts.extend((item.tag, item) for item in node.items)
    parts.extend((str(el.tag), el) for el in node.unknown)
    return parts


def _write_raw(writer: XmlWriter, element: etree._Element) -> None:
    attrs = [(str(k), str(v)) for k, v in element.attrib.items()]
    children = [c for c in element if isinstance(c.tag, str)]
    if not children:
        writer.element(str(element.tag), attrs, element.text or "")
        return
    writer.start(str(element.tag), attrs)
    for child in children:
        _write_raw(writer, child)
    writer.end(str(element.tag))


def _write_node(writer: XmlWriter, node: Node) -> None:
    if node.leading_text:
        writer.text_line(node.leading_text)
    attrs = _attrs(node)
    parts = _parts(node)
    if not parts and not node.trailing_text:
        writer.element(node.tag, attrs, node.text or "")
        return
    writer.start(node.tag, attrs)
    for tag, part in parts:
        if isinstance(part, Node):
            _write_node(writer, part)
        elif isinstance(part, str):
            writer.element(tag, [], part)
        else:
            _write_raw(writer, part)
    if node.trailing_text:
        writer.text_line(node.trailing_text)
    writer.end(node.tag)


def dump_rules(document: RulesDocument, style: XmlStyle | None = None) -> bytes:
    """Экспорт правил в XML в стиле КД; по умолчанию — в стиле исходного файла."""
    writer = XmlWriter(style or document.style)
    _write_node(writer, document.root)
    return writer.result()
