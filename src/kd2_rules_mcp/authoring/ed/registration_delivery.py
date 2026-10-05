"""Доставка файла регистрации и собственных булевых реквизитов узла.

Форма заимствованного плана: образцы из projects.yaml (перечень и строки
в tests/private/test_ed_registration_kit_corpus.py). Форма собственного реквизита
требует отдельного круга kdbase/ed_registration_kit_check.py; успешная сборка
комплекта не является свидетельством установки или регистрации объектов.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from string import Template
from types import MappingProxyType

from lxml import etree

from kd2_rules_mcp.authoring.registration_retarget import RetargetResult
from kd2_rules_mcp.errors import (
    RegistrationAttributeClashError,
    RegistrationAttributePrefixError,
    RegistrationDeliveryError,
    RegistrationDeliveryProfileError,
    RegistrationExtensionClashError,
    RegistrationMissingAttributeError,
    RegistrationPlanNotFoundError,
)
from kd2_rules_mcp.kd2.rules_io import dump_rules

from .hook import valid_identifier
from .identity import IdentityMap, logical_path, make_identity_map
from .instruction import table
from .manifest import json_bytes, sha256
from .model import AttributeDraft, AuthoringPreconditionError
from .xml_dump import (
    XR,
    XSI,
    Description,
    M,
    adopted_xml,
    attribute_xml,
    manager_compatibility_modes,
    new_object,
    node,
    parse_xml,
    profile_template,
    read_description,
    serialize,
    synonym,
)

SCHEMA_VERSION = "ed-registration/1"


@dataclass(frozen=True, slots=True)
class OwnNodeAttribute:
    """Собственный реквизит узла; в первом профиле тип только «Булево»."""

    name: str
    type_name: str
    synonym: str


@dataclass(frozen=True, slots=True)
class NodeValueHint:
    """Строка инструкции: прежнее поле, новое поле или настройка и пояснение."""

    source: str
    target: str
    instruction: str = ""


@dataclass(frozen=True, slots=True)
class PlanHost:
    """Снимок целевого плана, языка, режимов и явно перечисленных расширений."""

    configuration_uuid: str
    language: Description
    exchange_plan: Description
    compatibility_mode: str
    interface_compatibility_mode: str
    properties: frozenset[str]
    extension_attributes: frozenset[str]
    input_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_hashes", MappingProxyType(dict(self.input_hashes)))


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise RegistrationDeliveryProfileError("Не удалось прочитать XML выгрузки") from error


def _properties(obj: etree._Element) -> set[str]:
    """Имена реквизитов шапки, ТЧ и её полей; разыменование здесь не проверяется."""
    standard_names = {
        "Ref": "Ссылка",
        "DeletionMark": "ПометкаУдаления",
        "Description": "Наименование",
        "Code": "Код",
        "ThisNode": "ЭтотУзел",
        "ReceivedNo": "НомерПринятого",
        "SentNo": "НомерОтправленного",
    }
    found = {
        standard_names.get(n.get("name", ""), n.get("name", ""))
        for n in obj.findall(
            f"{{{M}}}Properties/{{{M}}}StandardAttributes/{{{XR}}}StandardAttribute"
        )
    }
    for item in obj.findall(f"{{{M}}}ChildObjects/*"):
        kind = etree.QName(item).localname
        if kind not in ("Attribute", "TabularSection"):
            continue
        name = item.findtext(f"{{{M}}}Properties/{{{M}}}Name", "")
        found.add(name)
        if kind == "TabularSection":
            found.add(f"[{name}]")
            for field in item.findall(f"{{{M}}}ChildObjects/{{{M}}}Attribute"):
                field_name = field.findtext(f"{{{M}}}Properties/{{{M}}}Name", "")
                found.add(f"[{name}].{field_name}")
    return found - {""}


def read_plan_host(dump: Path, plan_name: str, *, extensions: Sequence[Path] = ()) -> PlanHost:
    """Читает только описания конфигурации, языка и выбранного плана с диска."""
    if not valid_identifier(plan_name):
        raise RegistrationDeliveryProfileError("Недопустимое имя плана обмена")
    plan_path = f"ExchangePlans/{plan_name}.xml"
    if not (dump / plan_path).is_file():
        raise RegistrationPlanNotFoundError("План обмена не найден в выгрузке")
    try:
        config_raw = _read(dump / "Configuration.xml")
        config = read_description(
            "Configuration.xml", config_raw.decode("utf-8-sig"), "Configuration"
        )
        compatibility, interface = manager_compatibility_modes(config)
        language_name = config.props.get("DefaultLanguage", "").removeprefix("Language.")
        if not valid_identifier(language_name):
            raise RegistrationDeliveryProfileError("В выгрузке не задан язык конфигурации")
        language_path = f"Languages/{language_name}.xml"
        language_raw = _read(dump / language_path)
        language = read_description(
            language_path, language_raw.decode("utf-8-sig"), "Language", language_name
        )
        if not language.props.get("LanguageCode"):
            raise RegistrationDeliveryProfileError("Не задан код языка")
        plan_raw = _read(dump / plan_path)
        plan_text = plan_raw.decode("utf-8-sig")
        plan = read_description(plan_path, plan_text, "ExchangePlan", plan_name)
        obj = parse_xml(plan_path, plan_text)[0]
        if plan.props.get("ObjectBelonging") == "Adopted":
            raise RegistrationDeliveryProfileError("Нужен план основной конфигурации")
        hashes = {
            "Configuration.xml": sha256(config_raw),
            language_path: sha256(language_raw),
            plan_path: sha256(plan_raw),
        }
        properties = _properties(obj)
        own: set[str] = set()
        for index, extension in enumerate(extensions):
            # Даже расширение без этого плана должно быть существующей выгрузкой.
            ext_config = _read(extension / "Configuration.xml")
            parse_xml("Configuration.xml", ext_config.decode("utf-8-sig"))
            hashes[f"extensions/{index}/Configuration.xml"] = sha256(ext_config)
            path = extension / plan_path
            if not path.is_file():
                continue
            raw = _read(path)
            text = raw.decode("utf-8-sig")
            description = read_description(plan_path, text, "ExchangePlan", plan_name)
            if (
                description.props.get("ObjectBelonging") != "Adopted"
                or description.props.get("ExtendedConfigurationObject", "").casefold()
                != plan.uuid.casefold()
            ):
                raise RegistrationDeliveryProfileError("Расширение заимствует другой план обмена")
            ext_obj = parse_xml(plan_path, text)[0]
            properties.update(_properties(ext_obj))
            for attr in ext_obj.findall(f"{{{M}}}ChildObjects/{{{M}}}Attribute"):
                if attr.findtext(f"{{{M}}}Properties/{{{M}}}ObjectBelonging") != "Adopted":
                    own.add(attr.findtext(f"{{{M}}}Properties/{{{M}}}Name", "").casefold())
            hashes[f"extensions/{index}/{plan_path}"] = sha256(raw)
        return PlanHost(
            config.uuid,
            language,
            plan,
            compatibility,
            interface,
            frozenset(p.casefold() for p in properties),
            frozenset(own),
            hashes,
        )
    except (AuthoringPreconditionError, UnicodeError) as error:
        raise RegistrationDeliveryProfileError("Выгрузка вне профиля XML 2.20") from error


@dataclass(frozen=True, slots=True)
class RegistrationManifest:
    """Опись без времени сборки; manifest.json не хеширует сам себя."""

    input_hashes: Mapping[str, str]
    file_hashes: Mapping[str, str]
    counters: Mapping[str, int]
    compatibility_mode: str
    interface_compatibility_mode: str
    extension_name: str
    prefix: str
    build_hash: str

    def __post_init__(self) -> None:
        for field in ("input_hashes", "file_hashes", "counters"):
            object.__setattr__(self, field, MappingProxyType(dict(getattr(self, field))))

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "input_hashes": dict(self.input_hashes),
            "file_hashes": dict(self.file_hashes),
            "counters": dict(self.counters),
            "compatibility_mode": self.compatibility_mode,
            "interface_compatibility_mode": self.interface_compatibility_mode,
            "extension_name": self.extension_name,
            "prefix": self.prefix,
            "build_hash": self.build_hash,
            "extension_version": self.build_hash[:12],
            "runtime_verified": False,
            # Форма собственного булева реквизита заимствованного плана подтверждена кругом
            # «загрузить — проверить — обновить — выгрузить» на платформе 8.3.27 (сценарий
            # kdbase/ed_registration_kit_check.py). Регистрация и обмен этим не проверены.
            "xml_form_verified": True,
            "unverified": [],
        }

    def to_bytes(self) -> bytes:
        return json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, content: bytes) -> RegistrationManifest:
        try:
            value = json.loads(content)
            if value["schema_version"] != SCHEMA_VERSION or value["runtime_verified"] is not False:
                raise ValueError("Неизвестный формат")
            result = cls(**{k: value[k] for k in cls.__dataclass_fields__})
            if result.to_bytes() != content:
                raise ValueError("Неканонический манифест")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RegistrationDeliveryError("Повреждён манифест регистрации") from error


@dataclass(frozen=True, slots=True)
class RegistrationKit:
    files: Mapping[str, bytes]
    manifest: RegistrationManifest
    remarks: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(sorted(self.files.items()))))


def _extension_files(
    host: PlanHost, attrs: Sequence[OwnNodeAttribute], name: str, prefix: str, version: str
) -> dict[str, bytes]:
    """Заимствование из образцов, реквизит — профиль до подтверждения кругом 1С."""
    key = "ExchangePlan/" + host.exchange_plan.name
    language_key = "Language/" + host.language.name
    categories = ("Object", "Ref", "Selection", "List", "Manager")
    paths = (
        "Configuration",
        *("Contained/" + c for c in profile_template()["class_ids"]),
        key,
        language_key,
        key + "/ThisNode",
        *(key + "/GeneratedType/" + c + "/" + r for c in categories for r in ("TypeId", "ValueId")),
        *(key + "/Attribute/" + a.name for a in attrs),
    )
    identity = make_identity_map(
        host.configuration_uuid,
        name,
        paths,
        {key: host.exchange_plan.uuid, language_key: host.language.uuid},
    )
    root, obj = new_object("Configuration", "Configuration", identity)
    info = node(obj, "InternalInfo")
    for class_id in profile_template()["class_ids"]:
        item = node(info, "xr:ContainedObject")
        node(item, "xr:ClassId", class_id)
        node(item, "xr:ObjectId", identity.objects[logical_path("Contained/" + class_id)])
    props = node(obj, "Properties")
    node(props, "ObjectBelonging", "Adopted")
    node(props, "Name", name)
    synonym(props, "Реквизиты узлов для правил регистрации", host.language.props["LanguageCode"])
    node(props, "Comment")
    for prop, value in (
        ("ConfigurationExtensionPurpose", "Customization"),
        ("KeepMappingToExtendedConfigurationObjectsByIDs", "true"),
        ("NamePrefix", prefix),
        ("ConfigurationExtensionCompatibilityMode", host.compatibility_mode),
        ("DefaultRunMode", "ManagedApplication"),
    ):
        node(props, prop, value)
    use = node(node(props, "UsePurposes"), "v8:Value", "PlatformApplication")
    use.set(f"{{{XSI}}}type", "app:ApplicationUsePurpose")
    for prop, value in (
        ("ScriptVariant", "Russian"),
        ("DefaultRoles", ""),
        ("Vendor", ""),
        ("Version", version),
        ("DefaultLanguage", "Language." + host.language.name),
        *((p, "") for p in profile_template()["configuration_empty_information"]),
        ("InterfaceCompatibilityMode", host.interface_compatibility_mode),
    ):
        node(props, prop, value)
    children = node(obj, "ChildObjects")
    node(children, "Language", host.language.name)
    node(children, "ExchangePlan", host.exchange_plan.name)
    result = {"extension/Configuration.xml": serialize(root)}
    language, _ = adopted_xml(host.language, identity)
    result[f"extension/Languages/{host.language.name}.xml"] = serialize(language)
    root, obj = new_object("ExchangePlan", key, identity)
    info = node(obj, "InternalInfo")
    _plan_internal_info(info, key, host.exchange_plan.name, categories, identity)
    props = node(obj, "Properties")
    for prop, value in (
        ("ObjectBelonging", "Adopted"),
        ("Name", host.exchange_plan.name),
        ("Comment", ""),
        ("ExtendedConfigurationObject", host.exchange_plan.uuid),
    ):
        node(props, prop, value)
    children = node(obj, "ChildObjects")
    for attr in attrs:
        attribute_xml(
            children,
            key,
            AttributeDraft(attr.name, attr.synonym, "boolean", {}),
            host.language.props["LanguageCode"],
            identity,
        )
    result[f"extension/ExchangePlans/{host.exchange_plan.name}.xml"] = serialize(root)
    return result


def _plan_internal_info(
    info: etree._Element, key: str, name: str, categories: Sequence[str], identity: IdentityMap
) -> None:
    node(info, "xr:ThisNode", identity.objects[logical_path(key + "/ThisNode")])
    for category in categories:
        item = node(
            info, "xr:GeneratedType", name=f"ExchangePlan{category}.{name}", category=category
        )
        for role in ("TypeId", "ValueId"):
            node(
                item,
                "xr:" + role,
                identity.objects[logical_path(key + "/GeneratedType/" + category + "/" + role)],
            )


def render_registration_kit(
    result: RetargetResult,
    host: PlanHost,
    *,
    own_attributes: Sequence[OwnNodeAttribute],
    node_values: Sequence[NodeValueHint] = (),
    extension_name: str,
    prefix: str,
    previous_manifest: RegistrationManifest | None = None,
) -> RegistrationKit:
    """Собирает повторяемые байты; никакой установки в базу и записи на диск."""
    if not valid_identifier(extension_name) or not valid_identifier(prefix):
        raise RegistrationDeliveryProfileError("Недопустимое имя расширения или префикс")
    if result.document.exchange_plan.casefold() != host.exchange_plan.name.casefold():
        raise RegistrationDeliveryProfileError("Правила предназначены для другого плана")
    if not result.only_expected or result.document.origin is None:
        raise RegistrationDeliveryProfileError(
            "Нет подтверждённого переноса или исходных байтов правил"
        )
    attrs = tuple(sorted(own_attributes, key=lambda a: a.name.casefold()))
    names: set[str] = set()
    for attr in attrs:
        key = attr.name.casefold()
        if not valid_identifier(attr.name) or not key.startswith(prefix.casefold()):
            raise RegistrationAttributePrefixError(
                "Имя собственного реквизита должно начинаться с префикса"
            )
        if key in host.extension_attributes:
            raise RegistrationExtensionClashError(
                "Реквизит уже добавлен другим расширением: " + attr.name
            )
        if key in host.properties or key in names:
            raise RegistrationAttributeClashError("Реквизит уже существует: " + attr.name)
        if attr.type_name != "Булево":
            raise RegistrationDeliveryProfileError(
                "Тип собственного реквизита вне профиля «Булево»"
            )
        names.add(key)
    for remark in result.remarks:
        if remark.property_name.casefold() not in host.properties | names:
            raise RegistrationMissingAttributeError(
                "Правила ссылаются на реквизит, которого не будет в базе: " + remark.property_name
            )
    remarks = tuple(
        r.message
        + ("; реквизит будет добавлен расширением" if r.property_name.casefold() in names else "")
        for r in result.remarks
    ) + tuple(m.message for m in result.mentions)
    if result.code_mentions and not result.mentions:
        remarks += (f"В коде остались старые имена: {result.code_mentions} упоминаний.",)
    content = dump_rules(result.document)
    hashes = {
        **host.input_hashes,
        "source_rules": sha256(result.document.origin),
        "retargeted_rules": sha256(content),
        "decisions": sha256(
            json_bytes(
                {
                    "attributes": [asdict(a) for a in attrs],
                    "node_values": [asdict(v) for v in node_values],
                    "extension_name": extension_name,
                    "prefix": prefix,
                    "remarks": remarks,
                }
            )
        ),
        "instruction_template": sha256(
            files("kd2_rules_mcp.authoring.ed")
            .joinpath("templates/registration_instruction.md")
            .read_bytes()
        ),
        "xml_profile": sha256(
            files("kd2_rules_mcp.authoring.ed")
            .joinpath("templates/xml_profile_2_20.json")
            .read_bytes()
        ),
    }
    counters = {
        "rules": len(result.rules),
        "renamed_leaves": sum(r.renamed for r in result.rules),
        "untouched_leaves": sum(r.untouched for r in result.rules),
        "code_mentions": result.code_mentions,
        "remarks": len(result.remarks),
    }
    build_hash = sha256(
        json_bytes({"inputs": hashes, "counters": counters, "schema": SCHEMA_VERSION})
    )
    output = _extension_files(host, attrs, extension_name, prefix, build_hash[:12])
    output["registration/RegistrationRules.xml"] = content
    template = Template(
        files("kd2_rules_mcp.authoring.ed")
        .joinpath("templates/registration_instruction.md")
        .read_text("utf-8")
    )
    incomplete = ""
    if result.code_mentions or remarks:
        incomplete = (
            "## Перенос неполон\n\n"
            + "\n".join("- " + r for r in remarks)
            + "\n\nПроверьте замечания до загрузки правил.\n"
        )
    instruction = template.substitute(
        incomplete=incomplete,
        plan_name=host.exchange_plan.name,
        extension_name=extension_name,
        compatibility=host.compatibility_mode,
        interface=host.interface_compatibility_mode,
        node_values=table(
            ("Старый реквизит", "Новый реквизит или настройка", "Как перенести"),
            ((v.source, v.target, v.instruction) for v in node_values),
        ),
        own_attributes=table(
            ("Реквизит", "Тип", "Подпись"), ((a.name, a.type_name, a.synonym) for a in attrs)
        ),
    )
    output["ИНСТРУКЦИЯ.md"] = instruction.encode("utf-8")
    manifest = RegistrationManifest(
        hashes,
        {p: sha256(b) for p, b in sorted(output.items())},
        counters,
        host.compatibility_mode,
        host.interface_compatibility_mode,
        extension_name,
        prefix,
        build_hash,
    )
    if (
        previous_manifest
        and previous_manifest.input_hashes == manifest.input_hashes
        and previous_manifest.to_bytes() != manifest.to_bytes()
    ):
        raise RegistrationDeliveryError("Прежняя сборка с теми же входами не совпадает")
    output["manifest.json"] = manifest.to_bytes()
    return RegistrationKit(output, manifest, remarks)
