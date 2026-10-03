"""Ошибки чтения схемы, общие для чистого слоя и сервиса."""

from kd2_rules_mcp.errors import (
    EdSchemaAmbiguousImportError,
    EdSchemaConflictError,
    EdSchemaFormatError,
    EdSchemaNotFoundError,
    EdSchemaProfileMismatchError,
    EdSchemaReadError,
    EdSchemaResourceLimitError,
    EdSchemaTypeNotFoundError,
)

__all__ = [
    "EdSchemaAmbiguousImportError",
    "EdSchemaConflictError",
    "EdSchemaFormatError",
    "EdSchemaNotFoundError",
    "EdSchemaProfileMismatchError",
    "EdSchemaReadError",
    "EdSchemaResourceLimitError",
    "EdSchemaTypeNotFoundError",
]
