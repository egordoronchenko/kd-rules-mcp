"""Один детерминированный After-hook; формы §3.1–3.3 спецификации автора."""

import hashlib
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from types import MappingProxyType

from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.lexer import lex, split_arguments, tokenize
from kd2_rules_mcp.ed.model import EdDocument, Expr, ObjectRule, SourceFile

from .context import AuthoringContext
from .model import (
    FILLER,
    AddHeaderProperty,
    AuthoringInputs,
    AuthoringPreconditionError,
    CanonicalHeaderProperty,
    ExtensionIdentity,
    Failure,
    GeneratedHook,
    digest,
    order_operations,
)

# Ключевые слова языка, включая английские платформенные эквиваленты.
RESERVED = frozenset(
    [
        "если",
        "тогда",
        "иначе",
        "иначеесли",
        "конецесли",
        "для",
        "каждого",
        "по",
        "из",
        "цикл",
        "конеццикла",
        "пока",
        "процедура",
        "конецпроцедуры",
        "функция",
        "конецфункции",
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
        "новый",
        "истина",
        "ложь",
        "неопределено",
        "null",
        "и",
        "или",
        "не",
        "добавитьобработчик",
        "удалитьобработчик",
        "выполнить",
        "перейти",
        "if",
        "then",
        "else",
        "elsif",
        "elseif",
        "endif",
        "for",
        "each",
        "to",
        "in",
        "do",
        "enddo",
        "while",
        "procedure",
        "endprocedure",
        "function",
        "endfunction",
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
        "new",
        "true",
        "false",
        "undefined",
        "and",
        "or",
        "not",
        "addhandler",
        "removehandler",
        "execute",
        "goto",
    ]
)


def valid_identifier(value: str) -> bool:
    return (
        bool(value)
        and value.casefold() not in RESERVED
        and (value[0].isalpha() or value[0] == "_")
        and all(c.isalnum() or c == "_" for c in value)
    )


def identifier_failures(
    document: EdDocument,
    operations: Iterable[AddHeaderProperty],
    identity: ExtensionIdentity,
) -> tuple[Failure, ...]:
    failures = []
    names = {r.name.casefold() for r in document.routines}
    for op in operations:
        values = (identity.name, identity.prefix, op.configuration_attribute)
        invalid = next((v for v in values if not valid_identifier(v)), None)
        if op.new_attribute and (
            op.new_attribute.name.casefold() != op.configuration_attribute.casefold()
            or not op.new_attribute.name.casefold().startswith(identity.prefix.casefold())
        ):
            invalid = op.new_attribute.name
        if invalid is not None:
            message = f"Недопустимый идентификатор «{invalid}» или префикс нового реквизита"
        elif (identity.prefix + FILLER).casefold() in names:
            message = f"Имя перехватчика «{identity.prefix + FILLER}» занято"
        else:
            continue
        failures.append(Failure("ed.author.identifier_conflict", op.target.pko_address, message))
    return tuple(failures)


def bsl_string(value: str) -> str:
    """Строки ED не являются именами BSL; кавычки удваиваются (§3.3)."""
    if any(unicodedata.category(c).startswith("C") or c in ("\u2028", "\u2029") for c in value):
        raise ValueError("Управляющий символ в строковом литерале BSL")
    return '"' + value.replace('"', '""') + '"'


def generate_hook(
    base: EdDocument,
    operations: tuple[AddHeaderProperty, ...],
    identity: ExtensionIdentity,
    *,
    path: str = "modules/Module.bsl",
    inputs: AuthoringInputs | None = None,
    context: AuthoringContext | None = None,
) -> GeneratedHook:
    """Положения вызовов фиксируются при записи, а не повторным поиском текста."""
    if inputs is not None:
        from .canonical import canonicalize_operations
        from .operations import validate_preconditions

        if inputs.document is not base:
            raise ValueError("Документ генератора не совпадает с входным снимком")
        context = context or AuthoringContext(inputs)
        operations = canonicalize_operations(inputs, operations, context)
        invalid = identifier_failures(base, operations, identity)
        if invalid:
            raise AuthoringPreconditionError(invalid)
        validate_preconditions(
            inputs, operations, identity, version_scope="manager", context=context
        )
    if not all(isinstance(op, CanonicalHeaderProperty) for op in operations):
        raise ValueError("Для генерации требуются разрешённые операции или AuthoringInputs")
    operations = order_operations(operations)
    failures = identifier_failures(base, operations, identity)
    if failures:
        raise AuthoringPreconditionError(failures)
    if base.manager_version not in (1, 2, 3):
        raise AuthoringPreconditionError(
            tuple(
                Failure(
                    "ed.author.manager_signature",
                    op.target.pko_address,
                    "Сигнатура заполнителя не соответствует интерфейсу 1/2/3",
                )
                for op in operations
            )
        )
    index = context.index(base) if context else build_addresses(base)
    groups: dict[tuple[str, str], list[AddHeaderProperty]] = defaultdict(list)
    for op in operations:
        rule = index.find(op.target.pko_address)
        assert isinstance(rule, ObjectRule)
        groups[(op.target.direction, rule.declared_name or rule.name)].append(op)
    v3 = base.manager_version == 3
    parameters = (
        "КомпонентыОбмена, ПравилаКонвертации, ТолькоЗаголовки"
        if v3
        else "НаправлениеОбмена, ПравилаКонвертации"
    )
    lines = [
        "// Сформировано из проверенных операций прямых ПКС. "
        "Изменять через повторное порождение комплекта.",
        "// Заголовочный проход не изменяет свойства правил."
        if v3
        else "// Область действия: этот менеджер, указанные направления; "
        "карта версий не изменяется.",
        f'&После("{FILLER}")',
        f"Процедура {identity.prefix + FILLER}({parameters})",
        "",
    ]
    positions: dict[str, tuple[int, int]] = {}
    direction_positions: dict[str, tuple[int, int]] = {}
    offset = sum(len(line) + 1 for line in lines)

    def append(line: str) -> None:
        nonlocal offset
        lines.append(line)
        offset += len(line) + 1

    def emit(depth: int, line: str) -> None:
        append("\t" * depth + line)

    extra = int(v3)
    if v3:
        emit(1, "Если Не ТолькоЗаголовки Тогда")
    for direction in ("send", "receive"):
        selected = sorted((key for key in groups if key[0] == direction), key=lambda k: k[1])
        if not selected:
            continue
        depth = 1 + extra
        variable = "КомпонентыОбмена.НаправлениеОбмена" if v3 else "НаправлениеОбмена"
        literal = "Отправка" if direction == "send" else "Получение"
        condition = f'Если {variable} = "{literal}" Тогда'
        direction_positions[direction] = (offset + depth, offset + depth + len(condition))
        emit(depth, condition)
        for key in selected:
            emit(depth + 1, f'Правило = ПравилаКонвертации.Найти({bsl_string(key[1])}, "ИмяПКО");')
            emit(depth + 1, "Если Правило <> Неопределено Тогда")
            for op in sorted(
                groups[key], key=lambda o: (o.format_property, o.configuration_attribute)
            ):
                emit(
                    depth + 2,
                    "Свойство = Правило.Свойства.Найти("
                    f'{bsl_string(op.format_property)}, "СвойствоФормата");',
                )
                emit(depth + 2, "Если Свойство = Неопределено Тогда")
                call = (
                    f"ДобавитьПКС(Правило.Свойства, {bsl_string(op.configuration_attribute)}, "
                    f"{bsl_string(op.format_property)});"
                )
                start = offset + depth + 3
                positions[op.operation_id] = (start, start + len(call))
                emit(depth + 3, call)
                emit(depth + 2, "КонецЕсли;")
            emit(depth + 1, "КонецЕсли;")
        emit(depth, "КонецЕсли;")
    if v3:
        emit(1, "КонецЕсли;")
    append("")
    append("КонецПроцедуры")
    text = "\n".join(lines) + "\n"
    source = SourceFile(
        "generated-" + digest(path)[:24],
        path,
        text,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        (0, *(i + 1 for i, c in enumerate(text) if c == "\n")),
    )
    validate_hook_forms(source, base.manager_version, identity.prefix)
    arguments = {}
    for ident, (start, end) in positions.items():
        tokens = tokenize(text[start:end])
        args = split_arguments(tokens[2:-2])
        arguments[ident] = tuple(
            Expr(
                text[start + part[0].start : start + part[-1].end],
                source.span(start + part[0].start, start + part[-1].end),
                "string" if len(part) == 1 and part[0].kind == "string" else None,
                part[0].value if len(part) == 1 and part[0].kind == "string" else None,
                tuple(t.value for t in part if t.kind == "identifier"),
            )
            for part in args
        )
    return GeneratedHook(
        source,
        MappingProxyType({k: source.span(*v) for k, v in positions.items()}),
        MappingProxyType(arguments),
        MappingProxyType({k: source.span(*v) for k, v in direction_positions.items()}),
        operations,
    )


def validate_hook_forms(source: SourceFile, version: int, prefix: str) -> None:
    """Белый список форм и их вложенности; произвольный BSL наружу не выходит."""
    parsed = lex(source)
    if parsed.warnings:
        raise ValueError("Предупреждения лексера порождённого перехватчика")
    stack = []
    phase = "directive"
    pending = ""
    found_property = None
    calls = 0
    for statement in parsed.statements:
        tokens = statement.tokens
        values = tuple(t.folded if t.kind == "identifier" else t.value for t in tokens)
        if phase == "directive" and values == (f'&После("{FILLER}")',):
            phase = "header"
            continue
        parameters = (
            ("компонентыобмена", ",", "правилаконвертации", ",", "толькозаголовки")
            if version == 3
            else ("направлениеобмена", ",", "правилаконвертации")
        )
        if phase == "header" and values == (
            "процедура",
            (prefix + FILLER).casefold(),
            "(",
            *parameters,
            ")",
        ):
            phase = "body"
            continue
        if phase != "body":
            raise ValueError("Форма вне белого списка перехватчика")
        if values == ("если", "не", "толькозаголовки", "тогда") and version == 3 and not stack:
            stack.append("headers")
        elif values in tuple(
            (
                "если",
                *(
                    ("компонентыобмена", ".", "направлениеобмена")
                    if version == 3
                    else ("направлениеобмена",)
                ),
                "=",
                direction,
                "тогда",
            )
            for direction in ("Отправка", "Получение")
        ) and stack == (["headers"] if version == 3 else []):
            stack.append("direction")
        elif (
            len(values) == 11
            and values[:6] == ("правило", "=", "правилаконвертации", ".", "найти", "(")
            and tokens[6].kind == "string"
            and values[7:] == (",", "ИмяПКО", ")", ";")
            and stack[-1:] == ["direction"]
        ):
            pending = "rule"
        elif (
            values == ("если", "правило", "<>", "неопределено", "тогда")
            and pending == "rule"
            and stack[-1:] == ["direction"]
        ):
            stack.append("rule")
            pending = ""
        elif (
            len(values) == 13
            and values[:8] == ("свойство", "=", "правило", ".", "свойства", ".", "найти", "(")
            and tokens[8].kind == "string"
            and values[9:] == (",", "СвойствоФормата", ")", ";")
            and stack[-1:] == ["rule"]
        ):
            pending = "property"
            found_property = tokens[8].value
        elif (
            values == ("если", "свойство", "=", "неопределено", "тогда")
            and pending == "property"
            and stack[-1:] == ["rule"]
        ):
            stack.append("property")
            pending = ""
        elif (
            values[:2] == ("добавитьпкс", "(")
            and values[-2:] == (")", ";")
            and stack[-3:] == ["direction", "rule", "property"]
        ):
            args = split_arguments(tokens[2:-2])
            if (
                len(args) != 3
                or tuple(t.folded for t in args[0]) != ("правило", ".", "свойства")
                or any(len(a) != 1 or a[0].kind != "string" for a in args[1:])
                or args[2][0].value != found_property
            ):
                raise ValueError("ДобавитьПКС вне белого списка")
            calls += 1
        elif values == ("конецесли", ";") and stack and not pending:
            stack.pop()
        elif values == ("конецпроцедуры",) and not stack and calls:
            phase = "done"
        else:
            raise ValueError("Форма вне белого списка перехватчика")
    if phase != "done":
        raise ValueError("Незавершённый перехватчик")
