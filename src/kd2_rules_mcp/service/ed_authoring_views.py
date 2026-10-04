"""Компактные страницы автора; полнота проверки не зависит от фильтра страницы."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from kd2_rules_mcp.authoring.ed.manifest import sha256
from kd2_rules_mcp.authoring.ed.model import AuthoringInputs, PreparedAuthoring
from kd2_rules_mcp.authoring.ed.render import RenderedAuthoring
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key


def page(rows: Sequence[dict], offset: int, limit: int) -> dict:
    return {
        "items": list(rows[offset : offset + limit]),
        "offset": offset,
        "limit": limit,
        "total": len(rows),
        "has_more": offset + limit < len(rows),
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
) -> dict[str, Any]:
    rows: list[dict] = []
    scopes = []
    before, after = [], []
    before_skipped, after_skipped = [], []
    profiles = []
    other_states = {}
    changed_rules = set()
    added_attributes = set()
    new = []
    skipped = []

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
        scope: dict[str, Any] = {
            "manager": manager,
            "manager_path": prepared.projection_before.files[0].path,
            "version_scope": "manager",
            "operations": len(prepared.operations),
            "directions": sorted({op.target.direction for op in prepared.operations}),
        }
        if number < len(inputs):
            routes = inputs[number].routes

            def relevant(entries, manager_name=manager):
                return [
                    asdict(e)
                    for e in entries
                    if e.manager_name and e.manager_name.casefold() == manager_name.casefold()
                ]

            scope["plans"] = [
                {
                    "plan": plan.plan_name,
                    "status": plan.status,
                    "variants": [asdict(v) for v in plan.variants],
                    "entries": entries,
                }
                for plan in routes.plans
                if (entries := relevant(plan.entries))
            ]
            scope["without_node"] = {
                "status": routes.without_node_status,
                "entries": relevant(routes.without_node_entries),
            }
        scopes.append(scope)
        for comparison in (*prepared.selected_profiles, *prepared.other_profiles):
            selected = comparison in prepared.selected_profiles
            profile = {
                "manager": manager,
                "format_version": comparison.before.version,
                "direction": comparison.before.direction,
                "selected": selected,
            }
            for kind, report in (
                ("issue_before", comparison.before),
                ("issue_after", comparison.after),
            ):
                rows.extend(
                    {"kind": kind, "profile": profile, **i.to_dict()} for i in report.issues
                )
            if selected:
                profiles.append(profile)
                before.extend(comparison.before.issues)
                after.extend(comparison.after.issues)
                before_skipped.extend(comparison.before.skipped)
                after_skipped.extend(comparison.after.skipped)
                new.extend(comparison.delta.new)
                skipped.extend(comparison.delta.new_relevant_skipped)
            else:
                other_state(
                    (manager, comparison.before.version, comparison.before.direction),
                    "incompatible" if comparison.delta.new else "compatible",
                )
        for notice in prepared.notices:
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
        rows.extend(
            {"kind": "skipped", "manager": manager, **s.to_dict()} for s in prepared.skipped
        )
    for path, content in bundle.files.items():
        rows.append({"kind": "file", "name": path, "size": len(content), "sha256": sha256(content)})

    def summary(issues, skipped):
        levels = Counter(i.level.value for i in issues)
        return {
            "errors": levels["error"],
            "warnings": levels["warning"],
            "skipped": len(skipped),
            "by_check": dict(Counter(i.check for i in issues)),
            "text": f"Ошибок: {levels['error']}, предупреждений: {levels['warning']}, "
            f"не проведено: {len(skipped)}",
        }

    operations = bundle.manifest.operations
    other = Counter(other_states.values())
    return {
        "status": status,
        "build_hash": build_hash,
        "output_dir": output_path,
        "written": written,
        "runtime_verified": bundle.manifest.runtime_verified,
        "scopes": scopes,
        "change_counts": {
            "pko_changed": len(changed_rules),
            "pks_added": len(operations),
            "attributes_added": len(added_attributes),
        },
        "validation": {
            "before": summary(before, before_skipped),
            "after": summary(after, after_skipped),
            "delta": {
                "new_errors": sum(i.level.value == "error" for i in new),
                "new_warnings": sum(i.level.value == "warning" for i in new),
                "new_relevant_skipped": len(skipped),
            },
            "result": "no_new_issues",
            "validated_profiles": profiles,
            "other_profiles": {k: other[k] for k in ("compatible", "incompatible", "unverified")},
            "runtime": "not_run",
        },
        "required_acknowledgements": list(bundle.manifest.notices),
        **page(rows, offset, limit),
    }
