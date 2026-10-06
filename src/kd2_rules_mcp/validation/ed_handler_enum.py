"""Перечисление XDTO приходит структурой: XDTO:6264–6310; риск удаления:7489–7497."""

from collections import deque
from dataclasses import dataclass

from kd2_rules_mcp.ed import model as ed
from kd2_rules_mcp.ed.canonical import model_addresses
from kd2_rules_mcp.ed.lexer import Token, lex, split_arguments
from kd2_rules_mcp.ed.schema.model import EdSchema, SchemaType
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.ed.schema.resolver import property_type
from kd2_rules_mcp.ed.writer_model import ManagerModel
from kd2_rules_mcp.validation.ed_schema import enum_values
from kd2_rules_mcp.validation.report import ValidationReport

CHECK = "ed.handler.format_enum_as_string"
EVENTS = {"ПриОбработке", "ПриКонвертацииДанныхXDTO", "ПередЗаписьюПолученныхДанных"}
STRING_FUNCTIONS = {"стрнайти", "врег", "strfind", "upper"}


@dataclass(frozen=True)
class EnumUse:
    property: str
    line: int


def _arguments(tokens: tuple[Token, ...], opening: int):
    depth = 0
    for end in range(opening, len(tokens)):
        t = tokens[end]
        if t.kind == "symbol":
            depth += (t.value == "(") - (t.value == ")")
        if depth == 0:
            return split_arguments(tokens[opening + 1 : end]), end
    return (), opening


def enum_uses(body: str, resolve_enum, *, seeds: dict[str, str] | None = None):
    """Имена отслеживаются внутри тела; извлечение .Значение снимает метку.

    Возвращает также аргументы локальных вызовов: принимающая обёртка нередко передаёт
    перечисление отдельному алгоритму. Поток ветвей и произвольные преобразования не доказываются.
    """
    offsets = (0, *(i + 1 for i, c in enumerate(body) if c == "\n"))
    source = ed.SourceFile("enum-body", "", body, "", offsets)
    aliases = {k.casefold(): v for k, v in (seeds or {}).items()}
    warnings, calls = [], []
    for statement in lex(source).statements:
        tokens = statement.tokens
        # Выходной аргумент Структура.Свойство (XDTO содержит структуру данных формата).
        for i, t in enumerate(tokens):
            if (
                t.folded == "данныеxdto"
                and i + 3 < len(tokens)
                and tokens[i + 1].value == "."
                and tokens[i + 2].folded in ("свойство", "property")
            ):
                args, _ = _arguments(tokens, i + 3)
                if len(args) == 2 and len(args[0]) == len(args[1]) == 1:
                    name, variable = args[0][0], args[1][0]
                    if name.kind == "string" and variable.kind == "identifier":
                        path = resolve_enum(name.value)
                        aliases.pop(variable.folded, None)
                        if path:
                            aliases[variable.folded] = path

        reads = []
        for i, t in enumerate(tokens):
            if t.kind != "identifier" or (i and tokens[i - 1].value == "."):
                continue
            end, path = i + 1, aliases.get(t.folded)
            if t.folded == "данныеxdto":
                parts = []
                while (
                    end + 1 < len(tokens)
                    and tokens[end].value == "."
                    and tokens[end + 1].kind == "identifier"
                ):
                    parts.append(tokens[end + 1].value)
                    end += 2
                if (
                    not parts
                    and end + 2 < len(tokens)
                    and tokens[end].value == "["
                    and tokens[end + 1].kind == "string"
                    and tokens[end + 2].value == "]"
                ):
                    parts.append(tokens[end + 1].value)
                    end += 3
                    while (
                        end + 1 < len(tokens)
                        and tokens[end].value == "."
                        and tokens[end + 1].kind == "identifier"
                    ):
                        parts.append(tokens[end + 1].value)
                        end += 2
                path = resolve_enum(".".join(parts)) if parts else None
            elif end < len(tokens) and tokens[end].value in (".", "["):
                path = None  # Член структуры (в частности .Значение) — уже не сама структура.
            if path:
                reads.append((i, end, path))
        for start, end, path in reads:
            comparison = (
                end + 1 < len(tokens)
                and tokens[end].value in ("=", "<>", "<", ">", "<=", ">=")
                and tokens[end + 1].kind == "string"
            ) or (
                start > 1
                and tokens[start - 1].value in ("=", "<>", "<", ">", "<=", ">=")
                and tokens[start - 2].kind == "string"
            )
            # Присваивание строкового значения не является сравнением.
            if start == 0 and end < len(tokens) and tokens[end].value == "=":
                comparison = False
            if comparison:
                warnings.append(
                    EnumUse(path, source.span(tokens[start].start, tokens[start].end).line_start)
                )
        for i, t in enumerate(tokens[:-1]):
            if t.kind != "identifier" or tokens[i + 1].value != "(":
                continue
            args, end = _arguments(tokens, i + 1)
            if t.folded in STRING_FUNCTIONS:
                for start, stop, path in reads:
                    if i + 1 < start < stop <= end:
                        warnings.append(EnumUse(path, source.span(t.start, t.end).line_start))
            if not i or tokens[i - 1].value != ".":
                bindings = {}
                for position, arg in enumerate(args):
                    for start, stop, path in reads:
                        if arg and tokens[start:stop] == arg:
                            bindings[position] = path
                if bindings:
                    calls.append((t.value, bindings))
        if len(tokens) > 2 and tokens[0].kind == "identifier" and tokens[1].value == "=":
            name = tokens[0].folded
            aliases.pop(name, None)
            right = tokens[2:]
            if right and right[-1].value == ";":
                right = right[:-1]
            for start, stop, path in reads:
                if tokens[start:stop] == right:
                    aliases[name] = path
    return tuple(dict.fromkeys(warnings)), calls


def validate_enum_handlers(
    model: ManagerModel,
    document: ed.EdDocument,
    profile: ValidationProfile,
    *,
    include_preserved: bool = False,
) -> ValidationReport:
    """Редактируемые тела получения и их локальные помощники; preserve — только калибровка."""
    report = ValidationReport()
    app = Applicability.build(document, profile)
    methods = {r.name.casefold(): r for r in document.routines}
    units = {u.name.casefold(): u for u in model.code_units}
    addresses = model_addresses(model)
    queue = deque()
    for owner in (*document.pko, *document.pod):
        if app.evaluate(owner, "receive") is not True:
            continue
        if isinstance(owner, ed.ObjectRule):
            typ, _ = profile.owner_type(owner, "receive", app)
        else:
            typ, _ = profile.find_type(owner.format_selection.value or "")
        for event in owner.events:
            if event.event in EVENTS and app.evaluate(event, "receive", owner) is not False:
                queue.append((event.target_name.casefold(), typ, {}))
    visited = set()
    while queue:
        if len(visited) >= 512:
            report.skip(
                CHECK,
                "local_calls_limit: граф локальных вызовов слишком сложен; "
                "проверьте оставшиеся тела вручную",
            )
            break
        name, typ, seeds = queue.popleft()
        key = (name, typ.id if typ else "", tuple(sorted(seeds.items())))
        if key in visited:
            continue
        visited.add(key)
        unit, routine = units.get(name), methods.get(name)
        if not unit or not routine or not unit.body.strip():
            continue
        if not include_preserved and (unit.state != "editable" or unit.origin != "authored"):
            continue
        address = addresses[unit.logical_id]
        schema = profile.schema
        if schema is None:
            report.skip(CHECK, f"schema_required: {address}; откройте ed_schema_open нужной версии")
            continue
        if typ is None:
            report.skip(
                CHECK, f"format_type_unavailable: {address}; не определён тип формата владельца"
            )
            continue

        def resolve_enum(path: str, owner: SchemaType = typ, selected: EdSchema = schema):
            resolved = profile.resolve(owner, path)
            if resolved.status != "resolved":
                return None
            prop = profile.properties[resolved.property_ids[0]]
            target = property_type(selected, prop)
            return path if target and enum_values(selected, target)[0] else None

        source = next(s for s in document.files if s.file_id == routine.span.file_id)
        body = (
            source.text[routine.body_span.char_start : routine.body_span.char_end]
            .removeprefix("\r\n")
            .removeprefix("\n")
        )
        warnings, calls = enum_uses(body, resolve_enum, seeds=seeds)
        for use in warnings:
            report.warning(
                CHECK,
                address,
                f"{address}, строка тела {use.line}: перечисление формата «{use.property}» "
                "из ДанныеXDTO сравнивается со строкой или передаётся строковой функции "
                "как структура. Используйте .Значение (XDTO:6264–6310; грабля 22, "
                "kd2-ed-rules/references/pitfalls.md). Выключение всех ПКО может пометить "
                "существующий объект на удаление (XDTO:7489–7497).",
            )
        for target, bindings in calls:
            method = methods.get(target.casefold())
            if method:
                parameters = {
                    method.parameters[i].name: p
                    for i, p in bindings.items()
                    if i < len(method.parameters) and method.parameters[i].name
                }
                queue.append((target.casefold(), typ, parameters))
    report.issues = list(dict.fromkeys(report.issues))
    return report
