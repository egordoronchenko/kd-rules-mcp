"""Полная инструкция §4.2: только решения подготовки, без машинных путей."""

import re
from collections.abc import Iterable
from importlib.resources import files
from string import Template

from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.ed.schema.model import EdSchema

from .hook import bsl_string
from .manifest import Delivery
from .model import FILLER, PreparedAuthoring
from .xml_dump import PROFILE_NAME, DumpMetadata


def table(headers: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> str:
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", "<br>").replace("${", "&#36;{")

    body = ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    if not body:
        return "Нет"
    return "\n".join(
        ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |", *body]
    )


def direction_label(direction: str) -> str:
    return "Отправка" if direction == "send" else "Получение"


def runtime_probes(prepared: PreparedAuthoring, meta: DumpMetadata) -> str:
    """Только вызовы заполнителя и чтение коллекций; ничего не исполняется."""
    version = prepared.projection_before.manager_version
    index = build_addresses(prepared.projection_before)
    after = build_addresses(prepared.projection_after.document)
    blocks = []
    for direction in sorted({op.target.direction for op in prepared.operations}):
        lines = [
            "Правила = ОбменДаннымиXDTOСервер.КоллекцияПравилКонвертации("
            f"{bsl_string(str(version))});"
        ]
        if version == 3:
            lines += [
                "// КомпонентыОбмена берутся из типового пути согласованного обмена.",
                f"// НаправлениеОбмена компонентов: {direction_label(direction)}.",
                f"{meta.module.name}.{FILLER}(КомпонентыОбмена, Правила, Истина);",
                "// Заголовочный проход: новых ПКС нет.",
                f"{meta.module.name}.{FILLER}(КомпонентыОбмена, Правила, Ложь);",
            ]
        else:
            lines.append(
                f"{meta.module.name}.{FILLER}({bsl_string(direction_label(direction))}, Правила);"
            )
        for address in sorted(
            {
                op.target.pko_address
                for op in prepared.operations
                if op.target.direction == direction
            }
        ):
            rule = index.find(address)
            projected = after.find(address)
            assert isinstance(rule, ObjectRule) and isinstance(projected, ObjectRule)
            lines += [
                f"Правило = Правила.Найти({bsl_string(rule.declared_name or rule.name)}, "
                '"ИмяПКО");',
                "Если Правило <> Неопределено Тогда",
                "\tСообщить(Правило.Свойства.Количество()); // В модели: "
                f"{len(rule.properties)} → {len(projected.properties)}.",
                "КонецЕсли;",
            ]
        blocks.append("```bsl\n" + "\n".join(lines) + "\n```")
    return "\n\n".join(blocks)


def render_instruction(
    prepared: PreparedAuthoring, meta: DumpMetadata, delivery: Delivery, paths: tuple[str, ...]
) -> str:
    template = (
        files("kd2_rules_mcp.authoring.ed").joinpath("templates/instruction.md").read_text("utf-8")
    )
    # Две фиксированные ветки, а не интерпретатор языка шаблонов.
    for condition, active in (
        ("delivery != manual", delivery != "manual"),
        ("delivery == manual", delivery == "manual"),
    ):
        template = re.sub(
            r"\[if " + re.escape(condition) + r"\]\n(.*?)\[/if\]",
            lambda m, active=active: m[1] if active else "",
            template,
            flags=re.S,
        )
    inputs = prepared.preparation_inputs
    operations = prepared.operations
    targets = tuple(op.target for op in operations)
    profiles = prepared.selected_profiles
    before = tuple(i for c in profiles for i in c.before.issues)
    after = tuple(i for c in profiles for i in c.after.issues)

    def issues(items):
        return table(
            ("Проверка", "Адрес", "Замечание"), ((i.check, i.address, i.message) for i in items)
        )

    scope_rows = []
    if inputs:
        for plan in inputs.routes.plans:
            for entry in plan.entries:
                if entry.manager_name == meta.module.name:
                    scope_rows.append((plan.plan_name, entry.key, entry.state, plan.status))
        scope_rows.extend(
            ("Без узла", e.key, e.state, inputs.routes.without_node_status)
            for e in inputs.routes.without_node_entries
            if e.manager_name == meta.module.name
        )
    package_rows = []
    if inputs:
        for version in sorted({t.format_version for t in targets}):
            schema = inputs.schemas.get(version)
            if isinstance(schema, EdSchema):
                package_rows.extend(
                    (p.metadata_name or "Пакет формата", p.namespace)
                    for p in schema.packages
                    if p.namespace == schema.base_namespace
                )
    hook_lines = prepared.generated_hook.source.text.splitlines()
    start = next(i for i, line in enumerate(hook_lines) if line.startswith("Процедура ")) + 1
    insertion = (
        "Цель: `"
        + FILLER
        + "`. Вставка в тело существующего перехватчика:\n\n```bsl\n"
        + "\n".join(hook_lines[start:-1]).strip("\n")
        + "\n```"
    )
    skipped = tuple(s for c in profiles for s in c.after.skipped) + prepared.skipped
    substitutions = {
        "extension_name": prepared.identity.name,
        "prefix": prepared.identity.prefix,
        "build_hash": prepared.build_hash,
        "configuration_name": ", ".join(sorted({t.configuration for t in targets})),
        "base_uuid": meta.configuration.uuid,
        "plan": ", ".join(sorted({t.plan for t in targets})),
        "variant_display": ", ".join(sorted({t.variant or "Не выбран" for t in targets})),
        "format_version": ", ".join(sorted({t.format_version for t in targets})),
        "package_name": ", ".join(sorted({p[0] for p in package_rows}))
        or "Не указано в прочитанной схеме",
        "namespace": ", ".join(sorted({p[1] for p in package_rows}))
        or "Не указано в прочитанной схеме",
        "manager_version": str(prepared.projection_before.manager_version),
        "operations_table": table(
            ("Направление", "ПКО", "Реквизит", "Свойство формата", "Операция"),
            (
                (
                    direction_label(op.target.direction),
                    op.target.pko_address,
                    op.configuration_attribute,
                    op.format_property,
                    op.operation_id,
                )
                for op in operations
            ),
        ),
        "scope_table": table(("План", "Версия", "Маршрут", "Чтение карты"), scope_rows),
        "before_summary": f"Замечаний: {len(before)}",
        "issues_before_table": issues(before),
        "after_summary": f"Замечаний: {len(after)}",
        "issues_after_table": issues(after),
        "new_issue_count": str(sum(len(c.delta.new) for c in profiles)),
        "validated_profiles": ", ".join(
            c.before.version + "/" + direction_label(c.before.direction) for c in profiles
        ),
        "other_profiles_table": table(
            ("Версия", "Направление", "До", "После", "Новых"),
            (
                (
                    c.before.version,
                    direction_label(c.before.direction),
                    len(c.before.issues),
                    len(c.after.issues),
                    len(c.delta.new),
                )
                for c in prepared.other_profiles
            ),
        ),
        "notices_table": table(
            ("Идентификатор", "Адрес", "Риск"),
            ((n.notice_id, n.address, n.message) for n in prepared.notices),
        ),
        "skipped_table": table(
            ("Невыполненная проверка", "Причина"), ((s.check, s.reason) for s in skipped)
        ),
        "files_table": table(("Файл",), ((p,) for p in sorted(paths))),
        "compatibility_mode": meta.compatibility_mode,
        "dump_profile": PROFILE_NAME,
        "new_attributes_table": table(
            ("Владелец", "Реквизит", "Тип", "Квалификаторы", "Живой обмен"),
            (
                (
                    owner,
                    d.name,
                    d.primitive,
                    ", ".join(f"{k}={v}" for k, v in sorted(d.qualifiers.items())) or "Нет",
                    "Проверено" if prepared.runtime_verified else "Не проверено",
                )
                for owner, drafts in meta.attributes.items()
                for d in drafts
            ),
        ),
        "extension_folder": "extension",
        "language": meta.language.name,
        "manager_name": meta.module.name,
        "manual_insertions": insertion,
        "runtime_probes": runtime_probes(prepared, meta),
        "runtime_status": "Проверено живым обменом"
        if prepared.runtime_verified
        else "Не проверено живым обменом",
        "runtime_verified": str(prepared.runtime_verified).lower(),
        "unverified_table": table(("Что не проверено",), ((s.reason,) for s in prepared.skipped)),
    }
    result = Template(template).substitute(substitutions)
    # Имена проектов и исходные абсолютные адреса не являются подстановками инструкции.
    for project in sorted({t.project for t in targets}, key=len, reverse=True):
        if project:
            result = result.replace(project, "[проект]")
    result = re.sub(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s|`<>]+", "[исходный файл]", result)
    return result.rstrip() + "\n"
