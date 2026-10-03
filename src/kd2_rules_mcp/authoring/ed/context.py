"""Локальные индексы одной подготовки; входные снимки остаются неизменными."""

from kd2_rules_mcp.ed.address import AddressIndex, build_addresses
from kd2_rules_mcp.ed.model import EdDocument
from kd2_rules_mcp.ed.schema.model import EdSchema
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile

from .model import AuthoringInputs


class AuthoringContext:
    """Профиль один на пару версия/направление, применимость — на документ и профиль."""

    def __init__(self, inputs: AuthoringInputs):
        self.inputs = inputs
        self.indices: dict[int, AddressIndex] = {}
        self.profiles: dict[tuple[str, str], ValidationProfile] = {}
        self.folded_profiles: dict[int, ValidationProfile] = {}
        self.applications: dict[tuple[int, str, str], Applicability] = {}

    def index(self, document: EdDocument) -> AddressIndex:
        key = id(document)
        if key not in self.indices:
            self.indices[key] = build_addresses(document)
        return self.indices[key]

    def profile(self, version: str, direction: str) -> ValidationProfile:
        key = (version, direction)
        if key not in self.profiles:
            schema = self.inputs.schemas.get(version)
            self.profiles[key] = ValidationProfile.build(
                schema if isinstance(schema, EdSchema) else None, version, direction
            )
        return self.profiles[key]

    def applicable(self, document: EdDocument, version: str, direction: str) -> Applicability:
        key = (id(document), version, direction)
        if key not in self.applications:
            self.applications[key] = Applicability.build(document, self.profile(version, direction))
        return self.applications[key]
