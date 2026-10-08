"""Отправитель допустим только для состава плана: ОДСер:3898–3915, грабля 25."""

from dataclasses import dataclass

from kd_rules_mcp.ed import model as ed
from kd_rules_mcp.ed.canonical import model_addresses
from kd_rules_mcp.ed.lexer import Token, lex
from kd_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd_rules_mcp.ed.writer_model import ManagerModel
from kd_rules_mcp.structures.xmldump import KINDS
from kd_rules_mcp.validation.ed_handler_enum import EVENTS, _arguments
from kd_rules_mcp.validation.ed_structure_snapshot import (
    COLLECTIONS,
    StructureSnapshot,
    metadata_key,
)
from kd_rules_mcp.validation.report import ValidationReport

CHECK = "ed.handler.sender_outside_plan"
FACTORIES = {
    "регистрысведений": "создатьнаборзаписей",
    "регистрынакопления": "создатьнаборзаписей",
    "регистрыбухгалтерии": "создатьнаборзаписей",
    "регистрырасчета": "создатьнаборзаписей",
    "справочники": "создатьэлемент",
    "документы": "создатьдокумент",
}
MetadataKey = tuple[str, str]


@dataclass(frozen=True)
class SenderUse:
    object_key: MetadataKey
    line: int


def _object_type(tokens: tuple[Token, ...], aliases: dict[str, MetadataKey]):
    if len(tokens) == 1 and tokens[0].kind == "identifier":
        return aliases.get(tokens[0].folded)
    if (
        len(tokens) == 7
        and tokens[0].folded in FACTORIES
        and tokens[1].value == tokens[3].value == "."
        and tokens[2].kind == "identifier"
        and tokens[4].folded == FACTORIES[tokens[0].folded]
        and tokens[5].value == "("
        and tokens[6].value == ")"
    ):
        return COLLECTIONS[tokens[0].folded].casefold(), tokens[2].folded
    return None


def _content_guard(tokens: tuple[Token, ...]) -> bool:
    return any(
        tokens[i].folded == "состав"
        and tokens[i + 1].value == "."
        and tokens[i + 2].folded == "содержит"
        and tokens[i + 3].value == "("
        for i in range(len(tokens) - 3)
    )


def sender_uses(body: str, *, seeds: dict[str, MetadataKey] | None = None):
    """Лексические типы и аргументы локальных вызовов; поток ветвей не доказывается."""
    offsets = (0, *(i + 1 for i, char in enumerate(body) if char == "\n"))
    source = ed.SourceFile("sender-body", "", body, "", offsets)
    aliases = {name.casefold(): key for name, key in (seeds or {}).items()}
    guards: list[bool] = []
    warnings, calls = [], []
    for statement in lex(source).statements:
        tokens = statement.tokens
        if statement.head == "если":
            guards.append(_content_guard(tokens))
            continue
        if statement.head in {"иначе", "иначеесли"}:
            if guards:
                guards[-1] = _content_guard(tokens)
            continue
        if statement.head == "конецесли":
            if guards:
                guards.pop()
            continue
        if (
            len(tokens) > 5
            and tokens[0].kind == "identifier"
            and tokens[1].value == tokens[3].value == "."
            and tokens[2].folded == "обменданными"
            and tokens[4].folded == "отправитель"
            and tokens[5].value == "="
            and not any(guards)
            and (key := aliases.get(tokens[0].folded)) is not None
        ):
            warnings.append(SenderUse(key, source.span(tokens[0].start, tokens[0].end).line_start))
        for i, token in enumerate(tokens[:-1]):
            if (
                token.kind != "identifier"
                or tokens[i + 1].value != "("
                or (i and tokens[i - 1].value == ".")
            ):
                continue
            arguments, _ = _arguments(tokens, i + 1)
            bindings = {
                position: key
                for position, argument in enumerate(arguments)
                if (key := _object_type(argument, aliases)) is not None
            }
            # Проверка состава в вызывающем теле не удостоверяет тело помощника.
            calls.append((token.folded, bindings))
        if len(tokens) > 2 and tokens[0].kind == "identifier" and tokens[1].value == "=":
            right = tokens[2:-1] if tokens[-1].value == ";" else tokens[2:]
            key = _object_type(right, aliases)
            aliases.pop(tokens[0].folded, None)
            if key:
                aliases[tokens[0].folded] = key
    return tuple(dict.fromkeys(warnings)), calls


def validate_sender_handlers(
    model: ManagerModel,
    document: ed.EdDocument,
    profile: ValidationProfile,
    structure: StructureSnapshot | None,
    plan_name: str,
    additions: tuple[tuple[str, str, str], ...] = (),
    registration_objects: tuple[str, ...] = (),
    *,
    include_preserved: bool = False,
) -> ValidationReport:
    """Тела получения и один уровень локальных вызовов; preserve — только калибровка."""
    report = ValidationReport()
    if structure is None:
        report.skip(CHECK, "structure_required: нужна структура конфигурации с составом плана")
        return report
    plan = structure.objects.get(("планобмена", plan_name.casefold()))
    if plan is None or not any(
        prop.kind == "СоставПланаОбмена" for rows in plan.properties.values() for prop in rows
    ):
        report.skip(CHECK, f"plan_content_unavailable: ПланОбмена/{plan_name}; состав недоступен")
        return report
    included = {
        name.casefold()
        for rows in plan.properties.values()
        for prop in rows
        if prop.kind == "ЭлементСоставаПланаОбмена"
        for name in prop.types
    }
    added = {(KINDS[tag][1].casefold(), name.casefold()) for tag, name, _ in additions}
    added.update(
        (kind.casefold(), name.casefold())
        for full_name in registration_objects
        for kind, _, name in (full_name.partition("."),)
    )
    app = Applicability.build(document, profile)
    methods = {routine.name.casefold(): routine for routine in document.routines}
    units = {unit.name.casefold(): unit for unit in model.code_units}
    sources = {source.file_id: source for source in document.files}
    addresses = model_addresses(model)

    def check_body(name: str, seeds: dict[str, MetadataKey], caller: str = ""):
        unit, routine = units.get(name), methods.get(name)
        if unit is None or routine is None:
            return ()
        if not include_preserved and (unit.state != "editable" or unit.origin != "authored"):
            return ()
        source = sources[routine.span.file_id]
        body = source.text[routine.body_span.char_start : routine.body_span.char_end]
        body = body.removeprefix("\r\n").removeprefix("\n")
        uses, calls = sender_uses(body, seeds=seeds)
        address = addresses[unit.logical_id]
        for use in uses:
            obj = structure.objects.get(use.object_key)
            if obj is None or use.object_key in added or obj.type_name.casefold() in included:
                continue
            report.warning(
                CHECK,
                address,
                f"{address}, строка тела {use.line}"
                + (f" (вызов из обработчика {caller})" if caller else "")
                + f": {obj.kind}.{obj.name} вне состава плана {plan_name}; присваивание "
                "ОбменДанными.Отправитель вызывает «Несоответствие типов». Оберните его условием "
                "Узел.Метаданные().Состав.Содержит(Объект.Метаданные()) либо добавьте объект "
                "в состав плана через registration_objects (ОДСер:3909–3910; грабля 25).",
            )
        return calls

    for owner in (*document.pko, *document.pod):
        if app.evaluate(owner, "receive") is not True:
            continue
        key = (
            metadata_key(owner.configuration_object.value)[0]
            if isinstance(owner, ed.ObjectRule)
            else None
        )
        for event in owner.events:
            if event.event not in EVENTS or app.evaluate(event, "receive", owner) is False:
                continue
            name = event.target_name.casefold()
            routine, unit = methods.get(name), units.get(name)
            if routine is None or unit is None:
                continue
            seeds = {
                parameter.name: key
                for parameter in routine.parameters
                if key
                and parameter.name
                and parameter.name.casefold() in {"полученныеданные", "данныеиб"}
            }
            for target, bindings in check_body(name, seeds):
                helper = methods.get(target)
                if helper is not None:
                    parameters = {
                        helper.parameters[position].name: value
                        for position, value in bindings.items()
                        if position < len(helper.parameters) and helper.parameters[position].name
                    }
                    check_body(target, parameters, addresses[unit.logical_id])
    report.issues = list(dict.fromkeys(report.issues))
    return report
