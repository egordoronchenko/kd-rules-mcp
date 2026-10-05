"""Собственные UUID5 и ссылки заимствования — разные пространства (§1.3)."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import NoReturn
from uuid import NAMESPACE_URL, UUID, uuid5

from .model import AuthoringPreconditionError, Failure


def refuse(identifier: str, message: str, file: str = "", address: str = "") -> NoReturn:
    """Отказы писателя используют единственный класс исключения ядра."""
    raise AuthoringPreconditionError((Failure("ed.author." + identifier, address, message, file),))


def logical_path(path: str) -> str:
    parts = path.split("/")
    if any(not p or p in (".", "..") or "\\" in p or ":" in p for p in parts):
        refuse("metadata_profile_unsupported", "Недопустимый логический путь идентификатора")
    return path.casefold()


# зерно идентификаторов расширений; не менять никогда:
# от него зависят УИДы уже установленных расширений
EXTENSION_IDENTITY_SEED = "kd2-rules-mcp/ed-authoring/v1/"


def artifact_uuid(base_configuration_uuid: str, extension_name: str) -> str:
    """Namespace не зависит от списка операций, версии писателя и времени."""
    base = str(UUID(base_configuration_uuid))
    return str(
        uuid5(NAMESPACE_URL, EXTENSION_IDENTITY_SEED + base + "/" + extension_name.casefold())
    )


@dataclass(frozen=True, slots=True)
class IdentityMap:
    artifact_uuid: str
    objects: Mapping[str, str]
    borrowed: Mapping[str, str]

    def __post_init__(self) -> None:
        own = {logical_path(k): str(UUID(v)) for k, v in self.objects.items()}
        # Ссылки валидируются, но их написание копируется из основной выгрузки.
        for value in self.borrowed.values():
            UUID(value)
        borrowed = {logical_path(k): v for k, v in self.borrowed.items()}
        if len(own) != len(self.objects) or len(set(own.values())) != len(own):
            refuse("owned_content_changed", "Карта собственных UUID содержит коллизии")
        object.__setattr__(self, "objects", MappingProxyType(own))
        object.__setattr__(self, "borrowed", MappingProxyType(borrowed))
        object.__setattr__(self, "artifact_uuid", str(UUID(self.artifact_uuid)))


def make_identity_map(
    base_configuration_uuid: str,
    extension_name: str,
    paths: tuple[str, ...],
    borrowed: Mapping[str, str],
    *,
    previous: IdentityMap | None = None,
    external: IdentityMap | None = None,
) -> IdentityMap:
    """Внешняя карта допускается только явным параметром для сверки сериализации."""
    namespace = artifact_uuid(base_configuration_uuid, extension_name)
    keys = {logical_path(p) for p in paths}
    refs = {logical_path(k): v for k, v in borrowed.items()}
    source = external or previous
    if source and (
        source.artifact_uuid != namespace
        or not set(source.objects) <= keys
        or any(refs.get(k) != v for k, v in source.borrowed.items())
    ):
        refuse(
            "owned_content_changed", "Идентичность комплекта или ссылки заимствования изменились"
        )
    if external and set(external.objects) != keys:
        refuse("owned_content_changed", "Внешняя карта UUID не соответствует составу комплекта")
    if external and previous and external != previous:
        refuse("owned_content_changed", "Внешняя карта UUID отличается от прежнего manifest")
    if (
        previous
        and not external
        and any(str(uuid5(UUID(namespace), k)) != v for k, v in previous.objects.items())
    ):
        refuse(
            "owned_content_changed",
            "Собственные UUID прежнего manifest не соответствуют namespace комплекта",
        )
    own = {
        k: source.objects[k] if source and k in source.objects else str(uuid5(UUID(namespace), k))
        for k in sorted(keys)
    }
    return IdentityMap(namespace, own, refs)


def identity_map_from_xml(
    descriptions: Mapping[str, str], base_configuration_uuid: str, extension_name: str
) -> IdentityMap:
    """Извлекает роли UUID оракула; заимствованные ссылки не нормализуются."""
    from .xml_dump import XR, M, parse_xml

    own: dict[str, str] = {}
    borrowed: dict[str, str] = {}
    for path, text in sorted(descriptions.items()):
        obj = parse_xml(path, text)[0]
        kind = obj.tag.removeprefix("{" + M + "}")
        name = obj.findtext(f"{{{M}}}Properties/{{{M}}}Name", "")
        key = "Configuration" if kind == "Configuration" else kind + "/" + name
        own[key] = str(obj.attrib["uuid"])
        reference = obj.findtext(f"{{{M}}}Properties/{{{M}}}ExtendedConfigurationObject")
        if reference:
            borrowed[key] = reference
        for item in obj.findall(f"{{{M}}}InternalInfo/{{{XR}}}ContainedObject"):
            own["Contained/" + str(item.findtext(f"{{{XR}}}ClassId", ""))] = str(
                item.findtext(f"{{{XR}}}ObjectId", "")
            )
        for item in obj.findall(f"{{{M}}}InternalInfo/{{{XR}}}GeneratedType"):
            for role in ("TypeId", "ValueId"):
                own[key + "/GeneratedType/" + str(item.attrib["category"]) + "/" + role] = str(
                    item.findtext(f"{{{XR}}}{role}", "")
                )
        for item in obj.findall(f"{{{M}}}ChildObjects/{{{M}}}Attribute"):
            attribute = item.findtext(f"{{{M}}}Properties/{{{M}}}Name", "")
            own[key + "/Attribute/" + attribute] = str(item.attrib["uuid"])
    return IdentityMap(artifact_uuid(base_configuration_uuid, extension_name), own, borrowed)
