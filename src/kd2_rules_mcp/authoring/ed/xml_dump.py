"""Закрытый профиль xml-2.20-platform-8.3.27 по §1.2; никакого обхода диска."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

from lxml import etree

from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.structures.xmldump import registration_source_object
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key

from .hook import valid_identifier
from .identity import IdentityMap, logical_path, refuse
from .model import AttributeDraft, ExtensionIdentity, PreparedAuthoring
from .operations import unsupported_qualifiers

M = "http://v8.1c.ru/8.3/MDClasses"
XR = "http://v8.1c.ru/8.3/xcf/readable"
V8 = "http://v8.1c.ru/8.1/data/core"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
PROFILE_NAME = "xml-2.20-platform-8.3.27"


def profile_template() -> dict:
    """Ресурс доступен и из установленного wheel, независимо от cwd."""
    return json.loads(
        files("kd2_rules_mcp.authoring.ed")
        .joinpath("templates/xml_profile_2_20.json")
        .read_text("utf-8")
    )


def parse_xml(path: str, text: str) -> etree._Element:
    """Входы без DTD, сущностей и сетевого разрешения."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(text.encode("utf-8"), parser)
        if getattr(root.getroottree().docinfo, "doctype", "") or any(
            isinstance(n, etree._Entity) for n in root.iter()
        ):
            raise ValueError("DTD и сущности запрещены")
        if root.tag != f"{{{M}}}MetaDataObject" or root.get("version") != "2.20" or len(root) != 1:
            raise ValueError("Требуется описание MetaDataObject версии 2.20")
    except (etree.XMLSyntaxError, ValueError) as error:
        refuse("metadata_profile_unsupported", f"Недопустимое описание XML: {error}", path)
        raise AssertionError("недостижимо") from error
    return root


@dataclass(frozen=True, slots=True)
class Description:
    path: str
    kind: str
    name: str
    uuid: str
    props: Mapping[str, str]
    generated_types: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SubscriptionAddition:
    """Типовая подписка и только добавляемые типы её источников."""

    description: Description
    sources: tuple[str, ...]


def read_description(path: str, text: str, kind: str, name: str | None = None) -> Description:
    obj = parse_xml(path, text)[0]
    if obj.tag != f"{{{M}}}{kind}":
        refuse("metadata_profile_unsupported", "Неверный вид описания метаданных: " + kind, path)
    props = obj.find(f"{{{M}}}Properties")
    if props is None:
        refuse("metadata_profile_unsupported", "Отсутствуют Properties", path)
        raise AssertionError("недостижимо")
    values = {etree.QName(p).localname: p.text or "" for p in props}
    actual_name = values.get("Name", "")
    if not valid_identifier(actual_name) or (name is not None and actual_name != name):
        refuse(
            "snapshot_mismatch",
            "Имя описания не совпадает с каноническим именем: " + (name or kind),
            path,
        )
    try:
        uuid = str(obj.attrib["uuid"])
        UUID(uuid)
    except (KeyError, ValueError):
        refuse("metadata_profile_unsupported", "Не задан UUID объекта", path)
        raise AssertionError("недостижимо") from None
    use = props.find(f"{{{M}}}UsePurposes")
    if use is not None:
        values["UsePurposes"] = ",".join(n.text or "" for n in use)
    generated = tuple(
        (n.get("name", ""), n.get("category", ""))
        for n in obj.findall(f"{{{M}}}InternalInfo/{{{XR}}}GeneratedType")
    )
    if kind in ("Catalog", "Document") and (
        not generated
        or len({c for _, c in generated}) != len(generated)
        or any(not n or not valid_identifier(c) for n, c in generated)
    ):
        refuse(
            "metadata_profile_unsupported", "Нет однозначного списка GeneratedType владельца", path
        )
    return Description(path, kind, actual_name, uuid, MappingProxyType(values), generated)


@dataclass(frozen=True, slots=True)
class DumpMetadata:
    configuration: Description
    language: Description
    module: Description
    owners: tuple[Description, ...]
    attributes: Mapping[str, tuple[AttributeDraft, ...]]
    compatibility_mode: str
    source_texts: Mapping[str, str]


def read_metadata(prepared: PreparedAuthoring, descriptions: Mapping[str, str]) -> DumpMetadata:
    """Берёт только описания объектов из канонических решений подготовки."""
    used: dict[str, str] = {}

    def take(path: str, kind: str, name: str | None = None) -> Description:
        if path not in descriptions:
            refuse("metadata_profile_unsupported", "Не передано описание " + path, path)
        used[path] = descriptions[path]
        return read_description(path, used[path], kind, name)

    configuration = take("Configuration.xml", "Configuration")
    expected = {
        "DefaultRunMode": "ManagedApplication",
        "ScriptVariant": "Russian",
        "UsePurposes": "PlatformApplication",
        "InterfaceCompatibilityMode": "TaxiEnableVersion8_2",
    }
    for key, value in expected.items():
        if configuration.props.get(key) != value:
            refuse(
                "metadata_profile_unsupported",
                "Неподдерживаемое свойство конфигурации: " + key,
                configuration.path,
            )
    language_ref = configuration.props.get("DefaultLanguage", "")
    if not language_ref.startswith("Language.") or not valid_identifier(language_ref[9:]):
        refuse("metadata_profile_unsupported", "Не разрешён DefaultLanguage", configuration.path)
    language = take("Languages/" + language_ref[9:] + ".xml", "Language", language_ref[9:])
    if not language.props.get("LanguageCode"):
        refuse("metadata_profile_unsupported", "Не задан LanguageCode", language.path)
    path_parts = prepared.generated_hook.source.path.replace("\\", "/").split("/")
    if "CommonModules" not in path_parts:
        refuse(
            "metadata_profile_unsupported", "Не определено имя менеджера проверенного перехватчика"
        )
    manager = path_parts[path_parts.index("CommonModules") + 1]
    module = take("CommonModules/" + manager + ".xml", "CommonModule", manager)
    for key in profile_template()["module_flags"]:
        if module.props.get(key) not in ("true", "false"):
            refuse(
                "metadata_profile_unsupported", "Не задан флаг общего модуля: " + key, module.path
            )
    compatibility = prepared.identity.compatibility_mode or configuration.props.get(
        "CompatibilityMode", ""
    )
    if compatibility not in profile_template()["compatibility_modes"]:
        refuse(
            "metadata_profile_unsupported",
            "Неподдерживаемая совместимость: " + compatibility,
            configuration.path,
        )
    index = build_addresses(prepared.projection_before)
    attrs: dict[str, dict[str, AttributeDraft]] = {}
    owner_names: dict[str, tuple[str, str]] = {}
    for op in prepared.operations:
        if op.new_attribute is None:
            continue
        rule = index.find(op.target.pko_address)
        assert isinstance(rule, ObjectRule)
        key, _ = metadata_key(rule.configuration_object.value)
        if key is None or key[0].casefold() not in ("справочник", "документ"):
            refuse(
                "metadata_profile_unsupported",
                "Вид владельца вне профиля Catalog/Document",
                address=op.target.pko_address,
            )
        owner = prepared.structure_after.objects[key]
        kind = "Catalog" if key[0].casefold() == "справочник" else "Document"
        path = kind + "/" + owner.name
        owner_names[path] = kind, owner.name
        invalid = unsupported_qualifiers(op.new_attribute)
        if invalid:
            refuse(
                "metadata_profile_unsupported",
                "Недопустимые квалификаторы: " + ", ".join(invalid),
                address=op.target.pko_address,
            )
        draft = op.new_attribute
        old = attrs.setdefault(path, {}).get(draft.name)
        if old and old != draft:
            refuse(
                "metadata_profile_unsupported",
                "Разные описания одного нового реквизита",
                address=op.target.pko_address,
            )
        attrs[path][draft.name] = draft
    owners = tuple(
        take(kind + "s/" + name + ".xml", kind, name) for kind, name in sorted(owner_names.values())
    )
    return DumpMetadata(
        configuration,
        language,
        module,
        owners,
        MappingProxyType({k: tuple(v[n] for n in sorted(v)) for k, v in sorted(attrs.items())}),
        compatibility,
        MappingProxyType(used),
    )


def identity_roles(metadata: DumpMetadata) -> tuple[tuple[str, ...], Mapping[str, str]]:
    paths = ["Configuration", *["Contained/" + c for c in profile_template()["class_ids"]]]
    borrowed = {}
    for description in (metadata.language, metadata.module, *metadata.owners):
        key = description.kind + "/" + description.name
        paths.append(key)
        borrowed[key] = description.uuid
        for _, category in description.generated_types:
            paths.extend(
                key + "/GeneratedType/" + category + "/" + role for role in ("TypeId", "ValueId")
            )
        paths.extend(key + "/Attribute/" + a.name for a in metadata.attributes.get(key, ()))
    return tuple(paths), borrowed


def node(
    parent: etree._Element, tag: str, value: str | None = None, **attributes: str
) -> etree._Element:
    namespace, _, local = tag.partition(":")
    ns = {"v8": V8, "xr": XR}.get(namespace, M)
    child = etree.SubElement(parent, f"{{{ns}}}{local if local else tag}", attrib=attributes)
    if value:
        child.text = value
    return child


def new_object(kind: str, key: str, identity: IdentityMap) -> tuple[etree._Element, etree._Element]:
    namespaces = {None if k == "" else k: v for k, v in profile_template()["namespaces"]}
    # lxml принимает None как префикс default namespace; его stubs это не отражают.
    root = etree.Element(f"{{{M}}}MetaDataObject", nsmap=cast(Any, namespaces), version="2.20")
    return root, node(root, kind, uuid=identity.objects[logical_path(key)])


def synonym(parent: etree._Element, content: str, language_code: str) -> None:
    syn = node(parent, "Synonym")
    if content:
        item = node(syn, "v8:item")
        node(item, "v8:lang", language_code)
        node(item, "v8:content", content)


def serialize(root: etree._Element) -> bytes:
    etree.indent(root, space="\t")
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(root, encoding="UTF-8") + b"\n"
    )


def configuration_xml(
    prepared: PreparedAuthoring,
    meta: DumpMetadata,
    identity: IdentityMap,
    *,
    version: str | None = None,
) -> bytes:
    root, obj = new_object("Configuration", "Configuration", identity)
    info = node(obj, "InternalInfo")
    for class_id in profile_template()["class_ids"]:
        item = node(info, "xr:ContainedObject")
        node(item, "xr:ClassId", class_id)
        node(item, "xr:ObjectId", identity.objects[logical_path("Contained/" + class_id)])
    props = node(obj, "Properties")
    node(props, "ObjectBelonging", "Adopted")
    node(props, "Name", prepared.identity.name)
    synonym(props, prepared.identity.synonym, meta.language.props["LanguageCode"])
    node(props, "Comment")
    node(props, "ConfigurationExtensionPurpose", "Customization")
    node(props, "KeepMappingToExtendedConfigurationObjectsByIDs", "true")
    node(props, "NamePrefix", prepared.identity.prefix)
    node(props, "ConfigurationExtensionCompatibilityMode", meta.compatibility_mode)
    node(props, "DefaultRunMode", meta.configuration.props["DefaultRunMode"])
    use = node(props, "UsePurposes")
    value = node(use, "v8:Value", meta.configuration.props["UsePurposes"])
    value.set(f"{{{XSI}}}type", "app:ApplicationUsePurpose")
    node(props, "ScriptVariant", meta.configuration.props["ScriptVariant"])
    node(props, "DefaultRoles")
    node(props, "Vendor")
    node(props, "Version", prepared.identity.version if version is None else version)
    node(props, "DefaultLanguage", "Language." + meta.language.name)
    for key in profile_template()["configuration_empty_information"]:
        node(props, key)
    node(
        props, "InterfaceCompatibilityMode", meta.configuration.props["InterfaceCompatibilityMode"]
    )
    children = node(obj, "ChildObjects")
    node(children, "Language", meta.language.name)
    node(children, "CommonModule", meta.module.name)
    for owner in meta.owners:
        node(children, owner.kind, owner.name)
    return serialize(root)


def adopted_xml(
    description: Description, identity: IdentityMap
) -> tuple[etree._Element, etree._Element]:
    key = description.kind + "/" + description.name
    root, obj = new_object(description.kind, key, identity)
    info = node(obj, "InternalInfo")
    if description.kind == "CommonModule":
        state = node(info, "xr:PropertyState")
        node(state, "xr:Property", "Module")
        node(state, "xr:State", "Extended")
    for name, category in description.generated_types:
        generated = node(info, "xr:GeneratedType", name=name, category=category)
        for role in ("TypeId", "ValueId"):
            node(
                generated,
                "xr:" + role,
                identity.objects[logical_path(key + "/GeneratedType/" + category + "/" + role)],
            )
    props = node(obj, "Properties")
    node(props, "ObjectBelonging", "Adopted")
    node(props, "Name", description.name)
    node(props, "Comment")
    node(props, "ExtendedConfigurationObject", description.uuid)
    if description.kind == "Language":
        node(props, "LanguageCode", description.props["LanguageCode"])
    elif description.kind == "CommonModule":
        for key in profile_template()["module_flags"]:
            node(props, key, description.props[key])
    return root, obj


def attribute_xml(
    parent: etree._Element,
    owner_key: str,
    draft: AttributeDraft,
    language_code: str,
    identity: IdentityMap,
) -> None:
    attribute = node(
        parent,
        "Attribute",
        uuid=identity.objects[logical_path(owner_key + "/Attribute/" + draft.name)],
    )
    props = node(attribute, "Properties")
    node(props, "Name", draft.name)
    synonym(props, draft.synonym, language_code)
    node(props, "Comment")
    typ = node(props, "Type")
    primitive = {"string": "string", "boolean": "boolean", "number": "decimal", "date": "dateTime"}[
        draft.primitive
    ]
    node(typ, "v8:Type", "xs:" + primitive)
    q = draft.qualifiers
    if draft.primitive == "string":
        quals = node(typ, "v8:StringQualifiers")
        node(quals, "v8:Length", str(q["string_length"]))
        node(quals, "v8:AllowedLength", "Variable")
    elif draft.primitive == "number":
        quals = node(typ, "v8:NumberQualifiers")
        node(quals, "v8:Digits", str(q["number_length"]))
        node(quals, "v8:FractionDigits", str(q["number_precision"]))
        node(quals, "v8:AllowedSign", "Nonnegative" if q["number_nonnegative"] else "Any")
    elif draft.primitive == "date":
        quals = node(typ, "v8:DateQualifiers")
        date = str(q["date_parts"])
        node(
            quals,
            "v8:DateFractions",
            {"Дата": "Date", "Время": "Time", "ДатаВремя": "DateTime"}.get(date, date),
        )
    for key, value in profile_template()["attribute_before_fill"]:
        item = node(props, key, value)
        if key in ("MinValue", "MaxValue"):
            item.set(f"{{{XSI}}}nil", "true")
    fill = node(props, "FillValue", {"boolean": "false", "number": "0"}.get(draft.primitive))
    fill.set(
        f"{{{XSI}}}{'nil' if draft.primitive == 'date' else 'type'}",
        "true" if draft.primitive == "date" else "xs:" + primitive,
    )
    for key, value in profile_template()["attribute_after_fill"]:
        if key == "Use" and not owner_key.startswith("Catalog/"):
            continue
        node(props, key, value)


def dump_extension(
    prepared: PreparedAuthoring,
    metadata: DumpMetadata,
    identity: IdentityMap,
    *,
    version: str | None = None,
) -> Mapping[str, bytes]:
    result = {"Configuration.xml": configuration_xml(prepared, metadata, identity, version=version)}
    for description in (metadata.language, metadata.module, *metadata.owners):
        root, obj = adopted_xml(description, identity)
        key = description.kind + "/" + description.name
        drafts = metadata.attributes.get(key, ())
        if drafts:
            children = node(obj, "ChildObjects")
            for draft in drafts:
                attribute_xml(
                    children, key, draft, metadata.language.props["LanguageCode"], identity
                )
        result[description.kind + "s/" + description.name + ".xml"] = serialize(root)
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class ManagerHost:
    """Снимок принимающей конфигурации для доставки собственного менеджера.

    UUID языка и плана — из основной конфигурации, а не UUID объектов расширения.
    Оба режима совместимости берутся из основной конфигурации. Пустое значение
    означает отсутствующие сведения и не заменяется режимом платформы сборщика.
    """

    configuration_uuid: str
    language: Description
    exchange_plan: Description
    compatibility_mode: str = ""
    identity: ExtensionIdentity = field(
        default_factory=lambda: ExtensionIdentity("кд3м_Менеджер", "кд3м_", "Менеджер обмена ED")
    )
    interface_compatibility_mode: str = ""


def manager_compatibility_modes(configuration: Description) -> tuple[str, str]:
    """Обязательные режимы принимающей конфигурации, без умолчаний и подмен идентичностью."""
    modes = []
    for key in ("CompatibilityMode", "InterfaceCompatibilityMode"):
        value = configuration.props.get(key, "")
        if not value.strip():
            refuse(
                "metadata_profile_unsupported",
                "В описании основной конфигурации не задан " + key,
                configuration.path,
                address="Конфигурация/" + key,
            )
        modes.append(value)
    return modes[0], modes[1]


def read_manager_host(
    descriptions: Mapping[str, str],
    plan_name: str,
    *,
    identity: ExtensionIdentity | None = None,
) -> ManagerHost:
    """Читает переданные XML: без поиска соседних объектов и без доступа к диску."""
    if "Configuration.xml" not in descriptions:
        refuse("metadata_profile_unsupported", "Не передано описание Configuration.xml")
    config = read_description(
        "Configuration.xml", descriptions["Configuration.xml"], "Configuration"
    )
    compatibility, interface = manager_compatibility_modes(config)
    language_name = config.props.get("DefaultLanguage", "").removeprefix("Language.")
    language_path = "Languages/" + language_name + ".xml"
    plan_path = "ExchangePlans/" + plan_name + ".xml"
    for path in (language_path, plan_path):
        if path not in descriptions:
            refuse("metadata_profile_unsupported", "Не передано описание " + path, path)
    language = read_description(
        language_path, descriptions[language_path], "Language", language_name
    )
    plan = read_description(plan_path, descriptions[plan_path], "ExchangePlan", plan_name)
    identity = identity or ExtensionIdentity("кд3м_Менеджер", "кд3м_", "Менеджер обмена ED")
    return ManagerHost(config.uuid, language, plan, compatibility, identity, interface)


def manager_identity_roles(
    host: ManagerHost,
    module_name: str,
    content_objects: tuple[Description, ...] = (),
    subscription_additions: tuple[SubscriptionAddition, ...] = (),
    subscription_objects: tuple[Description, ...] = (),
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Роли UUID по составу пилота: конфигурация, язык, свой модуль и план."""
    plan_key = "ExchangePlan/" + host.exchange_plan.name
    language_key = "Language/" + host.language.name
    paths = [
        "Configuration",
        *("Contained/" + c for c in profile_template()["class_ids"]),
        language_key,
        "CommonModule/" + module_name,
        plan_key,
        plan_key + "/ThisNode",
        *(
            plan_key + "/GeneratedType/" + category + "/" + role
            for category in ("Object", "Ref", "Selection", "List", "Manager")
            for role in ("TypeId", "ValueId")
        ),
    ]
    borrowed = {language_key: host.language.uuid, plan_key: host.exchange_plan.uuid}
    for obj in (
        *content_objects,
        *subscription_objects,
        *(s.description for s in subscription_additions),
    ):
        key = obj.kind + "/" + obj.name
        paths.append(key)
        borrowed[key] = obj.uuid
        paths.extend(
            key + "/GeneratedType/" + category + "/" + role
            for _, category in obj.generated_types
            for role in ("TypeId", "ValueId")
        )
    return tuple(paths), borrowed


def check_subscription_sources(
    content_objects: tuple[Description, ...],
    subscription_additions: tuple[SubscriptionAddition, ...],
    subscription_objects: tuple[Description, ...],
) -> None:
    """Источник заимствованной подписки — только заимствованный объект расширения.

    Эталон — расширение из корпуса (опись ed-writer-plan-content-2026-10): подписка
    EventSubscriptions/ОбменБухгалтерияПредприятияДокументооборот20ПередЗаписьюДокумента.xml:10-12
    добавляет источник, документ заимствован (Configuration.xml:501).
    """
    adopted = {(o.kind, o.name.casefold()) for o in (*content_objects, *subscription_objects)}
    for addition in subscription_additions:
        for value in addition.sources:
            found = registration_source_object(value)
            if found is None or (found[0], found[1].casefold()) not in adopted:
                refuse(
                    "metadata_profile_unsupported",
                    "Источник подписки не заимствован расширением: " + value,
                    addition.description.path,
                )


def dump_manager_extension(
    host: ManagerHost,
    module_name: str,
    identity: IdentityMap,
    *,
    version: str,
    content_objects: tuple[Description, ...] = (),
    subscription_additions: tuple[SubscriptionAddition, ...] = (),
    subscription_objects: tuple[Description, ...] = (),
) -> Mapping[str, bytes]:
    """XML собственного модуля и заимствованного плана: состав (в) пилота маршрута.

    Флаги модуля: BR/CommonModules/МенеджерОбменаЧерезУниверсальныйФормат13.xml:13-20.
    ThisNode/GeneratedType/ManagerModule — XML работавшего расширения пилота, строки 4-30.
    Источники подписок сверяет `check_subscription_sources` до вызова.
    """
    root, obj = new_object("Configuration", "Configuration", identity)
    info = node(obj, "InternalInfo")
    for class_id in profile_template()["class_ids"]:
        item = node(info, "xr:ContainedObject")
        node(item, "xr:ClassId", class_id)
        node(item, "xr:ObjectId", identity.objects[logical_path("Contained/" + class_id)])
    props = node(obj, "Properties")
    node(props, "ObjectBelonging", "Adopted")
    node(props, "Name", host.identity.name)
    synonym(props, host.identity.synonym, host.language.props["LanguageCode"])
    node(props, "Comment")
    node(props, "ConfigurationExtensionPurpose", "Customization")
    node(props, "KeepMappingToExtendedConfigurationObjectsByIDs", "true")
    node(props, "NamePrefix", host.identity.prefix)
    node(props, "ConfigurationExtensionCompatibilityMode", host.compatibility_mode)
    node(props, "DefaultRunMode", "ManagedApplication")
    purpose = node(node(props, "UsePurposes"), "v8:Value", "PlatformApplication")
    purpose.set(f"{{{XSI}}}type", "app:ApplicationUsePurpose")
    node(props, "ScriptVariant", "Russian")
    node(props, "DefaultRoles")
    node(props, "Vendor")
    node(props, "Version", version)
    node(props, "DefaultLanguage", "Language." + host.language.name)
    for key in profile_template()["configuration_empty_information"]:
        node(props, key)
    node(props, "InterfaceCompatibilityMode", host.interface_compatibility_mode)
    children = node(obj, "ChildObjects")
    for kind, name in (
        ("Language", host.language.name),
        ("CommonModule", module_name),
        ("ExchangePlan", host.exchange_plan.name),
        *((obj.kind, obj.name) for obj in (*content_objects, *subscription_objects)),
        *(("EventSubscription", s.description.name) for s in subscription_additions),
    ):
        node(children, kind, name)
    result = {"Configuration.xml": serialize(root)}
    language, _ = adopted_xml(host.language, identity)
    result["Languages/" + host.language.name + ".xml"] = serialize(language)
    root, obj = new_object("CommonModule", "CommonModule/" + module_name, identity)
    props = node(obj, "Properties")
    node(props, "Name", module_name)
    synonym(
        props, "Менеджер обмена через универсальный формат", host.language.props["LanguageCode"]
    )
    node(props, "Comment")
    for key, value in (
        ("Global", "false"),
        ("ClientManagedApplication", "false"),
        ("Server", "true"),
        ("ExternalConnection", "true"),
        ("ClientOrdinaryApplication", "true"),
        ("ServerCall", "false"),
        ("Privileged", "false"),
        ("ReturnValuesReuse", "DontUse"),
    ):
        node(props, key, value)
    result["CommonModules/" + module_name + ".xml"] = serialize(root)
    key = "ExchangePlan/" + host.exchange_plan.name
    root, obj = new_object("ExchangePlan", key, identity)
    info = node(obj, "InternalInfo")
    node(info, "xr:ThisNode", identity.objects[logical_path(key + "/ThisNode")])
    for category in ("Object", "Ref", "Selection", "List", "Manager"):
        item = node(
            info,
            "xr:GeneratedType",
            name="ExchangePlan" + category + "." + host.exchange_plan.name,
            category=category,
        )
        for role in ("TypeId", "ValueId"):
            node(
                item,
                "xr:" + role,
                identity.objects[logical_path(key + "/GeneratedType/" + category + "/" + role)],
            )
    state = node(info, "xr:PropertyState")
    node(state, "xr:Property", "ManagerModule")
    node(state, "xr:State", "Extended")
    if content_objects:
        state = node(info, "xr:PropertyState")
        node(state, "xr:Property", "Content")
        node(state, "xr:State", "Extended")
    props = node(obj, "Properties")
    node(props, "ObjectBelonging", "Adopted")
    node(props, "Name", host.exchange_plan.name)
    node(props, "Comment")
    node(props, "ExtendedConfigurationObject", host.exchange_plan.uuid)
    node(obj, "ChildObjects")
    result["ExchangePlans/" + host.exchange_plan.name + ".xml"] = serialize(root)
    if content_objects:
        # Эталон выгрузки расширенного заимствованного плана и строки —
        # docs/plans/ed-writer-plan-content-2026-10.md.
        namespace = "http://v8.1c.ru/8.3/xcf/extrnprops"
        content = etree.Element(
            "{" + namespace + "}ExchangePlanContent",
            nsmap=cast(
                Any,
                {None: namespace, "xr": XR, "xs": "http://www.w3.org/2001/XMLSchema", "xsi": XSI},
            ),
            version="2.20",
        )
        extension = etree.Element("{" + namespace + "}ExtensionProperty")
        for description in content_objects:
            metadata = description.kind + "." + description.name
            item = etree.SubElement(content, "{" + namespace + "}Item")
            etree.SubElement(item, "{" + namespace + "}Metadata").text = metadata
            etree.SubElement(item, "{" + namespace + "}AutoRecord").text = "Deny"
            item = etree.SubElement(extension, "{" + namespace + "}Item")
            etree.SubElement(item, "{" + namespace + "}Metadata").text = metadata
            etree.SubElement(item, "{" + namespace + "}State").text = "Modify"
            root, obj = adopted_xml(description, identity)
            node(obj, "ChildObjects")
            result[description.path] = serialize(root)
        content.append(extension)
        result["ExchangePlans/" + host.exchange_plan.name + "/Ext/Content.xml"] = serialize(content)
    for description in subscription_objects:
        # Источник подписки вне добавлений состава заимствуется без изменения состава плана:
        # эталон — заимствованный документ расширения из корпуса (строки в описи
        # docs/plans/ed-writer-plan-content-2026-10.md, раздел о подписках).
        root, obj = adopted_xml(description, identity)
        node(obj, "ChildObjects")
        result[description.path] = serialize(root)
    for addition in subscription_additions:
        # эталон — заимствованная подписка расширения из корпуса (опись, раздел о подписках):
        # заимствование содержит Source, без Handler, Event, ChildObjects и файлов Ext/.
        root, obj = adopted_xml(addition.description, identity)
        props = obj.find(f"{{{M}}}Properties")
        assert props is not None
        source = node(props, "Source")
        for value in addition.sources:
            node(source, "v8:Type", "cfg:" + value)
        result[addition.description.path] = serialize(root)
    return MappingProxyType(result)
