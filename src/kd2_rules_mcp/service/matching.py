"""Кандидаты сопоставления объектов, свойств и значений."""

from typing import Any

from kd2_rules_mcp.authoring.candidates import (
    object_candidates,
    property_candidates,
    value_candidates,
)
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.views import (
    candidate_row,
    filter_confidence,
    flatten,
    mentions,
    object_row,
    require_found,
    slice_rows,
)


class MatchingMixin(ServiceBase):
    """Инструменты match_objects, match_properties, match_values."""

    def match_objects(
        self,
        source_structure: str,
        target_structure: str,
        kind: str | None,
        confidence: str | None,
        offset: int,
        limit: int,
        text: str | None = None,
    ) -> dict[str, Any]:
        with (
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            found = object_candidates(source, target, kind or None)
        if text:
            needle = text.casefold()
            found = [item for item in found if mentions(item, needle)]
        rows = [object_row(item) for item in found]
        return slice_rows(filter_confidence(rows, confidence), offset, limit)

    def match_properties(
        self,
        source_structure: str,
        target_structure: str,
        source_object: str,
        target_object: str,
        confidence: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        with (
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            found = require_found(property_candidates(source, target, source_object, target_object))
        rows = [candidate_row(item, path) for path, item in flatten(found)]
        return slice_rows(filter_confidence(rows, confidence), offset, limit)

    def match_values(
        self,
        source_structure: str,
        target_structure: str,
        source_object: str,
        target_object: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        with (
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            found = require_found(value_candidates(source, target, source_object, target_object))
        return slice_rows([candidate_row(item, "") for item in found], offset, limit)
