"""???????????? ???? ???????????? ??????? XDTO ??? ???????????? ?? ?????????."""

from dataclasses import dataclass, field
from typing import Literal

from lxml import etree

from kd_rules_mcp.ed.schema.model import QName
from kd_rules_mcp.ed.schema.xdto import FACETS
from kd_rules_mcp.errors import (
    EdFormatDuplicateNameError,
    EdFormatEmptyObjectError,
    EdFormatIdentifierError,
    EdFormatMissingKeyError,
    EdFormatNamespaceError,
    EdFormatShapeError,
)

TypeRole = Literal["object", "key", "reference", "row", "table", "enumeration", "value"]
VALUE_ROLES = frozenset(("reference", "enumeration", "value"))
ROLES = VALUE_ROLES | {"object", "key", "row", "table"}
# Примитивы XML Schema 1.0; неизвестное имя в URI xs не считается разрешённым.
PRIMITIVES = frozenset(
    [
        "anyType",
        "anySimpleType",
        "string",
        "boolean",
        "decimal",
        "float",
        "double",
        "duration",
        "dateTime",
        "time",
        "date",
        "gYearMonth",
        "gYear",
        "gMonthDay",
        "gDay",
        "gMonth",
        "hexBinary",
        "base64Binary",
        "anyURI",
        "QName",
        "NOTATION",
        "normalizedString",
        "token",
        "language",
        "NMTOKEN",
        "NMTOKENS",
        "Name",
        "NCName",
        "ID",
        "IDREF",
        "IDREFS",
        "ENTITY",
        "ENTITIES",
        "integer",
        "nonPositiveInteger",
        "negativeInteger",
        "long",
        "int",
        "short",
        "byte",
        "nonNegativeInteger",
        "unsignedLong",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
        "positiveInteger",
    ]
)


def _qname(name: QName) -> None:
    """Локальное имя XDTO — NCName: типовые имена Справочник.X содержат точку."""
    try:
        if not name.namespace or ":" in name.local:
            raise ValueError
        parsed = etree.QName(name.local)
        if parsed.namespace is not None or parsed.localname != name.local:
            raise ValueError
    except ValueError as error:
        raise EdFormatIdentifierError("Недопустимое QName: " + str(name)) from error


@dataclass(frozen=True, slots=True)
class FormatFacet:
    kind: str
    lexical: str
    value_type: QName | None = None
    fixed: bool | None = None
    attribute: bool = False

    def __post_init__(self) -> None:
        if self.kind not in FACETS | {"enumeration"} or (
            self.attribute
            and (self.kind == "enumeration" or self.value_type or self.fixed is not None)
        ):
            raise EdFormatShapeError("Неподдерживаемая форма фасета: " + self.kind)
        if self.value_type:
            _qname(self.value_type)


@dataclass(frozen=True, slots=True)
class FormatProperty:
    name: QName
    type: QName
    lower: int = 1
    upper: int | None = 1
    nillable: bool = False
    form: Literal["element", "attribute", "text"] | None = None
    inline_type: "FormatType | None" = None
    # Наличие атрибута нужно для сверки платформенной формы, отдельно от его значения.
    explicit_attributes: frozenset[str] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _qname(self.name)
        _qname(self.type)
        if self.explicit_attributes is not None:
            object.__setattr__(self, "explicit_attributes", frozenset(self.explicit_attributes))
        if (
            type(self.lower) is not int
            or (self.upper is not None and type(self.upper) is not int)
            or self.lower < 0
            or (self.upper is not None and self.upper < self.lower)
        ):
            raise EdFormatShapeError("Несогласованные границы свойства " + str(self.name))
        if self.form not in (None, "element", "attribute", "text"):
            raise EdFormatShapeError("Неизвестная форма свойства")
        if self.inline_type and self.inline_type.name != self.type:
            raise EdFormatShapeError("QName локального типа не совпадает с типом свойства")


@dataclass(frozen=True, slots=True)
class FormatType:
    name: QName
    role: TypeRole
    properties: tuple[FormatProperty, ...] = ()
    base: QName | None = None
    facets: tuple[FormatFacet, ...] = ()
    open: bool | None = None
    abstract: bool | None = None
    ordered: bool | None = None
    sequenced: bool | None = None
    variety: Literal["atomic", "list", "union"] | None = None
    members: tuple[QName, ...] = ()
    exported: bool = False
    key_property: QName | None = None

    def __post_init__(self) -> None:
        _qname(self.name)
        for attr in ("properties", "facets", "members"):
            object.__setattr__(self, attr, tuple(getattr(self, attr)))
        if self.role not in ROLES:
            raise EdFormatShapeError("Неизвестное назначение типа")
        if self.base:
            _qname(self.base)
        for member in self.members:
            _qname(member)
        if self.members or self.variety == "union":
            raise EdFormatShapeError("Union/memberTypes вне профиля сериализации пакета")
        if self.role in VALUE_ROLES:
            if self.properties or self.exported or self.key_property:
                raise EdFormatShapeError("Простой тип содержит объектные свойства")
            if self.role == "enumeration" and not any(f.kind == "enumeration" for f in self.facets):
                raise EdFormatShapeError("Перечисление без значений")
            if self.role == "reference" and self.base is None:
                raise EdFormatShapeError("Ссылочный тип без базового типа")
            if self.variety not in (None, "atomic", "list", "union"):
                raise EdFormatShapeError("Неизвестная разновидность простого типа")
        elif not self.properties:
            raise EdFormatEmptyObjectError("Пустой объектный тип: " + str(self.name))
        elif self.facets or self.members or self.variety:
            raise EdFormatShapeError("Объектный тип содержит фасеты простого типа")
        if len({p.name for p in self.properties}) != len(self.properties):
            raise EdFormatDuplicateNameError("Повтор свойства в " + str(self.name))
        if any(p.name.namespace != self.name.namespace for p in self.properties):
            raise EdFormatNamespaceError("URI свойства отличается от URI типа")
        if self.exported and (self.role != "object" or self.key_property is None):
            raise EdFormatMissingKeyError("Для выгружаемого объекта требуется свойство ключей")
        if self.key_property:
            _qname(self.key_property)
