"""Проверки писателя сверх платформы; связность делегируется существующим проверкам."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace

from kd2_rules_mcp.authoring.ed.manager_operations import ManagerOperation
from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.canonical import model_addresses
from kd2_rules_mcp.ed.errors import EdFormatError, EdReadError, EdResourceLimitError
from kd2_rules_mcp.ed.executor_profile import (
    PROFILES,
    ExecutorCapabilities,
    ProfileDetection,
    ReceivePath,
)
from kd2_rules_mcp.ed.forms import ENTRYPOINTS, EVENT_SIGNATURES
from kd2_rules_mcp.ed.lexer import lex, tokenize
from kd2_rules_mcp.ed.model import EdDocument
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.refs import ReferenceIndex, build_references, norm_name
from kd2_rules_mcp.ed.writer_import import (
    code_occurrences,
    code_rule_references,
    declarative_diagnostics,
    import_signature,
    property_directions,
)
from kd2_rules_mcp.ed.writer_model import ManagerModel
from kd2_rules_mcp.validation.ed_links import validate_links
from kd2_rules_mcp.validation.report import Issue, ValidationReport


def _direction(rule, direction: str) -> bool:
    return direction in rule.directions or "both" in rule.directions


def _declarative_checks(model: ManagerModel, report: ValidationReport, addresses) -> None:
    """Проверяет декларации, направления ссылок и заполнение ПКС."""
    guards = {g.logical_id: g for g in model.guards}
    messages = {
        "reference_case_mismatch": "Исполнитель сравнивает имена точно — проверьте. "
        "Описание платформы; живым обменом не подтверждено",
        "rule_namespace_collision": "Имена ПКО и ПКПД совпадают; ПКПД перехватывает поиск "
        "имени правила (XDTO:1543,6818)",
        "parameter_duplicate": "Имена параметров совпадают без учёта регистра: "
        "ключ Структуры будет перезаписан",
        "parameter_name": "Имя параметра должно быть идентификатором поля Структуры",
    }
    for code, address in declarative_diagnostics(model):
        emit = report.warning if code == "reference_case_mismatch" else report.error
        emit("ed.writer." + code, address, messages[code])
    names = defaultdict(list)
    for target in (*model.pko, *model.pkpd):
        names[target.name].append(target)
    for key, name, line, directions in code_rule_references(model):
        if not name:
            continue
        if name not in names and any(n.casefold() == name.casefold() for n in names):
            report.warning(
                "ed.writer.reference_case_mismatch",
                addresses[key],
                f"Строка {line}: " + messages["reference_case_mismatch"],
            )
            continue
        missing = [
            d for d in sorted(directions) if not any(_direction(t, d) for t in names.get(name, ()))
        ]
        if missing:
            report.error(
                "ed.writer.reference_direction",
                addresses[key],
                f"Строка {line}: правило инструкции «{name}» отсутствует "
                f"в направлении {', '.join(missing)}; исполнитель не найдёт цель "
                "(XDTO:1294–1300,1530–1554,502–508)",
            )
    for rule in model.pko:
        active_groups = defaultdict(list)
        for group in rule.groups:
            address = addresses[group.logical_id]
            if group.configuration_property and group.format_property and not group.properties:
                report.warning(
                    "ed.writer.table_part_empty",
                    address,
                    "У группы с обеими сторонами нет ПКС (XDTO:5749–5767)",
                )
            for prop in group.properties:
                if (
                    prop.property_kind == "direct"
                    and not (group.configuration_property and group.format_property)
                    and not (
                        property_directions(rule, group, guards) == {"send"}
                        and group.format_property
                        and any(p.algorithm_flag for p in group.properties)
                    )
                ):
                    report.warning(
                        "ed.writer.table_part_direct_empty",
                        addresses[prop.logical_id],
                        "Прямая ПКС в группе с пустой стороной не переносится напрямую "
                        "(XDTO:1223,6601–6604)",
                    )
            for direction in ("send", "receive"):
                if not _direction(rule, direction) or any(
                    guards[g].direction not in (None, "both", direction)
                    for g in (*rule.guards, *group.guards)
                    if g in guards
                ):
                    continue
                side = (
                    group.format_property if direction == "send" else group.configuration_property
                )
                if side:
                    active_groups[direction, side.casefold()].append(group)
                needs_code = any(p.algorithm_flag for p in group.properties)
                event = "ПриОтправкеДанных" if direction == "send" else "ПриКонвертацииДанныхXDTO"
                if needs_code and not any(e.event == event and e.target.name for e in rule.events):
                    report.warning(
                        "ed.writer.table_part_algorithm_handler",
                        address,
                        f"Алгоритмическая группа {direction} без обработчика «{event}» "
                        "(XDTO:1250–1270,6782–6790)",
                    )
        for (direction, side), groups in active_groups.items():
            if len(groups) > 1:
                emit = report.error if direction == "send" else report.warning
                emit(
                    "ed.writer.table_part_duplicate",
                    addresses[groups[0].logical_id],
                    f"Повтор ТЧ «{side}» в направлении {direction}: "
                    + ", ".join(addresses[g.logical_id] for g in groups)
                    + "; отправка затирает строки, получение заменяет ТЧ (XDTO:1246,7051–7062)",
                )
        for group_guards, props in [
            ((), rule.properties),
            *((g.guards, g.properties) for g in rule.groups),
        ]:
            for prop in props:
                name = prop.conversion.name
                if not name or prop.conversion.resolution == "computed" or name not in names:
                    continue
                missing = [
                    d
                    for d in sorted(property_directions(rule, prop, guards, group_guards))
                    if not any(_direction(t, d) for t in names.get(name, ()))
                ]
                if missing:
                    report.error(
                        "ed.writer.reference_direction",
                        addresses[prop.logical_id],
                        f"Правило «{name}» отсутствует в направлении {', '.join(missing)}; "
                        "исполнитель не найдёт цель ПКС (XDTO:502–508,1543,6818)",
                    )
    predefined = {r.logical_id: r for r in model.pkpd}
    unverified = {
        e.logical_id
        for e in model.import_report.entries
        if e.reason == "helper_semantics_unverified"
    }
    events = {
        "send": (
            "ПриОтправкеДанных",
            "ДанныеXDTOИзДанныхИБ, XDTO:1203–1205,1250–1270",
        ),
        "receive": (
            "ПриКонвертацииДанныхXDTO",
            "СтруктураОбъектаXDTOВДанныеИБ, XDTO:1855–1885; "
            "КонвертацияСвойстваСтруктурыОбъектаXDTO, XDTO:6772–6799",
        ),
    }

    def applies(member, direction):
        return all(
            g.direction in (None, "both", direction)
            for key in member.guards
            if (g := guards.get(key)) is not None
        )

    for rule in model.pko:
        for prop in rule.properties:
            # Старый помощник не удостоверяет смысл флага/ссылки; его уже
            # отмечает ed.writer.incomplete, заполнение ПКС здесь не угадываем.
            if prop.logical_id in unverified:
                continue
            for direction, (event, evidence) in events.items():
                if not _direction(rule, direction) or not applies(prop, direction):
                    continue
                needs_handler = (
                    not prop.configuration_property and bool(prop.format_property)
                    if direction == "send"
                    else not prop.format_property and bool(prop.configuration_property)
                )
                if (
                    prop.algorithm_flag
                    and needs_handler
                    and not any(
                        e.event == event
                        and applies(e, direction)
                        and (e.target.name.strip() or e.target.resolution == "computed")
                        for e in rule.events
                    )
                ):
                    report.warning(
                        "ed.writer.algorithm_handler",
                        addresses[prop.logical_id],
                        f"Алгоритмическая ПКС {direction}: нет обработчика «{event}» у ПКО "
                        f"«{rule.name}»; свойство не будет заполнено ({evidence})",
                    )
                target = predefined.get(prop.conversion.target_id or "")
                if target is not None and not any(
                    v.direction == direction for v in target.mappings
                ):
                    evidence = (
                        "ВыгрузитьСвойство, XDTO:1543–1550"
                        if direction == "send"
                        else "КонвертацияСвойстваСтруктурыОбъектаXDTO, XDTO:6818–6824"
                    )
                    report.warning(
                        "ed.writer.pkpd_direction",
                        addresses[prop.logical_id],
                        f"ПКПД «{target.name}» не содержит пар направления {direction}; "
                        f"значение свойства останется пустым ({evidence})",
                    )


def _signature_matches(actual, expected) -> bool:
    """Контракт позиционного вызова не зависит от имён формальных и их умолчаний."""
    return (
        actual.routine_kind == expected.routine_kind
        and actual.exported == expected.exported
        and len(actual.parameters) == len(expected.parameters)
        and tuple(p.by_value for p in actual.parameters)
        == tuple(p.by_value for p in expected.parameters)
    )


def _code_checks(
    model: ManagerModel, document: EdDocument, report: ValidationReport, addresses
) -> None:
    """Рамки и связи W2; тела только лексически индексируются, не исполняются."""
    routines = {r.name.casefold(): r for r in document.routines}
    units = {u.logical_id: u for u in model.code_units}
    bindings = [(rule, event) for rule in (*model.pko, *model.pod) for event in rule.events]
    bound = {e.target.target_id for _, e in bindings}
    by_name = {u.name.casefold(): u for u in model.code_units}
    cases = {c.literal_name: c for c in document.dispatcher_cases}
    sources = {s.file_id: s for s in document.files}
    for dispatcher in document.routines:
        if "dispatcher" not in dispatcher.roles:
            continue
        literals = {}
        for statement in lex(sources[dispatcher.span.file_id]).statements:
            if not (
                dispatcher.body_span.char_start
                <= statement.span.char_start
                < dispatcher.body_span.char_end
            ):
                continue
            tokens = statement.tokens
            if not (
                statement.head in ("если", "иначеесли")
                and len(tokens) >= 5
                and tokens[1].folded in ("имяпроцедуры", "имяфункции")
                and tokens[2].value == "="
                and tokens[3].kind == "string"
            ):
                continue
            literal = tokens[3].value
            if previous := literals.get(literal):
                unit = by_name.get(dispatcher.name.casefold())
                report.error(
                    "ed.writer.dispatcher_literal_duplicate",
                    addresses[unit.logical_id] if unit else "Код/" + dispatcher.name,
                    f"Литерал «{literal}» повторяется в одном диспетчере: "
                    f"строки {previous.span.line_start} и {statement.span.line_start}; "
                    "следующая ветка недостижима",
                )
            else:
                literals[literal] = statement
    for _rule, event in bindings:
        unit = units.get(event.target.target_id or "")
        method = routines.get((unit.name if unit else event.target.name).casefold())
        address = addresses[event.logical_id]
        if method is None:
            report.error(
                "ed.writer.binding_method",
                address,
                "Привязка указывает на отсутствующий метод; значение сохранено как есть",
            )
            continue
        case = cases.get(event.target.name)
        if case is None or case.target.raw.casefold() != method.name.casefold():
            report.error(
                "ed.writer.handler_dispatcher",
                address,
                "У метода привязки нет соответствующей ветки диспетчера; "
                "литерал сравнивается точно, обработчик не будет вызван",
            )
        if event.event == "ПослеЗагрузкиВсехДанных":
            continue
        expected = EVENT_SIGNATURES.get(event.event, ())
        signature = import_signature(method)
        parameters = len(signature.parameters)
        minimum = max(
            (n + 1 for n, p in enumerate(signature.parameters) if p.default.state == "unset"),
            default=0,
        )
        if not any(minimum <= len(p) <= parameters for p in expected) or signature.routine_kind != (
            "function" if event.event == "ВыборкаДанных" else "procedure"
        ):
            report.error(
                "ed.writer.handler_signature",
                address,
                "Сигнатура обработчика не соответствует событию; "
                "Template.txt:75–91, ObjectModule.bsl:3250–3287",
            )
    for unit in model.code_units:
        if "handler" in unit.roles and unit.logical_id not in bound:
            report.error(
                "ed.writer.handler_binding",
                addresses[unit.logical_id],
                "Метод обработчика не связан с правилом",
            )
        if "handler" in unit.roles and unit.name.casefold() not in {
            c.target.raw.casefold() for c in document.dispatcher_cases
        }:
            report.error(
                "ed.writer.handler_dispatcher",
                addresses[unit.logical_id],
                "У обработчика нет ветки диспетчера",
            )
    for case in document.dispatcher_cases:
        method = routines.get(case.target.raw.casefold())
        if method is not None:
            signature = import_signature(method)
            minimum = max(
                (n + 1 for n, p in enumerate(signature.parameters) if p.default.state == "unset"),
                default=0,
            )
            maximum = len(signature.parameters)
            if not minimum <= len(case.arguments) <= maximum:
                member = next(
                    (c for c in model.dispatcher_cases if c.name == case.literal_name), None
                )
                report.error(
                    "ed.writer.dispatcher_arguments",
                    addresses[member.logical_id] if member else "Ветка/" + case.literal_name,
                    f"Ветка передаёт {len(case.arguments)} аргументов, метод принимает "
                    f"{minimum}–{maximum} с учётом значений по умолчанию",
                )
        if case.target.raw.casefold() not in routines:
            if any(
                i.check == "ed.dispatcher.target_missing" and f"«{case.literal_name}»" in i.message
                for i in report.issues
            ):
                # validate_links уже выдал адресную ошибку этой ветки, не дублируем её.
                continue
            unit = by_name.get(case.literal_name.casefold())
            address = addresses[unit.logical_id] if unit else "Ветка/" + case.literal_name
            report.error(
                "ed.writer.dispatcher_method",
                address,
                "Ветка диспетчера вызывает отсутствующий метод",
            )
    used = (
        bound
        | {
            o.target_id
            for o in code_occurrences(model)
            if o.kind == "algorithm_call" and o.owner_id != o.target_id
        }
        | {c.target.target_id for c in model.dispatcher_cases}
    )
    for unit in model.code_units:
        if "algorithm" in unit.roles and unit.logical_id not in used:
            report.warning(
                "ed.writer.algorithm_unused",
                addresses[unit.logical_id],
                "Нет прямых вызовов, привязок и веток алгоритма; "
                "вычисляемые вызовы не устанавливаются",
            )


def _server_routines(document: EdDocument):
    """Кроме формы охраны всего модуля контекст препроцессора не удостоверяется."""
    typical = "#если сервер или толстыйклиентобычноеприложение или внешнеесоединение тогда"
    guards = {g.entity_id: g for g in document.guards if g.guard_kind == "preprocessor"}
    accepted = {
        key
        for key, guard in guards.items()
        if " ".join(guard.expression_raw.casefold().split()) == typical
        and guard.branch == "if"
        and guard.parent_id is None
        and all(key in r.guards for r in document.routines)
    }
    return tuple(r for r in document.routines if not (set(r.guards) & guards.keys()) - accepted)


def _indexed_code(document: EdDocument):
    """Индекс читателя расширяется ролями локальных помощников, без разбора их смысла."""
    routines = _server_routines(document)
    by_name = {(r.span.file_id, r.name.casefold()): r.entity_id for r in routines}
    sources = {s.file_id: s for s in document.files}
    calls: dict[str, set[str]] = defaultdict(set)
    for routine in routines:
        tokens = tuple(
            t
            for t in tokenize(
                sources[routine.span.file_id].text[
                    routine.body_span.char_start : routine.body_span.char_end
                ]
            )
            if t.kind != "comment"
        )
        for index, token in enumerate(tokens[:-1]):
            target = by_name.get((routine.span.file_id, token.folded))
            if (
                target
                and token.kind == "identifier"
                and tokens[index + 1].value == "("
                and (not index or tokens[index - 1].value != ".")
            ):
                calls[routine.entity_id].add(target)
    indexed = {
        r.entity_id for r in routines if r.roles & {"handler", "algorithm", "event", "callback"}
    }
    pending = list(indexed)
    while pending:
        for target in calls[pending.pop()] - indexed:
            indexed.add(target)
            pending.append(target)
    projection = replace(
        document,
        routines=tuple(
            replace(r, roles=r.roles | {"handler_helper"}) if r.entity_id in indexed else r
            for r in document.routines
        ),
    )
    return build_references(projection), calls


def _name_bounds(expression: str) -> tuple[str, str]:
    """Только внешние литеральные части конкатенации; строки внутри вызовов не ограничивают имя."""
    tokens = tuple(t for t in tokenize(expression) if t.kind != "comment")
    while len(tokens) >= 2 and tokens[0].value == "(" and tokens[-1].value == ")":
        depth = 0
        enclosing = False
        for index, token in enumerate(tokens):
            depth += (token.value == "(") - (token.value == ")")
            if depth == 0:
                enclosing = index == len(tokens) - 1
                break
        if not enclosing:
            break
        tokens = tokens[1:-1]
    parts, current, depth = [], [], 0
    for token in tokens:
        depth += (token.value in ("(", "[")) - (token.value in (")", "]"))
        if token.value == "+" and depth == 0:
            parts.append(tuple(current))
            current = []
        else:
            current.append(token)
    parts.append(tuple(current))
    if len(parts) < 2:
        return "", ""
    prefix = ""
    for part in parts:
        if len(part) != 1 or part[0].kind != "string":
            break
        prefix += part[0].value
    suffix = ""
    for part in reversed(parts):
        if len(part) != 1 or part[0].kind != "string":
            break
        suffix = part[0].value + suffix
    return prefix.lstrip().casefold(), suffix.rstrip().casefold()


def _reachable_rules(
    model: ManagerModel,
    document: EdDocument,
    references: ReferenceIndex,
    direction: str,
    addresses: Mapping[str, str],
    calls: Mapping[str, set[str]] | None = None,
) -> tuple[set[str], dict[str, set[str]]]:
    """Возможная достижимость: ПОД, ПКС шапки/ТЧ и индекс имён в коде.

    Цикл ссылок без входа не делает правила действующими. Вычисляемое имя в
    действующем обработчике не доказывает достижимость конкретного ПКО, но
    запрещает выдавать предупреждение о доказанном отсутствии пути.
    """
    rules = {r.logical_id: r for r in model.pko if _direction(r, direction)}
    pods = {r.logical_id: r for r in model.pod if _direction(r, direction)}
    active_ids = rules.keys() | pods.keys()
    names: dict[str, set[str]] = defaultdict(set)
    for rule in rules.values():
        names[norm_name(rule.name)].add(rule.logical_id)
    edges: dict[str, set[str]] = defaultdict(set)
    uncertain: dict[str, dict[str, set[str]]] = defaultdict(dict)
    global_owner = ""
    guards = {g.logical_id: g for g in model.guards}

    def applies(member) -> bool:
        return all(
            guards[key].direction in (None, "both", direction)
            for key in member.guards
            if key in guards
        )

    def link(owner: str, name: str, computed: bool, reason: str, expression: str = "") -> None:
        if not computed:
            edges[owner].update(names.get(norm_name(name), ()))
        if computed:
            prefix, suffix = _name_bounds(expression)
            candidates = {
                key
                for key, rule in rules.items()
                if norm_name(rule.name).startswith(prefix) and norm_name(rule.name).endswith(suffix)
            }
            uncertain[owner][reason + f"; выражением покрыто ПКО: {len(candidates)}"] = candidates

    for rule in rules.values():
        properties = list(rule.properties)
        properties.extend(p for g in rule.groups if applies(g) for p in g.properties)
        for prop in properties:
            if not applies(prop) or prop.conversion.kind == "pkpd":
                continue
            # XDTO:1441–1444,6723–6725: пустая сторона отключает это направление ПКС.
            property_name = (
                prop.format_property if direction == "send" else prop.configuration_property
            )
            if not property_name.strip():
                continue
            computed = prop.conversion.resolution == "computed" or (
                len(prop.argument_values) > 4 and prop.argument_values[4].state == "unknown"
            )
            link(
                rule.logical_id,
                prop.conversion.name,
                computed,
                addresses[prop.logical_id] + ": имя правила ПКС вычисляется",
                prop.argument_values[4].raw
                if len(prop.argument_values) > 4
                else prop.conversion.name,
            )
    for pod in pods.values():
        for ref in pod.used_pko:
            link(
                pod.logical_id,
                ref.name,
                ref.resolution in ("computed", "ambiguous"),
                addresses[pod.logical_id] + ": ИспользуемыеПКО не разрешены",
                ref.name,
            )
            if ref.target_id in rules:
                edges[pod.logical_id].add(ref.target_id)

    procedures = {m.procedure_name.casefold(): m.logical_id for m in (*model.pko, *model.pod)}
    reader_to_model = {
        r.entity_id: procedures[r.procedure_name.casefold()]
        for r in (*document.pko, *document.pod)
        if r.procedure_name.casefold() in procedures
    }
    if calls is None:
        references, calls = _indexed_code(document)
    all_bound = set()
    server_ids = {r.entity_id for r in _server_routines(document)}
    event_directions = {e.name: e.directions for p in PROFILES for e in p.events}
    event_directions.update({"ПриОбработке": ("send", "receive"), "ВыборкаДанных": ("send",)})
    for owner in (*document.pko, *document.pod):
        for event in owner.events:
            if event.target_id:
                all_bound.add(event.target_id)
                model_owner = reader_to_model.get(owner.entity_id)
                if (
                    model_owner in active_ids
                    and event.target_id in server_ids
                    and direction in event_directions.get(event.event, ())
                ):
                    edges[model_owner].add(event.target_id)
    routines = {r.entity_id: r for r in document.routines}
    unit_addresses = {u.name.casefold(): addresses[u.logical_id] for u in model.code_units}
    for routine in routines.values():
        if routine.entity_id not in server_ids:
            continue
        edges[routine.entity_id].update(calls.get(routine.entity_id, ()))
        # Область Алгоритмы — каталог кода, а не точка входа исполнителя.
        if routine.entity_id not in all_bound and routine.roles & {
            "event",
            "callback",
        }:
            edges[global_owner].add(routine.entity_id)
    for ref in references.entries:
        # Направление задаёт ребро события, не направления всех владельцев помощника в индексе.
        if ref.kind not in ("pko_lookup", "instruction_rule", "pod_use"):
            continue
        routine = routines[ref.owner_id]
        address = unit_addresses.get(routine.name.casefold(), "Код/" + routine.name)
        link(
            ref.owner_id,
            ref.name or "",
            ref.name is None,
            f"{address}: имя ПКО вычисляется, строка {ref.span.line_start}",
            ref.raw,
        )
    visited: set[str] = set()
    pending = [global_owner, *pods]
    reasons: dict[str, set[str]] = defaultdict(set)
    while pending:
        owner = pending.pop()
        if owner in visited:
            continue
        visited.add(owner)
        for reason, candidates in uncertain[owner].items():
            for candidate in candidates:
                reasons[candidate].add(reason)
        pending.extend(edges[owner] - visited)
    return visited & rules.keys(), reasons


def validate_writer(
    model: ManagerModel,
    text: str | bytes,
    *,
    detection: ProfileDetection | None = None,
    profile: ExecutorCapabilities | None = None,
    receive_path: ReceivePath | None = None,
    operations: Sequence[ManagerOperation] = (),
) -> ValidationReport:
    """Возвращает ValidationReport; зелёный статический отчёт не удостоверяет живой обмен.

    text допускает RenderResult.data либо text без BOM. В последнем случае BOM берётся из
    шапки модели. Подтверждение — только detection текущей сверки выгрузки; выбор профиля
    в модели и снимке не заменяет эту сверку.
    """
    report = ValidationReport()
    addresses = model_addresses(model)
    caps = detection.profile if detection and detection.profile else profile
    if caps is None:
        caps = next(
            (p for p in PROFILES if p.profile_id == model.executor_profile.profile_id), None
        )
    verified = bool(
        detection
        and detection.verified
        and detection.profile == caps
        and caps is not None
        and model.executor_profile.profile_id in ("", caps.profile_id)
    )
    # §4.1: изменённый исполнитель не получает ближайший профиль, preserve запрет не снимает.
    if not verified:
        details = (
            "; ".join(
                f"{m.module}: {m.reason}; ожидалось {m.expected or '—'}, найдено {m.actual or '—'}"
                for m in detection.mismatches
            )
            or "Выбранный профиль модели не подтверждён этой сверкой"
            if detection
            else "Выгрузка исполнителя не сверялась"
        )
        report.error(
            "ed.writer.profile",
            "Конвертация/executor_profile",
            "executor_profile_unverified: " + details,
        )
    interface = model.header.interface_version
    path = receive_path or model.executor_profile.receive_mode or "ordinary"
    if caps is not None and (interface not in caps.interfaces or path not in caps.receive_paths):
        report.error(
            "ed.writer.profile",
            "Конвертация/executor_profile",
            f"Профиль не поддерживает интерфейс {interface} или путь {path}",
        )
    try:
        if isinstance(text, bytes):
            source = text.decode("utf-8")
        else:
            source = (
                "\ufeff" if model.header.text_style.bom and not text.startswith("\ufeff") else ""
            ) + text
        document = read_manager_text(source)
    except (UnicodeError, EdFormatError, EdReadError, EdResourceLimitError) as error:
        report.error(
            "ed.writer.read",
            "Конвертация",
            f"Повторное чтение результата невозможно: {type(error).__name__}; §6.1",
        )
        return report
    routines = {}
    for routine in _server_routines(document):
        routines.setdefault(routine.name.casefold(), []).append(routine)
    # Пилот:21–34,133–176; XDTO:4763–4805. Отсутствие версии даёт тихий fallback 1.
    if caps is not None and interface in caps.interfaces:
        contracts = [c for c in caps.contracts if interface in c.interfaces]
        for contract in contracts:
            # XDTO:ПроизвестиВыгрузкуДанных:645; ВыборкаДанных:8485–8493.
            # Оба диспетчера порождаются всегда, но функция чужого модуля нужна
            # только при непустом обработчике выборки действующего ПОД отправки.
            if (
                contract.name == "ВыполнитьФункциюМодуляМенеджера"
                and not routines.get(contract.name.casefold())
                and not any(
                    _direction(pod, "send")
                    and any(
                        e.event == "ВыборкаДанных"
                        and (e.target.name.strip() or e.target.resolution == "computed")
                        for e in pod.events
                    )
                    for pod in model.pod
                )
            ):
                continue
            found = routines.get(contract.name.casefold(), ())
            if not contract.required and not found:
                continue
            address = next(
                (
                    addresses[u.logical_id]
                    for u in model.code_units
                    if u.name.casefold() == contract.name.casefold()
                ),
                "Конвертация",
            )
            if len(found) != 1:
                report.error(
                    "ed.writer.entrypoint",
                    address,
                    f"Обязательный метод «{contract.name}»: найдено {len(found)}; "
                    + contract.evidence,
                )
            elif not _signature_matches(import_signature(found[0]), contract.signature):
                report.error(
                    "ed.writer.entrypoint",
                    address,
                    f"Сигнатура или экспорт «{contract.name}» не соответствует "
                    f"интерфейсу {interface}; {contract.evidence}",
                )
        if document.manager_version != interface:
            report.error(
                "ed.writer.entrypoint",
                "Конвертация",
                f"Функция версии не возвращает интерфейс модели {interface}; XDTO:4763–4771",
            )
    all_routines = defaultdict(list)
    for routine in document.routines:
        all_routines[routine.name.casefold()].append(routine)
    for rows in all_routines.values():
        if len(rows) > 1:
            report.error(
                "ed.writer.name_collision",
                "Код/" + rows[0].name,
                "Повтор имени метода без учёта регистра; вызовы неоднозначны (XDTO:4716–4742)",
            )
    for name, rows in routines.items():
        if name in ("выполнитьпроцедурумодуляменеджера", "выполнитьфункциюмодуляменеджера"):
            for routine in rows:
                body = document.files[0].text[
                    routine.body_span.char_start : routine.body_span.char_end
                ]
                tokens = [t.folded for t in tokenize(body)]
                # §11.7 отменяет строгий fallback: пустое имя штатно приходит из XDTO:1869–1873.
                if "иначе" in tokens or "вызватьисключение" in tokens:
                    report.error(
                        "ed.writer.dispatcher",
                        "Код/" + routine.name,
                        "Диспетчер должен пропускать пустое имя без Иначе/исключения; "
                        "пилот, Особенности п. 1; §11.7",
                    )
    # Сохраняются прежние check IDs; алгоритмы разрешения обработчиков не копируются.
    reader_addresses = build_addresses(document)
    references, calls = _indexed_code(document)
    linked = validate_links(document, reader_addresses, references)
    selected = frozenset(
        (
            "ed.handler.missing",
            "ed.handler.ambiguous",
            "ed.dispatcher.target_missing",
            "ed.reference.rule_use_missing",
        )
    )
    aliases = {}
    for member in (*model.pko, *model.pod, *model.code_units):
        aliases.setdefault(member.name.casefold(), addresses[member.logical_id])
    procedure_addresses = {
        m.procedure_name.casefold(): addresses[m.logical_id] for m in (*model.pko, *model.pod)
    }
    procedure_addresses.update(
        {u.name.casefold(): addresses[u.logical_id] for u in model.code_units}
    )
    procedure_addresses.update(
        {
            c.name.casefold(): addresses[c.logical_id]
            for c in model.layouts
            if c.kind == "entrypoint"
        }
    )
    reader_rule_addresses = {
        r.entity_id: procedure_addresses[r.procedure_name.casefold()]
        for r in (*document.pko, *document.pod)
        if r.procedure_name.casefold() in procedure_addresses
    }
    for issue in linked.issues:
        if issue.check in selected:
            entity = reader_addresses.by_address.get(issue.address)
            target = issue.address
            if entity:
                target = (
                    reader_rule_addresses.get(entity.entity_id)
                    or procedure_addresses.get(entity.name.casefold())
                    or aliases.get(entity.name.casefold())
                    or issue.address
                )
            report.issues.append(Issue(issue.level, issue.check, target, issue.message))
    report.skipped.extend(
        s for s in linked.skipped if s.check in selected or s.check == "ed.handler.extended_events"
    )
    _declarative_checks(model, report, addresses)
    _code_checks(model, document, report, addresses)
    # XDTO:781–795,1199–1243,1527–1565,8402–8469: ПОД — не единственный вход.
    used, uncertain_send = _reachable_rules(model, document, references, "send", addresses, calls)
    for rule in model.pko:
        if _direction(rule, "send") and rule.logical_id not in used:
            if uncertain_send.get(rule.logical_id):
                report.skip(
                    "ed.writer.send_pod",
                    addresses[rule.logical_id]
                    + ": "
                    + "; ".join(sorted(uncertain_send[rule.logical_id])),
                )
            else:
                report.warning(
                    "ed.writer.send_pod",
                    addresses[rule.logical_id],
                    "ПКО отправки недостижим из ПОД, ПКС шапки/ТЧ и индексированных имён "
                    "в действующем коде (XDTO:781–795,1527–1565,8402–8469)",
                )
    # XDTO:9441–9481,3849–3892 — верхний уровень; 6800–6839 — вложенная ссылка.
    reachable, uncertain_receive = _reachable_rules(
        model, document, references, "receive", addresses, calls
    )
    for rule in model.pko:
        if _direction(rule, "receive") and rule.logical_id not in reachable:
            if uncertain_receive.get(rule.logical_id):
                report.skip(
                    "ed.writer.receive_pod",
                    addresses[rule.logical_id]
                    + ": "
                    + "; ".join(sorted(uncertain_receive[rule.logical_id])),
                )
            else:
                report.warning(
                    "ed.writer.receive_pod",
                    addresses[rule.logical_id],
                    "ПКО получения недостижим из ИспользуемыеПКО ПОД и вложенных значений "
                    "действующих "
                    "правил/кода (XDTO:9441–9481,6800–6839)",
                )
    if caps is not None:
        event_caps = {e.name: e for e in caps.events}
        for kind in ("pko", "pod"):
            limit = caps.name_limit(kind)
            for rule in getattr(model, kind):
                if limit is not None and len(rule.name) > limit:
                    report.error(
                        "ed.writer.name_length",
                        addresses[rule.logical_id],
                        f"Имя длиннее ограничения {limit}; колонка {kind}, "
                        f"профиль {caps.profile_id}",
                    )
                if kind != "pko":
                    continue
                mode_limit = next(
                    (
                        c.max_length
                        for c in caps.columns
                        if c.table == "pko" and c.name == "ВариантИдентификации"
                    ),
                    None,
                )
                mode = rule.identification.mode
                if (
                    mode_limit is not None
                    and mode.state == "string"
                    and len(mode.value) > mode_limit
                ):
                    report.error(
                        "ed.writer.name_length",
                        addresses[rule.identification.logical_id],
                        f"Вариант идентификации длиннее {mode_limit} символов (XDTO:3273)",
                    )
                for event in rule.events:
                    if not event.target.name:
                        continue
                    capability = event_caps.get(event.event)
                    relevant = capability is None or any(
                        _direction(rule, d) for d in capability.directions
                    )
                    if not relevant or capability is None or path not in capability.receive_paths:
                        reason = (
                            capability.evidence
                            if capability
                            else "XDTO:КоллекцияПравилКонвертации:3250–3289"
                        )
                        report.warning(
                            "ed.writer.event",
                            addresses[event.logical_id],
                            f"Событие «{event.event}» не вызывается на пути {path}; {reason}",
                        )
                    elif path == "object" and capability.object_condition:
                        report.warning(
                            "ed.writer.event",
                            addresses[event.logical_id],
                            f"Событие «{event.event}» на объектном пути условно: "
                            f"{capability.object_condition}; {capability.evidence}",
                        )
                property_limit = next(
                    (
                        c.max_length
                        for c in caps.columns
                        if c.table == "property" and c.name == "ПравилоКонвертацииСвойства"
                    ),
                    None,
                )
                for prop in (*rule.properties, *(p for g in rule.groups for p in g.properties)):
                    if property_limit is not None and len(prop.conversion.name) > property_limit:
                        report.error(
                            "ed.writer.name_length",
                            addresses[prop.logical_id],
                            f"Имя ссылки превышает {property_limit} символов; "
                            f"профиль {caps.profile_id}; XDTO:246",
                        )
    # Интерфейс 1 допускает сохранение старой ТЧ; новую операцию таблицы W1 не порождает (§4.2).
    if interface == 1:
        for rule in model.pko:
            for group in rule.groups:
                if group.state == "editable":
                    report.error(
                        "ed.writer.table_part_interface",
                        addresses[group.logical_id],
                        "Операции ТЧ интерфейса 1 не поддержаны (XDTO:3280–3286,4289–4331)",
                    )
        for operation in operations:
            if operation.kind == "table_part" or any(
                operation.owner_id == g.logical_id for r in model.pko for g in r.groups
            ):
                report.error(
                    "ed.writer.table_part_interface",
                    operation.address
                    or addresses.get(
                        operation.target_id or operation.owner_id or "", "Конвертация"
                    ),
                    "Операция над ТЧ интерфейса 1 требует отдельной формы (§4.2; XDTO:4289–4331)",
                )
    # XDTO:4716–4728; генератор:2300–2304 — режим заголовков не должен добавлять свойства.
    if interface == 3:
        for rule in model.pko:
            if not rule.properties and not rule.groups:
                continue
            found = routines.get(rule.procedure_name.casefold(), ())
            if len(found) != 1:
                continue
            routine = found[0]
            statements = tuple(
                s
                for s in lex(document.files[0]).statements
                if routine.body_span.char_start <= s.span.char_start < routine.body_span.char_end
            )
            rows = [tuple(t.folded for t in s.tokens) for s in statements]
            guard = next(
                (
                    n
                    for n in range(len(rows) - 2)
                    if rows[n : n + 3]
                    == [("если", "толькозаголовки", "тогда"), ("возврат", ";"), ("конецесли", ";")]
                ),
                None,
            )
            if guard is not None:
                context = next(
                    (
                        g
                        for g in document.guards
                        if g.span.char_start == statements[guard].span.char_start
                    ),
                    None,
                )
                if context is None or context.parent_id is not None:
                    guard = None
            first_property = next(
                (
                    n
                    for n, row in enumerate(rows)
                    if any(name in row for name in ("добавитьпкс", "добавитьпктч", "свойствашапки"))
                ),
                len(rows),
            )
            if guard is None or guard >= first_property:
                report.error(
                    "ed.writer.headers_only",
                    addresses[rule.logical_id],
                    "Нет охраны ТолькоЗаголовки перед свойствами "
                    "(XDTO:4716–4728; генератор:2300–2304)",
                )
    # helper_semantics_unverified остаётся препятствием готовности даже при сохранении листа.
    for diagnostic in document.diagnostics:
        if (
            diagnostic.code == "helper_semantics_unverified"
            and model.header.helper_variant != "legacy-v2"
        ):
            report.warning(
                "ed.writer.incomplete",
                "Код/ДобавитьПКС",
                "Помощник не подтверждён; результат нельзя выдавать как готовый комплект (§4.3)",
            )
    if caps is not None:
        helpers = routines.get("добавитьпкс", ())
        if not routines.get("добавитьпктч") and any(r.groups for r in model.pko):
            report.warning(
                "ed.writer.incomplete",
                "Код/ДобавитьПКТЧ",
                "ПКТЧ есть, помощник отсутствует; комплект не готов "
                "(reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/"
                "ШаблоныТекстовМодулей/Ext/Template.txt:175–188)",
            )
        if not helpers and any(r.properties or r.groups for r in model.pko):
            report.warning(
                "ed.writer.incomplete",
                "Код/ДобавитьПКС",
                "ПКС есть, помощник отсутствует; результат нельзя выдавать как готовый комплект "
                "(XDTO:240–254; §6.1)",
            )
        if (
            helpers
            and model.header.helper_variant != "legacy-v2"
            and tuple(p.name for p in helpers[0].parameters) != caps.helper_parameters
            and not any(
                i.check == "ed.writer.incomplete" and i.address == "Код/ДобавитьПКС"
                for i in report.issues
            )
        ):
            report.warning(
                "ed.writer.incomplete",
                "Код/ДобавитьПКС",
                "Вариант помощника отличается от профиля; комплект не готов; "
                + caps.helper_evidence,
            )
    entry_names = {name.casefold() for name in ENTRYPOINTS}
    sources = {s.file_id: s for s in model.source_files}
    opaque_entries = set()
    for block in model.retained_blocks:
        if block.kind != "routine" or block.file_id not in sources:
            continue
        # Имя блока может быть «Текст»; координаты относятся к исходнику, не к результату.
        declared = [t for t in tokenize(block.text) if t.kind != "comment"]
        routine_label = next(
            (
                declared[n + 1].value
                for n, t in enumerate(declared[:-1])
                if t.folded in ("процедура", "функция")
            ),
            "",
        )
        routine_name = routine_label.casefold()
        if routine_name not in entry_names:
            continue
        # Пустой заполнитель параметров сохраняется технически, но непрозрачной логики нет.
        signature_end, balance = 0, 0
        for n, token in enumerate(declared):
            balance += (token.value == "(") - (token.value == ")")
            if token.value == ")" and not balance:
                signature_end = n + 1
                break
        body_tokens = [
            t
            for t in declared[signature_end:]
            if t.folded not in ("экспорт", "конецпроцедуры", "конецфункции")
        ]
        if body_tokens:
            opaque_entries.add(routine_name)
            report.warning(
                "ed.writer.incomplete",
                addresses[block.logical_id],
                f"Непрозрачная точка входа «{routine_label}»; "
                "результат нельзя выдавать как готовый комплект (§6.1)",
            )
    layouts = {c.logical_id: c for c in model.layouts}
    blocks = {b.logical_id: b for b in model.retained_blocks}

    def inspect_entry(container, address):
        for element in container.elements:
            if element.container_id:
                inspect_entry(layouts[element.container_id], address)
            elif element.block_id:
                block = blocks[element.block_id]
                if block.locks_context:
                    report.warning(
                        "ed.writer.incomplete",
                        address,
                        "Сохранённый непрозрачный лист в точке входа; комплект не готов (§6.1)",
                    )

    for container in model.layouts:
        if container.kind == "entrypoint" and container.name.casefold() not in opaque_entries:
            inspect_entry(
                container, addresses.get(container.owner_id or "", "Код/" + container.name)
            )
    # Одинаковая причина может прийти от модели и результата повторного чтения.
    report.issues = list(dict.fromkeys(report.issues))
    report.skipped = list(dict.fromkeys(report.skipped))
    return report
