"""Разделы автора: компактная сводка и подробности только по явному запросу."""

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from kd_rules_mcp.authoring.ed.handler_render import procedure_block
from kd_rules_mcp.authoring.ed.handlers import HandlerOperationsPlan, operation_kind
from kd_rules_mcp.authoring.ed.manifest import ArtifactManifest, sha256
from kd_rules_mcp.authoring.ed.model import (
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    AuthoringInputs,
    PreparedAuthoring,
    PreserveMissingHeaderProperty,
    SetObjectHandler,
    operation_dependencies,
    order_operations,
)
from kd_rules_mcp.authoring.ed.render import RenderedAuthoring
from kd_rules_mcp.ed.lexer import tokenize
from kd_rules_mcp.ed.route_model import RouteEntry, RouteProfile
from kd_rules_mcp.service.ed_views import address_matches, skipped_addresses
from kd_rules_mcp.validation.ed_authoring_handlers import preset_procedure_text
from kd_rules_mcp.validation.ed_structure_snapshot import metadata_key
from kd_rules_mcp.validation.report import Issue, Level, Skipped

SECTIONS = ("summary", "issues_before", "issues_after", "scopes", "skipped", "operations")
PAGE_BYTES = 4096
# Страница notices сборки менеджера. 50 типовых предупреждений required_unfilled
# занимают около 52 КБ JSON с отступами; 64 КБ вмещает умолчание 50. Остальные
# разделы остаются на пороге 4 КБ.
MANAGER_NOTICES_BYTES = 65536


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


def compact_page(
    base: dict, rows: Sequence[dict], offset: int, limit: int, *, budget: int = PAGE_BYTES
) -> dict:
    """Лимит — верхняя граница числа записей; продолжение всегда по next_offset.

    Неделимую запись и обязательную сводку не обрезаем. Даже при необычно длинной
    записи страница продвигается; порог 4 КБ закреплён тестами для рабочего корпуса.
    Раздел notices сборки менеджера передаёт больший бюджет.
    """
    result = page(rows, offset, limit)
    while len(result["items"]) > 1 and json_size(result) > budget:
        result["items"].pop()
        result["next_offset"] = offset + len(result["items"])
        result["has_more"] = result["next_offset"] < result["total"]
        result["truncated_by"] = "size"
    return base | result


def artifact_changes(
    previous: ArtifactManifest | None,
    current: ArtifactManifest,
    previous_files: Mapping[str, bytes],
    current_files: Mapping[str, bytes],
) -> list[dict]:
    """Дельта решений и привязок; тексты процедур в просмотр не попадают."""
    if previous is None:
        return []
    changes = []
    if previous.schema_version != current.schema_version:
        changes.append(
            {
                "kind": "schema_version",
                "from": previous.schema_version,
                "to": current.schema_version,
            }
        )
    old = {op.operation_id: op for op in (*previous.operations, *previous.handler_operations)}
    new = {op.operation_id: op for op in (*current.operations, *current.handler_operations)}
    for kind, left, right in (("operation_removed", old, new), ("operation_added", new, old)):
        changes.extend(
            {
                "kind": kind,
                "operation_id": ident,
                "operation_kind": operation_kind(left[ident]),
                "target": asdict(left[ident].target),
            }
            for ident in sorted(left.keys() - right.keys())
        )

    def binding_key(record):
        return (
            record.get("module", ""),
            record["target"]["direction"],
            record["target"]["pko_address"],
            record["event"],
            record["handler_name"],
        )

    old_bindings = {binding_key(b): b for b in previous.handler_bindings}
    new_bindings = {binding_key(b): b for b in current.handler_bindings}
    for kind, left, right in (
        ("binding_removed", old_bindings, new_bindings),
        ("binding_added", new_bindings, old_bindings),
    ):
        changes.extend(
            {
                "kind": kind,
                "pko_address": key[2],
                "event": key[3],
                "handler_name": key[4],
                "direction": key[1],
                "module": key[0],
            }
            for key in sorted(left.keys() - right.keys())
        )

    def procedures(files):
        result = {}
        for path, content in files.items():
            if not path.startswith("modules/") or not path.endswith("Module.bsl"):
                continue
            text = content.decode("utf-8")
            tokens = tokenize(text)
            for number, token in enumerate(tokens[:-1]):
                if token.kind == "identifier" and token.folded == "процедура":
                    name = tokens[number + 1].value
                    result[(path.removeprefix("modules/"), name)] = procedure_block(text, name)
        return result

    old_procedures, new_procedures = procedures(previous_files), procedures(current_files)
    for key in sorted(old_procedures.keys() | new_procedures.keys()):
        if old_procedures.get(key) == new_procedures.get(key):
            continue
        kind = (
            "procedure_added"
            if key not in old_procedures
            else "procedure_removed"
            if key not in new_procedures
            else "procedure_changed"
        )
        changes.append({"kind": kind, "name": key[1], "module": key[0]})
    return changes


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
    rebuild: bool = False,
    changed_inputs: Mapping[str, Sequence[str]] | None = None,
    handler_plans: Sequence[HandlerOperationsPlan] = (),
    migration: bool = False,
    previous: ArtifactManifest | None = None,
    previous_files: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    rows: list[dict] = []
    scopes, scope_rows = [], []
    before, after, before_skipped, after_skipped = [], [], [], []
    profiles, new, disappeared, new_skipped = [], [], [], []
    other_states = {}
    other_skipped = Counter()
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
            if not handler_plans:
                changed_rules.add((manager, change.owner_id))
            if operation.new_attribute:
                owner, _ = metadata_key(rules[change.owner_id].configuration_object.value)
                added_attributes.add((owner, operation.new_attribute.name.casefold()))
        source = inputs[number] if number < len(inputs) else prepared.preparation_inputs
        operations = handler_plans[number].operations if handler_plans else prepared.operations
        changed_rules.update((manager, op.target.pko_address) for op in operations if handler_plans)
        scope, detail = _scope(
            source.routes if source else None,
            manager,
            sorted({op.target.direction for op in operations}),
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
                if selected:
                    rows.extend(
                        {"kind": "skipped_new", "profile": profile, **s.to_dict()}
                        for s in comparison.delta.new_relevant_skipped
                    )
                else:
                    other_skipped.update(
                        (s.check, s.reason.partition("; ")[0].rpartition(": ")[0] or s.reason)
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
                    for op in operations
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
        rows.sort(
            key=lambda r: (r["kind"] != "notice", r.get("notice_id") not in bundle.manifest.notices)
        )
        rows.extend(
            {"kind": "skipped_new_count", "check": check, "reason": reason, "count": count}
            for (check, reason), count in sorted(other_skipped.items())
        )
        rows.extend(
            {"kind": "file", "name": path, "size": len(content), "sha256": sha256(content)}
            for path, content in bundle.files.items()
        )
        rows.extend(
            {"kind": "input_changed", "name": name}
            for name in sorted({n for names in (changed_inputs or {}).values() for n in names})
        )
    elif section == "scopes":
        rows = scope_rows
    elif section == "operations":
        rows = operation_rows(bundle, handler_plans)
    other = Counter(other_states.values())
    base = {
        "section": section,
        "status": status,
        "build_hash": build_hash,
        "output_dir": output_path,
        "written": written,
        "rebuild": rebuild,
        "changed_input_groups": sorted(changed_inputs or {}),
        "runtime_verified": bundle.manifest.runtime_verified,
        "runtime_unverified": sorted(unverified),
        "scopes": scopes,
        "change_counts": {
            "pko_changed": len(changed_rules),
            "pks_added": len(bundle.manifest.operations)
            + sum(
                isinstance(op, AddAlgorithmicHeaderProperty)
                for op in bundle.manifest.handler_operations
            ),
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
        "changes": artifact_changes(previous, bundle.manifest, previous_files or {}, bundle.files),
    }
    if section == "summary":
        base["acknowledgement_notices"] = [
            r for r in rows if r["kind"] == "notice" and r["notice_id"] in bundle.manifest.notices
        ]
    if handler_plans:
        report = json.loads(bundle.files["validation.json"])
        reports = report.get("managers", [report])
        layer_rows = [r["layer"] for r in reports]
        base["summary"] = {
            "handlers": sum(len(p.bindings) for p in handler_plans),
            "presets": sum(
                isinstance(op, PreserveMissingHeaderProperty)
                for p in handler_plans
                for op in p.operations
            ),
            "algorithmic_properties": sum(
                isinstance(op, AddAlgorithmicHeaderProperty)
                for p in handler_plans
                for op in p.operations
            ),
            "runtime_verified": False,
            "bindings": {
                "runtime_verified": 0,
                "runtime_unverified": sum(len(p.bindings) for p in handler_plans),
            },
            "body_unparsed": sum(r["body_refs"]["unparsed"] for r in reports),
            "layer": {
                "certain": all(r["certain"] for r in layer_rows),
                **{
                    k: sum(r[k] for r in layer_rows)
                    for k in ("unknown_lines", "unresolved_dispatch", "new_issues")
                },
            },
            "template_evidence": {
                "verified_bindings": sum(
                    b.runtime_verified for p in handler_plans for b in p.bindings
                )
            },
            "schema_version": 2,
            "migration": {"from": 1, "to": 2} if migration else None,
        }
    return compact_page(base, rows, offset, limit)


def operation_rows(bundle: RenderedAuthoring, plans: Sequence[HandlerOperationsPlan]) -> list[dict]:
    """Тела доступны только здесь; preset показывает тело всей общей привязки."""
    operations = order_operations(
        (*bundle.manifest.operations, *bundle.manifest.handler_operations)
    )
    by_id = {op.operation_id: op for op in operations}
    bindings = {ident: b for p in plans for b in p.bindings for ident in b.operation_ids}
    rows = []
    for op in operations:
        row = {
            "operation_id": op.operation_id,
            "kind": operation_kind(op),
            "target": asdict(op.target),
            "dependencies": list(operation_dependencies(op)),
        }
        if isinstance(op, (AddHeaderProperty, AddAlgorithmicHeaderProperty)):
            row.update(
                configuration_attribute=op.configuration_attribute,
                format_property=op.format_property,
            )
        if isinstance(op, AddAlgorithmicHeaderProperty):
            row["conversion_rule"] = op.conversion_rule
        elif isinstance(op, AddHeaderProperty) and op.new_attribute:
            draft = op.new_attribute
            row["new_attribute"] = {
                "name": draft.name,
                "synonym": draft.synonym,
                "primitive": draft.primitive,
                "qualifiers": dict(draft.qualifiers),
            }
        binding = bindings.get(op.operation_id)
        if isinstance(op, AddAlgorithmicHeaderProperty):
            binding = bindings.get(op.handler_operation_id)
        if binding:
            row.update(
                event=binding.event, handler_name=binding.handler_name, runtime_verified=False
            )
        if isinstance(op, SetObjectHandler):
            body = op.body
            row.update(body=body, body_sha256=sha256(body.encode("utf-8")), body_origin="agent")
        elif isinstance(op, PreserveMissingHeaderProperty) and binding:
            pairs = []
            for ident in binding.operation_ids:
                preset = by_id[ident]
                assert isinstance(preset, PreserveMissingHeaderProperty)
                prop = by_id[preset.property_operation_id]
                assert isinstance(prop, AddHeaderProperty)
                pairs.append((prop.format_property, prop.configuration_attribute))
            procedure = preset_procedure_text(binding.handler_name, tuple(pairs))
            body = procedure[procedure.index("\n") + 1 : procedure.rindex("КонецПроцедуры")]
            row.update(body=body, body_sha256=sha256(body.encode("utf-8")), body_origin="preset")
        rows.append(row)
    return rows
