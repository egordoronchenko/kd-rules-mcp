"""Модель и XML собственного пакета XDTO; без сети, баз и модуля менеджера.

Образцы ниже — XDTOPackages выгрузки проекта из projects.yaml, пакет
EnterpriseData_1_20_2. Пути относительно XDTOPackages; исходники только читаются.
Роли и признак выгружаемого объекта — решения автора, в XML пакета их нет.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, uuid5

from lxml import etree

from kd_rules_mcp.ed.errors import EdFormatError
from kd_rules_mcp.ed.layer_reader import module_routines, source_from_text
from kd_rules_mcp.ed.lexer import tokenize
from kd_rules_mcp.ed.route_model import RouteProfile
from kd_rules_mcp.ed.schema.model import EdSchema, QName, SchemaType
from kd_rules_mcp.ed.schema.xdto import FACETS, XDTO, XS, XSI
from kd_rules_mcp.errors import (
    EdFormatDuplicateNameError,
    EdFormatEmptyObjectError,
    EdFormatIdentifierError,
    EdFormatMissingKeyError,
    EdFormatNamespaceError,
    EdFormatShapeError,
    EdFormatUnknownTypeError,
)

from .hook import bsl_string, valid_identifier
from .identity import IdentityMap, logical_path, make_identity_map
from .manager_render import ManagerRoute, render_manager_route
from .model import ExtensionIdentity
from .xml_dump import (
    Description,
    ManagerHost,
    adopted_xml,
    dump_manager_extension,
    manager_compatibility_modes,
    manager_identity_roles,
    new_object,
    node,
    profile_template,
    read_description,
    serialize,
    synonym,
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


@dataclass(frozen=True, slots=True)
class FormatPackage:
    metadata_name: str
    namespace: str
    base_version: str
    base_namespace: str
    types: tuple[FormatType, ...]
    base_schema: EdSchema = field(repr=False, compare=False)
    imports: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not valid_identifier(self.metadata_name):
            raise EdFormatIdentifierError("Недопустимое имя пакета метаданных")
        if not self.namespace.strip() or self.namespace == self.base_namespace:
            raise EdFormatNamespaceError("Собственный URI пуст или совпадает с базовым")
        if not self.base_version.strip():
            raise EdFormatShapeError("Не задана базовая версия формата")
        object.__setattr__(self, "types", tuple(self.types))
        object.__setattr__(self, "imports", tuple(self.imports))
        imports = (self.base_namespace, *self.imports)
        packages = {p.namespace for p in self.base_schema.packages}
        if len(set(imports)) != len(imports) or self.namespace in imports:
            raise EdFormatNamespaceError("Повтор или собственный URI в импортах")
        if any(uri not in packages for uri in imports):
            raise EdFormatUnknownTypeError("Импорт отсутствует в прочитанной базовой схеме")
        own: dict[QName, FormatType] = {}

        def register(typ: FormatType) -> None:
            if typ.name.namespace != self.namespace:
                raise EdFormatNamespaceError("Тип объявлен вне собственного URI")
            if typ.name in own:
                raise EdFormatDuplicateNameError("Повтор локального имени: " + str(typ.name))
            own[typ.name] = typ
            for prop in typ.properties:
                if prop.inline_type:
                    register(prop.inline_type)

        for typ in self.types:
            register(typ)

        def resolve(name: QName) -> None:
            if name.namespace == self.namespace:
                found = name in own
            elif name.namespace == XS:
                found = name.local in PRIMITIVES
            else:
                found = name.namespace in imports and name in self.base_schema.types
            if not found:
                raise EdFormatUnknownTypeError("Тип не разрешён: " + str(name))

        for typ in own.values():
            for name in (*((typ.base,) if typ.base else ()), *typ.members):
                resolve(name)
            for facet in typ.facets:
                if facet.value_type:
                    resolve(facet.value_type)
            for prop in typ.properties:
                resolve(prop.type)
            if typ.exported:
                key = next((p for p in typ.properties if p.name == typ.key_property), None)
                own_key = key is not None and key.type in own and own[key.type].role == "key"
                base_key = (
                    key is not None
                    and key.type.namespace in imports
                    and key.type in self.base_schema.types
                    and self.base_schema.types[key.type].kind == "object"
                )
                if not own_key and not base_key:
                    raise EdFormatMissingKeyError("Нет свойства объектного типа ключей")


def format_package_from_schema(
    schema: EdSchema,
    *,
    namespace: str,
    metadata_name: str,
    base_version: str,
    base_namespace: str,
    roles: Mapping[QName, TypeRole] | None = None,
    exported: Mapping[QName, QName] | None = None,
    selected: tuple[QName, ...] | None = None,
) -> FormatPackage:
    """Переносит прочитанные значения, не угадывая роли и выгружаемость.

    Локальным typeDef даётся внутренний QName владельца/свойства; в XML он
    остаётся безымянным. Фасеты и явные атрибуты не теряются.
    """
    package = next((p for p in schema.packages if p.namespace == namespace), None)
    if package is None:
        raise EdFormatShapeError("Пакет отсутствует в прочитанной схеме")
    roles, exported = roles or {}, exported or {}
    chosen = set(selected) if selected is not None else None

    def convert(typ: SchemaType, name: QName) -> FormatType:
        if typ.status != "complete":
            raise EdFormatShapeError("Неполностью прочитанный тип " + str(name))
        properties = []
        for prop in typ.properties:
            inline = None
            ref = prop.type_ref
            if prop.type_id:
                ref = QName(namespace, name.local + "." + prop.name.local + "__type")
                inline = convert(schema.by_id[prop.type_id], ref)
            if ref is None:
                raise EdFormatShapeError("Свойство без прочитанного типа")
            properties.append(
                FormatProperty(
                    prop.name,
                    ref,
                    prop.lower,
                    prop.upper,
                    prop.nillable,
                    cast(Any, prop.form),
                    inline,
                    prop.explicit_attributes,
                )
            )
        role = roles.get(name, "object" if typ.kind == "object" else "value")
        if (role in VALUE_ROLES) != (typ.kind == "value"):
            raise EdFormatShapeError("Роль не соответствует виду прочитанного типа")
        return FormatType(
            name,
            role,
            tuple(properties),
            typ.base,
            tuple(
                FormatFacet(
                    f.kind, f.lexical, f.value_type, f.fixed, f.kind in typ.explicit_attributes
                )
                for f in typ.facets
            ),
            open=typ.open if "open" in typ.explicit_attributes else None,
            abstract=typ.abstract if "abstract" in typ.explicit_attributes else None,
            ordered=typ.ordered if "ordered" in typ.explicit_attributes else None,
            sequenced=typ.sequenced if "sequenced" in typ.explicit_attributes else None,
            variety=cast(Any, typ.variety) if "variety" in typ.explicit_attributes else None,
            members=typ.members,
            exported=name in exported,
            key_property=exported.get(name),
        )

    types = tuple(
        convert(t, t.qname)
        for t in package.types
        if t.qname and (chosen is None or t.qname in chosen)
    )
    if chosen is not None and {t.name for t in types} != chosen:
        raise EdFormatUnknownTypeError("Не все выбранные типы присутствуют в пакете")
    return FormatPackage(
        metadata_name,
        namespace,
        base_version,
        base_namespace,
        types,
        schema,
        tuple(i.namespace for i in package.imports if i.namespace != base_namespace),
    )


def _xdto_xml(model: FormatPackage) -> bytes:
    """Формы EnterpriseData_1_20_2/Ext/Package.bin:

    package/nsmap/import:1-2; valueType/фасеты:3-24, 363-381;
    ссылка valueType base Message.Ref:29-30; objectType/ключи:2719-2723,
    4206-4219; ТЧ/границы:2016-2024; typeDef:4022-4024;
    nillable: ExchangeMessage/Ext/Package.bin:16.
    QName имеет локальный префикс d2p1/d3p1 по глубине, как в образце.
    """
    root = etree.Element(
        f"{{{XDTO}}}package",
        nsmap=cast(Any, {None: XDTO, "xs": XS, "xsi": XSI}),
        targetNamespace=model.namespace,
    )
    for uri in (model.base_namespace, *model.imports):
        etree.SubElement(root, f"{{{XDTO}}}import", namespace=uri)

    def element(
        parent: etree._Element, tag: str, attrs: Mapping[str, str | QName], depth: int
    ) -> etree._Element:
        namespaces: dict[str, str] = {}
        values: dict[str, str] = {}
        for key, value in attrs.items():
            if isinstance(value, QName):
                if value.namespace in (XS, XDTO):
                    prefix = "xs" if value.namespace == XS else ""
                else:
                    prefix = next((k for k, v in namespaces.items() if v == value.namespace), "")
                    if not prefix:
                        prefix = f"d{depth}p{len(namespaces) + 1}"
                        namespaces[prefix] = value.namespace
                values[key] = (prefix + ":" if prefix else "") + value.local
            else:
                values[key] = value
        return etree.SubElement(parent, f"{{{XDTO}}}{tag}", attrib=values, nsmap=namespaces)

    def render_type(
        parent: etree._Element, typ: FormatType, depth: int, inline: bool = False
    ) -> None:
        value = typ.role in VALUE_ROLES
        attrs: dict[str, str | QName] = (
            {f"{{{XSI}}}type": "ValueType" if value else "ObjectType"}
            if inline
            else {"name": typ.name.local}
        )
        if typ.base:
            attrs["base"] = typ.base
        for flag in ("open", "abstract", "ordered", "sequenced"):
            state = getattr(typ, flag)
            if state is not None:
                attrs[flag] = str(state).lower()
        if typ.variety is not None:
            attrs["variety"] = typ.variety.title()
        for facet in typ.facets:
            if facet.attribute:
                attrs[facet.kind] = facet.lexical
        obj = element(
            parent, "typeDef" if inline else "valueType" if value else "objectType", attrs, depth
        )
        for facet in typ.facets:
            if not facet.attribute:
                fattrs: dict[str, str | QName] = {}
                if facet.value_type:
                    fattrs[f"{{{XSI}}}type"] = facet.value_type
                if facet.fixed is not None:
                    fattrs["fixed"] = str(facet.fixed).lower()
                element(obj, facet.kind, fattrs, depth + 1).text = facet.lexical
        for prop in typ.properties:
            pattrs: dict[str, str | QName] = {"name": prop.name.local}
            if prop.inline_type is None:
                pattrs["type"] = prop.type
            explicit = prop.explicit_attributes or frozenset()
            for key, raw, default in (
                ("lowerBound", prop.lower, 1),
                ("upperBound", prop.upper, 1),
                ("nillable", prop.nillable, False),
            ):
                if raw != default or key in explicit:
                    pattrs[key] = "-1" if raw is None else str(raw).lower()
            if prop.form:
                pattrs["form"] = prop.form.title()
            item = element(obj, "property", pattrs, depth + 1)
            if prop.inline_type:
                render_type(item, prop.inline_type, depth + 2, True)

    for typ in model.types:
        render_type(root, typ, 2)
    etree.indent(root, space="\t")
    return etree.tostring(root, encoding="UTF-8") + b"\n"


def render_format_package(
    model: FormatPackage, *, identity: IdentityMap | None = None
) -> dict[str, bytes]:
    """Метаданные по EnterpriseData_1_20_2.xml:3-14; UUID из общей карты.

    Самостоятельная сборка привязана к URI/имени; носитель передаёт общую карту
    конфигурации, чтобы идентичность пакета совпадала со всем расширением.
    """
    key = "XDTOPackage/" + model.metadata_name
    identity = identity or make_identity_map(
        str(uuid5(NAMESPACE_URL, model.namespace)),
        model.metadata_name,
        (key,),
        {},
    )
    root, obj = new_object("XDTOPackage", key, identity)
    props = node(obj, "Properties")
    node(props, "Name", model.metadata_name)
    synonym(props, "", "")
    node(props, "Comment")
    node(props, "Namespace", model.namespace)
    return {
        "XDTOPackages/" + model.metadata_name + ".xml": serialize(root),
        "XDTOPackages/" + model.metadata_name + "/Ext/Package.bin": _xdto_xml(model),
    }


@dataclass(frozen=True, slots=True)
class FormatHost:
    configuration_uuid: str
    language: Description
    compatibility_mode: str
    interface_compatibility_mode: str


FORMAT_OVERRIDE_MODULE = "ОбменДаннымиПереопределяемый"
FORMAT_DECLARE_PROCEDURE = "ПриПолученииДоступныхРасширенийФормата"


@dataclass(frozen=True, slots=True)
class FormatDeclaration:
    """Объявление для точной версии; исходники и карта — снимки хозяина.

    extension_sources содержит тексты явно переданных расширений. Литеральное
    упоминание URI в них считается коллизией консервативно: BSL не исполняется.
    Карта routes читается read_routes из той же выгрузки, что descriptions.
    """

    version_key: str
    descriptions: Mapping[str, str]
    routes: RouteProfile
    plan_name: str | None = None
    extension_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "descriptions", MappingProxyType(dict(self.descriptions)))
        object.__setattr__(self, "extension_sources", tuple(self.extension_sources))


def _declaration_parameter(text: str, procedure: str, expected: str | None = None) -> str:
    """Поддерживается один параметр по ссылке без умолчания; имя берётся из хозяина."""
    message = (
        "Исполнитель этой версии БСП расширения формата так не объявляет: "
        + procedure
        + " отсутствует или имеет неподдерживаемую сигнатуру"
    )
    try:
        source = source_from_text(text, "host", "<host>", False, text.encode())
        routines = [r for r in module_routines(source) if r.name == procedure]
    except EdFormatError as error:
        raise EdFormatShapeError(message) from error
    if len(routines) != 1:
        raise EdFormatShapeError(message)
    routine = routines[0]
    if routine.routine_kind != "procedure" or not routine.exported or len(routine.parameters) != 1:
        raise EdFormatShapeError(message)
    parameter = routine.parameters[0]
    if (
        parameter.by_value
        or parameter.default is not None
        or not parameter.name
        or not valid_identifier(parameter.name)
        or [t.kind for t in tokenize(parameter.raw) if t.kind != "comment"] != ["identifier"]
        or (expected is not None and parameter.name != expected)
    ):
        raise EdFormatShapeError(message)
    return parameter.name


def _declaration_host(
    model: FormatPackage, declaration: FormatDeclaration, host: FormatHost | ManagerHost
) -> tuple[Description, str, Description | None, str]:
    """Отказы проверяются до порождения первого байта носителя."""
    if declaration.version_key != model.base_version:
        raise EdFormatShapeError("Версия объявления не совпадает с версией пакета буква в букву")
    descriptions = declaration.descriptions
    declared_host = read_format_host(descriptions)
    if (
        declared_host.configuration_uuid != host.configuration_uuid
        or declared_host.language != host.language
        or declared_host.compatibility_mode != host.compatibility_mode
        or declared_host.interface_compatibility_mode != host.interface_compatibility_mode
    ):
        raise EdFormatShapeError("Описание объявления относится к другому хозяину")
    path = "CommonModules/" + FORMAT_OVERRIDE_MODULE
    if path + ".xml" not in descriptions or path + "/Ext/Module.bsl" not in descriptions:
        raise EdFormatShapeError("Исполнитель этой версии БСП расширения формата так не объявляет")
    module = read_description(
        path + ".xml", descriptions[path + ".xml"], "CommonModule", FORMAT_OVERRIDE_MODULE
    )
    text = descriptions[path + "/Ext/Module.bsl"]
    parameter = _declaration_parameter(text, FORMAT_DECLARE_PROCEDURE, "РасширенияФормата")
    for flag in profile_template()["module_flags"]:
        if module.props.get(flag) not in ("true", "false"):
            raise EdFormatShapeError("Не задан флаг модуля хозяина: " + flag)
    if module.props.get("Server") != "true":
        raise EdFormatShapeError("Модуль объявления недоступен на сервере")
    if any(e.uri == model.namespace for e in declaration.routes.format_extensions) or any(
        token.kind == "string" and token.value == model.namespace
        for source in (text, *declaration.extension_sources)
        for token in tokenize(source)
    ):
        raise EdFormatNamespaceError("URI уже объявлен хозяином или переданным расширением")
    plan = None
    settings = ""
    if declaration.plan_name is not None:
        plans = [p for p in declaration.routes.plans if p.plan_name == declaration.plan_name]
        if (
            len(plans) != 1
            or plans[0].status != "complete"
            or not any(
                entry.state == "effective"
                and entry.key_raw == declaration.version_key
                and entry.key == declaration.version_key
                for entry in plans[0].entries
            )
        ):
            raise EdFormatShapeError(
                "Версия не совпадает буква в букву с ключом карты плана хозяина"
            )
        path = "ExchangePlans/" + declaration.plan_name
        if path + ".xml" not in descriptions or path + "/Ext/ManagerModule.bsl" not in descriptions:
            raise EdFormatShapeError("Не переданы описание и модуль плана хозяина")
        plan = read_description(
            path + ".xml", descriptions[path + ".xml"], "ExchangePlan", declaration.plan_name
        )
        settings = _declaration_parameter(
            descriptions[path + "/Ext/ManagerModule.bsl"], "ПриПолученииНастроек"
        )
    return module, parameter, plan, settings


def read_format_host(descriptions: Mapping[str, str]) -> FormatHost:
    """Общий с менеджером читатель режимов; план обмена для пакета не нужен."""
    if "Configuration.xml" not in descriptions:
        raise EdFormatShapeError("Не передано описание Configuration.xml")
    config = read_description(
        "Configuration.xml", descriptions["Configuration.xml"], "Configuration"
    )
    compatibility, interface = manager_compatibility_modes(config)
    ref = config.props.get("DefaultLanguage", "")
    if not ref.startswith("Language."):
        raise EdFormatShapeError("Не задан язык принимающей конфигурации")
    name = ref.removeprefix("Language.")
    path = "Languages/" + name + ".xml"
    if path not in descriptions:
        raise EdFormatShapeError("Не передано описание языка конфигурации")
    language = read_description(path, descriptions[path], "Language", name)
    if not language.props.get("LanguageCode"):
        raise EdFormatShapeError("Не задан LanguageCode")
    return FormatHost(config.uuid, language, compatibility, interface)


def render_format_extension(
    model: FormatPackage,
    host: FormatHost | ManagerHost,
    *,
    extension_name: str,
    prefix: str,
    declaration: FormatDeclaration | None = None,
) -> dict[str, bytes]:
    """Носитель по xml_dump.dump_manager_extension и xml_profile_2_20.json.

    Без declaration состав и байты прежнего носителя сохраняются.
    Заимствованный общий модуль: CommonModules/ОбщегоНазначения.xml:4-24
    в выгрузке расширения проекта из projects.yaml: Module=Extended, Adopted,
    ExtendedConfigurationObject — UUID хозяина, флаги контекстов — как у хозяина.
    Режимы совместимости — обязательные сведения хозяина, без подмены версией сборщика.
    """
    if not all(valid_identifier(s) for s in (extension_name, prefix)):
        raise EdFormatIdentifierError("Недопустимые имя или префикс расширения")
    if not model.metadata_name.startswith(prefix):
        raise EdFormatIdentifierError("Имя собственного пакета должно начинаться с префикса")
    if not host.compatibility_mode.strip() or not host.interface_compatibility_mode.strip():
        raise EdFormatShapeError("Не переданы режимы совместимости хозяина")
    language_key = "Language/" + host.language.name
    borrowed = {language_key: host.language.uuid}
    additional_paths: tuple[str, ...] = ()
    plan_host = None
    module = None
    parameter = settings = ""
    if declaration is not None:
        module, parameter, plan, settings = _declaration_host(model, declaration, host)
        module_key = "CommonModule/" + module.name
        borrowed[module_key] = module.uuid
        additional_paths = (module_key,)
        if plan is not None:
            plan_host = ManagerHost(
                host.configuration_uuid,
                host.language,
                plan,
                host.compatibility_mode,
                ExtensionIdentity(extension_name, prefix, ""),
                host.interface_compatibility_mode,
            )
            plan_paths, plan_borrowed = manager_identity_roles(plan_host, module.name)
            additional_paths += tuple(p for p in plan_paths if p.startswith("ExchangePlan/"))
            borrowed.update(plan_borrowed)
    paths = (
        "Configuration",
        *("Contained/" + c for c in profile_template()["class_ids"]),
        language_key,
        "XDTOPackage/" + model.metadata_name,
        *additional_paths,
    )
    identity = make_identity_map(
        host.configuration_uuid,
        extension_name,
        paths,
        borrowed,
    )
    root, obj = new_object("Configuration", "Configuration", identity)
    info = node(obj, "InternalInfo")
    for class_id in profile_template()["class_ids"]:
        item = node(info, "xr:ContainedObject")
        node(item, "xr:ClassId", class_id)
        node(item, "xr:ObjectId", identity.objects[logical_path("Contained/" + class_id)])
    props = node(obj, "Properties")
    for key, value in (
        ("ObjectBelonging", "Adopted"),
        ("Name", extension_name),
    ):
        node(props, key, value)
    synonym(props, "", "")
    for key, value in (
        ("Comment", ""),
        ("ConfigurationExtensionPurpose", "Customization"),
        ("KeepMappingToExtendedConfigurationObjectsByIDs", "true"),
        ("NamePrefix", prefix),
        ("ConfigurationExtensionCompatibilityMode", host.compatibility_mode),
        ("DefaultRunMode", "ManagedApplication"),
    ):
        node(props, key, value)
    node(node(props, "UsePurposes"), "v8:Value", "PlatformApplication").set(
        f"{{{XSI}}}type", "app:ApplicationUsePurpose"
    )
    for key, value in (
        ("ScriptVariant", "Russian"),
        ("DefaultRoles", ""),
        ("Vendor", ""),
        ("Version", model.base_version),
        ("DefaultLanguage", "Language." + host.language.name),
        *((key, "") for key in profile_template()["configuration_empty_information"]),
        ("InterfaceCompatibilityMode", host.interface_compatibility_mode),
    ):
        node(props, key, value)
    children = node(obj, "ChildObjects")
    node(children, "Language", host.language.name)
    language, _ = adopted_xml(host.language, identity)
    result = {
        "Languages/" + host.language.name + ".xml": serialize(language),
        **render_format_package(model, identity=identity),
    }
    if declaration is not None:
        assert module is not None
        node(children, "CommonModule", module.name)
        module_xml, _ = adopted_xml(module, identity)
        result["CommonModules/" + module.name + ".xml"] = serialize(module_xml)
        result["CommonModules/" + module.name + "/Ext/Module.bsl"] = (
            "#Если Сервер Или ТолстыйКлиентОбычноеПриложение Или ВнешнееСоединение Тогда\n\n"
            f'&После("{FORMAT_DECLARE_PROCEDURE}")\n'
            f"Процедура {prefix}{FORMAT_DECLARE_PROCEDURE}({parameter})\n"
            f"\t{parameter}.Вставить({bsl_string(model.namespace)}, "
            f"{bsl_string(declaration.version_key)});\n"
            "КонецПроцедуры\n\n#КонецЕсли\n"
        ).encode()
        if plan_host is not None:
            plan_name = plan_host.exchange_plan.name
            node(children, "ExchangePlan", plan_name)
            plan_path = "ExchangePlans/" + plan_name
            result[plan_path + ".xml"] = dump_manager_extension(
                plan_host, module.name, identity, version=model.base_version
            )[plan_path + ".xml"]
            result[plan_path + "/Ext/ManagerModule.bsl"] = render_manager_route(
                ManagerRoute(plan_name, declaration.version_key),
                prefix=prefix,
                format_namespace=model.namespace,
                settings_parameter=settings,
            ).encode("utf-8")
    # Порядок состава — как его выгружает платформа: общий модуль, план обмена, затем пакет XDTO
    # (проба на файловой базе, docs/plans/evals/2026-10-05-ed-format-declare-probe.md).
    node(children, "XDTOPackage", model.metadata_name)
    result["Configuration.xml"] = serialize(root)
    return result
