"""Лексическая граница тела и предусловия обработчиков; BSL не исполняется."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from kd2_rules_mcp.authoring.ed.context import AuthoringContext
from kd2_rules_mcp.authoring.ed.handlers import (
    DISPATCHER,
    EVENT_PARAMETERS,
    HandlerBindingPlan,
    handler_name,
)
from kd2_rules_mcp.authoring.ed.model import (
    FILLER,
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    AuthoringInputs,
    AuthoringPreconditionError,
    ExtensionIdentity,
    Failure,
    Notice,
    Operation,
    PreserveMissingHeaderProperty,
    SetObjectHandler,
)
from kd2_rules_mcp.ed import read_manager_text
from kd2_rules_mcp.ed.address import AmbiguousAddressError, EntityNotFoundError
from kd2_rules_mcp.ed.errors import EdFormatError
from kd2_rules_mcp.ed.lexer import Token, lex, split_arguments, tokenize
from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.ed.refs import EdReference, build_references
from kd2_rules_mcp.errors import EdAuthoringResourceLimitError
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key, standard_attribute

_FIELD_TITLES = {
    "format_property": "свойство формата",
    "configuration_attribute": "реквизит конфигурации",
    "conversion_rule": "правило конвертации",
    "received_property": "реквизит полученных данных",
    "pko_lookup": "поиск правила",
    "instruction_rule": "правило конвертации",
    "parameter": "параметр",
    "additional_key": "дополнительное свойство",
    "pod_use": "правило обработки",
}


def _named_field(name: str) -> str:
    """Имя поля входа в обратных кавычках, пояснение — по-русски."""
    title = _FIELD_TITLES.get(name)
    if title is None:
        return f"`{name}`"
    return f"{title} (`{name}`)"


# XDTO:502–517,4493–4540 — известные функции поиска возвращают строки правил.
RULE_LOOKUP_CALLS = frozenset(
    {
        "обменданнымиxdtoсервер.пкопоимени",
        "обменданнымиxdtoсервер.подпотипуссылкиxdto",
        "обменданнымиxdtoсервер.подпотипуобъектаxdto",
    }
)
# XDTO:78–210 — таблицы компонентов; само лексическое обращение запрещено §3.3.
RULE_TABLES = frozenset(
    {
        "правилаконвертацииобъектов",
        "правилаобработкиданных",
        "правилаконвертациипредопределенныхданных",
    }
)
MUTATING_METHODS = frozenset(
    {
        "вставить",
        "добавить",
        "удалить",
        "очистить",
        "загрузить",
        "загрузитьколонку",
        "заполнитьзначения",
        "сортировать",
        "свернуть",
        "сдвинуть",
        "insert",
        "add",
        "delete",
        "clear",
        "load",
        "loadcolumn",
        "fillvalues",
        "sort",
        "collapse",
        "move",
    }
)
# Перечень используется только для замечания, отсутствие в нём никогда не является отказом.
# XDTO:4469–4491 — проверка разрешения записи возвращает bool.
KNOWN_SAFE_CALLS = RULE_LOOKUP_CALLS | frozenset(
    {
        "обменданнымиxdtoсервер.разрешеназаписьобъекта",
        "структура",
        "массив",
        "соответствие",
        "строка",
        "число",
        "дата",
        "булево",
        "тип",
        "типзнч",
        "значениезаполнено",
        "стрнайти",
        "стрразделить",
        "стрсоединить",
        "сокрлп",
        "пустаястрока",
        "лев",
        "прав",
        "сред",
        "стрдлина",
        "данныеxdto.вставить",
        "данныеxdto.свойство",
        "данныеxdto.ключевыесвойства.свойство",
        "данныеxdto.общиесвойстваобъектовформата.свойство",
        "данныеxdto.удалить",
        "полученныеданные.дополнительныесвойства.вставить",
        "полученныеданные.дополнительныесвойства.свойство",
        "конвертациясвойств.найти",
        "конвертациясвойств.количество",
    }
)
MISSING_MARKER = "СвойстваОтсутствующиеВПолученныхДанных"
# XDTO:7193–7206,8018 — свойства объекта платформы, не реквизиты метаданных.
OBJECT_RUNTIME_FIELDS = frozenset({"дополнительныесвойства", "обменданными"})
# Набор записей регистра: Отбор — свойство платформы, не реквизит метаданных.
REGISTER_RUNTIME_FIELDS = OBJECT_RUNTIME_FIELDS | {"отбор"}
# Методы структуры у самого параметра КомпонентыОбмена; вложенное поле сюда не входит.
COMPONENT_MUTATORS = frozenset({"очистить", "вставить", "удалить", "clear", "insert", "delete"})
# Построители правил и точки входа заполнения: и корень, и квалифицированный вызов.
RULE_BUILDERS = frozenset(
    {
        "добавитьпкс",
        "добавитьпктч",
        DISPATCHER.casefold(),
        "выполнитьфункциюмодуляменеджера",
        FILLER.casefold(),
        "заполнитьправилаобработкиданных",
        "заполнитьправилаконвертациипредопределенныхданных",
    }
)
# Английский вариант скрипта распознаётся теми же проверками, что и русский.
_EN_KW = {
    "if": "если",
    "then": "тогда",
    "elsif": "иначеесли",
    "else": "иначе",
    "endif": "конецесли",
    "for": "для",
    "each": "каждого",
    "in": "из",
    "do": "цикл",
    "enddo": "конеццикла",
    "while": "пока",
    "try": "попытка",
    "except": "исключение",
    "endtry": "конецпопытки",
    "return": "возврат",
    "break": "прервать",
    "continue": "продолжить",
    "procedure": "процедура",
    "function": "функция",
    "endprocedure": "конецпроцедуры",
    "endfunction": "конецфункции",
    "var": "перем",
    "goto": "перейти",
    "execute": "выполнить",
    "eval": "вычислить",
    "export": "экспорт",
    "not": "не",
    "and": "и",
    "or": "или",
    "new": "новый",
    "undefined": "неопределено",
    "async": "асинх",
}
_STATEMENT_START = frozenset({"тогда", "иначе", "цикл", "попытка", "исключение"})
_OPENERS = {
    "если": "конецесли",
    "для": "конеццикла",
    "пока": "конеццикла",
    "попытка": "конецпопытки",
}
_GENERATED_NAME = re.compile(r"^(?P<prefix>.+)пко_(?P<digest>[0-9a-f]{16})$")
RULE_TABLE_HINT = (
    "лексическое обращение к таблице правил; читать правило через ОбменДаннымиXDTOСервер.ПКОПоИмени"
)


def _kw(folded: str) -> str:
    return _EN_KW.get(folded, folded)


def _handler_prefix(wrapper_name: str) -> str:
    match = _GENERATED_NAME.fullmatch(wrapper_name.casefold())
    return match.group("prefix") if match else ""


def _forbidden_call(name: str, prefix: str) -> bool:
    tail = name.rsplit(".", 1)[-1]
    if tail in RULE_BUILDERS:
        return True
    if not prefix:
        return False
    return tail == prefix + "диспетчер" or bool(
        _GENERATED_NAME.fullmatch(tail) and tail.startswith(prefix)
    )


def _at_statement_start(tokens: tuple[Token, ...], index: int) -> bool:
    return (
        index == 0
        or tokens[index - 1].value == ";"
        or _kw(tokens[index - 1].folded) in _STATEMENT_START
    )


def _directive_head(token: Token) -> str:
    text = token.value.lstrip("#&").strip()
    return text.split(None, 1)[0].casefold() if text else ""


def object_runtime_fields(owner: object) -> frozenset[str]:
    kind = getattr(owner, "kind", "")
    if isinstance(kind, str) and kind.casefold().startswith("регистр"):
        return REGISTER_RUNTIME_FIELDS
    return OBJECT_RUNTIME_FIELDS


def preset_procedure_text(procedure_name: str, pairs: tuple[tuple[str, str], ...]) -> str:
    """Тело §5.4. Пары — (свойство формата, реквизит); повтор реквизита не пишется."""
    chosen: list[tuple[str, str]] = []
    seen_attributes: set[str] = set()
    for format_name, attribute in sorted(pairs):
        if attribute in seen_attributes:
            continue
        seen_attributes.add(attribute)
        chosen.append((format_name, attribute))
    blocks = "".join(
        "\t\tЕсли Имена.Найти(" + _bsl_string(format_name) + ") <> Неопределено Тогда\n"
        "\t\t\tЕсли Имена.Найти(" + _bsl_string(attribute) + ") = Неопределено Тогда\n"
        "\t\t\t\tИмена.Добавить(" + _bsl_string(attribute) + ");\n"
        "\t\t\t\tПолученныеДанные.ДополнительныеСвойства.Вставить(\n"
        "\t\t\t\t\t" + _bsl_string(MISSING_MARKER) + ', СтрСоединить(Имена, ","));\n'
        "\t\t\tКонецЕсли;\n"
        "\t\tКонецЕсли;\n"
        for format_name, attribute in chosen
    )
    return (
        f"Процедура {procedure_name}(ПолученныеДанные, ДанныеИБ, "
        "КонвертацияСвойств, КомпонентыОбмена)\n"
        "\tЕсли ПолученныеДанные = Неопределено Тогда\n"
        "\t\tВозврат;\n"
        "\tКонецЕсли;\n"
        '\tОтсутствующие = "";\n'
        "\tЕсли ПолученныеДанные.ДополнительныеСвойства.Свойство(\n"
        "\t\t" + _bsl_string(MISSING_MARKER) + ", Отсутствующие) Тогда\n"
        '\t\tИмена = СтрРазделить(Отсутствующие, ",", Ложь);\n'
        "\t\tДля Индекс = 0 По Имена.ВГраница() Цикл\n"
        "\t\t\tИмена[Индекс] = СокрЛП(Имена[Индекс]);\n"
        "\t\tКонецЦикла;\n"
        f"{blocks}"
        "\tКонецЕсли;\n"
        "КонецПроцедуры\n"
    )


def _bsl_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _runtime_notice(version: int | None, event: str) -> str:
    path = (
        "отправка" if event == "ПриОтправкеДанных" else "получение (обычный путь и объектный путь)"
    )
    return (
        f"Шаблон {version}/{event}/{path} ещё не подтверждён живым обменом. "
        + ("Объектный путь доказан кодом, не обменом; " if event != "ПриОтправкеДанных" else "")
        + "текущий комплект не имеет runtime-проверки."
    )


@dataclass(frozen=True, slots=True)
class OpaqueCall:
    name: str
    line: int


@dataclass(frozen=True, slots=True)
class BodyCheck:
    references: tuple[EdReference, ...]
    failures: tuple[Failure, ...]
    body_sha256: str
    opaque_calls: tuple[OpaqueCall, ...] = ()
    guarded_reads: tuple[tuple[str, int], ...] = ()


def _root(tokens: tuple[Token, ...], index: int) -> bool:
    return tokens[index].kind == "identifier" and (index == 0 or tokens[index - 1].value != ".")


def _calls(tokens: tuple[Token, ...]) -> list[tuple[str, tuple[tuple[Token, ...], ...], int, int]]:
    """Статические вызовы с диапазонами; порядок исполнения не используется."""
    result = []
    for i, token in enumerate(tokens):
        if not _root(tokens, i) or token.folded in (
            "если",
            "пока",
            "не",
            "и",
            "или",
            "if",
            "while",
            "not",
            "and",
            "or",
        ):
            continue
        j = i + 1
        parts = [token.folded]
        while j + 1 < len(tokens) and tokens[j].value == "." and tokens[j + 1].kind == "identifier":
            parts.append(tokens[j + 1].folded)
            j += 2
        if j >= len(tokens) or tokens[j].value != "(":
            continue
        level = 1
        end = j + 1
        while end < len(tokens) and level:
            if tokens[end].value == "(":
                level += 1
            elif tokens[end].value == ")":
                level -= 1
            end += 1
        if not level:
            result.append((".".join(parts), split_arguments(tokens[j + 1 : end - 1]), i, end))
    return result


def _path_end(tokens: tuple[Token, ...], start: int) -> tuple[int, tuple[str, ...], bool]:
    """Конец непосредственного пути: члены, вызовы и индексы, без анализа выражений."""
    cursor = start + 1
    members = []
    mutation = False
    while cursor < len(tokens):
        if tokens[cursor].value == "." and cursor + 1 < len(tokens):
            member = tokens[cursor + 1].folded
            members.append(member)
            mutation |= (
                member in MUTATING_METHODS
                and cursor + 2 < len(tokens)
                and tokens[cursor + 2].value == "("
            )
            cursor += 2
        elif tokens[cursor].value in ("(", "["):
            if (
                tokens[cursor].value == "["
                and cursor + 2 < len(tokens)
                and tokens[cursor + 1].kind == "string"
                and tokens[cursor + 2].value == "]"
            ):
                members.append(tokens[cursor + 1].value.casefold())
            stack = [tokens[cursor].value]
            cursor += 1
            while cursor < len(tokens) and stack:
                if tokens[cursor].value in ("(", "["):
                    stack.append(tokens[cursor].value)
                elif tokens[cursor].value in (")", "]"):
                    stack.pop()
                cursor += 1
        else:
            break
    return cursor, tuple(members), mutation


def check_body(
    inputs: AuthoringInputs,
    operation: SetObjectHandler,
    context: AuthoringContext,
    *,
    wrapper_name: str = "АвторскийОбработчик",
    operations: tuple[Operation, ...] = (),
    known_attributes: Mapping[tuple[str, str], frozenset[str]] | None = None,
) -> BodyCheck:
    """Принятое тело не выходит из процедуры и не скрывает изменение правил."""
    body = operation.body
    failures: list[Failure] = []

    def fail(
        check: str, reason: str, token: Token | None = None, *, line: int | None = None
    ) -> None:
        if line is None:
            line = body.count("\n", 0, token.start) + 1 if token else 1
        failures.append(
            Failure(
                "ed.author." + check,
                operation.target.pko_address,
                f"Операция {operation.operation_id}: {reason}; "
                f"ПКО {operation.target.pko_address}, событие {operation.event}, "
                f"строка тела {line}",
                "<agent-body>",
                line,
            )
        )

    if len(body.encode("utf-8")) > 64 * 1024 or len(body.splitlines()) > 2000:
        raise EdAuthoringResourceLimitError("Тело превышает 64 KiB или 2000 строк")
    try:
        tokens = tuple(t for t in tokenize(body) if t.kind != "comment")
        if any(
            _root(tokens, i)
            and t.folded
            in (
                "процедура",
                "функция",
                "конецпроцедуры",
                "конецфункции",
                "procedure",
                "function",
                "endprocedure",
                "endfunction",
            )
            for i, t in enumerate(tokens)
        ):
            fail("body_form_unsupported", "объявление метода или выход из процедуры")
            return BodyCheck((), tuple(failures), hashlib.sha256(body.encode()).hexdigest())
        wrapper_prefix = (
            "Процедура АвторскийОбработчик(" + ", ".join(EVENT_PARAMETERS[operation.event]) + ")\n"
        )
        wrapped = (
            wrapper_prefix
            + body
            + "КонецПроцедуры\n"
            + f"Процедура {FILLER}(НаправлениеОбмена, ПравилаКонвертации)\nКонецПроцедуры\n"
        )
        document = read_manager_text(wrapped, path="<agent-body>")
        if lex(document.files[0]).warnings or len(document.routines) != 2:
            fail("body_lexical", "нарушена лексическая граница процедуры")
    except EdFormatError:
        fail("body_lexical", "незавершённая строка или нарушенная лексическая граница")
        return BodyCheck((), tuple(failures), hashlib.sha256(body.encode()).hexdigest())
    unavailable = {p.casefold() for ps in EVENT_PARAMETERS.values() for p in ps} - {
        p.casefold() for p in EVENT_PARAMETERS[operation.event]
    }
    unavailable.add("параметры")
    formal_names = {p.casefold() for ps in EVENT_PARAMETERS.values() for p in ps} | {"параметры"}
    for statement in lex(document.files[0]).statements:
        shadowed = [t.value for t in statement.tokens[1:] if t.folded in formal_names]
        if statement.head == "перем" and shadowed:
            fail(
                "body_parameter_unavailable",
                "нельзя затенять формальный параметр объявлением Перем: " + ", ".join(shadowed),
                line=max(1, statement.span.line_start - 1),
            )
    forbidden = {
        "процедура",
        "функция",
        "конецпроцедуры",
        "конецфункции",
        "экспорт",
        "procedure",
        "function",
        "endprocedure",
        "endfunction",
        "export",
        "перейти",
        "goto",
        "выполнить",
        "execute",
        "вычислить",
        "eval",
        "продолжитьвызов",
        "continuecall",
        DISPATCHER.casefold(),
        FILLER.casefold(),
        "выполнитьфункциюмодуляменеджера",
        wrapper_name.casefold(),
        "добавитьпкс",
    }
    stack: list[str] = []
    brackets: list[str] = []
    region_stack = 0
    loop_depth = 0
    for i, token in enumerate(tokens):
        if token.kind == "directive":
            head = _directive_head(token)
            if head in ("область", "region"):
                region_stack += 1
            elif head in ("конецобласти", "endregion"):
                if region_stack:
                    region_stack -= 1
                else:
                    fail("body_form_unsupported", "несбалансированная область", token)
            else:
                fail("body_form_unsupported", "аннотации, директивы и метки недоступны", token)
            continue
        if token.kind == "symbol" and token.value == "~":
            fail("body_form_unsupported", "аннотации, директивы и метки недоступны", token)
        if token.kind == "symbol":
            if token.value in ("(", "["):
                brackets.append(token.value)
            elif token.value in (")", "]") and (
                not brackets or brackets.pop() != {")": "(", "]": "["}[token.value]
            ):
                fail("body_lexical", "несбалансированные скобки", token)
        if not _root(tokens, i):
            continue
        name = token.folded
        if name in unavailable:
            fail("body_parameter_unavailable", f"у события нет параметра {token.value}", token)
        if name in forbidden:
            fail("body_form_unsupported", "запрещённая форма или вызов за границей тела", token)
        keyword = _kw(name)
        if keyword in _OPENERS:
            if keyword in ("для", "пока"):
                loop_depth += 1
            stack.append(_OPENERS[keyword])
        elif keyword in ("конецесли", "конеццикла", "конецпопытки"):
            if keyword == "конеццикла" and loop_depth:
                loop_depth -= 1
            if not stack or stack.pop() != keyword:
                fail("body_lexical", "несбалансированная конструкция тела", token)
        elif keyword in ("иначе", "иначеесли", "исключение") and (
            not stack or stack[-1] != ("конецпопытки" if keyword == "исключение" else "конецесли")
        ):
            fail("body_lexical", "ветка вне своей конструкции", token)
        elif keyword == "возврат" and (i + 1 >= len(tokens) or tokens[i + 1].value != ";"):
            fail("body_form_unsupported", "возврат значения в процедуре", token)
        elif keyword in ("прервать", "продолжить") and loop_depth == 0:
            fail("body_form_unsupported", "прервать или продолжить вне цикла", token)
    if region_stack:
        fail("body_form_unsupported", "несбалансированная область")
    if brackets or stack:
        fail("body_lexical", "незавершённая конструкция тела")
    prefix = _handler_prefix(wrapper_name)
    calls = _calls(tokens) if not brackets else []
    for call_name, _, start, _end in calls:
        if _forbidden_call(call_name, prefix):
            fail(
                "body_form_unsupported",
                "вызов перехватчика, порождённой процедуры или построителя правил",
                tokens[start],
            )
    if any(f.id in ("ed.author.body_lexical", "ed.author.body_form_unsupported") for f in failures):
        return BodyCheck(
            (), tuple(dict.fromkeys(failures)), hashlib.sha256(body.encode()).hexdigest()
        )
    properties = {"конвертациясвойств"}
    components = {"компонентыобмена"}
    rules: set[str] = set()
    property_rows: set[str] = set()
    # Прямые алиасы самой таблицы, без вывода типов сложных выражений.
    changed = True
    while changed:
        changed = False
        for i in range(len(tokens) - 3):
            if (
                _root(tokens, i)
                and tokens[i + 1].value == "="
                and tokens[i + 2].folded in properties | components
                and tokens[i + 3].value == ";"
            ):
                aliases = properties if tokens[i + 2].folded in properties else components
                if tokens[i].folded not in aliases:
                    aliases.add(tokens[i].folded)
                    changed = True
    # Только простое присваивание и непосредственное использование результата поиска.
    # Типы через структуры/функции/ветвления не выводятся, порядок исполнения не доказывается.
    for name, _, start, end in calls:
        if (
            start >= 2
            and _root(tokens, start - 2)
            and tokens[start - 1].value == "="
            and (end == len(tokens) or tokens[end].value == ";")
        ):
            if name in RULE_LOOKUP_CALLS:
                rules.add(tokens[start - 2].folded)
            elif name.split(".")[0] in properties and name.endswith((".найти", ".получить")):
                property_rows.add(tokens[start - 2].folded)
        if name in RULE_LOOKUP_CALLS and end < len(tokens) and tokens[end].value == ".":
            cursor, members, mutation = _path_end(tokens, end - 1)
            if mutation or (members and cursor < len(tokens) and tokens[cursor].value == "="):
                fail(
                    "body_form_unsupported",
                    "изменение найденного правила через вызов поиска",
                    tokens[start],
                )
    for i in range(len(tokens) - 3):
        if (
            not _root(tokens, i)
            or tokens[i + 1].value != "="
            or tokens[i + 2].folded not in properties
            or tokens[i + 3].value != "["
        ):
            continue
        depth = 0
        cursor = i + 3
        while cursor < len(tokens):
            if tokens[cursor].value == "[":
                depth += 1
            elif tokens[cursor].value == "]":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if (
            cursor + 1 < len(tokens)
            and tokens[cursor].value == "]"
            and tokens[cursor + 1].value == ";"
        ):
            property_rows.add(tokens[i].folded)
    for i in range(len(tokens) - 4):
        if (
            _kw(tokens[i].folded) == "для"
            and _kw(tokens[i + 1].folded) == "каждого"
            and _kw(tokens[i + 3].folded) == "из"
            and tokens[i + 4].folded in properties
        ):
            property_rows.add(tokens[i + 2].folded)
    changed = True
    while changed:
        changed = False
        for i in range(len(tokens) - 3):
            if not _root(tokens, i) or tokens[i + 1].value != "=" or tokens[i + 3].value != ";":
                continue
            for aliases in (rules, property_rows):
                if tokens[i + 2].folded in aliases and tokens[i].folded not in aliases:
                    aliases.add(tokens[i].folded)
                    changed = True
    opaque_calls = tuple(
        dict.fromkeys(
            OpaqueCall(
                "".join(
                    t.value
                    for t in tokens[
                        start : next(j for j in range(start, end) if tokens[j].value == "(")
                    ]
                ),
                body.count("\n", 0, tokens[start].start) + 1,
            )
            for name, _, start, end in calls
            if name not in KNOWN_SAFE_CALLS
        )
    )
    for name, args, start, _ in calls:
        if (
            name.split(".")[0] in components
            and name.rsplit(".", 1)[-1] in ("свойство", "получить", "вставить", "удалить")
            and args
            and len(args[0]) == 1
            and args[0][0].kind == "string"
            and args[0][0].value.casefold() in RULE_TABLES
        ):
            fail("body_form_unsupported", RULE_TABLE_HINT, tokens[start])
    for i, token in enumerate(tokens):
        if token.kind == "identifier" and token.folded in RULE_TABLES:
            fail("body_form_unsupported", RULE_TABLE_HINT, token)
        if not _root(tokens, i):
            continue
        name = token.folded
        if name in components and i + 1 < len(tokens) and tokens[i + 1].value == "[":
            if not (
                i + 3 < len(tokens)
                and tokens[i + 2].kind == "string"
                and tokens[i + 3].value == "]"
            ):
                fail("body_dynamic_reference", "вычисляемое имя свойства компонентов", token)
            elif tokens[i + 2].value.casefold() in RULE_TABLES:
                fail("body_form_unsupported", RULE_TABLE_HINT, token)
        if name in components:
            cursor, members, _mutation = _path_end(tokens, i)
            if (
                name == "компонентыобмена"
                and not members
                and _at_statement_start(tokens, i)
                and cursor < len(tokens)
                and tokens[cursor].value == "="
            ):
                fail("body_form_unsupported", "присваивание параметра КомпонентыОбмена", token)
            if members and members[0] in COMPONENT_MUTATORS:
                fail(
                    "body_form_unsupported",
                    "метод изменения корня КомпонентыОбмена",
                    token,
                )
        if name not in properties | property_rows | rules:
            continue
        if (
            name in rules
            and i + 1 < len(tokens)
            and tokens[i + 1].value == "["
            and not (
                i + 3 < len(tokens)
                and tokens[i + 2].kind == "string"
                and tokens[i + 3].value == "]"
            )
        ):
            fail("body_dynamic_reference", "вычисляемое имя поля найденного правила", token)
        cursor, members, mutation = _path_end(tokens, i)
        assignment = (
            _at_statement_start(tokens, i) and cursor < len(tokens) and tokens[cursor].value == "="
        )
        if mutation or (
            assignment
            and (
                (
                    name in properties | property_rows
                    and (cursor > i + 1 or name == "конвертациясвойств")
                )
                or (name in rules and bool(members))
            )
        ):
            fail(
                "body_form_unsupported",
                "изменение таблицы ПКС или привязки найденного правила",
                token,
            )
    # Узкое дополнение refs: ДанныеИБ.<реквизит> не выводит типы произвольных выражений.
    rule = context.index(inputs.document).find(operation.target.pko_address)
    assert isinstance(rule, ObjectRule)
    key, _ = metadata_key(rule.configuration_object.value)
    owner = inputs.structure.objects.get(key) if key else None
    new_attributes = set((known_attributes or {}).get(key, ())) if key else set()
    for prop in operations:
        if not isinstance(prop, AddHeaderProperty) or not prop.new_attribute:
            continue
        prop_rule = context.index(inputs.document).find(prop.target.pko_address)
        if (
            isinstance(prop_rule, ObjectRule)
            and metadata_key(prop_rule.configuration_object.value)[0] == key
        ):
            new_attributes.add(prop.new_attribute.name.casefold())
    profile = context.profile(operation.target.format_version, operation.target.direction)
    applicable = context.applicable(
        inputs.document, operation.target.format_version, operation.target.direction
    )
    typ, _ = profile.owner_type(rule, operation.target.direction, applicable)
    for i, token in enumerate(tokens[:-2]):
        if _root(tokens, i) and token.folded == "данныеиб" and tokens[i + 1].value == ".":
            attribute = tokens[i + 2].value
            if (
                owner
                and not owner.property(attribute)
                and not standard_attribute(owner, attribute)
                and attribute.casefold() not in object_runtime_fields(owner)
                and attribute.casefold() not in new_attributes
                and (i + 3 == len(tokens) or tokens[i + 3].value != "(")
            ):
                fail(
                    "body_reference_unresolved",
                    f"реквизит ДанныеИБ.{attribute} не найден в структуре или комплекте",
                    token,
                )
    body_doc = replace(
        document,
        routines=tuple(
            replace(r, roles=frozenset({"handler"}))
            for r in document.routines
            if r.name == "АвторскийОбработчик"
        ),
    )
    references = build_references(body_doc).entries
    # Полный путь проверяется только внутри прозрачных оболочек ключа/общих свойств.
    # В прочих свойствах BSL хранит инструкции (.Значение), таблицы (.Колонки),
    # произвольный anyType: это прикладные данные, не структура XML из схемы.
    body_offset = len(wrapper_prefix)
    positions = {t.start: i for i, t in enumerate(tokens)}
    transparent = {
        tuple(q.local for q in path[:length])
        for _, path in profile.effective.get(typ.id if typ else "", ())
        for length in range(1, len(path))
    }
    expanded = []
    for reference in references:
        i = positions.get(reference.span.char_start - body_offset)
        if i is not None and reference.kind in (
            "format_property",
            "received_property",
            "parameter",
        ):
            root = i
            if reference.form == "index":
                root = max(0, i - 2)
            elif reference.form == "call":
                root = next(
                    (
                        start
                        for _, args, start, _ in calls
                        if args
                        and args[0]
                        and args[0][0].start <= tokens[i].start <= args[0][-1].end
                    ),
                    root,
                )
            while root >= 2 and tokens[root - 1].value == ".":
                root -= 2
            expected_root = {
                "format_property": "данныеxdto",
                "received_property": "полученныеданные",
                "parameter": "компонентыобмена",
            }[reference.kind]
            if tokens[root].folded != expected_root:
                # Совпавшее имя поля локальной структуры не является параметром события.
                continue
        if reference.kind == "format_property" and reference.form == "member" and i is not None:
            parts = [tokens[i].value]
            cursor = i + 1
            while (
                tuple(parts) in transparent
                and cursor + 1 < len(tokens)
                and tokens[cursor].value == "."
            ):
                if tokens[cursor + 1].kind != "identifier" or (
                    cursor + 2 < len(tokens) and tokens[cursor + 2].value == "("
                ):
                    break
                parts.append(tokens[cursor + 1].value)
                cursor += 2
            reference = replace(reference, name=".".join(parts))
        expanded.append(reference)
    references = tuple(expanded)

    def reference_line(reference: EdReference) -> int:
        offset = reference.span.char_start - body_offset
        if offset < 0:
            return 1
        return body.count("\n", 0, min(offset, len(body))) + 1

    guarded_names = {
        args[0][0].value.casefold()
        for _name, args, _, _ in calls
        if args
        and len(args[0]) == 1
        and args[0][0].kind == "string"
        and (
            _name == "данныеxdto.свойство"
            or (_name.startswith("данныеxdto.") and _name.endswith(".свойство"))
        )
    }
    guarded: list[tuple[str, int]] = []
    for reference in references:
        if reference.kind == "additional_key":
            continue
        if reference.kind == "instruction_rule" and reference.name == "":
            continue
        line = reference_line(reference)
        label = (
            reference.name
            or ", ".join(
                token.value for token in tokenize(reference.raw) if token.kind == "identifier"
            )
            or reference.kind
        )
        if reference.name is None:
            fail(
                "body_dynamic_reference",
                f"вычисляемое имя {_named_field(reference.kind)} «{label}»",
                line=line,
            )
            continue
        name = reference.name
        if reference.kind in ("pko_lookup", "instruction_rule"):
            candidates = [
                r
                for r in (*inputs.document.pko, *inputs.document.pkpd)
                if (r.declared_name or r.name).casefold() == name.casefold()
                and applicable.evaluate(r, operation.target.direction) is True
            ]
            valid = len(candidates) == 1
        elif reference.kind == "parameter":
            valid = any(p.name.casefold() == name.casefold() for p in inputs.document.parameters)
        elif reference.kind == "format_property":
            valid = bool(typ and profile.resolve(typ, name).status == "resolved")
        elif reference.kind == "received_property":
            valid = bool(
                owner
                and (
                    owner.property(name)
                    or name.casefold() in new_attributes
                    or standard_attribute(owner, name)
                    or name.casefold() in object_runtime_fields(owner)
                )
            )
        else:
            # ДополнительныеСвойства содержит служебные ключи, это не реквизиты объекта.
            valid = True
        if not valid:
            if (
                reference.kind == "format_property"
                and reference.access == "read"
                and name.casefold() in guarded_names
            ):
                guarded.append((name, line))
                continue
            fail(
                "body_reference_unresolved",
                f"известная ссылка {_named_field(reference.kind)} «{label}» не разрешилась",
                line=line,
            )
    return BodyCheck(
        references,
        tuple(dict.fromkeys(failures)),
        hashlib.sha256(body.encode()).hexdigest(),
        opaque_calls,
        tuple(dict.fromkeys(guarded)),
    )


def dispatcher_failures(inputs: AuthoringInputs, operation: Operation) -> tuple[Failure, ...]:
    """Диспетчер базы: плоская цепочка literal → единственный статический вызов."""
    document = inputs.document
    dispatchers = [r for r in document.routines if r.name.casefold() == DISPATCHER.casefold()]

    def failure(check: str, reason: str) -> tuple[Failure, ...]:
        return (Failure("ed.author." + check, operation.target.pko_address, reason),)

    if document.manager_version not in (1, 2, 3) or len(dispatchers) != 1:
        return failure("handler_signature", "Требуется один процедурный диспетчер интерфейса 1/2/3")
    routine = dispatchers[0]
    if (
        routine.routine_kind != "procedure"
        or tuple(p.name for p in routine.parameters) != ("ИмяПроцедуры", "Параметры")
        or any(p.by_value or p.default for p in routine.parameters)
    ):
        return failure(
            "handler_signature", "Сигнатура диспетчера: ИмяПроцедуры, Параметры без Знач"
        )
    source = next(f for f in document.files if f.file_id == routine.span.file_id)
    statements = [
        s
        for s in lex(source).statements
        if routine.body_span.char_start <= s.span.char_start < routine.body_span.char_end
    ]
    if not statements:
        return ()
    need_call = False
    opened = False
    closed = False
    literals: set[str] = set()
    for statement in statements:
        ts = statement.tokens
        if statement.head in ("если", "иначеесли"):
            if (
                need_call
                or closed
                or (statement.head == "если" and opened)
                or (statement.head == "иначеесли" and not opened)
            ):
                return failure("handler_dispatch_unknown", "Неоднозначная структура диспетчера")
            if (
                len(ts) != 5
                or ts[1].folded != "имяпроцедуры"
                or ts[2].value != "="
                or ts[3].kind != "string"
                or ts[4].folded != "тогда"
                or ts[3].value.casefold() in literals
            ):
                return failure(
                    "handler_dispatch_unknown", "Вычисляемая или повторная ветка диспетчера"
                )
            literals.add(ts[3].value.casefold())
            opened, need_call = True, True
        elif statement.head == "конецесли":
            if not opened or need_call or closed:
                return failure("handler_dispatch_unknown", "Неоднозначное окончание диспетчера")
            closed = True
        elif need_call:
            calls = _calls(ts)
            if (
                len(calls) != 1
                or calls[0][2] != 0
                or calls[0][3] != len(ts) - 1
                or ts[-1].value != ";"
                or "." in calls[0][0]
            ):
                return failure(
                    "handler_dispatch_unknown", "Ветка должна содержать один прямой вызов"
                )
            need_call = False
        else:
            return failure("handler_dispatch_unknown", "Неизвестная операция диспетчера")
    if not closed or need_call:
        return failure("handler_dispatch_unknown", "Незавершённый диспетчер")
    return ()


def validate_handler_operations(
    inputs: AuthoringInputs,
    operations: tuple[Operation, ...],
    identity: ExtensionIdentity,
    context: AuthoringContext,
) -> tuple[tuple[Failure, ...], tuple[Notice, ...], tuple[HandlerBindingPlan, ...]]:
    """Проверки новых решений; общие предусловия первого среза вызываются снаружи."""
    failures: list[Failure] = []
    notices: list[Notice] = []
    bindings: list[HandlerBindingPlan] = []
    by_id = {op.operation_id: op for op in operations}
    handlers = [op for op in operations if isinstance(op, SetObjectHandler)]
    slots: dict[tuple, str] = {}
    preset_slots: dict[tuple, list[str]] = {}
    property_slots: set[tuple] = set()
    attribute_slots: set[tuple] = set()
    generated_names: dict[str, tuple] = {}
    algorithmic_send = {
        item.handler_operation_id
        for item in operations
        if isinstance(item, AddAlgorithmicHeaderProperty) and item.target.direction == "send"
    }
    interface = inputs.document.manager_version or 0
    if sum(len(op.body.encode("utf-8")) for op in handlers) > 1024 * 1024 or len(handlers) > 100:
        raise EdAuthoringResourceLimitError(
            "Совокупный предел тел или слотов обработчиков превышен"
        )
    for op in operations:
        if isinstance(op, AddHeaderProperty):
            continue
        try:
            rule = context.index(inputs.document).find(op.target.pko_address)
        except (EntityNotFoundError, AmbiguousAddressError):
            continue
        if not isinstance(rule, ObjectRule):
            continue
        target = op.target
        event = (
            op.event
            if isinstance(op, SetObjectHandler)
            else "ПередЗаписьюПолученныхДанных"
            if isinstance(op, PreserveMissingHeaderProperty)
            else "ПриОтправкеДанных"
            if target.direction == "send"
            else "ПриКонвертацииДанныхXDTO"
        )
        source = next(f for f in inputs.document.files if f.file_id == rule.span.file_id)
        op_verified = False

        def fail(
            check: str,
            reason: str,
            *,
            operation: Operation = op,
            _event: str = event,
            _file: str = source.path,
            _line: int = rule.span.line_start,
        ) -> None:
            failures.append(
                Failure(
                    "ed.author." + check,
                    operation.target.pko_address,
                    f"Операция {operation.operation_id}: {reason}; "
                    f"ПКО {operation.target.pko_address}, событие {_event}",
                    _file,
                    _line,
                )
            )

        if len(inputs.document.files) != 1 or (
            inputs.source_set.extensions and not inputs.extension_sources
        ):
            fail(
                "handler_foreign_hook",
                "Нужен снимок без слоя и доступные источники перечисленных расширений",
            )
        profile = context.profile(target.format_version, target.direction)
        applicable = context.applicable(inputs.document, target.format_version, target.direction)
        canonical_pko = rule.declared_name or rule.name
        name = handler_name(identity.prefix, canonical_pko, target.direction, event)
        if isinstance(op, (SetObjectHandler, PreserveMissingHeaderProperty)):
            binding_slot = (target.direction, rule.entity_id, event)
            if (
                name.casefold() in generated_names
                and generated_names[name.casefold()] != binding_slot
            ):
                fail("handler_name_occupied", "Коллизия имён порождённых обработчиков")
            generated_names[name.casefold()] = binding_slot
            occupied = {r.name.casefold() for r in inputs.document.routines}
            for path, text in inputs.extension_sources.items():
                if not path.casefold().endswith(".bsl"):
                    continue
                try:
                    tokens = tokenize(text)
                except EdFormatError:
                    fail("handler_foreign_hook", "Источник расширения не прочитан лексически")
                    continue
                occupied.update(
                    tokens[i + 1].folded
                    for i in range(len(tokens) - 1)
                    if tokens[i].folded in ("процедура", "функция")
                )
            if (
                name.casefold() in occupied
                or (identity.prefix + "Диспетчер").casefold() in occupied
            ):
                fail("handler_name_occupied", "Имя обработчика или перехватчика занято")
            for failure in dispatcher_failures(inputs, op):
                fail(failure.id.removeprefix("ed.author."), failure.message)
        if isinstance(op, SetObjectHandler):
            if (event == "ПриОтправкеДанных") != (target.direction == "send"):
                fail(
                    "handler_event_direction",
                    f"Событие {event} недоступно в направлении {target.direction}",
                )
            slot = (target.direction, rule.entity_id, event)
            if slot in slots:
                fail("handler_slot_conflict", "Разные тела на один слот")
            slots[slot] = op.operation_id
            previous_bindings = [
                b
                for b in rule.events
                if b.event == event and applicable.evaluate(b, target.direction) is not False
            ]
            previous = previous_bindings[0].target_name if len(previous_bindings) == 1 else ""
            if len(previous_bindings) > 1 or any(
                applicable.evaluate(b, target.direction) is None for b in previous_bindings
            ):
                fail("handler_dispatch_unknown", "Привязка события неоднозначна")
            if op.expected_previous != previous:
                fail(
                    "handler_previous_mismatch",
                    f"Изменилась привязка события {event}: "
                    f"ожидалось {op.expected_previous}, найдено {previous}",
                )
            if (bool(previous) and op.chain != "after_existing") or (
                not previous and op.chain != "none"
            ):
                fail(
                    "handler_chain_required",
                    "Нужен after_existing для непустого события, none для пустого",
                )
            branch_hash = routine_hash = ""
            if previous:
                routines = [
                    r for r in inputs.document.routines if r.name.casefold() == previous.casefold()
                ]
                cases = [
                    c
                    for c in inputs.document.dispatcher_cases
                    if c.literal_name.casefold() == previous.casefold()
                ]
                if (
                    len(routines) != 1
                    or routines[0].routine_kind != "procedure"
                    or tuple(p.name for p in routines[0].parameters) != EVENT_PARAMETERS[event]
                    or any(p.by_value or p.default for p in routines[0].parameters)
                ):
                    fail(
                        "handler_signature",
                        "Прежний обработчик не соответствует точной сигнатуре события",
                    )
                else:
                    routine_hash = hashlib.sha256(routines[0].raw_text.encode()).hexdigest()
                if (
                    len(cases) != 1
                    or cases[0].target.reference_parts != (previous,)
                    or tuple(a.reference_parts for a in cases[0].arguments)
                    != tuple(("Параметры", p) for p in EVENT_PARAMETERS[event])
                    or cases[0].returns
                ):
                    fail(
                        "handler_dispatch_unknown",
                        "Прежняя ветка не является точным одиночным вызовом",
                    )
                else:
                    branch_hash = hashlib.sha256(cases[0].raw_text.encode()).hexdigest()
                notices.append(
                    Notice(
                        "ed.author.handler_chained",
                        op.operation_id,
                        target.pko_address,
                        f"Для {canonical_pko}/{event} сначала вызывается типовой обработчик "
                        f"{previous}, затем переданное тело. Типовой код непрозрачен; "
                        "исключение в нём не даст выполнить новое тело. "
                        "Обновление конфигурации требует пересборки.",
                    )
                )
            checked = context.handler_bodies.get(op.operation_id)
            if checked is None:
                checked = check_body(inputs, op, context, wrapper_name=name, operations=operations)
            failures.extend(checked.failures)
            context.handler_bodies[op.operation_id] = checked
            notices.append(
                Notice(
                    "ed.author.handler_effect_unknown",
                    op.operation_id,
                    target.pko_address,
                    "Тело обработчика передано автором. Проверены структура вызова и известные "
                    "ссылки, но не результат алгоритма и не все побочные эффекты. "
                    "После установки нужен обмен контрольного объекта."
                    + (
                        " Непрозрачные прикладные вызовы: "
                        + "; ".join(f"{c.name}, строка тела {c.line}" for c in checked.opaque_calls)
                        + "."
                        if checked.opaque_calls
                        else ""
                    ),
                    methods=tuple(c.name for c in checked.opaque_calls),
                    detail_key="agent_body",
                )
            )
            if checked.guarded_reads:
                listed = ", ".join(
                    f"«{prop}» (строка тела {line})" for prop, line in checked.guarded_reads
                )
                notices.append(
                    Notice(
                        "ed.author.format_property_guarded",
                        op.operation_id,
                        target.pko_address,
                        "Свойство формата отсутствует в проверяемой версии и читается "
                        f"под защитой ДанныеXDTO.Свойство: {listed}. "
                        "Запись такого свойства остаётся отказом.",
                    )
                )
            # Стенд D, §8: docs/plans/evals/2026-10-05-ed-handlers-stand.md.
            # Интерфейс 2: отправка без цепочки (включая прямую ПКС) и after_existing.
            # Алгоритмическая ПКС этим стендом не проверялась.
            op_verified = (
                interface == 2
                and event == "ПриОтправкеДанных"
                and op.operation_id not in algorithmic_send
            )
            bindings.append(
                HandlerBindingPlan(
                    target,
                    event,
                    name,
                    EVENT_PARAMETERS[event],
                    (op.operation_id,),
                    previous,
                    EVENT_PARAMETERS[event] if previous else (),
                    branch_hash,
                    routine_hash,
                    op_verified,
                    interface,
                )
            )
        elif isinstance(op, PreserveMissingHeaderProperty):
            property_op = by_id.get(op.property_operation_id)
            if not isinstance(property_op, AddHeaderProperty) or property_op.target != target:
                fail(
                    "handler_property_dependency",
                    "Preset ссылается только на прямую ПКС того же target и комплекта",
                )
                continue
            key, _ = metadata_key(rule.configuration_object.value)
            owner = inputs.structure.objects.get(key) if key else None
            attrs = owner.property(property_op.configuration_attribute) if owner else ()
            primitive = (
                property_op.new_attribute.primitive in ("string", "number", "date", "boolean")
                if property_op.new_attribute
                else len(attrs) == 1
                and attrs[0].types in (("Строка",), ("Число",), ("Дата",), ("Булево",))
                and not attrs[0].unresolved
            )
            unsupported = (
                target.direction != "receive"
                or not primitive
                or "." in property_op.format_property
                or bool(rule.group_flag.value)
                or owner is None
                or owner.kind not in ("Справочник", "Документ")
            )
            typ, _ = profile.owner_type(rule, target.direction, applicable)
            resolved = profile.resolve(typ, property_op.format_property) if typ else None
            if resolved and resolved.status == "resolved":
                # Ключи формата и идентификация ПКО: сохранение ключей не является preset.
                prop = profile.properties[resolved.property_ids[0]]
                unsupported |= any(
                    q.local.casefold() == "ключевыесвойства" for q in resolved.physical_paths[0]
                )
                unsupported |= bool(
                    profile.schema and prop.name.namespace in profile.schema.extension_namespaces
                )
            if unsupported:
                fail(
                    "preserve_property_unsupported",
                    "Preset поддерживает плоскую неключевую прямую примитивную ПКС получения",
                )
            existing = any(
                b.target_name
                and b.event in ("ПриКонвертацииДанныхXDTO", "ПередЗаписьюПолученныхДанных")
                and applicable.evaluate(b, target.direction) is not False
                for b in rule.events
            )
            authored = any(
                h.target.direction == target.direction
                and h.target.pko_address == target.pko_address
                and h.event in ("ПриКонвертацииДанныхXDTO", "ПередЗаписьюПолученныхДанных")
                for h in handlers
            )
            conflict = (
                f"у правила «{canonical_pko}» есть операция сохранения значения "
                f"({op.operation_id}): она допускает только пустые события получения "
                "этого правила. Снимите сохранение (`drop_operations`) и перенесите "
                "его логику в свой обработчик `ПередЗаписьюПолученныхДанных` "
                "либо откажитесь от своего обработчика"
            )
            if authored:
                fail("handler_slot_conflict", conflict)
            from kd2_rules_mcp.authoring.ed.operations import extension_conflicts

            if (
                existing
                or authored
                or extension_conflicts(inputs, property_op, context)
                or (inputs.source_set.extensions and not inputs.extension_sources)
            ):
                fail("preserve_handler_conflict", conflict)
            preset_slots.setdefault((target, event), []).append(op.operation_id)
            # Стенд D, §8: сохранение с прямой ПКС, обычный путь, найденный объект.
            op_verified = interface == 2
            notices.append(
                Notice(
                    "ed.author.preserve_absence_keeps_value",
                    op.operation_id,
                    target.pko_address,
                    f"При отсутствии {property_op.format_property} значение "
                    f"{property_op.configuration_attribute} найденного объекта сохраняется. "
                    "Типовой отправитель может не писать пустое значение; такая очистка тоже "
                    "не переносится. Явный пустой элемент очищает реквизит обычного пути, "
                    "в том числе при шаблоне сохранения значения (проверено обменом). "
                    "Объектная конвертация при отсутствующем или пустом значении реквизит "
                    "не меняет; отсутствие проверено обменом, пустое значение — по коду КОС.",
                )
            )
        else:
            if target.direction != "send":
                fail(
                    "not_supported",
                    "Алгоритмическая ПКС получения не подтверждена живым обменом (H7)",
                )
                continue
            op_verified = False
            handler = by_id.get(op.handler_operation_id)
            if (
                not isinstance(handler, SetObjectHandler)
                or handler.target != target
                or handler.event != "ПриОтправкеДанных"
            ):
                fail(
                    "handler_property_dependency",
                    "Нужен обработчик отправки (`set_object_handler`) того же правила конвертации",
                )
                continue
            checked = context.handler_bodies.get(handler.operation_id)
            if checked is None:
                checked = check_body(inputs, handler, context, operations=operations)
            key, _ = metadata_key(rule.configuration_object.value)
            owner = inputs.structure.objects.get(key) if key else None
            attrs = owner.property(op.configuration_attribute) if owner else ()
            if len(attrs) != 1 or attrs[0].kind != "Реквизит" or attrs[0].parent_kind:
                fail(
                    "configuration_attribute_missing",
                    "Алгоритмическая ПКС требует существующий обычный реквизит",
                )
            typ, _ = profile.owner_type(rule, target.direction, applicable)
            resolved = profile.resolve(typ, op.format_property) if typ else None
            if typ is None or resolved is None or resolved.status != "resolved":
                fail("schema_property_missing", "Свойство формата не разрешено")
                continue
            if not any(
                r.kind == "format_property"
                and r.name is not None
                and r.access == "write"
                and profile.resolve(typ, r.name).property_ids == resolved.property_ids
                for r in checked.references
            ):
                fail("handler_property_unwritten", "Тело не пишет указанное свойство формата")
            from kd2_rules_mcp.authoring.ed.candidates import occupied_properties, primitive_limits

            ids, config, conditional = occupied_properties(
                rule, profile, applicable, typ, target.direction
            )
            if resolved.property_ids[0] in ids or op.configuration_attribute.casefold() in config:
                fail("format_property_occupied", "Алгоритмическая ПКС занята типовым правилом")
            if conditional:
                fail("applicability_unknown", "Применимость существующих ПКС не доказана")
            slot = (target.direction, rule.entity_id, resolved.property_ids[0])
            attribute_slot = (
                target.direction,
                rule.entity_id,
                op.configuration_attribute.casefold(),
            )
            if (
                slot in property_slots
                or attribute_slot in attribute_slots
                or any(
                    isinstance(p, AddHeaderProperty)
                    and p.target == target
                    and (
                        p.format_property == op.format_property
                        or p.configuration_attribute == op.configuration_attribute
                    )
                    for p in operations
                )
            ):
                fail("handler_slot_conflict", "Дубли зависимых ПКС")
            property_slots.add(slot)
            attribute_slots.add(attribute_slot)
            if attrs:
                if attrs[0].unresolved:
                    fail("handler_property_rule", "Состав типа реквизита не разрешён полностью")
                primitive = (
                    len(attrs[0].types) == 1
                    and attrs[0].types[0] in ("Строка", "Число", "Дата", "Булево")
                    and not attrs[0].unresolved
                )
                field = profile.properties[resolved.property_ids[0]]
                assert profile.schema is not None
                if (
                    not primitive
                    or primitive_limits(profile, field)[0] is None
                    or op.conversion_rule
                ):
                    _validate_conversion_rule(
                        inputs,
                        op,
                        attrs[0].types,
                        profile,
                        applicable,
                        profile.properties[resolved.property_ids[0]],
                        fail,
                    )
            notices.append(
                Notice(
                    "ed.author.handler_type_unproven",
                    op.operation_id,
                    target.pko_address,
                    f"Значение алгоритмической ПКС {op.format_property} вычисляется обработчиком. "
                    "Статически проверены объявления и ссылки; "
                    "фактический тип результата не доказан. "
                    "Проверка типа отмечена skipped и требует стенда.",
                )
            )
        if not op_verified:
            notices.append(
                Notice(
                    "ed.author.handler_runtime_unverified",
                    op.operation_id,
                    target.pko_address,
                    _runtime_notice(interface, event),
                )
            )
    for (target, event), ids in sorted(
        preset_slots.items(), key=lambda row: (row[0][0].pko_address, row[0][1])
    ):
        rule = context.index(inputs.document).find(target.pko_address)
        assert isinstance(rule, ObjectRule)
        bindings.append(
            HandlerBindingPlan(
                target,
                event,
                handler_name(
                    identity.prefix, rule.declared_name or rule.name, target.direction, event
                ),
                EVENT_PARAMETERS[event],
                tuple(sorted(ids)),
                runtime_verified=interface == 2,
                manager_interface=interface,
            )
        )
    return tuple(dict.fromkeys(failures)), tuple(notices), tuple(bindings)


def _validate_conversion_rule(inputs, op, types, profile, applicable, prop, fail) -> None:
    """Явная ссылочная конвертация: обе стороны целевого ПКО/ПКПД должны совпасть."""
    candidates = [
        r
        for r in (*inputs.document.pko, *inputs.document.pkpd)
        if (r.declared_name or r.name) == op.conversion_rule
        and applicable.evaluate(r, op.target.direction) is True
    ]
    if not op.conversion_rule or len(candidates) != 1:
        fail(
            "handler_property_rule",
            "Для сложного типа нужно существующее "
            f"{_named_field('conversion_rule')} выбранного направления",
        )
        return
    conversion = candidates[0]
    field = (
        conversion.configuration_object
        if isinstance(conversion, ObjectRule)
        else conversion.configuration_type
    )
    key, _ = metadata_key(field.value)
    owner = inputs.structure.objects.get(key) if key else None
    converted, status = (
        profile.owner_type(conversion, op.target.direction, applicable)
        if isinstance(conversion, ObjectRule)
        else profile.find_type(conversion.format_type.value or "")
    )
    from kd2_rules_mcp.ed.schema.resolver import property_type

    expected = property_type(profile.schema, prop) if profile.schema else None
    format_matches = expected is not None and converted is not None and expected.id == converted.id
    if isinstance(conversion, ObjectRule) and converted and expected:
        # XDTO:1530–1567 — ссылочный результат ПКО определяется ключевыми свойствами.
        key_property = profile.resolve(converted, "Ссылка")
        if key_property.status == "resolved":
            reference = profile.properties[key_property.property_ids[0]]
            key_type = property_type(profile.schema, reference) if profile.schema else None
            format_matches |= key_type is not None and key_type.id == expected.id
    if (
        owner is None
        or len(types) != 1
        or types[0].casefold() != owner.type_name.casefold()
        or status != "resolved"
        or converted is None
        or not format_matches
    ):
        fail(
            "handler_property_rule",
            "Типы "
            f"{_named_field('configuration_attribute')} и {_named_field('format_property')} "
            f"не совпадают с {_named_field('conversion_rule')}",
        )


def enforce_handler_result(
    *,
    certain: bool,
    unknown_lines: int,
    unresolved_dispatch: int,
    failures: tuple[Failure, ...] = (),
) -> None:
    """Вход B: замечание/ack не заменяет проверку целиком отрисованного слоя."""
    if not certain or unknown_lines or unresolved_dispatch:
        failures += (
            Failure(
                "ed.author.layer_not_certain",
                "",
                "Порождённый слой не доказан: unknown, taint или недоставленный обработчик",
            ),
        )
    if failures:
        raise AuthoringPreconditionError(failures)
