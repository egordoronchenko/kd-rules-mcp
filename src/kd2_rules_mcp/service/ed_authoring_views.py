"""Разделы автора: компактная сводка и подробности только по явному запросу."""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from kd2_rules_mcp.authoring.ed.manifest import sha256
from kd2_rules_mcp.authoring.ed.model import AuthoringInputs, PreparedAuthoring
from kd2_rules_mcp.authoring.ed.render import RenderedAuthoring
from kd2_rules_mcp.ed.route_model import RouteEntry, RouteProfile
from kd2_rules_mcp.service.ed_views import address_matches, skipped_addresses
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key
from kd2_rules_mcp.validation.report import Issue, Level, Skipped

SECTIONS = ("summary", "issues_before", "issues_after", "scopes", "skipped")
PAGE_BYTES = 4096


def validate_options(section, level, check_prefix, address_prefix) -> None:
    if section not in SECTIONS:
        raise ValueError("Раздел авторинга: " + ", ".join(SECTIONS))
    if level is not None:
        if not isinstance(level, str):
            raise ValueError("level должен быть строкой")
        Level(level)
    for name, value in (("check_prefix", check_prefix), ("address_prefix", address_prefix)):
        if value is not None and not isinstance(value, str):
            raise ValueError(name + " должен быть строкой")


def json_size(value: dict) -> int:
    """Размер JSON UTF-8 с отступами, как в текстовом ответе MCP SDK."""
    return len(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def page(rows: Sequence[dict], offset: int, limit: int) -> dict:
    items = list(rows[offset : offset + limit])
    return {
        "items": items,
        "offset": offset,
        "limit": limit,
        "total": len(rows),
        "has_more": offset + len(items) < len(rows),
        "next_offset": offset + len(items),
    }


def compact_page(base: dict, rows: Sequence[dict], offset: int, limit: int) -> dict:
    """Лимит — верхняя граница числа записей; продолжение всегда по next_offset.

    Неделимую запись и обязательную сводку не обрезаем. Даже при необычно длинной
    записи страница продвигается; порог 4 КБ закреплён тестами для рабочего корпуса.
    """
    result = base | page(rows, offset, limit)
    while len(result["items"]) > 1 and json_size(result) > PAGE_BYTES:
        result["items"].pop()
        result["next_offset"] = offset + len(result["items"])
        result["has_more"] = result["next_offset"] < result["total"]
    return result


def _entries(entries: Sequence[RouteEntry], manager: str) -> list[RouteEntry]:
    return [
        e for e in entries if e.manager_name and e.manager_name.casefold() == manager.casefold()
    ]


def _scope(routes: RouteProfile | None, manager: str, directions: list[str]) -> tuple[dict, list]:
    scope: dict[str, Any] = {
        "manager": manager,
        "version_scope": "manager",
        "directions": directions,
        "plan_count": 0,
        "plans": [],
    }
    rows = []
    if routes is None:
        return scope, rows
    for plan in routes.plans:
        entries = _entries(plan.entries, manager)
        active = [e for e in entries if e.state in ("effective", "conditional")]
        if active:
            scope["plans"].append(
                {
                    "plan": plan.plan_name,
                    "status": plan.status,
                    "versions": sorted({e.key for e in active}),
                }
            )
        for entry in entries:
            for variant in plan.variants or (None,):
                rows.append(
                    {
                        "manager": manager,
                        "plan": plan.plan_name,
                        "variant": (variant.id or variant.raw_id) if variant else None,
                        "format_version": entry.key,
                        "state": entry.state,
                        "route_status": plan.status,
                        "source": f"{entry.source.relative_file}:{entry.source.line_start}",
                    }
                )
    scope["plan_count"] = len(scope["plans"])
    entries = _entries(routes.without_node_entries, manager)
    scope["without_node"] = {
        "status": routes.without_node_status,
        "versions": sorted({e.key for e in entries if e.state in ("effective", "conditional")}),
    }
    rows.extend(
        {
            "manager": manager,
            "plan": None,
            "variant": None,
            "format_version": e.key,
            "state": e.state,
            "route_status": routes.without_node_status,
            "source": f"{e.source.relative_file}:{e.source.line_start}",
        }
        for e in entries
    )
    return scope, rows


def _issue_matches(issue: Issue, level, check_prefix, address_prefix) -> bool:
    return (
        (not level or issue.level.value == level)
        and (not check_prefix or issue.check.startswith(check_prefix))
        and (not address_prefix or address_matches(issue.address, address_prefix))
    )


def _skipped_matches(item: Skipped, check_prefix, address_prefix) -> bool:
    return (not check_prefix or item.check.startswith(check_prefix)) and (
        not address_prefix
        or any(address_matches(a, address_prefix) for a in skipped_addresses(item.reason))
    )


def _counts(issues, skipped) -> dict:
    levels = Counter(i.level.value for i in issues)
    return {
        "errors": levels[Level.ERROR.value],
        "warnings": levels[Level.WARNING.value],
        "skipped": len(skipped),
    }


def build_view(
    bundle: RenderedAuthoring,
    preparations: Sequence[PreparedAuthoring],
    *,
    build_hash: str,
    status: str,
    output_path: str,
    written: bool,
    offset: int,
    limit: int,
    inputs: Sequence[AuthoringInputs] = (),
    section: str = "summary",
    level: str | None = None,
    check_prefix: str | None = None,
    address_prefix: str | None = None,
) -> dict[str, Any]:
    rows: list[dict] = []
    scopes, scope_rows = [], []
    before, after, before_skipped, after_skipped = [], [], [], []
    profiles, new, disappeared, new_skipped = [], [], [], []
    other_states = {}
    changed_rules, added_attributes, unverified = set(), set(), set()

    def other_state(key, state):
        priority = {"compatible": 0, "incompatible": 1, "unverified": 2}
        previous = other_states.get(key, "compatible")
        other_states[key] = max((previous, state), key=priority.__getitem__)

    for number, prepared in enumerate(preparations):
        manager = prepared.generated_hook.source.path.split("/")[-3]
        rules = {rule.entity_id: rule for rule in prepared.projection_before.pko}
        changes = {change.operation_id: change for change in prepared.projection_after.changes}
        for operation in prepared.operations:
            change = changes[operation.operation_id]
            changed_rules.add((manager, change.owner_id))
            if operation.new_attribute:
                owner, _ = metadata_key(rules[change.owner_id].configuration_object.value)
                added_attributes.add((owner, operation.new_attribute.name.casefold()))
        source = inputs[number] if number < len(inputs) else prepared.preparation_inputs
        scope, detail = _scope(
            source.routes if source else None,
            manager,
            sorted({op.target.direction for op in prepared.operations}),
        )
        scopes.append(scope)
        scope_rows.extend(detail)
        for comparison in (*prepared.selected_profiles, *prepared.other_profiles):
            selected = comparison in prepared.selected_profiles
            profile = {
                "manager": manager,
                "format_version": comparison.before.version,
                "direction": comparison.before.direction,
                "selected": selected,
            }
            if section in ("issues_before", "issues_after"):
                report = comparison.before if section == "issues_before" else comparison.after
                rows.extend(
                    {"profile": profile, **i.to_dict()}
                    for i in report.issues
                    if _issue_matches(i, level, check_prefix, address_prefix)
                )
            elif section == "skipped":
                for phase, report in (("before", comparison.before), ("after", comparison.after)):
                    rows.extend(
                        {"profile": profile, "phase": phase, **s.to_dict()}
                        for s in report.skipped
                        if _skipped_matches(s, check_prefix, address_prefix)
                    )
            elif section == "summary":
                for kind, issues in (
                    ("issue_new", comparison.delta.new),
                    ("issue_disappeared", comparison.delta.disappeared),
                ):
                    rows.extend({"kind": kind, "profile": profile, **i.to_dict()} for i in issues)
                rows.extend(
                    {"kind": "skipped_new", "profile": profile, **s.to_dict()}
                    for s in comparison.delta.new_relevant_skipped
                )
            if selected:
                profiles.append(profile)
                before.extend(comparison.before.issues)
                after.extend(comparison.after.issues)
                before_skipped.extend(comparison.before.skipped)
                after_skipped.extend(comparison.after.skipped)
                new.extend(comparison.delta.new)
                disappeared.extend(comparison.delta.disappeared)
                new_skipped.extend(comparison.delta.new_relevant_skipped)
            else:
                other_state(
                    (manager, comparison.before.version, comparison.before.direction),
                    "incompatible" if comparison.delta.new else "compatible",
                )
        for notice in prepared.notices:
            if section == "summary":
                rows.append({"kind": "notice", **asdict(notice), "notice_id": notice.notice_id})
            if notice.id in (
                "ed.author.other_version_unverified",
                "ed.author.other_version_incompatible",
            ):
                direction = next(
                    op.target.direction
                    for op in prepared.operations
                    if op.operation_id == notice.operation_id
                )
                for version in notice.version_keys:
                    other_state(
                        (manager, version, direction),
                        "unverified"
                        if notice.id == "ed.author.other_version_unverified"
                        else "incompatible",
                    )
        unverified.update(s.check for s in prepared.skipped)
        if section == "skipped":
            rows.extend(
                {"manager": manager, "phase": "runtime", **s.to_dict()}
                for s in prepared.skipped
                if _skipped_matches(s, check_prefix, address_prefix)
            )
    if section == "summary":
        # Риски нужны до файлов и необязательных подробностей других профилей.
        rows.sort(key=lambda r: r["kind"] != "notice")
        rows.extend(
            {"kind": "file", "name": path, "size": len(content), "sha256": sha256(content)}
            for path, content in bundle.files.items()
        )
    elif section == "scopes":
        rows = scope_rows
    other = Counter(other_states.values())
    base = {
        "section": section,
        "status": status,
        "build_hash": build_hash,
        "output_dir": output_path,
        "written": written,
        "runtime_verified": bundle.manifest.runtime_verified,
        "runtime_unverified": sorted(unverified),
        "scopes": scopes,
        "change_counts": {
            "pko_changed": len(changed_rules),
            "pks_added": len(bundle.manifest.operations),
            "attributes_added": len(added_attributes),
        },
        "validation": {
            "before": _counts(before, before_skipped),
            "after": _counts(after, after_skipped),
            "delta": {
                "new_errors": sum(i.level == Level.ERROR for i in new),
                "new_warnings": sum(i.level == Level.WARNING for i in new),
                "disappeared_errors": sum(i.level == Level.ERROR for i in disappeared),
                "disappeared_warnings": sum(i.level == Level.WARNING for i in disappeared),
                "new_relevant_skipped": len(new_skipped),
            },
            "result": "no_new_issues",
            "validated_profiles": profiles,
            "other_profiles": {k: other[k] for k in ("compatible", "incompatible", "unverified")},
            "runtime": "not_run",
        },
        "required_acknowledgements": list(bundle.manifest.notices),
    }
    return (
        compact_page(base, rows, offset, limit)
        if section == "summary"
        else base | page(rows, offset, limit)
    )
