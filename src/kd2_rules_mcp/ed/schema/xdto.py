"""Безопасное чтение XDTO без исполнения платформы и загрузки ресурсов сети."""

import hashlib
import re
from collections import Counter
from pathlib import Path
from types import MappingProxyType

from lxml import etree as ET

from .errors import EdSchemaFormatError, EdSchemaReadError, EdSchemaResourceLimitError
from .model import (
    Facet,
    OriginStep,
    QName,
    SchemaDiagnostic,
    SchemaImport,
    SchemaPackage,
    SchemaProperty,
    SchemaSource,
    SchemaSpan,
    SchemaType,
)

XDTO = "http://v8.1c.ru/8.1/xdto"
MD = "http://v8.1c.ru/8.3/MDClasses"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XS = "http://www.w3.org/2001/XMLSchema"
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_XML_DEPTH = 128
FACETS = frozenset(
    [
        "length",
        "minLength",
        "maxLength",
        "totalDigits",
        "fractionDigits",
        "whiteSpace",
        "minInclusive",
        "maxInclusive",
        "minExclusive",
        "maxExclusive",
        "pattern",
    ]
)


def read_xml(path: Path) -> tuple[ET._Element, SchemaSource]:
    """Ограничивает байты до разбора, запрещает DTD, сущности и XInclude."""
    if path.suffix.lower() == ".xsd":
        raise EdSchemaFormatError(
            "XSD не поддерживается: укажите пакет XDTO из выгрузки конфигурации"
        )
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        raise EdSchemaReadError("Файл пакета XDTO недоступен") from error
    if len(raw) > MAX_FILE_BYTES:
        raise EdSchemaResourceLimitError("Файл схемы превышает 32 MiB")
    try:
        root = ET.fromstring(
            raw,
            ET.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False),
        )
    except ET.XMLSyntaxError as error:
        raise EdSchemaFormatError("Повреждённый XML пакета XDTO") from error
    if getattr(root.getroottree().docinfo, "doctype", "") or any(
        isinstance(e, ET._Entity) or e.tag == "{http://www.w3.org/2001/XInclude}include"
        for e in root.iter()
    ):
        raise EdSchemaFormatError("DTD, сущности и XInclude в схеме запрещены")
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_XML_DEPTH:
            raise EdSchemaResourceLimitError("Глубина XML превышает 128")
        stack.extend((child, depth + 1) for child in node if isinstance(child.tag, str))
    digest = hashlib.sha256(raw).hexdigest()
    source_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
    return root, SchemaSource(source_id, str(path.resolve()), digest, "xdto", len(raw))


def span(node: ET._Element, source: SchemaSource) -> SchemaSpan:
    return SchemaSpan(source.source_id, node.sourceline or 1, node.getroottree().getpath(node))


def parse_qname(node: ET._Element, value: str) -> QName:
    """QName разрешается в nsmap самого узла; Clark-имя не делится по двоеточию."""
    if value.startswith("{"):
        match = re.fullmatch(r"\{([^}]*)\}([^{}\s]+)", value)
        if not match:
            raise ValueError("Некорректное Clark-имя")
        return QName(*match.groups())
    if ":" in value:
        prefix, local = value.split(":", 1)
        uri = node.nsmap.get(prefix)
        if uri is None or not local or ":" in local:
            raise ValueError("Неизвестный префикс QName")
        return QName(uri, local)
    if not value or any(c.isspace() for c in value):
        raise ValueError("Пустое или некорректное QName")
    return QName(node.nsmap.get(None, ""), value)


def metadata(path: Path) -> tuple[str, str, str | None, SchemaSource]:
    root, source = read_xml(path)
    if root.tag != f"{{{MD}}}MetaDataObject":
        raise EdSchemaFormatError("Ожидается описание пакета XDTO")
    props = root.find(f"{{{MD}}}XDTOPackage/{{{MD}}}Properties")
    if props is None:
        raise EdSchemaFormatError("В описании нет свойств пакета XDTO")
    name = props.findtext(f"{{{MD}}}Name", "")
    uri = props.findtext(f"{{{MD}}}Namespace", "")
    if not name or name in (".", "..") or any(c in name for c in "/\\:") or not uri:
        raise EdSchemaFormatError("Некорректное имя или Namespace описания пакета")
    synonym = props.findtext(".//{http://v8.1c.ru/8.3/common}content")
    return (
        name,
        uri,
        synonym,
        SchemaSource(source.source_id, source.path, source.sha256, "metadata", source.bytes),
    )


def read_package(path: Path, role: str = "base") -> SchemaPackage:
    """Регистрирует именованные и локальные типы; ссылки разрешаются следующим проходом."""
    root, source = read_xml(path)
    if root.tag != f"{{{XDTO}}}package" or not root.get("targetNamespace"):
        raise EdSchemaFormatError("Ожидается package XDTO с targetNamespace")
    uri = root.get("targetNamespace", "")
    types: list[SchemaType] = []
    diagnostics: list[SchemaDiagnostic] = []
    origin_role = {"base": "base_package", "dependency": "import", "extension": "extension"}[role]

    def diagnostic(code: str, node: ET._Element, message: str) -> None:
        diagnostics.append(SchemaDiagnostic(code, message, span(node, source)))

    def ref(node: ET._Element, value: str | None) -> QName | None:
        if value is None:
            return None
        try:
            return parse_qname(node, value)
        except ValueError:
            diagnostic("unknown_qname", node, f"Не удалось разрешить QName: {value}")
            return None

    def boolean(node: ET._Element, name: str, default: bool) -> bool:
        value = node.get(name)
        if value is None:
            return default
        if value not in ("true", "false", "1", "0"):
            diagnostic("unsupported_attribute", node, f"Неизвестное значение {name}={value}")
        return value in ("true", "1")

    def bound(node: ET._Element, name: str) -> int | None:
        value = node.get(name, "1")
        try:
            result = int(value)
            if result == -1 and name == "upperBound":
                return None
            if result < 0:
                raise ValueError
            return result
        except ValueError as error:
            raise EdSchemaFormatError(f"Некорректная граница {name}") from error

    def parse_type(node: ET._Element, inline: bool = False) -> str:
        start = len(diagnostics)
        location = span(node, source)
        ident = source.source_id + location.xpath
        name = node.get("name")
        qname = None if inline else QName(uri, name or "")
        if not inline and not name:
            raise EdSchemaFormatError("Именованный тип без имени")
        if inline:
            discriminator = ref(node, node.get(f"{{{XSI}}}type"))
            kind = "value" if discriminator and discriminator.local == "ValueType" else "object"
            if not discriminator or discriminator.local not in ("ValueType", "ObjectType"):
                diagnostic("unsupported_type", node, "Неизвестный вид локального типа")
        else:
            kind = "object" if node.tag == f"{{{XDTO}}}objectType" else "value"
        origin = (OriginStep(origin_role, uri, span(root, source)),)
        origin += (OriginStep("inline" if inline else origin_role, uri, location, qname),)
        properties: list[SchemaProperty] = []
        facets: list[Facet] = []
        base = ref(node, node.get("base"))
        members = tuple(
            q for value in node.get("memberTypes", "").split() if (q := ref(node, value))
        )
        for attr in node.attrib:
            local = ET.QName(attr).localname
            if local in FACETS:
                facets.append(Facet(local, str(node.attrib[attr]), None, None, location))
            elif local not in {
                "name",
                "base",
                "open",
                "abstract",
                "ordered",
                "sequenced",
                "variety",
                "memberTypes",
                "type",
            }:
                diagnostic("unsupported_attribute", node, f"Атрибут {attr}={node.attrib[attr]}")
        for child in node:
            if not isinstance(child.tag, str):
                continue
            tag = ET.QName(child).localname
            if child.tag == f"{{{XDTO}}}property":
                pstart = len(diagnostics)
                pname = child.get("name")
                if not pname:
                    raise EdSchemaFormatError("Свойство без имени")
                inline_nodes = child.findall(f"{{{XDTO}}}typeDef")
                type_id = parse_type(inline_nodes[0], True) if inline_nodes else None
                type_ref = ref(child, child.get("type"))
                if len(inline_nodes) > 1 or (inline_nodes and child.get("type")):
                    diagnostic("ambiguous_property_type", child, "Одновременно type и typeDef")
                form = child.get("form")
                if form not in (None, "element", "attribute", "text"):
                    diagnostic("unsupported_form", child, f"Неизвестная форма: {form}")
                for attr, value in child.attrib.items():
                    if attr not in {"name", "type", "lowerBound", "upperBound", "nillable", "form"}:
                        diagnostic("unsupported_attribute", child, f"Атрибут {attr}={value}")
                for nested in child:
                    if isinstance(nested.tag, str) and nested.tag != f"{{{XDTO}}}typeDef":
                        diagnostic(
                            "unsupported_node", nested, f"Узел {nested.tag}: {dict(nested.attrib)}"
                        )
                lower = bound(child, "lowerBound")
                upper = bound(child, "upperBound")
                if lower is None or (upper is not None and lower > upper):
                    raise EdSchemaFormatError("Несогласованные границы свойства")
                nillable = boolean(child, "nillable", False)
                plocation = span(child, source)
                properties.append(
                    SchemaProperty(
                        source.source_id + plocation.xpath,
                        QName(uri, pname),
                        type_id,
                        type_ref,
                        lower,
                        upper,
                        nillable,
                        form,
                        frozenset(str(a) for a in child.attrib),
                        (*origin, OriginStep(origin_role, uri, plocation, qname)),
                        "partial" if len(diagnostics) > pstart else "complete",
                    )
                )
            elif tag in FACETS or tag == "enumeration":
                facets.append(
                    Facet(
                        tag,
                        child.text or "",
                        ref(child, child.get(f"{{{XSI}}}type")),
                        boolean(child, "fixed", False) if "fixed" in child.attrib else None,
                        span(child, source),
                    )
                )
            else:
                diagnostic("unsupported_node", child, f"Узел {child.tag}: {dict(child.attrib)}")
        variety = node.get("variety", "Atomic").lower()
        if variety not in ("atomic", "list", "union"):
            variety = "unknown"
            diagnostic("unsupported_variety", node, "Неизвестный variety")
        flags = tuple(
            boolean(node, attr, default)
            for attr, default in (
                ("open", False),
                ("abstract", False),
                ("ordered", True),
                ("sequenced", False),
            )
        )
        types.append(
            SchemaType(
                ident,
                qname,
                kind,
                base,
                members,
                variety,
                tuple(properties),
                tuple(facets),
                flags[0],
                flags[1],
                flags[2],
                flags[3],
                frozenset(str(a) for a in node.attrib),
                origin,
                "partial" if len(diagnostics) > start else "complete",
            )
        )
        return ident

    imports: list[SchemaImport] = []
    for child in root:
        if child.tag in (f"{{{XDTO}}}objectType", f"{{{XDTO}}}valueType"):
            parse_type(child)
        elif child.tag == f"{{{XDTO}}}import":
            namespace = child.get("namespace")
            if not namespace:
                raise EdSchemaFormatError("Import без namespace")
            imports.append(SchemaImport(namespace, None, None, "missing", span(child, source)))
        elif isinstance(child.tag, str):
            diagnostic("unsupported_node", child, f"Узел {child.tag}: {dict(child.attrib)}")
    counts = Counter(ET.QName(e).localname for e in root.iter() if isinstance(e.tag, str))
    return SchemaPackage(
        uri,
        None,
        None,
        (source,),
        tuple(imports),
        tuple(types),
        role,
        MappingProxyType(dict(counts)),
        tuple(diagnostics),
    )
