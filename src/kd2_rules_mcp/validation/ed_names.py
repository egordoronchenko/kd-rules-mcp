"""Лексическая область имён редактируемых тел BSL, без исполнения кода."""

import hashlib
from collections.abc import Collection
from dataclasses import dataclass

from kd2_rules_mcp.ed.canonical import model_addresses
from kd2_rules_mcp.ed.lexer import lex
from kd2_rules_mcp.ed.model import EdDocument, SourceFile
from kd2_rules_mcp.ed.writer_model import ManagerModel
from kd2_rules_mcp.validation.ed_globals import (
    PLATFORM_ENUMS,
    PLATFORM_FUNCTIONS,
    PLATFORM_PROPERTIES,
    PLATFORM_TYPES,
)
from kd2_rules_mcp.validation.report import ValidationReport

_KEYWORDS = frozenset(
    [
        "если",
        "тогда",
        "иначе",
        "иначеесли",
        "конецесли",
        "для",
        "каждого",
        "из",
        "по",
        "цикл",
        "конеццикла",
        "пока",
        "перем",
        "экспорт",
        "знач",
        "возврат",
        "прервать",
        "продолжить",
        "попытка",
        "исключение",
        "конецпопытки",
        "вызватьисключение",
        "и",
        "или",
        "не",
        "новый",
        "истина",
        "ложь",
        "неопределено",
        "null",
        "goto",
        "перейти",
        "if",
        "then",
        "else",
        "elsif",
        "elseif",
        "endif",
        "for",
        "each",
        "in",
        "to",
        "do",
        "enddo",
        "while",
        "var",
        "export",
        "val",
        "return",
        "break",
        "continue",
        "try",
        "except",
        "endtry",
        "raise",
        "and",
        "or",
        "not",
        "new",
        "true",
        "false",
        "undefined",
        "процедура",
        "конецпроцедуры",
        "функция",
        "конецфункции",
        "procedure",
        "endprocedure",
        "function",
        "endfunction",
    ]
)

# Ключи структур обёрток не вводят переменные в теле обработчика.
# XDTO:8416–8437 (ПОД), 8549–8558 (отправка), 7198–7202 (отложенное событие).
_WRAPPER_HINTS = {
    "объектобработки": ("ДанныеXDTO", "ДанныеИБ"),
    "объект": ("ДанныеИБ", "ПолученныеДанные"),
    "полученныеданные": ("ДанныеXDTO",),
}


@dataclass(frozen=True)
class UnknownName:
    name: str
    line: int


def unknown_names(
    body: str,
    parameters: Collection[str],
    *,
    common_modules: Collection[str],
    module_methods: Collection[str] = (),
    global_methods: Collection[str] = (),
) -> tuple[UnknownName, ...]:
    """Присваивание вводит имя после RHS; строки, комментарии и члены цепочек исключены.

    Перем и переменные циклов вводятся явно. Выходной аргумент Свойство вводит локальное
    имя после вызова (синтакс-помощник: Структура.Свойство, параметр Значение).
    Проверка не доказывает присваивание во всех ветках управления.
    """
    offsets = [0]
    offsets.extend(i + 1 for i, c in enumerate(body) if c == "\n")
    source = SourceFile(
        "scope", "", body, hashlib.sha256(body.encode()).hexdigest(), tuple(offsets)
    )
    known = {n.casefold() for n in parameters} | {n.casefold() for n in common_modules}
    known |= PLATFORM_PROPERTIES | PLATFORM_ENUMS | {"этотобъект", "thisobject"}
    calls = PLATFORM_FUNCTIONS | {n.casefold() for n in (*module_methods, *global_methods)}
    result = []
    for statement in lex(source).statements:
        tokens = statement.tokens
        if statement.head in ("перем", "var"):
            known.update(
                t.folded for t in tokens[1:] if t.kind == "identifier" and t.folded not in _KEYWORDS
            )
            continue
        introduced: set[int] = set()
        after: set[str] = set()
        if len(tokens) >= 2 and tokens[0].kind == "identifier" and tokens[1].value == "=":
            introduced.add(0)
            after.add(tokens[0].folded)
        if statement.head in ("для", "for"):
            position = 2 if len(tokens) > 2 and tokens[1].folded in ("каждого", "each") else 1
            if len(tokens) > position and tokens[position].kind == "identifier":
                introduced.add(position)
                after.add(tokens[position].folded)
        # Свойство(Имя, Значение) имеет выходной аргумент; не любое чтение аргумента.
        for i, token in enumerate(tokens):
            if (
                token.folded not in ("свойство", "property")
                or i + 1 >= len(tokens)
                or tokens[i + 1].value != "("
            ):
                continue
            depth, commas = 0, []
            for j in range(i + 1, len(tokens)):
                if tokens[j].value == "(":
                    depth += 1
                elif tokens[j].value == ")":
                    depth -= 1
                    if depth == 0:
                        if (
                            len(commas) == 1
                            and j == commas[0] + 2
                            and tokens[j - 1].kind == "identifier"
                        ):
                            introduced.add(j - 1)
                            after.add(tokens[j - 1].folded)
                        break
                elif tokens[j].value == "," and depth == 1:
                    commas.append(j)
        for i, token in enumerate(tokens):
            if token.kind != "identifier" or i in introduced or token.folded in _KEYWORDS:
                continue
            previous = tokens[i - 1] if i else None
            following = tokens[i + 1] if i + 1 < len(tokens) else None
            if previous and previous.value == ".":
                continue
            if previous and previous.value == "~":  # Метка перехода, не переменная.
                continue
            if previous and previous.folded in ("новый", "new") and token.folded in PLATFORM_TYPES:
                continue
            if token.folded in known or (
                following and following.value == "(" and token.folded in calls
            ):
                continue
            result.append(UnknownName(token.value, source.span(token.start, token.end).line_start))
        known.update(after)
    return tuple(dict.fromkeys(result))


def validate_handler_names(
    model: ManagerModel,
    document: EdDocument,
    *,
    common_modules: Collection[str] | None,
    global_methods: Collection[str] = (),
    include_preserved: bool = False,
) -> ValidationReport:
    """Параметры фактической сигнатуры; стандартная рамка задаётся _handler_signature.

    Сохранённые тела не удостоверяются. include_preserved нужен только для калибровки корпуса.
    """
    report = ValidationReport()
    addresses = model_addresses(model)
    routines = {r.name.casefold(): r for r in document.routines}
    sources = {s.file_id: s for s in document.files}
    for unit in model.code_units:
        if not (set(unit.roles) & {"handler", "algorithm", "event"}):
            continue
        if (unit.state != "editable" or unit.origin == "imported_opaque") and not include_preserved:
            continue
        routine = routines.get(unit.name.casefold())
        if routine is None:
            continue  # Отсутствие метода диагностирует проверка рамки.
        body = sources[routine.span.file_id].text[
            routine.body_span.char_start : routine.body_span.char_end
        ]
        body = body.removeprefix("\r\n").removeprefix("\n")
        if not body.strip():
            continue
        address = addresses[unit.logical_id]
        if common_modules is None:
            report.skip(
                "ed.handler.unknown_name",
                f"structure_required: {address}; "
                "нужна структура с доступным перечнем общих модулей",
            )
            continue
        parameters = tuple(p.name for p in routine.parameters if p.name)
        for unknown in unknown_names(
            body,
            parameters,
            common_modules=common_modules,
            module_methods=tuple(routines),
            global_methods=global_methods,
        ):
            hint = ""
            # XDTO:8416–8437 — ключ обёртки ПОД; имя формального параметра другое.
            suggestions = _WRAPPER_HINTS.get(unknown.name.casefold(), ())
            parameter_names = {p.casefold() for p in parameters}
            replacement = next((p for p in suggestions if p.casefold() in parameter_names), None)
            if replacement:
                direction = " при получении" if replacement == "ДанныеXDTO" else ""
                hint = f" Используйте {replacement}{direction} (XDTO:8416–8437 и обёртки события)."
            report.error(
                "ed.handler.unknown_name",
                address,
                f"{address}, строка тела {unknown.line}: «{unknown.name}» — имя не является "
                f"параметром события ({', '.join(parameters) or 'нет параметров'}), "
                "локальной переменной, методом модуля или общим модулем." + hint,
            )
    return report
