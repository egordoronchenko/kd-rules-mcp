"""Полная инструкция §4.2: только решения подготовки, без машинных путей."""

import re
from collections.abc import Iterable, Mapping
from importlib.resources import files
from string import Template

from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.ed.schema.model import EdSchema
from kd2_rules_mcp.ed.writer_model import ManagerModel
from kd2_rules_mcp.validation.ed_required import RequiredUnfilled

from .handlers import HandlerBindingPlan, HandlerOperationsPlan, operation_kind
from .hook import bsl_string, valid_identifier
from .manifest import Delivery, overlay_report_view
from .model import (
    FILLER,
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    Operation,
    PreparedAuthoring,
    PreserveMissingHeaderProperty,
    SetObjectHandler,
)
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


def render_data_preflight(risks: Iterable[RequiredUnfilled]) -> str:
    """Запросы только отрисовываются; подтверждение не означает проверку данных базы."""
    groups: dict[tuple[str, str, str, tuple[str, ...]], list[RequiredUnfilled]] = {}
    for risk in risks:
        groups.setdefault(
            (risk.object_kind, risk.object_name, risk.property_path, risk.source_types), []
        ).append(risk)
    if not groups:
        return ""
    blocks = []
    for (kind, name, path, types), items in sorted(groups.items()):
        addresses = ", ".join(sorted({r.address for r in items}))
        blocks.append(f"### {kind}.{name}.{path}\n\nПКС: {addresses}.")
        if any(r.reference_key for r in items):
            blocks.append(
                "Ключ ссылки: пустое значение остановит также выгрузку документов, "
                "ссылающихся на объект."
            )
        if not all(valid_identifier(part) for part in (kind, name, *path.split("."))):
            blocks.append(
                "Путь нельзя безопасно подставить в запрос: проверьте его вручную в конфигураторе."
            )
            continue
        parent, _, attribute = path.rpartition(".")
        source = f"{kind}.{name}" + (f".{parent}" if parent else "")
        field_name = "Объект." + (attribute if parent else path)
        empty, parameters = [], []
        for type_name in types:
            if "Ссылка." in type_name:
                ref_kind, _, ref_name = type_name.partition("Ссылка.")
                if not all(valid_identifier(p) for p in (ref_kind, ref_name)):
                    continue
                if ref_kind == "Перечисление":
                    parameter = "Пусто" if len(types) == 1 else f"Пусто{len(parameters) + 1}"
                    parameters.append(f"`&{parameter}` = `Перечисления.{ref_name}.ПустаяСсылка()`.")
                    empty.append(f"{field_name} = &{parameter}")
                else:
                    empty.append(f"{field_name} = ЗНАЧЕНИЕ({ref_kind}.{ref_name}.ПустаяСсылка)")
            elif type_name in {"Строка", "Число", "Дата"}:
                parameter = "Пусто" if len(types) == 1 else f"Пусто{len(parameters) + 1}"
                value = {"Строка": '""', "Число": "0", "Дата": "Дата(1, 1, 1)"}[type_name]
                parameters.append(f"`&{parameter}` = `{value}` ({type_name}).")
                empty.append(f"{field_name} = &{parameter}")
        if len(types) > 1:
            parameters.append("`&Неопределено` = `Неопределено` (пустое значение составного типа).")
            empty.append(f"{field_name} = &Неопределено")
        if not empty:
            blocks.append("Тип пустого значения не разрешён: проверьте данные вручную.")
            continue
        deleted = "Объект.Ссылка.ПометкаУдаления" if parent else "Объект.ПометкаУдаления"
        prefix = (
            f"НЕ {deleted} И "
            if kind
            in {
                "Справочник",
                "Документ",
                "ПланСчетов",
                "ПланВидовХарактеристик",
                "ПланВидовРасчета",
                "ПланОбмена",
                "БизнесПроцесс",
                "Задача",
            }
            else ""
        )
        query = (
            "ВЫБРАТЬ КОЛИЧЕСТВО(*) КАК Количество\n"
            f"ИЗ {source} КАК Объект\nГДЕ {prefix}(" + " ИЛИ ".join(empty) + ")"
        )
        blocks.append("```sql\n" + query + "\n```")
        if parameters:
            blocks.append("Параметры запроса: " + " ".join(parameters))
        blocks.append(
            "Если количество больше нуля — заполните реквизит у найденных объектов "
            "до первого обмена и повторите запрос."
        )
    template = (
        files("kd2_rules_mcp.authoring.ed")
        .joinpath("templates/manager_data_preflight.md")
        .read_text("utf-8")
    )
    return "\n\n" + Template(template).substitute(checks="\n\n".join(blocks))


def manager_instruction_substitutions(
    model: "ManagerModel",
    *,
    extension_name: str,
    module_name: str,
    plan_name: str,
    version_key: str,
    build_hash: str,
    paths: tuple[str, ...],
    form_evidence: Mapping[str, bool] | None = None,
    compatibility_mode: str = "",
    interface_compatibility_mode: str = "",
) -> dict[str, str]:
    """Данные русского шаблона; команды только отрисовываются и не исполняются.

    Каталоги и строку соединения подставляет администратор. Каждый пакетный шаг
    сохраняет отдельные журнал и результат в уже существующий каталог комплекта.
    """
    counts = []
    names = []
    for direction in ("send", "receive"):
        pko = [r for r in model.pko if direction in r.directions or "both" in r.directions]
        pod = [r for r in model.pod if direction in r.directions or "both" in r.directions]
        pks = sum(len(r.properties) + sum(len(g.properties) for g in r.groups) for r in pko)
        counts.append(
            (
                direction_label(direction),
                len(pko),
                len(pod),
                pks,
                pks + (len(pko) if direction == "send" else 0),
            )
        )
        names.append(
            (
                direction_label(direction),
                ", ".join(r.name for r in pko) or "Нет",
                ", ".join(r.name for r in pod) or "Нет",
            )
        )
    evidence = dict(form_evidence or {"Интерфейс 2: собственный модуль и маршрут с узлом": False})
    result = dict(
        extension_name=extension_name,
        module_name=module_name,
        module_name_literal=bsl_string(module_name),
        plan_name=plan_name,
        version_key=version_key,
        version_literal=bsl_string(version_key),
        build_hash=build_hash,
        extension_version=build_hash[:12],
        compatibility_mode=compatibility_mode,
        interface_compatibility_mode=interface_compatibility_mode,
        counts_table=table(
            ("Направление", "ПКО", "ПОД", "ПКС в модели", "ПКС после инициализации"), counts
        ),
        names_table=table(("Направление", "Имена ПКО", "Имена ПОД"), names),
        evidence_table=table(
            ("Форма", "Проверена обменом на стенде"),
            ((name, "Да" if proven else "Нет") for name, proven in sorted(evidence.items())),
        ),
        version_probe=extension_version_probe(extension_name, build_hash[:12]),
        files_table=table(("Файл",), ((path,) for path in sorted(paths))),
    )
    target = 'DESIGNER /IBConnectionString "<строка соединения>"'
    build = 'DESIGNER /F "<каталог пустой базы>"'
    load = '/LoadConfigFromFiles "<каталог комплекта>/extension"'
    steps = (
        ("create_infobase", 'CREATEINFOBASE File="<каталог пустой базы>"', ""),
        ("load_extension", target, load + " -Format Hierarchical"),
        ("check_extension", target, "/CheckCanApplyConfigurationExtensions"),
        (
            "check_modules",
            target,
            "/CheckModules -Server -ExternalConnection -ThickClientOrdinaryApplication",
        ),
        ("update_extension", target, "/UpdateDBCfg"),
        ("load_build_extension", build, load + " -Format Hierarchical"),
        ("dump_extension", build, '/DumpCfg "<каталог комплекта>/' + extension_name + '.cfe"'),
    )
    for key, connection, action in steps:
        command = "1cv8 " + connection
        if action:
            command += " " + action + ' -Extension "' + extension_name + '"'
        result["command_" + key] = (
            command
            + ' /DisableStartupDialogs /Out "<каталог комплекта>/'
            + key
            + '.log"'
            + ' /DumpResult "<каталог комплекта>/'
            + key
            + '.result"'
        )
    return result


def render_manager_instruction(
    model: "ManagerModel",
    *,
    extension_name: str,
    module_name: str,
    plan_name: str,
    version_key: str,
    build_hash: str,
    paths: tuple[str, ...],
    form_evidence: Mapping[str, bool] | None = None,
    compatibility_mode: str = "",
    interface_compatibility_mode: str = "",
    plan_content_additions: tuple[str, ...] = (),
    registration_subscriptions: tuple[str, ...] = (),
    registration_objects: tuple[str, ...] = (),
    subscription_objects: tuple[str, ...] = (),
) -> str:
    """Инструкция доставки W1 по (г) пилота; числа деклараций берутся из модели."""
    template = (
        files("kd2_rules_mcp.authoring.ed")
        .joinpath("templates/manager_instruction.md")
        .read_text("utf-8")
    )
    values = manager_instruction_substitutions(
        model,
        extension_name=extension_name,
        module_name=module_name,
        plan_name=plan_name,
        version_key=version_key,
        build_hash=build_hash,
        paths=paths,
        form_evidence=form_evidence,
        compatibility_mode=compatibility_mode,
        interface_compatibility_mode=interface_compatibility_mode,
    )
    result = Template(template).substitute(values).rstrip() + "\n"
    if plan_content_additions:
        result += (
            "\n## Состав плана дополнен\n\n"
            + "\n".join("- `" + item + "`" for item in plan_content_additions)
            + "\n\nПосле установки откройте в конфигураторе состав плана `"
            + plan_name
            + "`: каждый объект должен быть виден с признаком расширения. "
            "Авторегистрация — «Запретить»; регистрацию ведут ПРО. "
            "Проверьте действующие ПРО и регистрацию изменений этих объектов.\n"
        )
    if registration_subscriptions:
        result += (
            "\n## Источники подписок регистрации дополнены\n\n"
            + "\n".join("- `" + item + "`" for item in registration_subscriptions)
            + "\n\nПосле установки проверьте источники типовых подписок в конфигураторе, "
            "сохранение и удаление объектов, выполнение ПРО и регистрацию на нужных узлах.\n"
        )
        if subscription_objects:
            result += (
                "\nРади источников подписок заимствованы объекты штатного состава плана "
                "(состав плана не меняется): "
                + ", ".join("`" + item + "`" for item in subscription_objects)
                + ".\n"
            )
    if registration_objects:
        result += (
            "\n## Объекты только для регистрации\n\n"
            + "\n".join("- `" + item + "`" for item in registration_objects)
            + "\n\nПКО для этих объектов нет; настройте ПРО, регистрирующие владельца.\n"
        )
    return result


def runtime_probes(prepared: PreparedAuthoring, meta: DumpMetadata) -> str:
    """Только вызовы заполнителя и чтение коллекций; ничего не исполняется."""
    version = prepared.projection_before.manager_version
    index = build_addresses(prepared.projection_before)
    after = build_addresses(prepared.projection_after.document)
    blocks = []
    for direction in ("send", "receive"):
        lines = [
            "Правила = ОбменДаннымиXDTOСервер.КоллекцияПравилКонвертации("
            f"{bsl_string(str(version))});"
        ]
        if version == 3:
            lines += [
                "// КомпонентыОбмена берутся из типового пути согласованного обмена.",
                f"// Сформируйте компоненты для направления {direction_label(direction)}.",
                f"{meta.module.name}.{FILLER}(КомпонентыОбмена, Правила, Истина);",
                "// Заголовочный проход: новых ПКС нет.",
                f"{meta.module.name}.{FILLER}(КомпонентыОбмена, Правила, Ложь);",
            ]
        else:
            lines.append(
                f"{meta.module.name}.{FILLER}({bsl_string(direction_label(direction))}, Правила);"
            )
        for address in sorted({op.target.pko_address for op in prepared.operations}):
            rule = index.find(address)
            projected = after.find(address)
            assert isinstance(rule, ObjectRule) and isinstance(projected, ObjectRule)
            missing = direction_label(direction) + ": ПКО " + rule.name + " отсутствует"
            changed = any(
                op.target.pko_address == address and op.target.direction == direction
                for op in prepared.operations
            )
            expected = len(projected.properties) if changed else len(rule.properties)
            lines += [
                f"Правило = Правила.Найти({bsl_string(rule.declared_name or rule.name)}, "
                '"ИмяПКО");',
                "Если Правило = Неопределено Тогда",
                f"\tСообщить({bsl_string(missing)});",
                "Иначе",
                "\tСообщить(Правило.Свойства.Количество()); // В модели: "
                f"{len(rule.properties)} → {expected}.",
            ]
            for op in prepared.operations:
                if op.target.pko_address != address:
                    continue
                lines += [
                    "\tСовпаденийПары = 0;",
                    "\tДля Каждого ПКС Из Правило.Свойства Цикл",
                    f"\t\tЕсли ПКС.СвойствоФормата = {bsl_string(op.format_property)} Тогда",
                    "\t\t\tСообщить(Строка(ПКС.СвойствоКонфигурации) + "
                    '" ↔ " + Строка(ПКС.СвойствоФормата));',
                    "\t\t\tЕсли ПКС.СвойствоКонфигурации = "
                    f"{bsl_string(op.configuration_attribute)} Тогда",
                    "\t\t\t\tСовпаденийПары = СовпаденийПары + 1;",
                    "\t\t\tКонецЕсли;",
                    "\t\tКонецЕсли;",
                    "\tКонецЦикла;",
                    '\tСообщить("Совпадений нужной пары: " + Строка(СовпаденийПары));',
                ]
            lines.append("КонецЕсли;")
        for attribute in sorted({op.configuration_attribute for op in prepared.operations}):
            lines += [
                "// Проверяем реквизит по всей таблице, даже если целевого ПКО нет.",
                "Для Каждого ПКО Из Правила Цикл",
                "\tДля Каждого ПКС Из ПКО.Свойства Цикл",
                f"\t\tЕсли ПКС.СвойствоКонфигурации = {bsl_string(attribute)} Тогда",
                f"\t\t\tСообщить({bsl_string(direction_label(direction) + ': ')} + ПКО.ИмяПКО + "
                '": " + Строка(ПКС.СвойствоКонфигурации) + " ↔ " + Строка(ПКС.СвойствоФормата));',
                "\t\tКонецЕсли;",
                "\tКонецЦикла;",
                "КонецЦикла;",
            ]
        blocks.append("```bsl\n" + "\n".join(lines) + "\n```")
    return "\n\n".join(blocks)


def render_instruction(
    prepared: PreparedAuthoring, meta: DumpMetadata, delivery: Delivery, paths: tuple[str, ...]
) -> str:
    prepared = overlay_report_view(prepared)
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
                    scope_rows.append(
                        (
                            plan.plan_name,
                            entry.key,
                            entry.state,
                            _route_explanation(entry.state),
                            plan.status,
                        )
                    )
        scope_rows.extend(
            (
                "Без узла",
                e.key,
                e.state,
                _route_explanation(e.state),
                inputs.routes.without_node_status,
            )
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
        "scope_table": table(
            ("План", "Версия", "Маршрут", "Пояснение", "Чтение карты"), scope_rows
        ),
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
        "direction_risks": _direction_risks(prepared),
        "header_pass_note": (
            "У интерфейса 3 проход ТолькоЗаголовки не добавляет ПКС, "
            "полный проход добавляет указанные ПКС."
        )
        if prepared.projection_before.manager_version == 3
        else "",
        "empty_value_probe": (
            "Проверьте очистку значения в источнике и сообщение от источника "
            "без доработки: для приёмника отсутствие свойства "
            "в обоих случаях неразличимо."
        )
        if any(op.target.direction == "receive" for op in operations)
        else (
            "Проверьте пустой реквизит источника: свойство и группа общих свойств "
            "могут отсутствовать в XML."
        ),
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
            # URI не является именем проекта или путём файловой системы.
            result = re.sub(
                r"https?://[^\s|`<>]+|" + re.escape(project),
                lambda m: m[0] if m[0].startswith(("http://", "https://")) else "[проект]",
                result,
            )
    if inputs:
        paths_to_redact = [f.path for f in inputs.document.files] + [inputs.routes.root]
        paths_to_redact += [
            s.path
            for schema in inputs.schemas.values()
            if isinstance(schema, EdSchema)
            for package in schema.packages
            for s in package.sources
        ]
        for path in sorted(paths_to_redact, key=len, reverse=True):
            if path.startswith(("/", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", path):
                result = result.replace(path, "[исходный файл]").replace(
                    path.replace("\\", "/"), "[исходный файл]"
                )
    result = re.sub(r"(?<![\w:/])(?:[A-Za-z]:[\\/]|\\\\)[^\s|`<>]+", "[исходный файл]", result)
    return re.sub(r"\n{3,}", "\n\n", result).rstrip() + "\n"


def render_handlers_instruction(
    plan: HandlerOperationsPlan,
    *,
    extension_name: str,
    prefix: str,
    build_hash: str,
    interface: int,
    delivery: Delivery,
    paths: tuple[str, ...],
    projects: tuple[str, ...] = (),
    form_evidence: Mapping[str, bool] | None = None,
    pko_names: Mapping[str, str] | None = None,
    extension_version: str = "",
) -> str:
    """Инструкция §7. Подстановки только для применимых событий и preset.

    ``form_evidence`` — проверена ли живым обменом форма привязки (по имени процедуры): это
    свидетельство стенда, а не проверка данного комплекта в базе. Без него берётся признак
    привязки.
    """
    template = (
        files("kd2_rules_mcp.authoring.ed")
        .joinpath("templates/handlers_instruction.md")
        .read_text("utf-8")
    )
    has_preset = any(isinstance(op, PreserveMissingHeaderProperty) for op in plan.operations)
    has_missing_section = has_preset or any(
        isinstance(op, AddHeaderProperty) and op.target.direction == "receive"
        for op in plan.operations
    )
    has_receive = any(op.target.direction == "receive" for op in plan.operations)
    for condition, active in (
        ("has_missing_section", has_missing_section),
        ("has_preset", has_preset),
        ("no_preset", has_missing_section and not has_preset),
        ("has_receive", has_receive),
        ("extension", delivery == "extension"),
        ("manual", delivery != "extension"),
    ):
        template = re.sub(
            r"\[if " + re.escape(condition) + r"\]\n(.*?)\[/if\]\n",
            lambda match, active=active: match[1] if active else "",
            template,
            flags=re.S,
        )
    names = _pko_labels(plan.operations, plan.bindings)
    if pko_names is not None:
        names.update(pko_names)
    paragraphs = []
    for binding in _bindings_in_order(plan):
        label = names.get(binding.target.pko_address, binding.target.pko_address)
        text = (
            f"Для правила {label}, направление {direction_label(binding.target.direction)}, "
            f"добавлен обработчик события {binding.event}. "
            f"В правиле назначено имя {binding.handler_name}."
        )
        if binding.previous_name:
            text += (
                f" Сначала вызывается типовой обработчик {binding.previous_name}, "
                "затем код доработки."
            )
        paragraphs.append(text)
    preserves = []
    by_id = {op.operation_id: op for op in plan.operations}
    for op in plan.operations:
        if not isinstance(op, PreserveMissingHeaderProperty):
            continue
        prop = by_id.get(op.property_operation_id)
        if not isinstance(prop, AddHeaderProperty):
            continue
        preserves.append(
            f"Если в сообщении нет свойства {prop.format_property}, обработчик возвращает "
            f"реквизиту {prop.configuration_attribute} найденного объекта прежнее значение: "
            "без обработчика "
            "обычная загрузка реквизит очищает. У нового объекта реквизит остаётся с начальным "
            "значением. Отправитель пустое значение в сообщение не пишет, поэтому «очистили "
            "в источнике» до приёмника не доходит — значение сохранится. Если же в сообщении "
            "свойство передано явно пустым элементом, реквизит очищается. Подписки и обработчики "
            "конфигурации при записи объекта могут изменить реквизит независимо от этого правила."
        )
    evidence = {
        binding.handler_name: binding.runtime_verified
        if form_evidence is None
        else form_evidence.get(binding.handler_name, False)
        for binding in plan.bindings
    }
    unverified = tuple(name for name, proven in evidence.items() if not proven)
    if unverified:
        status = (
            "Формы обработчиков, которые ещё не проверялись живым обменом на стенде, "
            "перечислены ниже."
        )
    else:
        status = "Формы всех обработчиков комплекта проверены живым обменом на стенде."
    header_note = (
        "У интерфейса 3 проход ТолькоЗаголовки не меняет правила; "
        "полный проход выполняет перечисленные операции."
        if interface == 3
        else ""
    )
    result = Template(template).substitute(
        {
            "extension_name": extension_name,
            "prefix": prefix,
            "build_hash": build_hash,
            "manager_version": str(interface),
            "handler_paragraphs": "\n\n".join(paragraphs) or "Нет",
            "operations_table": table(
                ("Вид", "Направление", "ПКО", "Событие или свойство", "Операция"),
                tuple(_operation_row(op, names) for op in plan.operations),
            ),
            "bindings_table": table(
                ("ПКО", "Событие", "Процедура", "Прежний обработчик", "Форма проверена обменом"),
                tuple(
                    (
                        names.get(binding.target.pko_address, binding.target.pko_address),
                        binding.event,
                        binding.handler_name,
                        binding.previous_name or "Нет",
                        "Да" if evidence[binding.handler_name] else "Нет",
                    )
                    for binding in _bindings_in_order(plan)
                ),
            ),
            "preserve_paragraphs": "\n\n".join(preserves),
            "runtime_verified": str(plan.runtime_verified).lower(),
            "runtime_status": status,
            "runtime_probes": handler_runtime_probes(plan, names, paths),
            "version_probe": extension_version_probe(extension_name, extension_version),
            "extension_version": extension_version,
            "unverified_table": table(
                ("Форма не проверена обменом",), ((name,) for name in unverified)
            )
            if unverified
            else "",
            "header_pass_note": header_note,
            "files_table": table(("Файл",), ((path,) for path in sorted(paths))),
            "delivery": "готовая выгрузка" if delivery == "extension" else "ручное внесение",
        }
    )
    for project in sorted(
        {*projects, *(op.target.project for op in plan.operations)}, key=len, reverse=True
    ):
        if project:
            result = re.sub(
                r"https?://[^\s|`<>]+|" + re.escape(project),
                lambda match: (
                    match[0] if match[0].startswith(("http://", "https://")) else "[проект]"
                ),
                result,
            )
    return re.sub(r"\n{3,}", "\n\n", result).rstrip() + "\n"


def handler_runtime_probes(
    plan: HandlerOperationsPlan, names: Mapping[str, str], paths: tuple[str, ...]
) -> str:
    """Проба назначенного имени: только заполнение правил и чтение события."""
    module = next(
        (path.split("/")[2] for path in paths if path.startswith("modules/CommonModules/")),
        "<менеджер>",
    )
    blocks = []
    for direction in ("send", "receive"):
        bindings = [b for b in _bindings_in_order(plan) if b.target.direction == direction]
        if not bindings:
            continue
        lines = [
            "Правила = ОбменДаннымиXDTOСервер.КоллекцияПравилКонвертации("
            f"{bsl_string(str(plan.manager_interface))});"
        ]
        if plan.manager_interface == 3:
            lines.extend(
                [
                    f"// КомпонентыОбмена из типового пути: {direction_label(direction)}.",
                    f"{module}.{FILLER}(КомпонентыОбмена, Правила, Истина);",
                    f"{module}.{FILLER}(КомпонентыОбмена, Правила, Ложь);",
                ]
            )
        else:
            lines.append(f"{module}.{FILLER}({bsl_string(direction_label(direction))}, Правила);")
        for binding in bindings:
            name = names[binding.target.pko_address]
            lines.extend(
                [
                    f'Правило = Правила.Найти({bsl_string(name)}, "ИмяПКО");',
                    "Если Правило = Неопределено Тогда",
                    f"\tРезультат = {bsl_string('ПКО ' + name + ' отсутствует')};",
                    "\tСообщить(Результат);",
                    "Иначе",
                    f"\tРезультат = Строка(Правило.{binding.event});",
                    f"\tСообщить(Результат); // Ожидается: {binding.handler_name}",
                    "КонецЕсли;",
                ]
            )
        blocks.append("```bsl\n" + "\n".join(lines) + "\n```")
    return "\n\n".join(blocks) or "В комплекте нет привязок обработчиков."


def extension_version_probe(name: str, version: str) -> str:
    """Проба свойства «Версия»: результат и в переменную, и в сообщение."""
    lines = [
        f'Найденные = РасширенияКонфигурации.Получить(Новый Структура("Имя", {bsl_string(name)}));',
        "Если Найденные.Количество() = 0 Тогда",
        '\tРезультат = "";',
        "Иначе",
        "\tРезультат = Найденные[0].Версия;",
        "КонецЕсли;",
        f"Сообщить(Результат); // Ожидается: {version}",
    ]
    return "```bsl\n" + "\n".join(lines) + "\n```"


def _bindings_in_order(plan: HandlerOperationsPlan) -> tuple[HandlerBindingPlan, ...]:
    by_name = {binding.handler_name: binding for binding in plan.bindings}
    if plan.dispatcher_order and set(plan.dispatcher_order) == set(by_name):
        return tuple(by_name[name] for name in plan.dispatcher_order)
    return plan.bindings


def _pko_labels(
    operations: tuple[Operation, ...], bindings: tuple[HandlerBindingPlan, ...]
) -> dict[str, str]:
    labels = {}
    for op in (*operations, *bindings):
        address = op.target.pko_address
        labels[address] = address.removeprefix("ПКО/")
    return labels


def _operation_row(op: Operation, names: dict[str, str]) -> tuple[str, str, str, str, str]:
    if isinstance(op, SetObjectHandler):
        detail = op.event
    elif isinstance(op, (AddHeaderProperty, AddAlgorithmicHeaderProperty)):
        detail = op.configuration_attribute + " ↔ " + op.format_property
    else:
        detail = "сохранение при отсутствии свойства"
    return (
        operation_kind(op),
        direction_label(op.target.direction),
        names.get(op.target.pko_address, op.target.pko_address),
        detail,
        op.operation_id,
    )


def _route_explanation(state: str) -> str:
    return {
        "effective": "Действующая запись карты",
        "unreachable": "Недостижимая ветвь; менеджер по ней не вызывается",
        "overwritten": "Запись заменена последующей вставкой",
        "unresolved": "Выбор менеджера не подтверждён",
        "conditional": "Зависит от условия",
    }.get(state, "Состояние чтения маршрута: " + state)


def _direction_risks(prepared: PreparedAuthoring) -> str:
    directions = {op.target.direction for op in prepared.operations}
    paragraphs = []
    if "send" in directions:
        paragraphs.append(
            "Отправка: пустой реквизит источника может не записываться в XML; при этом "
            "отсутствуют и свойство, и группа общих свойств формата. "
            "Проверьте фактическое сообщение."
        )
    if "receive" in directions:
        paragraphs.append(
            "Получение: очистка значения в источнике и источник без доработки для приёмника "
            "неразличимы — в обоих случаях свойство может отсутствовать во входящем сообщении. "
            "При разных именах реквизита и свойства это может очистить реквизит найденного "
            "объекта. Комплект не содержит обработчика сохранения прежнего значения; если такое "
            "поведение неприемлемо, не устанавливайте его. Влияние существующих обработчиков "
            "и объектный путь получения проверяются отдельно."
        )
    return "\n\n".join(paragraphs)
