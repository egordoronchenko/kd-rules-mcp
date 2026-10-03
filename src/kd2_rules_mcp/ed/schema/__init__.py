"""Чтение неизменяемых схем формата EnterpriseData из пакетов XDTO."""

from .model import EdSchema, QName, ResolvedProperty
from .profile import Applicability, ValidationProfile
from .resolver import load_schema, resolve_property

__all__ = [
    "Applicability",
    "EdSchema",
    "QName",
    "ResolvedProperty",
    "ValidationProfile",
    "load_schema",
    "resolve_property",
]
