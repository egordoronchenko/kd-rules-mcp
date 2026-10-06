"""Перевод снимка читателя в авторский оригинал без исполнения и потери BSL."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import fields, replace
from functools import lru_cache
from itertools import pairwise
from pathlib import PureWindowsPath
from typing import Any, Literal, cast

from . import model as reader
from .address import build_addresses
from .canonical import model_addresses
from .errors import EdFormatError, EdReadError
from .executor_profile import PROFILES
from .forms import ENTRYPOINTS, POD_COLUMN_STATEMENT, VERSION_ROUTINE, helper_forms
from .lexer import lex, split_arguments, tokenize
from .refs import build_references
from .writer_model import (
    CodeOccurrence,
    CodeUnit,
    Direction,
    DispatcherCase,
    EntityStyle,
    Event,
    ExecutorProfile,
    Formal,
    FormatBinding,
    Guard,
    Header,
    Host,
    Identification,
    ImportEntry,
    ImportReport,
    ImportState,
    LayoutContainer,
    LayoutElement,
    ManagerModel,
    Member,
    ObjectRule,
    Parameter,
    PredefinedRule,
    ProcessingRule,
    Property,
    PropertyGroup,
    Reference,
    RetainedBlock,
    RuleUse,
    SearchSet,
    Signature,
    SourceMapEntry,
    SourceSlice,
    SourceSnapshot,
    TextStyle,
    Value,
    ValueMapping,
    ValueState,
    leaf_fingerprint,
    logical_id,
    partition_report,
    text_hash,
    validate_model,
)


def code_texts(model: ManagerModel) -> tuple[tuple[str, str], ...]:
    """Тела методов и непрозрачные операторы деклараций, без повторов и диспетчеров."""
    units = {u.logical_id for u in model.code_units}
    return tuple(
        (u.logical_id, u.body) for u in model.code_units if "dispatcher" not in u.roles
    ) + tuple(
        (b.logical_id, b.text)
        for b in model.retained_blocks
        if b.kind in ("unknown", "routine") and b.owner_id not in units
    )


def code_occurrences(model: ManagerModel) -> tuple[CodeOccurrence, ...]:
    """Только токены: идентификатор с '(' без точки и отдельные строковые литералы.

    Вызовы — опись W2 §6; строка с именем правила не доказывает его использование.
    Диспетчеры исключены: их ссылки представлены собственными сущностями модели.
    """
    algorithms = {u.name.casefold(): u for u in model.code_units if "algorithm" in u.roles}
    rules = {}
    for rule in (*model.pko, *model.pod):
        rules.setdefault(rule.name.casefold(), []).append(rule)
    result = []
    for owner_id, body in code_texts(model):
        tokens = [t for t in tokenize(body) if t.kind != "comment"]
        for n, token in enumerate(tokens):
            targets = []
            kind = "rule_literal"
            if token.kind == "string":
                targets = rules.get(token.value.casefold(), [])
                if token.value.casefold() in algorithms:
                    targets = [algorithms[token.value.casefold()]]
                    kind = "algorithm_literal"
            elif (
                token.kind == "identifier"
                and n + 1 < len(tokens)
                and tokens[n + 1].value == "("
                and (not n or tokens[n - 1].value != ".")
                and (not n or tokens[n - 1].folded not in ("процедура", "функция"))
                and token.folded in algorithms
            ):
                targets = [algorithms[token.folded]]
                kind = "algorithm_call"
            for target in targets:
                result.append(
                    CodeOccurrence(
                        owner_id,
                        target.logical_id,
                        kind,
                        target.name,
                        token.start,
                        token.end,
                        body.count("\n", 0, token.start) + 1,
                    )
                )
    return tuple(result)


@lru_cache(maxsize=2048)
def parameter_accesses(body: str) -> tuple[tuple[str | None, int], ...]:
    """Обращения к структуре параметров; строковые/вычисляемые формы не разрешаются."""
    if "параметрыконвертации" not in body.casefold():
        return ()
    tokens = [t for t in tokenize(body) if t.kind != "comment"]
    result = []
    for n, token in enumerate(tokens):
        if token.kind == "string" and (
            token.folded == "параметрыконвертации"
            or "параметрыконвертации." in token.folded
            or "параметрыконвертации[" in token.folded
        ):
            result.append((None, body.count("\n", 0, token.start) + 1))
            continue
        if token.kind != "identifier" or token.folded != "параметрыконвертации":
            continue
        if n + 1 < len(tokens) and tokens[n + 1].value == "." and n + 2 < len(tokens):
            member = tokens[n + 2]
            if member.kind == "identifier" and not (
                n + 3 < len(tokens) and tokens[n + 3].value == "("
            ):
                result.append((member.value, body.count("\n", 0, member.start) + 1))
            else:
                result.append((None, body.count("\n", 0, token.start) + 1))
        else:
            result.append((None, body.count("\n", 0, token.start) + 1))
    return tuple(result)


def parameter_dependencies(body: str, parameters: tuple[Parameter, ...]) -> tuple[Reference, ...]:
    names = {p.name.casefold(): p for p in parameters}
    return tuple(
        dict.fromkeys(
            Reference("parameter", names[name.casefold()].logical_id, name, "resolved")
            if name is not None and name.casefold() in names
            else Reference(
                "parameter", name=name or "", resolution="computed" if name is None else "missing"
            )
            for name, _ in parameter_accesses(body)
        )
    )


def refresh_code_dependencies(before: ManagerModel, model: ManagerModel) -> ManagerModel:
    """Повторно индексирует тела при изменении кода или пространства имён целей."""
    from .reader import read_manager_text
    from .writer_forms import code_open, empty_module, routine_close

    old = {u.logical_id: u for u in before.code_units}
    changed_names = {
        name.casefold()
        for catalog in ("pko", "pkpd", "parameters")
        for _, name in (
            {(r.logical_id, r.name) for r in getattr(before, catalog)}
            ^ {(r.logical_id, r.name) for r in getattr(model, catalog)}
        )
    }
    selected = [
        u
        for u in model.code_units
        if "dispatcher" not in u.roles
        and (
            u.logical_id not in old
            or u.body != old[u.logical_id].body
            or any(name in u.body.casefold() for name in changed_names)
        )
    ]
    if not selected:
        return model
    text = (
        empty_module()
        + "#Область Алгоритмы\n"
        + "\n".join(code_open(u) + u.body + routine_close(u.signature) + "\n" for u in selected)
        + "#КонецОбласти\n"
    )
    document = read_manager_text(text)
    index = build_references(document)
    deps = {}
    if len(document.routines) != 12 + len(selected):
        raise ValueError("Тело изменило границы метода")
    # Первые 12 методов принадлежат пустому каркасу; дальше определения идут
    # в порядке selected. Имя не различает сохранённые копии под #Если/#Иначе.
    for unit, routine in zip(selected, document.routines[12:], strict=True):
        if routine.name.casefold() != unit.name.casefold():
            raise ValueError("Тело изменило границы метода: " + unit.name)
        rows = []
        for ref in index.entries:
            if ref.owner_id != routine.entity_id or ref.kind == "parameter":
                continue
            targets = (
                [r for r in model.pko if ref.name and r.name.casefold() == ref.name.casefold()]
                if ref.kind in ("pko_lookup", "instruction_rule", "pod_use")
                else []
            )
            rows.append(
                Reference(
                    "pko" if targets else ref.kind,
                    targets[0].logical_id if len(targets) == 1 else None,
                    ref.name or "",
                    "resolved"
                    if len(targets) == 1
                    else "computed"
                    if ref.name is None
                    else "ambiguous"
                    if targets
                    else "missing",
                )
            )
        for token in tokenize(unit.body):
            if token.kind == "string":
                rule = next(
                    (r for r in model.pkpd if r.name.casefold() == token.value.casefold()), None
                )
                if rule:
                    rows.append(Reference("pkpd", rule.logical_id, rule.name, "resolved"))
        deps[unit.logical_id] = tuple(rows) + parameter_dependencies(unit.body, model.parameters)
    return replace(
        model,
        code_units=tuple(
            replace(u, dependencies=deps[u.logical_id]) if u.logical_id in deps else u
            for u in model.code_units
        ),
    )


@lru_cache(maxsize=4096)
def direction_contexts(body: str) -> tuple[tuple[int, int, frozenset[str]], ...]:
    """Только точная охрана НаправлениеОбмена; произвольные условия не вычисляются."""
    source = reader.SourceFile("code-context", "", body, text_hash(body), (0,))
    current = {"send", "receive"}
    stack = []
    result = []
    for statement in lex(source).statements:
        tokens = statement.tokens
        if statement.head in ("если", "иначеесли"):
            expression = tokens[1:-1]
            condition = None
            if (
                len(expression) >= 3
                and expression[-2].value in ("=", "<>")
                and expression[-1].kind == "string"
            ):
                path = "".join(t.folded for t in expression[:-2])
                if path in (
                    "направлениеобмена",
                    "компонентыобмена.направлениеобмена",
                    "параметры.компонентыобмена.направлениеобмена",
                ) and expression[-1].value in ("Отправка", "Получение"):
                    direction = "send" if expression[-1].value == "Отправка" else "receive"
                    condition = (
                        {direction}
                        if expression[-2].value == "="
                        else {"send", "receive"} - {direction}
                    )
            if statement.head == "если":
                parent, remaining = set(current), {"send", "receive"}
                stack.append((parent, remaining))
            elif stack:
                parent, remaining = stack[-1]
                current = parent & remaining
            else:
                parent, remaining = set(current), {"send", "receive"}
            result.append((statement.span.char_start, statement.span.char_end, frozenset(current)))
            current = (
                parent & remaining & (condition if condition is not None else {"send", "receive"})
            )
            if condition is not None:
                remaining -= condition
        else:
            if statement.head == "иначе" and stack:
                parent, remaining = stack[-1]
                current = parent & remaining
            elif statement.head == "конецесли" and stack:
                current = stack.pop()[0]
            result.append((statement.span.char_start, statement.span.char_end, frozenset(current)))
    return tuple(result)


def _code_context(body: str, position: int) -> frozenset[str]:
    return next(
        (
            directions
            for start, end, directions in direction_contexts(body)
            if start <= position < end
        ),
        frozenset(("send", "receive")),
    )


@lru_cache(maxsize=4096)
def rule_accesses(body: str) -> tuple[tuple[str | None, int, frozenset[str]], ...]:
    """Имена в инструкциях и поиске правил: тот же лексический индекс читателя."""
    if not any(word in body.casefold() for word in ("имяпко", "пкопоимени")):
        return ()
    from .reader import read_manager_text
    from .writer_forms import empty_module

    whole_routine = any(
        t.kind == "identifier" and t.folded in ("процедура", "функция") for t in tokenize(body)
    )
    prefix = (
        empty_module() + "#Область Алгоритмы\n" + ("" if whole_routine else "Процедура Indexed()\n")
    )
    try:
        document = read_manager_text(
            prefix + body + ("\n" if whole_routine else "\nКонецПроцедуры\n") + "#КонецОбласти\n"
        )
    except (EdFormatError, EdReadError):
        # Сохранённая рамка не обязана подходить для отдельного разбора.
        # Её текст остаётся на месте, затрагивающая правка требует пересмотра.
        return ((None, 1, frozenset(("send", "receive"))),)
    return tuple(
        (
            r.name,
            r.span.line_start - prefix.count("\n"),
            _code_context(body, r.span.char_start - len(prefix)),
        )
        for r in build_references(document).entries
        if r.kind in ("pko_lookup", "instruction_rule")
    )


@lru_cache(maxsize=4096)
def local_calls(body: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """Имена непосредственных вызовов без точки; выражения не исполняются."""
    tokens = [t for t in tokenize(body) if t.kind != "comment"]
    return tuple(
        dict.fromkeys(
            (t.folded, _code_context(body, t.start))
            for n, t in enumerate(tokens[:-1])
            if t.kind == "identifier"
            and tokens[n + 1].value == "("
            and (not n or tokens[n - 1].value != ".")
        )
    )


def code_rule_references(
    model: ManagerModel,
) -> tuple[tuple[str, str | None, int, frozenset[str]], ...]:
    """Направление события распространяется по непосредственным вызовам помощников.

    События ПКО — executor_profile с указателями XDTO/ОСКД; ПриОбработке —
    XDTO:3254–3258; ВыборкаДанных — отправка (XDTO:596–645,8483–8493).
    """
    event_directions = {e.name: set(e.directions) for profile in PROFILES for e in profile.events}
    event_directions["ВыборкаДанных"] = {"send"}
    event_directions["ПриОбработке"] = {"send", "receive"}
    guards = {g.logical_id: g for g in model.guards}
    directions: dict[str, set[str]] = defaultdict(set)

    def guarded(values, keys):
        result = set(values)
        for key in keys:
            if (guard := guards.get(key)) and guard.direction in ("send", "receive"):
                result &= {guard.direction}
        return result

    for rule in (*model.pko, *model.pod):
        owner = {"send", "receive"} if "both" in rule.directions else set(rule.directions)
        for event in rule.events:
            if event.target.target_id:
                directions[event.target.target_id].update(
                    guarded(
                        owner & event_directions.get(event.event, set()),
                        (*rule.guards, *event.guards),
                    )
                )
    for event in model.conversion_events:
        if event.target.target_id:
            directions[event.target.target_id].update(
                {"receive"}
                if event.event in ("ПередОтложеннымЗаполнением", "ПередОбработкойУдаляемогоОбъекта")
                else {"send", "receive"}
            )
    units = {u.logical_id: u for u in model.code_units if "dispatcher" not in u.roles}
    by_name = defaultdict(list)
    for unit in units.values():
        by_name[unit.name.casefold()].append(unit.logical_id)
    edges = {
        key: tuple(
            (target, context)
            for name, context in local_calls(unit.body)
            for target in by_name.get(name, ())
        )
        for key, unit in units.items()
    }
    pending = list(directions)
    while pending:
        key = pending.pop()
        for target, context in edges.get(key, ()):
            added = guarded(directions[key] & context, units[target].guards) - directions[target]
            if added:
                directions[target].update(added)
                pending.append(target)
    blocks = {b.logical_id: b for b in model.retained_blocks}
    return tuple(
        (
            key,
            name,
            line,
            frozenset(
                guarded(
                    context
                    & set(
                        directions.get(
                            key,
                            directions.get(blocks[key].owner_id or "", ()) if key in blocks else (),
                        )
                    ),
                    units[key].guards if key in units else blocks[key].guards,
                )
            ),
        )
        for key, body in code_texts(model)
        for name, line, context in rule_accesses(body)
    )


def property_directions(
    rule: ObjectRule, prop: Property | PropertyGroup, guards, group_guards=()
) -> set[str]:
    """Направления фактического вызова ПКС, включая охраны и пустые стороны.

    ВыгрузитьСвойство, XDTO:1442–1444; КонвертацияСвойстваСтруктурыОбъектаXDTO,
    XDTO:6724–6726; поиск правила XDTO:502–508,1543,6818.
    """
    result: set[str] = {"send", "receive"} if "both" in rule.directions else set(rule.directions)
    for key in (*rule.guards, *group_guards, *prop.guards):
        guard = guards.get(key)
        if guard and guard.direction in ("send", "receive"):
            result &= {guard.direction}
    if not prop.format_property:
        result.discard("send")
    if not prop.configuration_property:
        result.discard("receive")
    return result


def declarative_diagnostics(model: ManagerModel) -> tuple[tuple[str, str], ...]:
    """Дефекты импортированного текста диагностируются без исправления исходника."""
    addresses = model_addresses(model)
    result = []
    rules = (*model.pko, *model.pkpd)
    names = {r.name for r in rules}
    folded = {r.name.casefold() for r in rules}
    for rule in model.pko:
        for prop in (*rule.properties, *(p for g in rule.groups for p in g.properties)):
            name = prop.conversion.name
            if name and name not in names and name.casefold() in folded:
                result.append(("reference_case_mismatch", addresses[prop.logical_id]))
        if any(p.name.casefold() == rule.name.casefold() for p in model.pkpd):
            result.append(("rule_namespace_collision", addresses[rule.logical_id]))
    seen = set()
    for parameter in model.parameters:
        if parameter.name.casefold() in seen:
            result.append(("parameter_duplicate", addresses[parameter.logical_id]))
        seen.add(parameter.name.casefold())
        if not parameter.name.isidentifier():
            result.append(("parameter_name", addresses[parameter.logical_id]))
    return tuple(result)


def infer_module_styles(model: ManagerModel) -> tuple[EntityStyle, ...]:
    """Мода оформления из исходника; также адаптер для снимков до добавления module_styles."""
    members = {m.logical_id: m for m in model.members()}
    sources = {s.file_id: s.text for s in model.source_files}
    widths, fields, gaps, indents, suffixes = (defaultdict(Counter) for _ in range(5))
    seen = []
    for container in model.layouts:
        rule = members.get(container.logical_id)
        if not isinstance(rule, ObjectRule | ProcessingRule):
            continue
        kind = "pko" if isinstance(rule, ObjectRule) else "pod"
        direction = "receive" if any(d in ("receive", "both") for d in rule.directions) else "send"
        key = (kind, direction)
        if key not in seen:
            seen.append(key)
        if container.opening:
            gaps[key][container.opening.opening_blank_lines] += 1
            span = container.opening
            opening = sources[span.file_id][span.char_start : span.char_end]
            for line in opening.splitlines()[1:]:
                if " = " in line:
                    fields[(*key, "opening")][len(line.split(" = ")[0].lstrip(" \t"))] += 1
                    break
        for element in container.elements:
            span = element.source
            if span is None or element.block_id:
                continue
            member = members.get(element.entity_id or "")
            selected = ("identification", direction) if isinstance(member, Identification) else key
            if selected not in seen:
                seen.append(selected)
            text = sources[span.file_id][span.char_start : span.char_end]
            first = text.splitlines()[0] if text else ""
            prefix = first[: len(first) - len(first.lstrip(" \t"))]
            if prefix:
                indents[selected][prefix] += 1
            assignment_width = len(first.split(" = ")[0].lstrip(" \t")) if " = " in first else 0
            if assignment_width:
                widths[selected][assignment_width] += 1
                fields[(*selected, member.event if isinstance(member, Event) else element.field)][
                    assignment_width
                ] += 1
            if element.field:
                suffixes[(*selected, element.field)][span.line_suffix] += 1

    for container in model.layouts:
        rule = members.get(container.logical_id)
        if isinstance(rule, PredefinedRule) and container.opening:
            direction = rule.directions[0] if len(rule.directions) == 1 else "both"
            key = ("pkpd", direction)
            if key not in seen:
                seen.append(key)
            span = container.opening
            first = sources[span.file_id][span.char_start : span.char_end].splitlines()[0]
            prefix = first[: len(first) - len(first.lstrip(" \t"))]
            depth = 1 if direction == "both" else 2
            if prefix:
                indents[key][prefix[: max(1, len(prefix) // depth)]] += 1
        for element in container.elements:
            if (
                not isinstance(members.get(element.entity_id or ""), Parameter)
                or not element.source
            ):
                continue
            key = ("parameter", "both")
            if key not in seen:
                seen.append(key)
            span = element.source
            first = sources[span.file_id][span.char_start : span.char_end].splitlines()[0]
            prefix = first[: len(first) - len(first.lstrip(" \t"))]
            if prefix:
                indents[key][prefix] += 1

    def mode(counts, default):
        return counts.most_common(1)[0][0] if counts else default

    return tuple(
        EntityStyle(
            kind=cast(Any, kind),
            direction=cast(Any, direction),
            assignment_width=mode(widths[kind, direction], 0),
            field_widths=tuple(
                (f, mode(c, 0)) for (k, d, f), c in fields.items() if (k, d) == (kind, direction)
            ),
            opening_blank_lines=mode(gaps[kind, direction], 0),
            indent=mode(indents[kind, direction], model.header.text_style.indent),
            line_suffixes=tuple(
                (f, mode(c, "")) for (k, d, f), c in suffixes.items() if (k, d) == (kind, direction)
            ),
        )
        for kind, direction in seen
    )


def import_expression(expression: reader.Expr | None) -> Value:
    if expression is None or not expression.raw:
        return Value()
    if expression.literal_type == "number" and "." in expression.raw:
        return Value("number", expression.raw.strip())
    if expression.literal_type == "date" and not str(expression.literal_value).isdigit():
        return Value("unknown", raw=expression.raw)
    if expression.literal_type in ("string", "boolean", "number", "date", "undefined"):
        return Value(cast(ValueState, expression.literal_type), expression.literal_value)
    if expression.reference_parts and expression.reference_parts[0].casefold() == "метаданные":
        return Value("reference", reference_parts=expression.reference_parts)
    return Value("unknown", raw=expression.raw, reference_parts=expression.reference_parts)


def import_field(field: reader.Field) -> Value:
    if field.presence == "absent":
        return Value()
    if field.presence == "ambiguous":
        return Value("unknown", alternatives=tuple(import_expression(e) for e in field.assignments))
    if field.assignments:
        return import_expression(field.assignments[0])
    return Value("unknown")


def import_signature(routine: reader.Routine) -> Signature:
    return Signature(
        cast(Literal["procedure", "function"], routine.routine_kind),
        routine.exported,
        tuple(Formal(p.name, p.by_value, import_expression(p.default)) for p in routine.parameters),
    )


def _parameter_value(tokens) -> Value:
    """Только типизированный литерал; произвольное BSL-выражение остаётся непрозрачным."""
    if len(tokens) == 1:
        token = tokens[0]
        if token.kind == "date" and not token.value.isdigit():
            return Value("unknown", raw="'" + token.value + "'")
        if token.kind in ("string", "date"):
            return Value(cast(ValueState, token.kind), token.value)
        if token.kind == "number":
            return Value("number", token.value if "." in token.value else int(token.value))
        if token.folded in ("истина", "ложь"):
            return Value("boolean", token.folded == "истина")
        if token.folded == "неопределено":
            return Value("undefined")
    if len(tokens) == 2 and tokens[0].value in ("-", "+") and tokens[1].kind == "number":
        value = _parameter_value(tokens[1:])
        assert isinstance(value.value, str | int | float)
        if isinstance(value.value, str):
            return Value("number", ("-" if tokens[0].value == "-" else "") + value.value)
        return Value("number", value.value * (-1 if tokens[0].value == "-" else 1))
    if (
        tokens
        and tokens[0].value in ("Метаданные", "Перечисления", "Справочники")
        and all(
            t.kind == "identifier" if n % 2 == 0 else t.value == "." for n, t in enumerate(tokens)
        )
        and len(tokens) % 2
    ):
        return Value("reference", reference_parts=tuple(t.value for t in tokens[::2]))
    return Value("unknown", raw=" ".join(t.value for t in tokens))


def import_manager(
    document: reader.EdDocument,
    *,
    project_id: str,
    manager_name: str = "Manager",
    host: Host | None = None,
    format_bindings: tuple[FormatBinding, ...] = (),
    executor_profile: ExecutorProfile | None = None,
) -> tuple[ManagerModel, ImportReport]:
    """Ключи маршрутов задаёт вызывающий: текстовые упоминания версий ими не становятся."""
    return _Importer(document, project_id).build(
        manager_name, host or Host(), format_bindings, executor_profile or ExecutorProfile()
    )


class _LayoutOverlap(ValueError):
    """Границы перекрытия позволяют сохранить метод вместо всего модуля."""

    def __init__(self, file_id: str, start: int, end: int):
        super().__init__("Перекрытие элементов раскладки")
        self.file_id, self.start, self.end = file_id, start, end


class _Importer:
    def __init__(self, document: reader.EdDocument, project_id: str):
        self.document = document
        self.project_id = project_id
        self.addresses = build_addresses(document)
        self.sources = {s.file_id: s for s in document.files}
        self.ids: dict[str, str] = {}
        self.entries: list[ImportEntry] = []
        self.source_map: list[SourceMapEntry] = []
        self.blocks: list[RetainedBlock] = []
        self.block_spans: dict[str, tuple[str, int, int]] = {}
        self.blocked_ids = {
            entity.entity_id
            for entity in document.entities()
            if entity.span.file_id not in self.sources
            or not 0
            <= entity.span.char_start
            <= entity.span.char_end
            <= len(self.sources[entity.span.file_id].text)
            or self.sources[entity.span.file_id].text[entity.span.char_start : entity.span.char_end]
            != entity.raw_text
        }
        self.occurrences: dict[str, int] = defaultdict(int)
        self.routines = {r.name.casefold(): r for r in document.routines}
        self.reader_guards = {g.entity_id: g for g in document.guards}
        self.statements = {s.file_id: lex(s).statements for s in document.files}
        self.statement_starts = {
            key: [s.span.char_start for s in rows] for key, rows in self.statements.items()
        }
        self.guards_at = defaultdict(list)
        for guard in document.guards:
            self.guards_at[guard.span.file_id, guard.span.char_start].append(guard)
        self.contexts = {}
        self.unknown_starts = defaultdict(list)
        for unknown in document.unknown:
            self.unknown_starts[unknown.span.file_id].append(unknown.span.char_start)
        for rows in self.unknown_starts.values():
            rows.sort()
        for file_id, rows in self.statements.items():
            active = []
            for statement in rows:
                active = [g for g in active if g.span.char_end > statement.span.char_start]
                active.extend(self.guards_at[file_id, statement.span.char_start])
                self.contexts[file_id, statement.span.char_start] = tuple(active)
        self.unverified_pks = any(
            d.code == "helper_semantics_unverified"
            and (
                d.owner_id == self.routines.get("добавитьпкс", d).entity_id
                or "добавитьпкс" in d.raw_text.casefold()
            )
            for d in document.diagnostics
        )

        # Z13:672–690; §11.13.3: проверяются все токены обоих определений.
        def normalized(text):
            return tuple(
                (t.kind, t.value if t.kind == "string" else t.folded)
                for t in tokenize(text)
                if t.kind != "comment"
            )

        variants = {}
        for helper in ("ДобавитьПКС", "ДобавитьПКТЧ"):
            definitions = [r for r in document.routines if r.name.casefold() == helper.casefold()]
            if len(definitions) != 1:
                variants[helper.casefold()] = None
                continue
            actual = normalized(definitions[0].raw_text)
            modern = helper_forms(helper, document.manager_version)
            legacy = modern[0].replace(', ПространствоИмен = ""', "")
            legacy = "\n".join(
                line for line in legacy.split("\n") if ".ПространствоИмен =" not in line
            )
            variants[helper.casefold()] = (
                "modern"
                if any(actual == normalized(form) for form in modern)
                else "legacy-v2"
                if actual == normalized(legacy)
                else None
            )
        present_variants = set(variants.values())
        self.helper_variant = next(iter(present_variants)) if len(present_variants) == 1 else None
        # Модуль без групп может иметь только помощник ПКС (старый авторский оригинал).
        if not document.routines or "добавитьпктч" not in self.routines:
            self.helper_variant = variants["добавитьпкс"]
        self.unverified_pks = "добавитьпкс" in self.routines and variants["добавитьпкс"] is None
        if all(v is not None for v in variants.values()) and self.helper_variant is None:
            self.unverified_pks = True
        self.unverified_pktch = variants["добавитьпктч"] is None or self.helper_variant is None
        if not any(name in self.routines for name in variants):
            self.helper_variant = "modern"
        self.module_identifier = Value()
        self.identifier_routine = None
        identifier = self.routines.get("подключаемый_идентификатормодуля")
        if identifier is not None:
            rows = [
                s
                for s in self.statements[identifier.span.file_id]
                if identifier.body_span.char_start
                <= s.span.char_start
                < identifier.body_span.char_end
            ]
            if (
                identifier.routine_kind == "function"
                and identifier.exported
                and not identifier.parameters
                and len(rows) == 1
                and len(rows[0].tokens) == 3
                and rows[0].head == "возврат"
                and rows[0].tokens[1].kind == "string"
            ):
                self.module_identifier = Value("string", rows[0].tokens[1].value)
                self.identifier_routine = identifier.entity_id
        self.clear_data_column = any(
            normalized(s.raw_text) == normalized(POD_COLUMN_STATEMENT)
            for rows in self.statements.values()
            for s in rows
        )
        self.directions: dict[str, tuple] = {}
        self.predefined_bounds: dict[str, tuple[int, int]] = {}
        for rule in (*document.pko, *document.pod):
            self.directions[rule.entity_id] = tuple(
                dict.fromkeys(
                    use.direction
                    for use in document.rule_uses
                    if use.rule_id == rule.entity_id and use.direction is not None
                )
            )
        for entity in document.entities():
            address = self.address(entity)
            identity = address
            if isinstance(entity, reader.ObjectRule | reader.ProcessingRule):
                identity = f"{entity.kind}/{entity.name}/{entity.procedure_name}"
            elif entity.kind in (
                "unknown",
                "guard",
                "use",
                "case",
                "diagnostic",
                "binding",
                "version",
            ):
                identity = f"{entity.kind}/{entity.name}"
            self.occurrences[identity] += 1
            self.ids[entity.entity_id] = logical_id(
                project_id, f"import/{identity}/{self.occurrences[identity]}"
            )
        # Правило и описывающая его процедура — одна декларация, не два тела.
        for rule in (*document.pko, *document.pod):
            routine = self.routines.get(rule.procedure_name.casefold())
            if routine:
                self.ids[routine.entity_id] = self.ids[rule.entity_id]
        self.ids["conversion"] = self.ids[document.conversion.entity_id]
        self.guard_ids = {
            g
            for e in document.entities()
            if e.kind not in ("routine", "guard", "diagnostic")
            for g in e.guards
        }
        for rule in (*document.pko, *document.pod):
            for field_name in (
                "configuration_object",
                "format_object",
                "group_flag",
                "identification",
                "configuration_selection",
                "format_selection",
                "clear_data",
            ):
                field = getattr(rule, field_name, None)
                if field:
                    self.guard_ids.update(
                        g for expression in field.assignments for g in expression.guards
                    )
            # В RuleRef/расширении читатель не отдаёт guards; восстанавливаем
            # только факт наличия контекста по границам уже прочитанных условий.
            for guard in document.guards:
                if rule.span.char_start < guard.span.char_start < rule.span.char_end:
                    self.guard_ids.add(guard.entity_id)
        pending = list(self.guard_ids)
        # Пустая ветка заполнителя тоже часть рамки: смена направлений не
        # должна превращать её при повторном чтении в другой вид сущности.
        for guard in document.guards:
            if guard.known_direction and any(
                r.name in ENTRYPOINTS
                and r.body_span.char_start <= guard.span.char_start < r.body_span.char_end
                for r in document.routines
            ):
                self.guard_ids.add(guard.entity_id)
                pending.append(guard.entity_id)
        guards = {g.entity_id: g for g in document.guards}
        while pending:
            parent = guards[pending.pop()].parent_id
            if parent and parent not in self.guard_ids:
                self.guard_ids.add(parent)
                pending.append(parent)
        # Условия диспетчера выводятся из веток, а не являются непрозрачными guards кода.
        self.guard_ids.difference_update(g for c in document.dispatcher_cases for g in c.guards)

    def address(self, entity: reader.Entity) -> str:
        return self.addresses.by_id.get(entity.entity_id, (f"{entity.kind}/{entity.name}",))[0]

    def common(self, entity: reader.Entity, ordinal: int, state: ImportState) -> dict:
        if entity.entity_id in self.blocked_ids:
            state = "blocked"
        source = self.sources[entity.span.file_id]
        span = entity.span
        self.source_map.append(
            SourceMapEntry(
                entity.entity_id,
                self.ids[entity.entity_id],
                self.address(entity),
                source.file_id,
                source.sha256,
                span.char_start,
                span.char_end,
                span.line_start,
                span.line_end,
            )
        )
        self.entries.append(
            ImportEntry(
                entity.kind,
                self.address(entity),
                self.ids[entity.entity_id],
                state,
                "unproven_source_span"
                if state == "blocked"
                else "outside_w1"
                if state == "retained"
                else "",
            )
        )
        return dict(
            logical_id=self.ids[entity.entity_id],
            name=entity.name,
            state=state,
            guards=tuple(self.ids[g] for g in entity.guards),
        )

    def retain(
        self,
        entity: reader.Entity,
        owner: str | None = None,
        *,
        reason: str = "outside_w1",
        lock: bool = False,
    ) -> None:
        source = self.sources[entity.span.file_id]
        key = self.ids.setdefault(
            entity.entity_id, logical_id(self.project_id, "source/" + entity.entity_id)
        )
        self.blocks.append(
            RetainedBlock(
                logical_id=logical_id(self.project_id, "block/" + key),
                name=entity.name,
                state="blocked" if entity.entity_id in self.blocked_ids else "retained",
                kind=entity.kind,
                text=entity.raw_text,
                sha256=text_hash(entity.raw_text),
                file_id=source.file_id,
                source_hash=source.sha256,
                owner_id=owner or key,
                reason=reason,
                regions=entity.regions,
                tags=entity.tag_ids,
                guards=tuple(self.ids[g] for g in entity.guards),
                locks_context=lock,
                char_start=entity.span.char_start,
                char_end=entity.span.char_end,
            )
        )

        self.block_spans[self.blocks[-1].logical_id] = (
            source.file_id,
            entity.span.char_start,
            entity.span.char_end,
        )

    def reference(
        self, kind: str, target: str | None, name: str, resolution: str = "missing"
    ) -> Reference:
        resolved = self.ids.get(target or "")
        return Reference(
            kind,
            resolved,
            name,
            cast(
                Literal["resolved", "missing", "ambiguous", "computed"],
                "resolved" if resolved else resolution,
            ),
        )

    def unsafe(self, entity: reader.Entity) -> bool:
        unknowns = self.unknown_starts[entity.span.file_id]
        n = bisect_left(unknowns, entity.span.char_start)
        return entity.entity_id in self.blocked_ids or (
            entity.status != reader.ParseStatus.COMPLETE
            or any(not self.exact_direction(self.reader_guards[g]) for g in entity.guards)
            or (n < len(unknowns) and unknowns[n] < entity.span.char_end)
        )

    @staticmethod
    def exact_direction(guard: reader.Guard) -> bool:
        tokens = tokenize(guard.expression_raw)
        return (
            guard.guard_kind == "direction"
            and len(tokens) == 3
            and tokens[0].folded == "направлениеобмена"
            and tokens[1].value == "="
            and tokens[2].kind == "string"
            and tokens[2].value in ("Отправка", "Получение")
        )

    def rule_statements(self, rule: reader.Entity):
        rows = self.statements[rule.span.file_id]
        positions = self.statement_starts[rule.span.file_id]
        return rows[
            bisect_left(positions, rule.span.char_start) : bisect_left(
                positions, rule.span.char_end
            )
        ]

    def predefined(self, rule: reader.PredefinedRule, ordinal: int) -> PredefinedRule:
        # reader.py:891–899 оставляет рамку направления в диапазоне последнего ПКПД.
        # В раскладку писателя входят лишь операторы самого правила; SourceMap исходный.
        rows = []
        for statement in self.rule_statements(rule):
            if statement.head in ("если", "иначеесли", "конецесли"):
                break
            rows.append(statement)
        self.predefined_bounds[self.ids[rule.entity_id]] = (
            rule.span.char_start,
            rows[-1].span.char_end if rows else rule.span.char_start,
        )
        configuration = import_field(rule.configuration_type)
        format_type = import_field(rule.format_type)
        mappings = []
        supported = (
            not self.unsafe(rule)
            and configuration.state == "reference"
            and len(configuration.reference_parts) == 3
            and configuration.reference_parts[1] in ("Перечисления", "Справочники")
            and format_type.state == "string"
        )
        # Карта генератора заканчивается присваиванием соответствия правилу.
        # Частичная форма читателя и чужие операторы в шапке не порождаются.
        variables = {
            "значениядляотправки": "конвертациизначенийприотправке",
            "значениядляполучения": "конвертациизначенийприполучении",
        }
        mapping_positions = {v.span.char_start for v in rule.mappings}
        assigned = set()
        starts = {}
        for statement in rows:
            tokens = statement.tokens
            folded = [t.folded for t in tokens]
            if folded == [
                "правилоконвертации",
                "=",
                "правилаконвертации",
                ".",
                "добавить",
                "(",
                ")",
                ";",
            ]:
                continue
            if len(tokens) >= 6 and folded[:2] == ["правилоконвертации", "."] and folded[3] == "=":
                field = folded[2]
                if field in ("имяпкпд", "типданных", "типxdto"):
                    supported &= field not in assigned and not starts
                    assigned.add(field)
                    continue
                if len(tokens) == 6 and folded[4] in variables and variables[folded[4]] == field:
                    supported &= starts.get(folded[4], False)
                    starts[folded[4]] = False
                    continue
            if statement.span.char_start in mapping_positions:
                supported &= starts.get(statement.head, False)
                continue
            if statement.head in variables and folded[1:] == ["=", "новый", "соответствие", ";"]:
                supported &= statement.head not in starts
                starts[statement.head] = True
                continue
            supported = False
        supported &= assigned == {"имяпкпд", "типданных", "типxdto"} and not any(starts.values())
        for n, item in enumerate(rule.mappings, 1):
            value = import_expression(item.configuration_value)
            if (
                item.configuration_value.reference_parts
                and item.configuration_value.reference_parts[0] in ("Перечисления", "Справочники")
            ):
                value = Value("reference", reference_parts=item.configuration_value.reference_parts)
            format_value = import_expression(item.format_value)
            supported &= value.state == "reference" and format_value.state == "string"
            mappings.append((item, n, value, format_value))
        state: ImportState = "editable" if supported else "retained"
        directions = tuple(
            dict.fromkeys(
                self.reader_guards[g].known_direction
                for g in rule.guards
                if self.reader_guards[g].known_direction
            )
        ) or ("both",)
        result = PredefinedRule(
            **self.common(rule, ordinal, state),
            directions=cast(tuple[Direction, ...], directions),
            data_kind="predefined"
            if "Справочники" in configuration.reference_parts
            else "enumeration",
            configuration_type=configuration,
            format_type=format_type,
            mappings=tuple(
                ValueMapping(
                    **self.common(item, n, state),
                    configuration_value=value,
                    format_value=format_value,
                    direction=cast(Direction, item.direction),
                )
                for item, n, value, format_value in mappings
            ),
        )
        if state != "editable":
            self.retain(rule)
            self.entries = [
                replace(e, reason="unsupported_predefined_form")
                if e.logical_id == result.logical_id
                else e
                for e in self.entries
            ]
        return result

    def parameters(self) -> tuple[Parameter, ...]:
        routine = self.routines.get("заполнитьпараметрыконвертации")
        if routine is None:
            return ()
        read = {p.span.char_start: p for p in self.document.parameters}
        result = []
        for statement in self.rule_statements(routine):
            tokens = statement.tokens
            if (
                len(tokens) < 7
                or [t.folded for t in tokens[:4]] != ["параметрыконвертации", ".", "вставить", "("]
                or [t.value for t in tokens[-2:]] != [")", ";"]
            ):
                continue
            arguments = split_arguments(tokens[4:-2])
            if (
                len(arguments) not in (1, 2)
                or len(arguments[0]) != 1
                or arguments[0][0].kind != "string"
            ):
                continue
            item = read.get(statement.span.char_start)
            if item is None:
                item = reader.Parameter(
                    entity_id=f"writer:parameter:{statement.span.char_start}",
                    kind="parameter",
                    name=arguments[0][0].value,
                    span=statement.span,
                    raw_text=statement.raw_text,
                    guards=tuple(
                        g.entity_id
                        for g in self.contexts[statement.span.file_id, statement.span.char_start]
                    ),
                )
                self.ids[item.entity_id] = logical_id(
                    self.project_id, f"import/parameter/{item.name}/{len(result) + 1}"
                )
            default = _parameter_value(arguments[1]) if len(arguments) == 2 else Value()
            state: ImportState = (
                "editable" if default.state != "unknown" and not item.guards else "retained"
            )
            result.append(
                Parameter(
                    **self.common(item, len(result) + 1, state),
                    default=default,
                    default_source="explicit" if len(arguments) == 2 else "implicit",
                )
            )
        return tuple(result)

    def header_groups(self, rule):
        """Охрана интерфейса 3 из ObjectModule.bsl:2300–2304, непосредственно перед ПКС."""
        if self.document.manager_version != 3 or not isinstance(rule, reader.ObjectRule):
            return ()
        rows = self.rule_statements(rule)
        return tuple(
            tuple(rows[n : n + 3])
            for n in range(len(rows) - 3)
            if [t.folded for t in rows[n].tokens] == ["если", "толькозаголовки", "тогда"]
            and [t.folded for t in rows[n + 1].tokens] == ["возврат", ";"]
            and [t.folded for t in rows[n + 2].tokens] == ["конецесли", ";"]
            and [t.folded for t in rows[n + 3].tokens]
            == ["свойствашапки", "=", "правилоконвертации", ".", "свойства", ";"]
        )

    def rule_unsafe(self, rule: reader.ObjectRule | reader.ProcessingRule) -> bool:
        if self.unsafe(rule):
            return True
        rows = self.rule_statements(rule)
        source = self.sources[rule.span.file_id].text
        if any(
            "\n" not in source[left.span.char_end : right.span.char_start]
            and not (left.head == right.head == "добавитьпкс")
            for left, right in pairwise(rows)
        ):
            # Совместная строка каркаса/поля не имеет независимой редактируемой формы.
            return True
        if any(
            import_expression(p.default).state == "unknown"
            for p in self.routines[rule.procedure_name.casefold()].parameters
        ):
            return True
        for name in (
            "configuration_object",
            "format_object",
            "group_flag",
            "identification",
            "configuration_selection",
            "format_selection",
            "clear_data",
        ):
            field = getattr(rule, name, None)
            if field and import_field(field).state == "unknown":
                return True
            if field and any(set(e.guards) - set(rule.guards) for e in field.assignments):
                return True
        if isinstance(rule, reader.ObjectRule) and any(
            set(s.guards) - set(rule.guards) for s in rule.search_sets
        ):
            return True
        header_rows = {s.span.char_start for group in self.header_groups(rule) for s in group}
        for statement in self.rule_statements(rule):
            if statement.span.char_start in header_rows:
                continue
            tokens = statement.tokens
            context = self.contexts[statement.span.file_id, statement.span.char_start]
            conditional = any(
                g.entity_id not in rule.guards
                and g.span.char_start <= statement.span.char_start < g.span.char_end
                for g in context
            )
            if conditional and statement.head not in ("если", "иначеесли", "иначе", "конецесли"):
                calls = {t.folded for t in tokens if t.kind == "identifier"}
                if not calls & {"добавитьпкс", "добавитьпктч"}:
                    return True
            if tokens[0].kind == "directive" or statement.head == "возврат":
                # В W1 нет editable-формы раннего возврата/препроцессора правила.
                return True
            if statement.head in ("если", "иначеесли"):
                guards = [
                    g
                    for g in self.guards_at[statement.span.file_id, statement.span.char_start]
                    if g.span.char_start == statement.span.char_start
                ]
                if not guards or not all(self.exact_direction(g) for g in guards):
                    return True
            extension = "инициализироватьрасширениеправилаконвертацииобъекта"
            if any(t.folded == extension for t in tokens):
                exact = [t.folded if t.kind != "string" else "<string>" for t in tokens]
                if exact != [
                    "обменданнымиxdtoсервер",
                    ".",
                    extension,
                    "(",
                    "правилоконвертации",
                    ",",
                    "<string>",
                    ")",
                    ";",
                ]:
                    return True
                if any(
                    g.entity_id not in rule.guards
                    and g.span.char_start <= statement.span.char_start < g.span.char_end
                    for g in context
                ):
                    return True
            if (
                isinstance(rule, reader.ProcessingRule)
                and "используемыепко" in statement.raw_text.casefold()
                and any(
                    g.entity_id not in rule.guards
                    and g.span.char_start <= statement.span.char_start < g.span.char_end
                    for g in context
                )
            ):
                return True
        return False

    def events(self, bindings: tuple[reader.HandlerBinding, ...]) -> tuple[Event, ...]:
        return tuple(
            Event(
                **self.common(e, n, "editable"),
                event=e.event,
                target=self.reference("code_unit", e.target_id, e.target_name, e.resolution),
            )
            for n, e in enumerate(bindings, 1)
        )

    def property(
        self,
        item: reader.PropertyRule,
        ordinal: int,
        owner: str,
        *,
        in_group: bool = False,
        unsafe: bool = False,
    ) -> Property:
        kind = (
            "algorithm"
            if item.algorithm_flag
            else "reference"
            if item.conversion_rule
            else "direct"
        )
        state: ImportState = (
            "editable"
            if not unsafe
            and not self.unsafe(item)
            and not self.unverified_pks
            and (self.helper_variant != "legacy-v2" or len(item.argument_presence) <= 5)
            and (
                (kind == "direct" and not in_group)
                or (not item.namespace and not item.condition_name)
            )
            and tokenize(item.raw_text)[0].folded == "добавитьпкс"
            else "retained"
        )
        candidates = [
            r for r in (*self.document.pko, *self.document.pkpd) if r.name == item.conversion_rule
        ]
        target = candidates[0] if len(candidates) == 1 else None
        conversion = self.reference(
            target.kind if target else "conversion",
            target.entity_id if target else None,
            item.conversion_rule,
            "ambiguous" if len(candidates) > 1 else "missing",
        )
        if kind == "reference" and conversion.kind == "pkpd":
            kind = "pkpd"
        result = Property(
            **self.common(item, ordinal, state),
            configuration_property=item.configuration_property,
            format_property=item.format_property,
            property_kind=cast(Literal["direct", "reference", "pkpd", "algorithm"], kind),
            algorithm_flag=item.algorithm_flag,
            conversion=conversion,
            namespace=item.namespace,
            condition_name=item.condition_name,
            argument_presence=item.argument_presence,
            argument_values=tuple(import_expression(e) for e in item.raw_arguments[1:]),
        )
        if state != "editable":
            self.retain(item, owner)
            if self.unverified_pks:
                self.entries[-1] = replace(self.entries[-1], reason="helper_semantics_unverified")
            elif self.helper_variant == "legacy-v2" and len(item.argument_presence) > 5:
                self.entries[-1] = replace(self.entries[-1], reason="helper_arguments_mismatch")
        return result

    def object_rule(self, rule: reader.ObjectRule, ordinal: int) -> ObjectRule:
        unsafe = self.rule_unsafe(rule)
        common = self.common(rule, ordinal, "retained" if unsafe else "editable")
        key = self.ids[rule.entity_id]
        searches = tuple(
            SearchSet(**self.common(s, n, "retained" if unsafe else "editable"), fields=s.fields)
            for n, s in enumerate(rule.search_sets, 1)
        )
        identification = Identification(
            logical_id=logical_id(self.project_id, "identification/" + key),
            name="Идентификация",
            state="retained" if unsafe else "editable",
            mode=import_field(rule.identification),
            search_sets=searches,
        )
        self.entries.append(
            ImportEntry(
                "identification",
                self.address(rule) + "/Идентификация",
                identification.logical_id,
                identification.state,
            )
        )
        properties = tuple(
            self.property(p, n, key, unsafe=unsafe) for n, p in enumerate(rule.properties, 1)
        )
        groups = []
        for n, group in enumerate(rule.groups, 1):
            call_tokens = tuple(t for t in tokenize(group.raw_text) if t.kind != "comment")
            opening = next(i for i, t in enumerate(call_tokens) if t.value == "(")
            closing = len(call_tokens) - (2 if call_tokens[-1].value == ";" else 1)
            arguments = split_arguments(call_tokens[opening + 1 : closing])
            presence = tuple(bool(a) for a in arguments)
            valid_names = all(
                not name
                or (
                    (name[0].isalpha() or name[0] == "_")
                    and all(c.isalnum() or c == "_" for c in name)
                )
                for name in (group.configuration_property, group.format_property)
            )
            supported = (
                not unsafe
                and valid_names
                and not self.unsafe(group)
                and not self.unverified_pks
                and not self.unverified_pktch
                and not group.namespace
                and not group.condition_name
                and presence == (True, True, True)
            )
            groups.append(
                PropertyGroup(
                    **self.common(group, n, "editable" if supported else "retained"),
                    configuration_property=group.configuration_property,
                    format_property=group.format_property,
                    namespace=group.namespace,
                    condition_name=group.condition_name,
                    argument_presence=presence,
                    properties=tuple(
                        self.property(
                            p, i, self.ids[group.entity_id], in_group=True, unsafe=not supported
                        )
                        for i, p in enumerate(group.properties, 1)
                    ),
                )
            )
            if not supported:
                self.retain(group, key)
                self.entries = [
                    replace(
                        e,
                        reason="invalid_table_part_name"
                        if not valid_names
                        else "helper_semantics_unverified"
                        if self.unverified_pktch
                        else "unsupported_table_part_form",
                    )
                    if e.logical_id == self.ids[group.entity_id]
                    else e
                    for e in self.entries
                ]
        if unsafe:
            self.retain(rule, reason="unsafe_declaration", lock=True)
        routine = self.routines[rule.procedure_name.casefold()]
        return ObjectRule(
            **common,
            procedure_name=rule.procedure_name,
            signature=import_signature(routine),
            directions=self.directions[rule.entity_id],
            configuration_object=import_field(rule.configuration_object),
            format_object=import_field(rule.format_object),
            group_flag=import_field(rule.group_flag),
            identification=identification,
            properties=properties,
            groups=tuple(groups),
            events=self.events(rule.events),
            extensions=rule.extensions,
        )

    def build_layout(self, model: ManagerModel) -> ManagerModel:
        """Локальное перекрытие сохраняет метод; пересечение рамок — весь файл."""
        self.layout_overlap_routines = set()
        while True:
            try:
                result = self._build_layout(model)
                validate_model(result)
                return result
            except ValueError as error:
                if str(error) not in (
                    "Перекрытие элементов раскладки",
                    "Исходные листья раскладки перекрываются",
                ):
                    raise
                routine = (
                    next(
                        (
                            r
                            for r in self.document.routines
                            if r.span.file_id == error.file_id
                            and r.span.char_start <= error.start
                            and error.end <= r.span.char_end
                            and r.entity_id not in self.layout_overlap_routines
                        ),
                        None,
                    )
                    if isinstance(error, _LayoutOverlap)
                    else None
                )
                if routine is None:
                    break
                self.layout_overlap_routines.add(routine.entity_id)
                key = self.ids[routine.entity_id]
                source = self.sources[routine.span.file_id]
                body = source.text[routine.body_span.char_start : routine.body_span.char_end]
                model = replace(
                    model,
                    code_units=tuple(
                        replace(
                            u,
                            state="retained",
                            body=body,
                            sha256=text_hash(body),
                            parameters_text=None,
                            frame_comment="",
                        )
                        if u.logical_id == key
                        else u
                        for u in model.code_units
                    ),
                )
                self.entries = [
                    replace(e, state="retained", reason="layout_overlap")
                    if e.logical_id == key
                    else e
                    for e in self.entries
                ]
        roots, layouts, blocks, leaves = [], [], [], {}
        for source in model.source_files:
            key = logical_id(self.project_id, "module/" + source.file_id)
            leaf = logical_id(self.project_id, "overlap/" + source.file_id)
            leaves[source.file_id] = leaf
            roots.append(key)
            blocks.append(
                RetainedBlock(
                    logical_id=leaf,
                    name="Перекрывающиеся рамки",
                    state="retained",
                    kind="layout_overlap",
                    text=source.text,
                    sha256=text_hash(source.text),
                    file_id=source.file_id,
                    source_hash=source.sha256,
                    owner_id=key,
                    reason="layout_overlap",
                    locks_context=True,
                    char_end=len(source.text),
                )
            )
            layouts.append(
                LayoutContainer(
                    key,
                    "module",
                    "Manager",
                    elements=(
                        LayoutElement(
                            leaf,
                            "text",
                            block_id=leaf,
                            source=SourceSlice(source.file_id, 0, len(source.text)),
                        ),
                    ),
                )
            )
        mapped = {e.logical_id: e for e in model.source_map}
        routines = {self.ids[r.entity_id]: r for r in self.document.routines}

        def retained(member):
            entry = mapped.get(member.logical_id)
            updates = (
                {"state": "retained", "inside_leaf_id": leaves[entry.file_id]} if entry else {}
            )
            for field in fields(member):
                value = getattr(member, field.name)
                if isinstance(value, Member):
                    updates[field.name] = retained(value)
                elif isinstance(value, tuple) and value and isinstance(value[0], Member):
                    updates[field.name] = tuple(retained(m) for m in value)
            if isinstance(member, CodeUnit):
                routine = routines[member.logical_id]
                body = self.sources[routine.span.file_id].text[
                    routine.body_span.char_start : routine.body_span.char_end
                ]
                updates.update(
                    body=body, sha256=text_hash(body), parameters_text=None, frame_comment=""
                )
            return replace(member, **updates)

        self.entries = [replace(e, state="retained", reason="layout_overlap") for e in self.entries]
        return replace(
            model,
            layouts=tuple(layouts),
            root_layouts=tuple(roots),
            retained_blocks=tuple(blocks),
            **{
                key: tuple(retained(m) for m in getattr(model, key))
                for key in (
                    "pko",
                    "pod",
                    "pkpd",
                    "parameters",
                    "code_units",
                    "conversion_events",
                    "rule_uses",
                    "guards",
                    "dispatcher_cases",
                )
            },
        )

    def _build_layout(self, model: ManagerModel) -> ManagerModel:
        """Разбиение строится сверху вниз; вложенные представления не выводятся."""
        doc = self.document
        members = {m.logical_id: m for m in model.members()}
        mapped = {e.logical_id: e for e in model.source_map}
        overlap_members = {e.logical_id for e in self.entries if e.reason == "layout_overlap"}
        blocks: list[RetainedBlock] = []
        containers: list[LayoutContainer] = []
        roots = []
        comments = {}
        procedures = {
            r.procedure_name.casefold(): (r.entity_id, r.name, r.kind) for r in (*doc.pko, *doc.pod)
        }
        # reader-ID в таблице имён переводится в устойчивый ID ровно один раз.
        procedures = {
            name: (self.ids[key], title, kind) for name, (key, title, kind) in procedures.items()
        }
        procedures.update(
            {u.name.casefold(): (u.logical_id, u.name, "code_unit") for u in model.code_units}
        )
        tokens_by_file = {s.file_id: lex(s).tokens for s in doc.files}
        token_starts = {key: [t.start for t in rows] for key, rows in tokens_by_file.items()}
        header_token = None
        for token in tokens_by_file[doc.files[0].file_id]:
            if token.kind != "comment":
                break
            if token.value.startswith(
                "// Менеджер обмена через универсальный формат ("
            ) and token.value.endswith(")"):
                header_token = token
                break
        rules = {r.procedure_name.casefold(): r for r in (*model.pko, *model.pod)}
        reader_rules = {r.procedure_name.casefold(): r for r in (*doc.pko, *doc.pod)}
        uses_by_routine = defaultdict(list)
        routine_starts = [r.span.char_start for r in doc.routines]
        for use in doc.rule_uses:
            n = bisect_right(routine_starts, use.span.char_start) - 1
            if n >= 0:
                uses_by_routine[doc.routines[n].entity_id].append(use)

        def span(file_id, left, right):
            return SourceSlice(file_id, left, right)

        def line_range(file_id, left, right):
            text = self.sources[file_id].text
            start = text.rfind("\n", 0, left) + 1
            if text[start:left].strip():
                start = left
            end = text.find("\n", right)
            # На одной строке могут быть несколько операторов: их не склеиваем.
            tail = text[right : end if end >= 0 else len(text)].strip()
            if tail and not tail.startswith("//"):
                return start, right
            return start, end + 1 if end >= 0 else len(text)

        def text_leaf(file_id, left, right, owner, kind="trivia", entity=None, lock=False):
            key = logical_id(self.project_id, f"leaf/{file_id}/{left}/{right}")
            text = self.sources[file_id].text[left:right]
            rows = tokens_by_file[file_id]
            starts = token_starts[file_id]
            tokens = rows[bisect_left(starts, left) : bisect_left(starts, right)]
            deps = []
            for n, token in enumerate(tokens[:-1]):
                target = procedures.get(token.folded)
                if (
                    target
                    and (
                        token.kind == "string"
                        or (token.kind == "identifier" and tokens[n + 1].value == "(")
                    )
                    and (not n or tokens[n - 1].folded not in ("процедура", "функция"))
                    and target[0] != owner
                ):
                    deps.append(Reference(target[2], target[0], target[1], "resolved"))
            blocks.append(
                RetainedBlock(
                    logical_id=key,
                    name=members[entity].name if entity in members else "Текст",
                    state="retained",
                    kind=kind,
                    text=text,
                    sha256=text_hash(text),
                    file_id=file_id,
                    source_hash=self.sources[file_id].sha256,
                    owner_id=owner,
                    char_start=left,
                    char_end=right,
                    locks_context=lock,
                    dependencies=tuple(dict.fromkeys(deps)),
                )
            )
            return LayoutElement(
                key,
                "entity" if entity else "text",
                entity_id=entity,
                block_id=key,
                source=span(file_id, left, right),
            )

        def gaps(file_id, left, right, owner):
            result = []
            text = self.sources[file_id].text
            while left < right:
                end = text.find("\n", left, right)
                end = end + 1 if end >= 0 else right
                if (
                    header_token is not None
                    and file_id == doc.files[0].file_id
                    and left <= header_token.start
                    and header_token.end <= end
                ):
                    result.append(
                        LayoutElement(
                            logical_id(self.project_id, "header/title"),
                            "entity",
                            entity_id=self.ids[doc.conversion.entity_id],
                            field="header.title",
                            source=span(file_id, left, end),
                        )
                    )
                else:
                    rule = members.get(owner)
                    if (
                        isinstance(rule, ObjectRule)
                        and text[left:end] in ("\n", "\r\n")
                        and rule.groups
                        and (
                            line_range(
                                file_id,
                                max(
                                    (
                                        mapped[p.logical_id].char_end
                                        for p in rule.groups[-1].properties
                                    ),
                                    default=mapped[rule.groups[-1].logical_id].char_end,
                                ),
                                max(
                                    (
                                        mapped[p.logical_id].char_end
                                        for p in rule.groups[-1].properties
                                    ),
                                    default=mapped[rule.groups[-1].logical_id].char_end,
                                ),
                            )[1]
                            == left
                        )
                    ):
                        result.append(
                            LayoutElement(
                                logical_id(self.project_id, owner + "/table-end"),
                                "entity",
                                entity_id=owner,
                                field="table_end",
                                source=span(file_id, left, end),
                            )
                        )
                    elif (
                        isinstance(rule, ObjectRule)
                        and not rule.groups
                        and text[left:end] in ("\n", "\r\n")
                        and (
                            (
                                rule.properties
                                and line_range(
                                    file_id,
                                    max(mapped[p.logical_id].char_end for p in rule.properties),
                                    max(mapped[p.logical_id].char_end for p in rule.properties),
                                )[1]
                                == left
                            )
                            or (
                                not rule.properties
                                and re.fullmatch(
                                    r"СвойстваШапки\s*=\s*ПравилоКонвертации\.Свойства(?:Шапки)?;",
                                    text[text.rfind("\n", 0, max(left - 1, 0)) + 1 : left].strip(),
                                    re.IGNORECASE,
                                )
                            )
                        )
                    ):
                        result.append(
                            LayoutElement(
                                logical_id(self.project_id, owner + "/table-end"),
                                "entity",
                                entity_id=owner,
                                field="properties_end",
                                source=span(file_id, left, end),
                            )
                        )
                    else:
                        result.append(text_leaf(file_id, left, end, owner))
                left = end
            return result

        def body(
            file_id,
            left,
            right,
            owner,
            kind,
            name,
            candidates,
            signature=None,
            direction=None,
            opening=None,
            closing=None,
        ):
            # candidates — неперекрывающиеся операторы и целые сохранённые ПКТЧ.
            # Условия представлены контейнерами, каркас не дублируется в блоках.
            result = []
            cursor = left
            for start, end, item in sorted(candidates, key=lambda row: row[0]):
                if start < cursor or end > right:
                    raise _LayoutOverlap(file_id, left, right)
                result.extend(gaps(file_id, cursor, start, owner))
                result.append(item)
                cursor = end
            result.extend(gaps(file_id, cursor, right, owner))
            container = LayoutContainer(
                owner,
                kind,
                name,
                direction=direction,
                signature=signature or Signature(),
                opening=opening,
                closing=closing,
                elements=tuple(result),
            )
            containers.append(container)
            return container

        def exact_entry(routine):
            all_rows = self.rule_statements(routine)
            source = self.sources[routine.span.file_id].text
            if any(
                "\n" not in source[left.span.char_end : right.span.char_start]
                for left, right in pairwise(all_rows)
            ):
                return False
            uses = {u.span.char_start for u in uses_by_routine[routine.entity_id]}
            scaffold = {s.span.char_start for group in column_groups(routine) for s in group}
            rows = [
                s
                for s in all_rows
                if routine.body_span.char_start <= s.span.char_start < routine.body_span.char_end
            ]
            if routine.name.casefold() == "заполнитьправилаконвертациипредопределенныхданных":
                bounds = [
                    self.predefined_bounds[r.logical_id]
                    for r in model.pkpd
                    if r.state == "editable"
                ]
                if any(r.state != "editable" for r in model.pkpd):
                    return False
                return all(
                    any(a <= s.span.char_start < b for a, b in bounds)
                    or s.head == "конецесли"
                    or (
                        s.head in ("если", "иначеесли")
                        and all(
                            self.exact_direction(g)
                            for g in self.guards_at[s.span.file_id, s.span.char_start]
                        )
                    )
                    for s in rows
                )
            if routine.name.casefold() == "заполнитьпараметрыконвертации":
                positions = {
                    mapped[p.logical_id].char_start
                    for p in model.parameters
                    if p.state == "editable"
                }
                return all(s.span.char_start in positions for s in rows)
            return all(
                s.span.char_start in uses
                or s.span.char_start in scaffold
                or s.head == "конецесли"
                or (
                    doc.manager_version == 3
                    and [t.folded for t in s.tokens]
                    in (
                        [
                            "направлениеобмена",
                            "=",
                            "компонентыобмена",
                            ".",
                            "направлениеобмена",
                            ";",
                        ],
                        [
                            "версияформатаобмена",
                            "=",
                            "компонентыобмена",
                            ".",
                            "версияформатаобмена",
                            ";",
                        ],
                    )
                )
                or (
                    s.head in ("если", "иначеесли")
                    and self.guards_at[s.span.file_id, s.span.char_start]
                    and all(
                        self.exact_direction(g)
                        for g in self.guards_at[s.span.file_id, s.span.char_start]
                    )
                )
                for s in rows
            ) and all(not self.unsafe(u) for u in uses_by_routine[routine.entity_id])

        def column_groups(routine):
            """Точная форма генератора: forms.POD_COLUMN_STATEMENT, ObjectModule:2690–2692."""
            if routine.name.casefold() != "заполнитьправилаобработкиданных":
                return ()
            rows = self.rule_statements(routine)
            condition = tokenize(
                'Если ПравилаОбработкиДанных.Колонки.Найти("ОчисткаДанных") = Неопределено Тогда'
            )
            call = tokenize(POD_COLUMN_STATEMENT)

            def tokens_equal(left, right):
                return tuple(
                    (t.kind, t.value if t.kind == "string" else t.folded) for t in left
                ) == tuple((t.kind, t.value if t.kind == "string" else t.folded) for t in right)

            return tuple(
                tuple(rows[n : n + 3])
                for n in range(len(rows) - 2)
                if tokens_equal(rows[n].tokens, condition)
                and tokens_equal(rows[n + 1].tokens, call)
                and rows[n + 2].head == "конецесли"
            )

        field_names = {
            "имяпко": "name",
            "объектданных": "configuration_object",
            "объектформата": "format_object",
            "этогруппа": "group_flag",
            "правилодлягруппысправочника": "group_flag",
            "вариантидентификации": "mode",
            "имя": "name",
            "объектвыборки": "configuration_selection",
            "объектвыборкиданные": "configuration_selection",
            "объектвыборкиметаданные": "configuration_selection",
            "объектвыборкиформат": "format_selection",
            "очисткаданных": "clear_data",
            "используемыепко": "used_pko",
            "инициализироватьрасширениеправилаконвертацииобъекта": "extensions",
        }

        def predefined_layout(rule):
            entry = mapped[rule.logical_id]
            file_id = entry.file_id
            left, right = line_range(file_id, *self.predefined_bounds[rule.logical_id])
            source = self.sources[file_id].text
            previous = source.rfind("\n", 0, max(0, left - 1)) + 1
            if source[previous:left].strip() == f"// {rule.name}.":
                left = previous
            rows = [s for s in self.statements[file_id] if left <= s.span.char_start < right]
            candidates = []
            starts = [
                s
                for s in rows
                if s.head in ("значениядляотправки", "значениядляполучения")
                and s.tokens[1].value == "="
            ]
            opening_end = (
                line_range(file_id, starts[0].span.char_start, starts[0].span.char_end)[0]
                if starts
                else right
            )
            field_comments = []
            previous_end = left
            previous_field = "comment"
            for statement in rows:
                if statement.span.char_start >= opening_end:
                    break
                for line in source[previous_end : statement.span.char_start].split("\n"):
                    if line.strip().startswith("//") and line.strip() != f"// {rule.name}.":
                        field_comments.append(("after_" + previous_field, line.strip()))
                end = source.find("\n", statement.span.char_end)
                tail = source[statement.span.char_end : end if end >= 0 else len(source)].strip()
                names = {t.folded for t in statement.tokens}
                field = (
                    "name"
                    if "имяпкпд" in names
                    else "configuration_type"
                    if "типданных" in names
                    else "format_type"
                    if "типxdto" in names
                    else "create"
                )
                if tail.startswith("//"):
                    field_comments.append((field, tail))
                previous_end = end + 1 if end >= 0 else len(source)
                previous_field = field
            for line in source[previous_end:opening_end].split("\n"):
                if line.strip().startswith("//"):
                    field_comments.append(("after_" + previous_field, line.strip()))
            for first in starts:
                direction = "send" if first.head == "значениядляотправки" else "receive"
                closing_field = (
                    "конвертациизначенийприотправке"
                    if direction == "send"
                    else "конвертациизначенийприполучении"
                )
                last = next(s for s in rows if any(t.folded == closing_field for t in s.tokens))
                start, inner_start = line_range(file_id, first.span.char_start, first.span.char_end)
                inner_end, end = line_range(file_id, last.span.char_start, last.span.char_end)
                for statement, boundary, side in (
                    (first, inner_start, "open"),
                    (last, end, "close"),
                ):
                    tail = source[statement.span.char_end : boundary].strip()
                    if tail.startswith("//"):
                        field_comments.append((f"values_{direction}_{side}", tail))
                key = logical_id(self.project_id, rule.logical_id + "/values/" + direction)
                children = []
                for item in rule.mappings:
                    if item.direction != direction:
                        continue
                    value = mapped[item.logical_id]
                    a, b = line_range(file_id, value.char_start, value.char_end)
                    tail = source[value.char_end : b].strip()
                    comment = tail if tail.startswith("//") else ""
                    if comment:
                        comments[item.logical_id] = comment
                    children.append(
                        (
                            a,
                            b,
                            LayoutElement(
                                item.logical_id,
                                "entity",
                                entity_id=item.logical_id,
                                source=span(file_id, a, b),
                                trailing_comment=comment,
                            ),
                        )
                    )
                container = body(
                    file_id,
                    inner_start,
                    inner_end,
                    key,
                    "values",
                    direction,
                    children,
                    direction=direction,
                    opening=span(file_id, start, inner_start),
                    closing=span(file_id, inner_end, end),
                )
                containers[-1] = replace(container, owner_id=rule.logical_id)
                candidates.append((start, end, LayoutElement(key, "container", container_id=key)))
            members[rule.logical_id] = replace(rule, field_comments=tuple(field_comments))
            container = body(
                file_id,
                opening_end,
                right,
                rule.logical_id,
                "predefined",
                rule.name,
                candidates,
                opening=span(file_id, left, opening_end),
            )
            return (
                left,
                right,
                LayoutElement(rule.logical_id, "container", container_id=container.logical_id),
            )

        def editable_body(routine, rule):
            file_id = routine.span.file_id
            key = rule.logical_id if rule else self.ids[routine.entity_id]
            left, right = line_range(file_id, routine.span.char_start, routine.span.char_end)
            statements = [
                s
                for s in self.rule_statements(routine)
                if routine.body_span.char_start <= s.span.char_start < routine.body_span.char_end
            ]
            header_end = statements[0].span.char_start if statements else routine.body_span.char_end
            # Заголовок заканчивается после закрывающей скобки, а не перед первым
            # оператором: комментарии пустого заполнителя остаются в его теле.
            all_rows = self.rule_statements(routine)
            header = next(s for s in all_rows if s.span.char_start == routine.span.char_start)
            _, header_end = line_range(file_id, header.span.char_start, header.span.char_end)
            if rule is not None and statements:
                # Инициализация — каркас процедуры: начало тела находится после
                # создания самого правила, а не перед ним.
                _, header_end = line_range(
                    file_id, statements[0].span.char_start, statements[0].span.char_end
                )
                statements = statements[1:]
            close_start, _ = line_range(file_id, routine.body_span.char_end, routine.span.char_end)
            candidates = []
            consumed = set()
            columns = (
                column_groups(routine)
                if rule is None
                else self.header_groups(reader_rules[routine.name.casefold()])
            )
            for column in columns:
                start, end = line_range(
                    file_id, column[0].span.char_start, column[-1].span.char_end
                )
                candidates.append(
                    (
                        start,
                        end,
                        LayoutElement(
                            logical_id(self.project_id, "header/clear_data_column"),
                            "entity",
                            entity_id=self.ids[doc.conversion.entity_id],
                            field="header.clear_data_column",
                            source=span(file_id, start, end),
                        )
                        if rule is None
                        else text_leaf(file_id, start, end, key, "scaffold"),
                    )
                )
                consumed.update(s.span.char_start for s in column)
            if isinstance(rule, ObjectRule):
                for child in (
                    *rule.properties,
                    *rule.groups,
                    *rule.identification.search_sets,
                    *rule.events,
                ):
                    entry = mapped.get(child.logical_id)
                    if entry is None:
                        continue
                    start, end = line_range(file_id, entry.char_start, entry.char_end)
                    if isinstance(child, PropertyGroup) and child.properties:
                        end = line_range(
                            file_id,
                            start,
                            max(mapped[p.logical_id].char_end for p in child.properties),
                        )[1]
                    if isinstance(child, PropertyGroup) and child.state == "editable":
                        group_header_end = line_range(file_id, entry.char_start, entry.char_end)[1]
                        tokens = tokens_by_file[file_id]
                        starts = token_starts[file_id]
                        tail = tokens[
                            bisect_left(starts, entry.char_end) : bisect_left(
                                starts, group_header_end
                            )
                        ]
                        comment = next(
                            (t.value for t in tail if t.kind == "comment"),
                            "",
                        )
                        if comment:
                            comments[child.logical_id] = comment
                        group_start = start
                        previous = self.sources[file_id].text.rfind("\n", 0, max(start - 1, 0)) + 1
                        if self.sources[file_id].text[previous:start].rstrip("\r\n") == "\t":
                            group_start = previous
                        group_candidates = []
                        for prop in child.properties:
                            p_entry = mapped[prop.logical_id]
                            a, b = line_range(file_id, p_entry.char_start, p_entry.char_end)
                            tail = tokens[
                                bisect_left(starts, p_entry.char_end) : bisect_left(starts, b)
                            ]
                            comment = next((t.value for t in tail if t.kind == "comment"), "")
                            if comment:
                                comments[prop.logical_id] = comment
                            group_candidates.append(
                                (
                                    a,
                                    b,
                                    LayoutElement(
                                        prop.logical_id,
                                        "entity",
                                        entity_id=prop.logical_id,
                                        source=span(file_id, a, b),
                                        trailing_comment=comment,
                                    )
                                    if prop.state == "editable"
                                    else text_leaf(
                                        file_id, a, b, child.logical_id, "pks", prop.logical_id
                                    ),
                                )
                            )
                        table = body(
                            file_id,
                            group_header_end,
                            end,
                            child.logical_id,
                            "table_part",
                            child.name,
                            group_candidates,
                            opening=span(file_id, group_start, group_header_end),
                        )
                        containers[-1] = replace(table, owner_id=key)
                        candidates.append(
                            (
                                group_start,
                                end,
                                LayoutElement(
                                    child.logical_id, "container", container_id=child.logical_id
                                ),
                            )
                        )
                        consumed.add(entry.char_start)
                        continue
                    if child.state != "editable":
                        element = text_leaf(
                            file_id,
                            start,
                            end,
                            key,
                            "pktch"
                            if isinstance(child, PropertyGroup)
                            else "pks"
                            if isinstance(child, Property)
                            else "binding",
                            child.logical_id,
                        )
                    else:
                        element = LayoutElement(
                            child.logical_id,
                            "entity",
                            entity_id=child.logical_id,
                            source=span(file_id, start, end),
                        )
                    candidates.append((start, end, element))
                    consumed.add(entry.char_start)
            elif isinstance(rule, ProcessingRule):
                for child in rule.events:
                    entry = mapped[child.logical_id]
                    start, end = line_range(file_id, entry.char_start, entry.char_end)
                    candidates.append(
                        (
                            start,
                            end,
                            LayoutElement(
                                child.logical_id,
                                "entity",
                                entity_id=child.logical_id,
                                source=span(file_id, start, end),
                            )
                            if child.state == "editable"
                            else text_leaf(file_id, start, end, key, "binding", child.logical_id),
                        )
                    )
                    consumed.add(entry.char_start)
            else:
                if routine.name.casefold() == "заполнитьправилаконвертациипредопределенныхданных":
                    for child in model.pkpd:
                        start, end, element = predefined_layout(child)
                        candidates.append((start, end, element))
                        consumed.update(
                            s.span.char_start
                            for s in statements
                            if start <= s.span.char_start < end
                        )
                if routine.name.casefold() == "заполнитьпараметрыконвертации":
                    for child in model.parameters:
                        entry = mapped[child.logical_id]
                        start, end = line_range(file_id, entry.char_start, entry.char_end)
                        candidates.append(
                            (
                                start,
                                end,
                                LayoutElement(
                                    child.logical_id,
                                    "entity",
                                    entity_id=child.logical_id,
                                    source=span(file_id, start, end),
                                ),
                            )
                        )
                        consumed.add(entry.char_start)
                for use in uses_by_routine[routine.entity_id]:
                    start, end = line_range(file_id, use.span.char_start, use.span.char_end)
                    use_id = self.ids[use.entity_id]
                    candidates.append(
                        (
                            start,
                            end,
                            LayoutElement(
                                use_id, "entity", entity_id=use_id, source=span(file_id, start, end)
                            ),
                        )
                    )
                    consumed.add(use.span.char_start)
            for statement in statements:
                if statement.span.char_start in consumed or statement.head in (
                    "если",
                    "иначеесли",
                    "конецесли",
                ):
                    continue
                start, end = line_range(file_id, statement.span.char_start, statement.span.char_end)
                if any(a <= start < b for a, b, _ in candidates):
                    continue
                names = [t.folded for t in statement.tokens]
                field_name = (
                    next((field_names[t] for t in names if t in field_names), "") if rule else ""
                )
                if rule and names[:3] == ["свойствашапки", "=", "правилоконвертации"]:
                    field_name = "properties_start"
                entity_id = (
                    rule.identification.logical_id
                    if isinstance(rule, ObjectRule) and field_name == "mode"
                    else key
                )
                element = (
                    LayoutElement(
                        logical_id(self.project_id, f"operator/{file_id}/{start}"),
                        "entity",
                        entity_id=entity_id,
                        field=field_name,
                        source=span(file_id, start, end),
                    )
                    if field_name
                    else text_leaf(file_id, start, end, key, "scaffold")
                )
                candidates.append((start, end, element))
            # Каркас цепочки Если/ИначеЕсли принадлежит её веткам. Цепочка —
            # единый элемент тела: между ветками нельзя вставить оператор.
            for n, (start, end, element) in enumerate(candidates):
                if element.entity_id and element.source and not element.block_id:
                    rows = tokens_by_file[file_id]
                    starts = token_starts[file_id]
                    inside = rows[
                        bisect_left(starts, element.source.char_start) : bisect_left(
                            starts, element.source.char_end
                        )
                    ]
                    if inside and inside[-1].kind == "comment":
                        candidates[n] = (
                            start,
                            end,
                            replace(element, trailing_comment=inside[-1].value),
                        )
                        if not element.field:
                            comments[element.entity_id] = inside[-1].value
            # Несколько инструкций одной строки нельзя переносить по отдельности.
            merged = []
            for start, end, element in sorted(candidates, key=lambda row: row[0]):
                text = self.sources[file_id].text
                line_start = text.rfind("\n", 0, start) + 1
                if merged and merged[-1][0] >= line_start:
                    previous_start, _, previous = merged.pop()
                    for old in (previous, element):
                        if old.block_id:
                            blocks[:] = [b for b in blocks if b.logical_id != old.block_id]
                        if old.entity_id:
                            comments.pop(old.entity_id, None)
                    end = text.find("\n", max(start, end - 1))
                    end = end + 1 if end >= 0 else len(text)
                    merged.append(
                        (
                            previous_start,
                            end,
                            text_leaf(file_id, previous_start, end, key, "shared_line"),
                        )
                    )
                else:
                    merged.append((start, end, element))
            candidates = merged
            stack = []
            for statement in statements:
                if statement.span.char_start in consumed:
                    continue
                if statement.head == "если":
                    stack.append([statement])
                elif statement.head == "иначеесли":
                    stack[-1].append(statement)
                elif statement.head == "конецесли":
                    branches = stack.pop()
                    branch_elements = []
                    for n in reversed(range(len(branches))):
                        first = branches[n]
                        guard = self.guards_at[file_id, first.span.char_start][0]
                        group_id = self.ids[guard.entity_id]
                        start, inner_start = line_range(
                            file_id, first.span.char_start, first.span.char_end
                        )
                        terminator = branches[n + 1] if n + 1 < len(branches) else statement
                        inner_end, end = line_range(
                            file_id, terminator.span.char_start, terminator.span.char_end
                        )
                        last = n == len(branches) - 1
                        if not last:
                            end = inner_end
                        nested = [
                            (a, b, e)
                            for a, b, e in candidates
                            if inner_start <= a and b <= inner_end
                        ]
                        candidates = [
                            (a, b, e) for a, b, e in candidates if not (start <= a and b <= end)
                        ]
                        group = body(
                            file_id,
                            inner_start,
                            inner_end,
                            group_id,
                            "conditional",
                            guard.expression_raw,
                            nested,
                            direction=cast(Direction, guard.known_direction),
                            opening=span(file_id, start, inner_start),
                            closing=span(file_id, inner_end, end) if last else None,
                        )
                        containers[-1] = replace(group, owner_id=key, branch=guard.branch)
                        nested_ids = {e.container_id for _, _, e in nested if e.container_id}
                        containers[:] = [
                            replace(c, owner_id=group_id) if c.logical_id in nested_ids else c
                            for c in containers
                        ]
                        element = LayoutElement(group_id, "container", container_id=group_id)
                        if len(branches) == 1:
                            candidates.append((start, end, element))
                        else:
                            branch_elements.insert(0, element)
                    if branch_elements:
                        chain_id = logical_id(
                            self.project_id, "chain/" + branch_elements[0].logical_id
                        )
                        branch_ids = {e.container_id for e in branch_elements}
                        containers[:] = [
                            replace(c, owner_id=chain_id) if c.logical_id in branch_ids else c
                            for c in containers
                        ]
                        containers.append(
                            LayoutContainer(
                                chain_id,
                                "conditional",
                                "Направления обмена",
                                owner_id=key,
                                branch="chain",
                                elements=tuple(branch_elements),
                            )
                        )
                        candidates.append(
                            (
                                line_range(
                                    file_id, branches[0].span.char_start, branches[0].span.char_end
                                )[0],
                                line_range(
                                    file_id, statement.span.char_start, statement.span.char_end
                                )[1],
                                LayoutElement(chain_id, "container", container_id=chain_id),
                            )
                        )
            container = body(
                file_id,
                header_end,
                close_start,
                key,
                "rule" if rule else "entrypoint",
                routine.name,
                candidates,
                import_signature(routine),
                opening=span(file_id, left, header_end),
                closing=span(file_id, close_start, right),
            )
            return left, right, LayoutElement(key, "container", container_id=container.logical_id)

        def code_layout(routine, unit):
            file_id = routine.span.file_id
            left, right = line_range(file_id, routine.span.char_start, routine.span.char_end)
            left -= len(unit.frame_comment)
            start, end = routine.body_span.char_start, routine.body_span.char_end
            if "dispatcher" not in unit.roles:
                element = LayoutElement(
                    logical_id(self.project_id, unit.logical_id + "/body"),
                    "entity",
                    entity_id=unit.logical_id,
                    field="body",
                    source=span(file_id, start, end),
                )
                container = LayoutContainer(
                    unit.logical_id,
                    "code",
                    unit.name,
                    signature=unit.signature,
                    opening=span(file_id, left, start),
                    closing=span(file_id, end, right),
                    elements=(element,),
                )
                containers.append(container)
            else:
                candidates = []
                for case in model.dispatcher_cases:
                    if case.dispatcher.target_id != unit.logical_id:
                        continue
                    entry = mapped[case.logical_id]
                    header = next(
                        s
                        for s in reversed(self.rule_statements(routine))
                        if s.head in ("если", "иначеесли") and s.span.char_start < entry.char_start
                    )
                    a, b = line_range(file_id, header.span.char_start, entry.char_end)
                    candidates.append(
                        (
                            a,
                            b,
                            text_leaf(
                                file_id, a, b, unit.logical_id, "dispatcher_case", case.logical_id
                            )
                            if case.state != "editable"
                            else LayoutElement(
                                case.logical_id,
                                "entity",
                                entity_id=case.logical_id,
                                source=span(file_id, a, b),
                            ),
                        )
                    )
                for statement in self.rule_statements(routine):
                    if statement.head == "конецесли":
                        a, b = line_range(
                            file_id, statement.span.char_start, statement.span.char_end
                        )
                        candidates.append(
                            (
                                a,
                                b,
                                LayoutElement(
                                    logical_id(self.project_id, unit.logical_id + "/end"),
                                    "entity",
                                    entity_id=unit.logical_id,
                                    field="dispatch_end",
                                    source=span(file_id, a, b),
                                ),
                            )
                        )
                body(
                    file_id,
                    start,
                    end,
                    unit.logical_id,
                    "dispatcher",
                    unit.name,
                    candidates,
                    unit.signature,
                    opening=span(file_id, left, start),
                    closing=span(file_id, end, right),
                )
            return (
                left,
                right,
                LayoutElement(unit.logical_id, "container", container_id=unit.logical_id),
            )

        for source in doc.files:
            module_id = logical_id(self.project_id, "module/" + source.file_id)
            roots.append(module_id)
            candidates = []
            for routine in doc.routines:
                if routine.span.file_id != source.file_id:
                    continue
                rule = rules.get(routine.name.casefold())
                rule_reader = reader_rules.get(routine.name.casefold())
                # Иначе не имеет чистого сравнения; сохраняем весь контекст.
                has_else = any(s.head == "иначе" for s in self.rule_statements(routine))
                is_entry = routine.name.casefold() in {name.casefold() for name in ENTRYPOINTS}
                local_overlap = routine.entity_id in self.layout_overlap_routines
                unit = members.get(self.ids[routine.entity_id])
                if isinstance(unit, CodeUnit) and unit.state == "editable":
                    candidates.append(code_layout(routine, unit))
                    continue
                if not local_overlap and (
                    (rule and rule.state == "editable" and not has_else)
                    or (is_entry and exact_entry(routine) and not has_else)
                ):
                    candidates.append(editable_body(routine, rule))
                else:
                    left, right = line_range(
                        source.file_id, routine.span.char_start, routine.span.char_end
                    )
                    if routine.entity_id == self.identifier_routine:
                        candidates.append(
                            (
                                left,
                                right,
                                LayoutElement(
                                    logical_id(self.project_id, "header/module_identifier"),
                                    "entity",
                                    entity_id=self.ids[doc.conversion.entity_id],
                                    field="header.module_identifier",
                                    source=span(source.file_id, left, right),
                                ),
                            )
                        )
                        continue
                    if routine.name.casefold() == VERSION_ROUTINE.casefold():
                        statements = [
                            s
                            for s in self.rule_statements(routine)
                            if routine.body_span.char_start
                            <= s.span.char_start
                            < routine.body_span.char_end
                        ]
                        if (
                            doc.manager_version in (1, 2, 3)
                            and routine.routine_kind == "function"
                            and not routine.parameters
                            and routine.exported
                            and len(statements) == 1
                            and statements[0].head == "возврат"
                            and len(statements[0].tokens) == 3
                        ):
                            candidates.append(
                                (
                                    left,
                                    right,
                                    LayoutElement(
                                        logical_id(self.project_id, "header/interface_version"),
                                        "entity",
                                        entity_id=self.ids[doc.conversion.entity_id],
                                        field="header.interface_version",
                                        source=span(source.file_id, left, right),
                                    ),
                                )
                            )
                            continue
                    # Сущности внутри процедуры — views, включая само правило.
                    element = text_leaf(
                        source.file_id,
                        left,
                        right,
                        module_id,
                        "routine",
                        lock=bool(rule or is_entry),
                    )
                    if local_overlap or self.ids[routine.entity_id] in overlap_members:
                        blocks[-1] = replace(blocks[-1], reason="layout_overlap")
                    if self.ids[routine.entity_id] in {u.logical_id for u in model.code_units}:
                        element = replace(
                            element, kind="entity", entity_id=self.ids[routine.entity_id]
                        )
                        if members[self.ids[routine.entity_id]].state == "editable":
                            blocks.pop()
                            element = replace(element, block_id=None)
                    if rule and rule.state == "blocked":
                        blocks[-1] = replace(blocks[-1], state="blocked")
                    candidates.append((left, right, element))
                    if rule_reader and rule:
                        members[rule.logical_id] = replace(
                            rule,
                            state=rule.state if rule.state == "blocked" else "retained",
                            inside_leaf_id=element.block_id,
                        )
            body(
                source.file_id,
                0,
                len(source.text),
                module_id,
                "module",
                source.path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                candidates,
                opening=span(source.file_id, 0, 0) if source.bom and not source.text else None,
            )

        # Родитель группы — ближайший контейнер, а не всегда тело процедуры.
        parents = {
            e.container_id: c.logical_id
            for c in containers
            for e in c.elements
            if e.container_id is not None
        }
        containers = [
            replace(c, owner_id=parents[c.logical_id])
            if c.kind in ("conditional", "predefined", "values", "table_part")
            else c
            for c in containers
        ]
        owners = {
            e.block_id: c.logical_id
            for c in containers
            for e in c.elements
            if e.block_id is not None
        }
        blocks = [
            replace(b, owner_id=owners[b.logical_id]) if b.owner_id != owners[b.logical_id] else b
            for b in blocks
        ]
        leaf_intervals = defaultdict(list)
        for block in blocks:
            leaf_intervals[block.file_id].append(block)
        for rows in leaf_intervals.values():
            rows.sort(key=lambda b: b.char_start)
        starts = {key: [b.char_start for b in rows] for key, rows in leaf_intervals.items()}
        own_entities = {e.entity_id for c in containers for e in c.elements if e.entity_id}
        container_ids = {c.logical_id for c in containers}

        def decorate(member: Any) -> Any:
            member = members.get(member.logical_id, member)
            entry = mapped.get(member.logical_id)
            leaf_id = member.inside_leaf_id
            if (
                entry is not None
                and member.logical_id not in own_entities
                and member.logical_id not in container_ids
            ):
                rows = leaf_intervals[entry.file_id]
                n = bisect_right(starts[entry.file_id], entry.char_start) - 1
                if n >= 0 and entry.char_end <= rows[n].char_end:
                    leaf_id = rows[n].logical_id
            updates = {
                "inside_leaf_id": leaf_id,
                "trailing_comment": comments.get(member.logical_id, ""),
            }
            if leaf_id and not isinstance(member, CodeUnit):
                updates["state"] = "retained"
                if member.state == "blocked":
                    updates["state"] = "blocked"
            if isinstance(member, ObjectRule):
                if leaf_id:
                    members[member.identification.logical_id] = replace(
                        member.identification, inside_leaf_id=leaf_id, state="retained"
                    )
                updates.update(
                    properties=tuple(decorate(p) for p in member.properties),
                    groups=tuple(decorate(g) for g in member.groups),
                    events=tuple(decorate(e) for e in member.events),
                    identification=decorate(member.identification),
                )
            elif isinstance(member, PropertyGroup):
                updates["properties"] = tuple(decorate(p) for p in member.properties)
            elif isinstance(member, Identification):
                updates["search_sets"] = tuple(decorate(s) for s in member.search_sets)
            elif isinstance(member, ProcessingRule):
                updates["events"] = tuple(decorate(e) for e in member.events)
            elif isinstance(member, PredefinedRule):
                updates["mappings"] = tuple(decorate(v) for v in member.mappings)
            return replace(member, **updates)

        model = replace(
            model,
            retained_blocks=tuple(blocks),
            layouts=tuple(containers),
            root_layouts=tuple(roots),
            **{
                key: tuple(decorate(m) for m in getattr(model, key))
                for key in (
                    "pko",
                    "pod",
                    "pkpd",
                    "parameters",
                    "code_units",
                    "conversion_events",
                    "rule_uses",
                    "guards",
                    "dispatcher_cases",
                )
            },
        )
        states = {m.logical_id: m.state for m in model.members()}
        self.entries = [
            replace(
                e,
                state=states.get(e.logical_id, e.state),
                reason=e.reason
                or (
                    "Внутри сохранённого листа раскладки"
                    if states.get(e.logical_id, e.state) != "editable"
                    else ""
                ),
            )
            for e in self.entries
        ]
        imported = {m.logical_id: m for m in model.members()}

        def formatted(source, item, container_id):
            text = self.sources[source.file_id].text[source.char_start : source.char_end]
            first = text.splitlines()[0] if text else ""
            width = 0
            if (
                isinstance(item, LayoutElement)
                and (item.field or isinstance(imported.get(item.entity_id or ""), Event))
                and " = " in first
            ):
                width = first.index(" = ") - (len(first) - len(first.lstrip(" \t")))
            last = text.rstrip("\r\n").rsplit("\n", 1)[-1]
            suffix = last[len(last.rstrip(" \t")) :]
            gap = (
                sum(not line.strip() for line in text.splitlines())
                if isinstance(item, LayoutContainer) and item.kind == "rule"
                else 0
            )
            return replace(
                source,
                fingerprint=leaf_fingerprint(model, item, imported),
                assignment_width=width,
                opening_blank_lines=gap,
                line_suffix=suffix,
                container_id=container_id,
                compact_rule_separator=bool(
                    isinstance(item, LayoutElement)
                    and isinstance(members.get(item.entity_id or ""), Property)
                    and ',"' in text
                ),
            )

        model = replace(
            model,
            layouts=tuple(
                replace(
                    c,
                    opening=formatted(c.opening, c, c.logical_id) if c.opening else None,
                    closing=formatted(c.closing, c, c.logical_id) if c.closing else None,
                    elements=tuple(
                        replace(
                            e,
                            source=formatted(e.source, e, c.logical_id),
                        )
                        if e.source and not e.block_id
                        else e
                        for e in c.elements
                    ),
                )
                for c in model.layouts
            ),
        )
        return model

    def build(
        self, name: str, host: Host, bindings: tuple[FormatBinding, ...], profile: ExecutorProfile
    ) -> tuple[ManagerModel, ImportReport]:
        doc = self.document
        self.common(
            doc.conversion, 1, "editable" if doc.manager_version in (1, 2, 3) else "blocked"
        )
        pko = tuple(self.object_rule(r, n) for n, r in enumerate(doc.pko, 1))
        pod = []
        for n, rule in enumerate(doc.pod, 1):
            state: ImportState = "retained" if self.rule_unsafe(rule) else "editable"
            pod.append(
                ProcessingRule(
                    **self.common(rule, n, state),
                    procedure_name=rule.procedure_name,
                    signature=import_signature(self.routines[rule.procedure_name.casefold()]),
                    directions=self.directions[rule.entity_id],
                    configuration_selection=import_field(rule.configuration_selection),
                    format_selection=import_field(rule.format_selection),
                    clear_data=import_field(rule.clear_data),
                    events=self.events(rule.events),
                    used_pko=tuple(
                        self.reference("pko", ref.target_id, ref.name, ref.resolution)
                        for ref in rule.used_pko
                    ),
                )
            )
            if state != "editable":
                self.retain(rule, reason="unsafe_declaration", lock=True)
        pkpd = [self.predefined(rule, n) for n, rule in enumerate(doc.pkpd, 1)]
        parameters = self.parameters()
        references = build_references(doc)
        code = []
        routine_names = Counter(r.name.casefold() for r in doc.routines)
        predefined_names = {r.name.casefold(): r for r in doc.pkpd}
        excluded = {s.casefold() for s in ENTRYPOINTS} | {VERSION_ROUTINE.casefold()}
        for routine in doc.routines:
            if routine.entity_id == self.identifier_routine:
                self.common(routine, len(code) + 1, "editable")
                continue
            if "rule" in routine.roles or routine.name.casefold() in excluded:
                if routine.name.casefold() in (
                    "заполнитьправилаконвертациипредопределенныхданных",
                    "заполнитьпараметрыконвертации",
                ):
                    self.retain(routine, reason="retained_declaration_routine")
                continue
            source = self.sources[routine.span.file_id]
            body = source.text[routine.body_span.char_start : routine.body_span.char_end]
            editable_code = bool(routine.roles & {"handler", "algorithm", "event", "dispatcher"})
            editable_code &= routine_names[routine.name.casefold()] == 1
            # Две рамки на одной физической строке не делятся шаблоном процедуры.
            line_start = source.text.rfind("\n", 0, routine.span.char_start) + 1
            line_end = source.text.find("\n", routine.span.char_end)
            suffix = source.text[
                routine.span.char_end : line_end if line_end >= 0 else len(source.text)
            ].strip()
            editable_code &= not source.text[line_start : routine.span.char_start].strip()
            editable_code &= not suffix or suffix.startswith("//")
            editable_code &= all(
                p.name is not None
                and (
                    "algorithm" in routine.roles or import_expression(p.default).state != "unknown"
                )
                for p in routine.parameters
            )
            dispatcher_overlap = False
            if "dispatcher" in routine.roles:
                headers = [
                    s.span.char_start
                    for s in self.rule_statements(routine)
                    if s.head in ("если", "иначеесли")
                ]
                calls_per_branch = Counter(
                    bisect_right(headers, c.span.char_start) - 1
                    for c in doc.dispatcher_cases
                    if c.dispatcher_id == routine.entity_id
                )
                dispatcher_overlap = any(count > 1 for count in calls_per_branch.values())
                # Обобщённые Else/теги/неизвестные операторы не являются формой T:110–118.
                editable_code &= (
                    routine.status == reader.ParseStatus.COMPLETE
                    and not routine.tag_ids
                    and not any(u.owner_id == routine.entity_id for u in doc.unknown)
                    and not any(s.head == "иначе" for s in self.rule_statements(routine))
                    and all(
                        len(s.tokens) == 5
                        and s.tokens[1].folded
                        == ("имяфункции" if routine.routine_kind == "function" else "имяпроцедуры")
                        and s.tokens[2].value == "="
                        and s.tokens[3].kind == "string"
                        and s.tokens[4].folded == "тогда"
                        for s in self.rule_statements(routine)
                        if s.head in ("если", "иначеесли")
                    )
                    and calls_per_branch == Counter(range(len(headers)))
                    and sum(s.head == "если" for s in self.rule_statements(routine))
                    == bool(headers)
                    and sum(s.head == "конецесли" for s in self.rule_statements(routine))
                    == bool(headers)
                )
            state: ImportState = "editable" if editable_code else "retained"
            if "dispatcher" in routine.roles and editable_code:
                body = ""
            comment_start = source.text.rfind("\n", 0, routine.span.char_start) + 1
            frame_start = comment_start
            if editable_code and frame_start:
                previous = source.text.rfind("\n", 0, max(0, frame_start - 1)) + 1
                # Разделы и описания пользователя не принадлежат удаляемой рамке.
                if source.text[previous:frame_start].strip() == "// " + routine.name:
                    frame_start = previous
            deps = []
            for ref in references.entries:
                if ref.owner_id != routine.entity_id or ref.kind == "parameter":
                    continue
                targets = (
                    [r for r in doc.pko if ref.name and r.name.casefold() == ref.name.casefold()]
                    if ref.kind in ("pko_lookup", "instruction_rule", "pod_use")
                    else []
                )
                deps.append(
                    self.reference(
                        "pko" if targets else ref.kind,
                        targets[0].entity_id if len(targets) == 1 else None,
                        ref.name or "",
                        "computed"
                        if ref.name is None
                        else "ambiguous"
                        if len(targets) > 1
                        else "missing",
                    )
                )
            for statement in self.rule_statements(routine):
                if (
                    not routine.body_span.char_start
                    <= statement.span.char_start
                    < routine.body_span.char_end
                ):
                    continue
                for token in statement.tokens:
                    target = (
                        predefined_names.get(token.value.casefold())
                        if token.kind == "string"
                        else None
                    )
                    if target is not None:
                        deps.append(self.reference("pkpd", target.entity_id, target.name))
            code.append(
                CodeUnit(
                    **self.common(routine, len(code) + 1, state),
                    signature=import_signature(routine),
                    body=body,
                    sha256=text_hash(body),
                    roles=tuple(sorted(routine.roles)),
                    dependencies=tuple(deps) + parameter_dependencies(body, parameters),
                    file_id=routine.body_span.file_id,
                    body_start=routine.body_span.char_start,
                    body_end=routine.body_span.char_end,
                    helper_verified=(not self.unverified_pks)
                    if routine.name.casefold() == "добавитьпкс"
                    else (not self.unverified_pktch)
                    if routine.name.casefold() == "добавитьпктч"
                    else None,
                    parameters_text=routine.parameters_raw if editable_code else None,
                    frame_comment=source.text[frame_start:comment_start],
                )
            )
            if not editable_code and (
                routine.roles & {"handler", "algorithm", "event", "dispatcher"}
                or routine_names[routine.name.casefold()] > 1
            ):
                reason = (
                    "name_collision"
                    if routine_names[routine.name.casefold()] > 1
                    else "layout_overlap"
                    if dispatcher_overlap
                    else "non_template_code_frame"
                )
                self.entries[-1] = replace(self.entries[-1], reason=reason)
            self.retain(routine)
        conversion_events = []
        for n, item in enumerate(doc.conversion.events, 1):
            unit = next(
                (r for r in code if r.logical_id == self.ids.get(item.target_id or "")), None
            )
            state = unit.state if unit else "retained"
            if state != "editable":
                self.retain(item, self.ids["conversion"])
            conversion_events.append(
                Event(
                    **self.common(item, n, state),
                    event=item.event,
                    target=self.reference(
                        "code_unit", item.target_id, item.target_name, item.resolution
                    ),
                )
            )
        uses = tuple(
            RuleUse(
                **self.common(
                    u,
                    n,
                    "editable"
                    if (u.direction or not u.guards) and not self.unsafe(u)
                    else "retained",
                ),
                rule=self.reference("rule", u.rule_id, u.target_name),
                direction=cast(Direction | None, u.direction or ("both" if not u.guards else None)),
            )
            for n, u in enumerate(doc.rule_uses, 1)
        )
        for use, item in zip(uses, doc.rule_uses, strict=True):
            if use.state != "editable":
                self.retain(item, reason="opaque_rule_use", lock=True)
        guards = tuple(
            Guard(
                **self.common(g, n, "editable" if self.exact_direction(g) else "retained"),
                expression=g.expression_raw,
                branch=g.branch,
                guard_kind=g.guard_kind,
                parent_id=self.ids.get(g.parent_id or ""),
                direction=cast(Direction | None, g.known_direction),
            )
            for n, g in enumerate((g for g in doc.guards if g.entity_id in self.guard_ids), 1)
        )
        for guard in doc.guards:
            if guard.entity_id in self.guard_ids and not self.exact_direction(guard):
                self.retain(guard, reason="opaque_guard", lock=True)
        cases = tuple(
            DispatcherCase(
                **{**self.common(c, n, "editable"), "guards": ()},
                dispatcher=self.reference("code_unit", c.dispatcher_id, ""),
                target=self.reference(
                    "code_unit",
                    next(
                        (
                            r.entity_id
                            for r in doc.routines
                            if r.name.casefold() == c.target.raw.casefold()
                            and routine_names[r.name.casefold()] == 1
                        ),
                        None,
                    ),
                    c.target.raw,
                ),
                arguments=tuple(import_expression(a) for a in c.arguments),
                returns=c.returns,
            )
            for n, c in enumerate(doc.dispatcher_cases, 1)
        )
        updated_cases = []
        for member, case in zip(cases, doc.dispatcher_cases, strict=True):
            routine = next(r for r in doc.routines if r.entity_id == case.dispatcher_id)
            header = next(
                (
                    s
                    for s in reversed(self.rule_statements(routine))
                    if s.head in ("если", "иначеесли") and s.span.char_start < case.span.char_start
                ),
                None,
            )
            source_text = self.sources[case.span.file_id].text
            line_end = source_text.find("\n", case.span.char_end)
            branch_end = line_end if line_end >= 0 else len(source_text)
            inner_comment = header is not None and any(
                t.kind == "comment"
                for t in tokenize(source_text[header.span.char_end : branch_end])
            )
            method = next((u for u in code if u.logical_id == member.target.target_id), None)
            if (method is not None and member.name != method.name) or inner_comment:
                member = replace(member, state="retained")
                self.entries = [
                    replace(e, state="retained", reason="non_template_dispatcher_case")
                    if e.logical_id == member.logical_id
                    else e
                    for e in self.entries
                ]
            updated_cases.append(member)
        cases = tuple(updated_cases)
        for item in doc.unknown:
            self.common(item, len(self.entries) + 1, "retained")
            owner = self.ids.get(item.owner_id or "")
            if owner is None:
                containing = next(
                    (
                        r
                        for r in (*doc.pko, *doc.pod)
                        if r.span.char_start <= item.span.char_start < r.span.char_end
                    ),
                    None,
                )
                owner = self.ids.get(containing.entity_id) if containing else None
            self.retain(item, owner, reason=item.reason, lock=True)
        # Полный исходник — baseline, а не обещание его регенерации. Комментарии между
        # методами сохраняются отдельными блоками; границы предоставляет читатель.

        # guards внутри непрозрачных тел: они не становятся отдельными решениями.
        mapped = {entry.reader_id for entry in self.source_map}
        for entity in doc.entities():
            if entity.entity_id in mapped:
                continue
            source = self.sources[entity.span.file_id]
            span = entity.span
            self.source_map.append(
                SourceMapEntry(
                    entity.entity_id,
                    self.ids[entity.entity_id],
                    self.address(entity),
                    source.file_id,
                    source.sha256,
                    span.char_start,
                    span.char_end,
                    span.line_start,
                    span.line_end,
                )
            )
        report = ImportReport(
            tuple(self.entries),
            tuple(
                (d.code, self.address(d))
                for d in doc.diagnostics
                if not (
                    d.code == "helper_semantics_unverified" and self.helper_variant == "legacy-v2"
                )
            ),
        )
        source = doc.files[0]
        model = ManagerModel(
            project_id=self.project_id,
            header=Header(
                manager_name=name,
                helper_variant=cast(Literal["modern", "legacy-v2"] | None, self.helper_variant),
                clear_data_column=self.clear_data_column,
                module_identifier=self.module_identifier,
                interface_version=doc.manager_version,
                title=Value("string", doc.conversion.title)
                if doc.conversion.title is not None
                else Value(),
                generated_at=Value("string", doc.conversion.generated_at_raw)
                if doc.conversion.generated_at_raw is not None
                else Value(),
                text_style=TextStyle(
                    # Смешанные тела не меняют стиль рамок: его задаёт первая физическая строка.
                    newline=cast(
                        Literal["\n", "\r\n", "mixed"],
                        source.newline
                        if source.newline != "mixed"
                        else "\r\n"
                        if source.text[: source.text.find("\n") + 1].endswith("\r\n")
                        else "\n",
                    ),
                    bom=source.bom,
                ),
            ),
            host=host,
            format_bindings=bindings,
            executor_profile=profile,
            pko=pko,
            pod=tuple(pod),
            pkpd=tuple(pkpd),
            parameters=tuple(parameters),
            code_units=tuple(code),
            conversion_events=tuple(conversion_events),
            rule_uses=uses,
            guards=guards,
            dispatcher_cases=cases,
            dispatcher_unknown_policy="retained",
            source_map=tuple(self.source_map),
            retained_blocks=tuple(self.blocks),
            source_files=tuple(
                SourceSnapshot(s.file_id, PureWindowsPath(s.path).name, s.text, s.sha256, s.bom)
                for s in doc.files
            ),
            import_report=report,
        )
        # Карта исходника служит диагностике; порядок и владение задаёт раскладка.
        model = self.build_layout(model)
        model = replace(model, module_styles=infer_module_styles(model))
        report = replace(
            report, entries=tuple(self.entries), source_partition=partition_report(model)
        )
        addresses = model_addresses(model)
        report = replace(
            report,
            entries=tuple(
                replace(entry, address=addresses.get(entry.logical_id, entry.address))
                for entry in report.entries
            ),
            diagnostics=report.diagnostics + declarative_diagnostics(model),
        )
        model = replace(model, import_report=report).with_revision()
        validate_model(model)
        return model, report
