"""Компактные страницы снимка маршрутов и отчёта пары."""

from collections.abc import Sequence
from functools import cmp_to_key
from typing import Any

from kd2_rules_mcp.ed.route_model import ManagerInfo, RouteEntry, RouteProfile, RouteSource
from kd2_rules_mcp.ed.routes import compare_versions
from kd2_rules_mcp.service.views import report_summary, slice_rows
from kd2_rules_mcp.validation.ed_routes import RouteComparison, SchemaDiff
from kd2_rules_mcp.validation.report import ValidationReport

ROUTE_SECTIONS = ("summary", "plans", "versions", "variants", "packages", "skipped")
COMPARE_SECTIONS = ("issues", "versions", "schema_diff", "skipped")
RAW_LIMIT = 240
CHAIN_LIMIT = 4
DIFF_LIMIT = 20


def clip(text: str, limit: int = RAW_LIMIT) -> str:
    """Обрезает сырой текст условия: страница не несёт тело процедуры."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def summary_counts(profile: RouteProfile) -> dict[str, int]:
    """Счётчики снимка до постраничного отбора."""
    return {
        "plans": len(profile.plans),
        "ed_plans": sum(plan.is_ed is True for plan in profile.plans),
        "versions": sum(len(plan.effective_map()) for plan in profile.plans),
        "without_node": len(profile.effective_without_node()),
        "packages": len(profile.packages),
        "variants": sum(len(plan.variants) for plan in profile.plans),
        "skipped": len(profile.skipped),
    }


def route_summary(
    profile: RouteProfile,
    source: dict[str, Any],
    *,
    reused: bool,
    stale: bool,
) -> dict[str, Any]:
    """Шапка одного снимка. `status` здесь — полнота чтения, не совместимость пары."""
    return {
        "profile_id": profile.profile_id,
        "source": source,
        "configuration_name": profile.configuration_name,
        "status": profile.status,
        "counts": summary_counts(profile),
        "node_state": "unknown",
        "extension_policy": "base_only",
        "available_sections": list(ROUTE_SECTIONS),
        "reused": reused,
        "stale": stale,
    }


def route_page(
    profile_id: str,
    section: str,
    rows: Sequence[Any],
    offset: int,
    limit: int,
    *,
    reused: bool,
    stale: bool,
) -> dict[str, Any]:
    """Страница раздела плюс идентификатор снимка, с которым её открыли."""
    return {
        "profile_id": profile_id,
        "reused": reused,
        "stale": stale,
        "section": section,
        **slice_rows(rows, offset, limit),
    }


def route_rows(profile: RouteProfile, section: str, plan_name: str | None) -> list[dict[str, Any]]:
    """Строки раздела. `plan_name` — уже написание из конфигурации."""
    if section == "plans":
        plans = profile.plans
        if plan_name is not None:
            plans = tuple(plan for plan in plans if plan.plan_name == plan_name)
        return [
            {
                "name": plan.plan_name,
                "is_ed": plan.is_ed,
                "base_namespace": plan.base_namespace,
                "status": plan.status,
                "versions": len(plan.effective_map()),
                "registration": plan.registration.mode,
                "empty_node_fallback": plan.empty_node_fallback,
                "variants": len(plan.variants),
            }
            for plan in plans
        ]
    if section == "versions":
        return _version_rows(profile, plan_name)
    if section == "variants":
        return _variant_rows(profile, plan_name)
    if section == "packages":
        return [
            {
                "metadata_name": item.metadata_name,
                "namespace": item.namespace,
                "revision": item.revision_label,
                "description_path": item.description_path,
                "package_path": item.package_path,
            }
            for item in profile.packages
        ]
    if section == "skipped":
        return [
            {
                "code": item.code,
                "reason": item.reason,
                "file": item.relative_file,
                "line": item.line,
            }
            for item in profile.skipped
        ]
    return []


def compare_response(
    *,
    left_id: str,
    right_id: str,
    comparison: RouteComparison,
    selected: dict[str, Any],
    left: RouteProfile,
    right: RouteProfile,
    section: str,
    level: str | None,
    check_prefix: str | None,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Отчёт пары. Итог и полный список пропусков считаются до отбора страницы."""
    report = comparison.report
    summary = report_summary(report)
    skipped = [item.to_dict() for item in report.skipped]
    profile = comparison.profile
    body: dict[str, Any] = {
        "left_profile_id": left_id,
        "right_profile_id": right_id,
        "profile": {
            "context": profile.context,
            "status": profile.status,
            "quality": profile.quality,
            "left_plan": profile.left_plan,
            "right_plan": profile.right_plan,
            "negotiated_candidate": profile.negotiated_candidate,
            "actual_node_version": profile.actual_node_version,
            "empty_node_fallback": {
                "left": profile.empty_node_fallback[0],
                "right": profile.empty_node_fallback[1],
            },
            "common_version_count": len(profile.common_versions),
            "common_versions": list(profile.common_versions),
            "tied_maxima": list(profile.tied_maxima),
            "selected": selected,
        },
        "summary": summary,
    }
    if section == "skipped":
        body["skipped"] = slice_rows(skipped, offset, limit)
        return body
    body["skipped"] = skipped
    if section == "issues":
        rows = _issue_rows(report, level, check_prefix)
    elif section == "versions":
        rows = _pair_versions(left, right, comparison)
    else:
        rows = [_diff_row(item) for item in comparison.schema_diffs]
    body[section] = slice_rows(rows, offset, limit)
    return body


def _issue_rows(
    report: ValidationReport, level: str | None, check_prefix: str | None
) -> list[dict[str, str]]:
    rows = [issue.to_dict() for issue in report.issues]
    if level:
        rows = [row for row in rows if row["level"] == level]
    if check_prefix:
        rows = [row for row in rows if row["check"].startswith(check_prefix)]
    return rows


def _version_rows(profile: RouteProfile, plan_name: str | None) -> list[dict[str, Any]]:
    full_chain = plan_name is not None
    plans = profile.plans
    if plan_name is not None:
        plans = tuple(plan for plan in plans if plan.plan_name == plan_name)
    rows: list[dict[str, Any]] = []
    for plan in plans:
        for entry in plan.entries:
            rows.append(
                _version_row(
                    profile,
                    "plan",
                    plan.plan_name,
                    plan.base_namespace,
                    entry,
                    full_chain,
                )
            )
    if plan_name is None:
        for entry in profile.without_node_entries:
            rows.append(_version_row(profile, "without_node", None, None, entry, False))
    return rows


def _version_row(
    profile: RouteProfile,
    context: str,
    plan_name: str | None,
    base: str | None,
    entry: RouteEntry,
    full_chain: bool,
) -> dict[str, Any]:
    manager = _manager(profile, entry.manager_name)
    raw = "; ".join(item.raw for item in entry.conditions)
    row: dict[str, Any] = {
        "context": context,
        "plan": plan_name,
        "key": entry.key,
        "manager_name": entry.manager_name,
        "exists": bool(manager and manager.source_exists),
        "interface_version": None if manager is None else manager.interface_version,
        "directions": list(manager.directions) if manager is not None else [],
        "package_candidates": _package_names(profile, base, entry.key),
        "state": entry.state,
        "condition": None if not entry.conditions else clip(raw),
        "source": _origin(entry.source),
        "call_steps": len(entry.source.call_chain),
    }
    if full_chain:
        row["call_chain"] = [
            {"file": step.file_id, "line": step.line_start}
            for step in entry.source.call_chain[:CHAIN_LIMIT]
        ]
    return row


def _variant_rows(profile: RouteProfile, plan_name: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for plan in profile.plans:
        if plan_name is not None and plan.plan_name != plan_name:
            continue
        for variant in plan.variants:
            unknown = [
                item.raw
                for item in (*variant.conditions, *variant.metadata_predicates)
                if item.value == "unknown"
            ]
            if variant.correspondent is None and variant.correspondent_raw:
                unknown.append(variant.correspondent_raw)
            rows.append(
                {
                    "plan": plan.plan_name,
                    "id": variant.id,
                    "raw": clip(variant.raw_id),
                    "correspondent": variant.correspondent,
                    "conditions": [
                        {"kind": item.kind, "raw": clip(item.raw), "value": item.value}
                        for item in variant.conditions
                    ],
                    "unresolved": clip(unknown[0]) if unknown else None,
                }
            )
    return rows


def _pair_versions(
    left: RouteProfile, right: RouteProfile, comparison: RouteComparison
) -> list[dict[str, Any]]:
    profile = comparison.profile
    left_map = _effective(left, profile.context, profile.left_plan)
    right_map = _effective(right, profile.context, profile.right_plan)
    ordered: dict[str, None] = {}
    for key in (*left_map, *right_map):
        ordered.setdefault(key, None)
    rows: list[dict[str, Any]] = []
    for key in sorted(ordered, key=cmp_to_key(_version_cmp)):
        rows.append(
            {
                "key": key,
                "selected": key == profile.negotiated_candidate,
                "left": _side_version(left, profile.context, profile.left_plan, key),
                "right": _side_version(right, profile.context, profile.right_plan, key),
            }
        )
    return rows


def _side_version(
    profile: RouteProfile, context: str, plan_name: str | None, key: str
) -> dict[str, Any]:
    current = _effective(profile, context, plan_name).get(key)
    entry = _effective_entry(profile, context, plan_name, key)
    base = _plan_base(profile, context, plan_name)
    return {
        "manager_name": current,
        "source": None if entry is None else _origin(entry.source),
        "call_steps": 0 if entry is None else len(entry.source.call_chain),
        "package_candidates": _package_names(profile, base, key),
    }


def _diff_row(diff: SchemaDiff) -> dict[str, Any]:
    changes = [
        {
            "type": item.type_qname,
            "path": item.property_path,
            "field": item.field,
            "left": item.left,
            "right": item.right,
        }
        for item in diff.changes[:DIFF_LIMIT]
    ]
    return {
        "uri": diff.uri,
        "added_types": list(diff.added_types[:DIFF_LIMIT]),
        "added_total": len(diff.added_types),
        "removed_types": list(diff.removed_types[:DIFF_LIMIT]),
        "removed_total": len(diff.removed_types),
        "changed_types": list(diff.changed_types[:DIFF_LIMIT]),
        "changed_total": len(diff.changed_types),
        "changes": changes,
        "changes_total": len(diff.changes),
    }


def _package_names(profile: RouteProfile, base: str | None, key: str) -> list[str]:
    if not base or not key:
        return []
    uri = f"{base}/{key}"
    return [item.metadata_name for item in profile.packages if item.namespace == uri]


def _manager(profile: RouteProfile, name: str | None) -> ManagerInfo | None:
    if not name:
        return None
    folded = name.casefold()
    for item in profile.managers:
        if item.name.casefold() == folded:
            return item
    return None


def _origin(source: RouteSource) -> str:
    place = f"{source.relative_file}:{source.line_start}"
    if source.procedure:
        return f"{place} {source.procedure}"
    return place


def _effective(profile: RouteProfile, context: str, plan_name: str | None) -> dict[str, str | None]:
    if context == "without_node":
        return profile.effective_without_node()
    for plan in profile.plans:
        if plan.plan_name == plan_name:
            return plan.effective_map()
    return {}


def _plan_base(profile: RouteProfile, context: str, plan_name: str | None) -> str | None:
    if context != "plan":
        return None
    for plan in profile.plans:
        if plan.plan_name == plan_name:
            return plan.base_namespace
    return None


def _entries(profile: RouteProfile, context: str, plan_name: str | None) -> tuple[RouteEntry, ...]:
    if context == "without_node":
        return profile.without_node_entries
    for plan in profile.plans:
        if plan.plan_name == plan_name:
            return plan.entries
    return ()


def _effective_entry(
    profile: RouteProfile, context: str, plan_name: str | None, key: str
) -> RouteEntry | None:
    found = [
        item
        for item in _entries(profile, context, plan_name)
        if item.key == key and item.state == "effective"
    ]
    return found[-1] if found else None


def _version_cmp(left: str, right: str) -> int:
    if left == right:
        return 0
    result = compare_versions(left, right)
    if result is None or result == 0:
        return (left > right) - (left < right)
    return result
