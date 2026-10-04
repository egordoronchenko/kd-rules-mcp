"""Кандидаты сопоставления объектов, свойств и значений."""

from typing import Any

from kd2_rules_mcp.authoring.candidates import (
    object_candidates,
    property_candidates,
    value_candidates,
)
from kd2_rules_mcp.authoring.edits import find_rule
from kd2_rules_mcp.kd2.model import ExchangeRules
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
from kd2_rules_mcp.validation.address import pks_address, plain_pks_path, side_name, walk_pks


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
        rules_project_id: str | None = None,
        code: str | None = None,
        uncovered: bool = False,
    ) -> dict[str, Any]:
        with (
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            found = require_found(property_candidates(source, target, source_object, target_object))
        rows = filter_confidence(
            [candidate_row(item, path) for path, item in flatten(found)], confidence
        )
        if rules_project_id or code or uncovered:
            if not rules_project_id or not code:
                raise ValueError(
                    "Чтобы отметить покрытие ПКС, передайте проект правил (rules_project_id) "
                    "и код ПКО (code)"
                )
            with self._lock:
                covered = _pks_coverage(self._exchange(rules_project_id), code)
            marked: list[dict[str, Any]] = []
            for row in rows:
                addresses = _covered_by(row, covered)
                if uncovered and addresses:
                    continue
                if addresses:
                    row = {**row, "covered": True, "pks": addresses}
                else:
                    row = {**row, "covered": False}
                marked.append(row)
            rows = marked
        return slice_rows(rows, offset, limit)

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


def _pks_coverage(rules: ExchangeRules, code: str) -> dict[tuple[str, str], list[str]]:
    """Путь свойства приёмника и имя источника → адреса ПКС этого ПКО."""
    pko = find_rule(rules, "pko", code)
    properties = pko.child("Свойства")
    covered: dict[tuple[str, str], list[str]] = {}
    if properties is None:
        return covered
    for path, node in walk_pks(properties):
        if node.is_group:
            continue
        key = (plain_pks_path(path), side_name(node, "Источник").casefold())
        covered.setdefault(key, []).append(pks_address(pko.code, path))
    return covered


def _covered_by(row: dict[str, Any], covered: dict[tuple[str, str], list[str]]) -> list[str]:
    path = str(row.get("path") or "")
    source = row.get("source")
    source_name = str(source.get("name") or "").casefold() if isinstance(source, dict) else ""
    if source_name:
        return list(covered.get((path, source_name), []))
    found: list[str] = []
    for (candidate_path, _source), addresses in covered.items():
        if candidate_path == path:
            found.extend(addresses)
    return found
