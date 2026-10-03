"""Чтение неизменяемых схем формата EnterpriseData из пакетов XDTO."""

from .model import EdSchema, QName, ResolvedProperty
from .resolver import load_schema, resolve_property

__all__ = ["EdSchema", "QName", "ResolvedProperty", "load_schema", "resolve_property"]
