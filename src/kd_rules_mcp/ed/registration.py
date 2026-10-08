"""Чтение менеджера регистрации: BSL не исполняется, XML отборов читается безопасно.

Распознаются только формы §5.2. Конкатенация XML, вычисляемый вызов, цикл и внешний
помощник остаются неизвестным фрагментом со строкой файла.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from lxml import etree as ET

from .address import escape_segment
from .coverage import build_coverage
from .errors import EdFormatError, EdReadError, EdResourceLimitError
from .lexer import Lexed, Statement, Token, lex, split_arguments
from .model import (
    Classification,
    DispatcherCase,
    Expr,
    Field,
    FormalParameter,
    ParseStatus,
    Routine,
    SourceFile,
    SourceSpan,
    UnknownFragment,
)
from .registration_model import (
    HANDLER_EVENTS,
    FilterTree,
    RegistrationModuleDocument,
    RegistrationParameter,
    RegistrationRule,
    XmlNode,
)

MAX_BYTES = 32 * 1024 * 1024
MAX_LINES = 1_000_000
MAX_FILTER_BYTES = 4 * 1024 * 1024
MAX_XML_DEPTH = 128

_FILTER_ROOTS = ("ОтборПоСвойствамПланаОбмена", "ОтборПоСвойствамОбъекта")
_STRING_FIELDS = (
    "Идентификатор",
    "ОбъектМетаданныхИмя",
    "ИмяПланаОбмена",
    "ИмяРеквизитаФлага",
)
_BOOL_FIELDS = (
    "ПравилоПоСвойствамОбъектаПустое",
    *(field_name for _, field_name in HANDLER_EVENTS),
)
_DISPATCHERS = frozenset(
    name.casefold()
    for name in (
        "ПередОбработкой",
        "ПриОбработке",
        "ПриОбработкеДополнительный",
        "ПослеОбработки",
        "ПакетнаяОбработка",
    )
)
_OPAQUE_CALLS = frozenset({"нстр", "форматированнаястрока", "подставитьпараметрывстроку"})
_PLAN_ITEM = frozenset(
    {
        "ЭтоСтрокаКонстанты",
        "ТипСвойстваОбъекта",
        "СвойствоПланаОбмена",
        "ВидСравнения",
        "СвойствоОбъекта",
        "ТаблицаСвойствОбъекта",
        "ТаблицаСвойствПланаОбмена",
    }
)
_OBJECT_ITEM = frozenset(
    {
        "ТипСвойстваОбъекта",
        "ВидСравнения",
        "СвойствоОбъекта",
        "Вид",
        "ЗначениеКонстанты",
        "ТаблицаСвойствОбъекта",
    }
)
_GROUP = frozenset({"БулевоЗначениеГруппы", "ЭлементОтбора", "Группа"})
_TABLE = frozenset({"Свойство"})
_PROPERTY = frozenset({"Наименование", "Тип", "Вид"})
_ROLES = {
    "info": "support",
    "parameters": "support",
    "init": "support",
    "dispatcher": "dispatcher",
    "helper": "support",
    "forwarder": "support",
    "rule": "rule",
    "handler": "handler",
    "filter": "filter",
}


class AmbiguousRegistrationAddress(LookupError):
    """Неквалифицированному адресу соответствуют несколько правил."""

    def __init__(self, address: str, candidates: tuple[str, ...]) -> None:
        super().__init__(f"Неоднозначный адрес: {address}")
        self.address = address
        self.candidates = candidates


def rule_address(rule: RegistrationRule) -> str:
    """Адрес правила: `Регистрация/ПРО/<идентификатор>`, у дублей суффикс `#N`."""
    return f"Регистрация/ПРО/{rule.qualified_id}"


def find_rule(document: RegistrationModuleDocument, address: str) -> RegistrationRule:
    """Ищет правило по адресу. Общий адрес дублей — ошибка со списком кандидатов."""
    exact = [rule for rule in document.rules if rule_address(rule) == address]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousRegistrationAddress(address, tuple(rule_address(rule) for rule in exact))
    rough = [rule for rule in document.rules if rule_address(rule).split("#", 1)[0] == address]
    if len(rough) > 1:
        raise AmbiguousRegistrationAddress(address, tuple(rule_address(rule) for rule in rough))
    if len(rough) == 1:
        return rough[0]
    raise KeyError(address)


def read_registration_manager(path: str | Path) -> RegistrationModuleDocument:
    """Читает один UTF-8 файл менеджера регистрации, не исполняя BSL."""
    path = Path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except OSError as error:
        raise EdReadError("Файл менеджера регистрации недоступен") from error
    if len(raw) > MAX_BYTES:
        raise EdResourceLimitError("Размер менеджера регистрации превышает 32 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise EdReadError("Ожидался UTF-8 файл менеджера регистрации") from error
    return _read(text, "module", str(path), raw.startswith(b"\xef\xbb\xbf"), raw)


def read_registration_manager_text(
    text: str, *, file_id: str = "module", path: str | Path = "<memory>"
) -> RegistrationModuleDocument:
    """Создаёт снимок строки без обращения к файловой системе."""
    try:
        raw = text.encode("utf-8")
    except UnicodeError as error:
        raise EdReadError("Некорректный Unicode в тексте менеджера регистрации") from error
    return _read(text.removeprefix("\ufeff"), file_id, str(path), text.startswith("\ufeff"), raw)


def _read(text: str, file_id: str, path: str, bom: bool, raw: bytes) -> RegistrationModuleDocument:
    if len(raw) > MAX_BYTES:
        raise EdResourceLimitError("Размер менеджера регистрации превышает 32 MiB")
    if not file_id:
        raise ValueError("Пустой идентификатор файла")
    lines = _source_lines(text)
    if len(lines) > MAX_LINES:
        raise EdResourceLimitError("Менеджер регистрации превышает миллион строк")
    if not text.strip():
        raise EdFormatError("Пустой менеджер регистрации")
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    newline = (
        "mixed"
        if "\r\n" in text and "\n" in text.replace("\r\n", "")
        else ("\r\n" if "\r\n" in text else "\n")
    )
    source = SourceFile(
        file_id,
        path,
        text,
        hashlib.sha256(raw).hexdigest(),
        tuple(offsets),
        bom=bom,
        newline=newline,
    )
    return _Reader(source, lex(source)).read()


def _bare(tokens: tuple[Token, ...]) -> tuple[Token, ...]:
    return tokens[:-1] if tokens and tokens[-1].value == ";" else tokens


def _name(tokens: tuple[Token, ...]) -> str:
    return "".join(token.value for token in tokens)


def _assignment(statement: Statement) -> tuple[str, tuple[Token, ...]] | None:
    tokens = _bare(statement.tokens)
    for index, token in enumerate(tokens):
        if token.value == "=" and token.kind == "symbol":
            if all(item.kind == "identifier" or item.value == "." for item in tokens[:index]):
                return _name(tokens[:index]), tokens[index + 1 :]
            break
    return None


def _call(tokens: tuple[Token, ...]) -> tuple[str, tuple[tuple[Token, ...], ...]] | None:
    tokens = _bare(tokens)
    if not tokens or tokens[-1].value != ")":
        return None
    for index, token in enumerate(tokens):
        if token.value == "(" and token.kind == "symbol":
            if not tokens[:index] or not all(
                item.kind == "identifier" or item.value == "." for item in tokens[:index]
            ):
                return None
            depth = 0
            for cursor in range(index, len(tokens)):
                current = tokens[cursor]
                if current.kind == "symbol":
                    depth += (current.value == "(") - (current.value == ")")
                    if depth == 0 and cursor != len(tokens) - 1:
                        return None
            return _name(tokens[:index]), split_arguments(tokens[index + 1 : -1])
    return None


def _source_lines(text: str) -> list[str]:
    """Строки только по LF. CR LF — один разделитель; U+2028 и прочие символы строку не рвут."""
    if not text:
        return []
    lines: list[str] = []
    start = 0
    while True:
        index = text.find("\n", start)
        if index < 0:
            lines.append(text[start:])
            break
        lines.append(text[start : index + 1])
        start = index + 1
        if start == len(text):
            break
    return lines


def _root_name(text: str) -> str | None:
    """Корень отбора. Пролог `<?xml …?>` не мешает: в функции и в аргументе он один и тот же."""
    stripped = text.lstrip()
    if stripped.startswith("<?"):
        end = stripped.find("?>")
        if end < 0:
            return None
        stripped = stripped[end + 2 :].lstrip()
    for name in _FILTER_ROOTS:
        if stripped.startswith("<" + name):
            return name
    return None


def _decoded_line_map(source: SourceFile, token: Token) -> tuple[int, ...]:
    """Строка исходника для каждой строки декодированного литерала, включая продолжения."""
    text = source.text

    def line_of(pos: int) -> int:
        return source.span(pos, min(pos + 1, len(text))).line_start

    mapping = [line_of(token.start)]
    index = token.start + 1
    limit = token.end - 1
    while index < limit:
        char = text[index]
        if char == '"':
            if index + 1 < token.end and text[index + 1] == '"':
                index += 2
                continue
            break
        if char in "\r\n":
            if text.startswith("\r\n", index):
                index += 1
            index += 1
            while index < len(text) and text[index] in " \t":
                index += 1
            while text.startswith("//", index):
                end = text.find("\n", index)
                index = len(text) if end < 0 else end + 1
                while index < len(text) and text[index] in " \t":
                    index += 1
            if index < len(text) and text[index] == "|":
                index += 1
                mapping.append(line_of(index))
                continue
            break
        index += 1
    return tuple(mapping)


def _allowed(tag: str, root: str) -> frozenset[str]:
    if tag in _FILTER_ROOTS:
        return frozenset({"ЭлементОтбора", "Группа"})
    if tag == "Группа":
        return _GROUP
    if tag == "ЭлементОтбора":
        return _PLAN_ITEM if root == "ОтборПоСвойствамПланаОбмена" else _OBJECT_ITEM
    if tag in ("ТаблицаСвойствОбъекта", "ТаблицаСвойствПланаОбмена"):
        return _TABLE
    if tag == "Свойство":
        return _PROPERTY
    return frozenset()


def _freeze(element: ET._Element) -> XmlNode:
    children = tuple(_freeze(child) for child in element if isinstance(child.tag, str))
    text = element.text or ""
    if children and not text.strip():
        text = ""
    attrib = tuple(sorted((str(key), str(value)) for key, value in element.attrib.items()))
    return XmlNode(str(element.tag), attrib, text, children)


def _unknown_nodes(node: XmlNode, root: str) -> tuple[XmlNode, ...]:
    found: list[XmlNode] = []
    allowed = _allowed(node.tag, root)
    for child in node.children:
        if child.tag not in allowed:
            found.append(child)
        else:
            found.extend(_unknown_nodes(child, root))
    return tuple(found)


def _depth(element: ET._Element, current: int = 1) -> int:
    best = current
    for child in element:
        if isinstance(child.tag, str):
            best = max(best, _depth(child, current + 1))
            if best > MAX_XML_DEPTH:
                return best
    return best


def _unsafe_markup(text: str) -> bool:
    folded = text.casefold()
    return "<!doctype" in folded or "<!entity" in folded


def _banned_markup(root: ET._Element) -> str | None:
    """DTD, сущности и XInclude запрещены. Комментарий — не DTD: исполнитель его не читает."""
    if getattr(root.getroottree().docinfo, "doctype", ""):
        return "DTD и сущности в отборе запрещены"
    for element in root.iter():
        tag = element.tag
        if tag is ET.Comment:
            continue
        if not isinstance(tag, str):
            return "неподдерживаемый узел XML отбора"
        if tag == "{http://www.w3.org/2001/XInclude}include":
            return "DTD и сущности в отборе запрещены"
    return None


def _read_filter_xml(
    text: str,
) -> tuple[str, XmlNode | None, tuple[XmlNode, ...], str | None]:
    guessed = _root_name(text) or ""
    if len(text.encode("utf-8")) > MAX_FILTER_BYTES:
        return guessed, None, (), "размер литерала отбора превышает 4 МиБ"
    if _unsafe_markup(text):
        return guessed, None, (), "DTD и сущности в отборе запрещены"
    parser = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = ET.fromstring(text.encode("utf-8"), parser)
    except ET.XMLSyntaxError:
        return guessed, None, (), "повреждённый XML отбора"
    banned = _banned_markup(root)
    if banned:
        return guessed, None, (), banned
    if _depth(root) > MAX_XML_DEPTH:
        return guessed, None, (), f"глубина XML отбора превышает {MAX_XML_DEPTH}"
    tag = str(root.tag)
    if tag not in _FILTER_ROOTS:
        return (
            tag,
            None,
            (),
            ("корень отбора не является отбором по свойствам плана обмена или объекта"),
        )
    tree = _freeze(root)
    return tag, tree, _unknown_nodes(tree, tag), None


def _qualify(rules: list[RegistrationRule]) -> tuple[RegistrationRule, ...]:
    """Уникальный код. Занятый суффикс `#N` заменяется экранированным идентификатором."""
    totals = Counter(rule.identifier for rule in rules)
    seen: Counter[str] = Counter()
    used: set[str] = set()
    result: list[RegistrationRule] = []
    for rule in rules:
        seen[rule.identifier] += 1
        suffix = f"#{seen[rule.identifier]}" if totals[rule.identifier] > 1 else ""
        base = rule.identifier if rule.identifier else "~empty"
        segment = (escape_segment(rule.identifier) if rule.identifier else "~empty") + suffix
        code = base + suffix
        if code in used:
            code = segment
        used.add(code)
        result.append(replace(rule, code=code, qualified_id=segment))
    return tuple(result)


@dataclass
class _Flow:
    """Глубина непрозрачного блока и признак «после возврата»: такие операторы не применяются."""

    depth: int = 0
    returned: bool = False

    @property
    def blocked(self) -> bool:
        return self.depth > 0 or self.returned


def _directive_word(statement: Statement) -> str:
    token = statement.tokens[0]
    if token.kind != "directive":
        return ""
    return token.value.split(None, 1)[0].casefold()


def _silent_directive(statement: Statement) -> bool:
    """`#Область` / `#КонецОбласти` и аннотации не несут правил и молча пропускаются."""
    token = statement.tokens[0]
    if token.kind != "directive":
        return False
    if token.value.startswith("&"):
        return True
    return _directive_word(statement) in ("#область", "#конецобласти")


def _preprocessor_kind(statement: Statement) -> str | None:
    word = _directive_word(statement)
    if not word or _silent_directive(statement):
        return None
    if word in ("#если", "#вставка", "#удаление"):
        return "open"
    if word in ("#иначеесли", "#иначе"):
        return "mid"
    if word in ("#конецесли", "#конецвставки", "#конецудаления"):
        return "close"
    return "other"


_BSL_OPEN = {"если": "if", "для": "loop", "пока": "loop", "попытка": "try"}
_BSL_MID = {"иначеесли": "if", "иначе": "if", "исключение": "try"}
_BSL_CLOSE = {"конецесли": "if", "конеццикла": "loop", "конецпопытки": "try"}


def _bsl_control(statement: Statement) -> tuple[str, str] | None:
    head = statement.head
    if head in _BSL_OPEN:
        return "open", _BSL_OPEN[head]
    if head in _BSL_MID:
        return "mid", _BSL_MID[head]
    if head in _BSL_CLOSE:
        return "close", _BSL_CLOSE[head]
    if head == "возврат":
        return "return", "return"
    return None


def _canon_field(name: str) -> str:
    """Имя поля шаблона без учёта регистра: BSL его не различает."""
    return _CANON_FIELDS.get(name.casefold(), name)


_CANON_FIELDS = {
    name.casefold(): name for name in (*_STRING_FIELDS, *_BOOL_FIELDS, "ИмяМенеджераРегистрации")
}
_ROOT_MISMATCH = "корень отбора не соответствует позиции аргумента"


@dataclass
class _Block:
    routine: Routine
    header: Statement
    body: tuple[Statement, ...]
    kind: str = ""


class _Reader:
    def __init__(self, source: SourceFile, lexical: Lexed) -> None:
        self.source = source
        self.lexical = lexical
        self.marks: list[tuple[SourceSpan, Classification]] = []
        self.unknown: list[UnknownFragment] = []
        self.filters: dict[str, FilterTree] = {}
        self.filter_list: list[FilterTree] = []
        self.rules_by_name: dict[str, RegistrationRule] = {}
        self.parameters: list[RegistrationParameter] = []
        self.cases: list[DispatcherCase] = []
        self.filter_helper = False

    def expr(self, tokens: tuple[Token, ...], fallback: SourceSpan) -> Expr:
        if not tokens:
            return Expr("", self.source.span(fallback.char_start, fallback.char_start))
        span = self.source.span(tokens[0].start, tokens[-1].end)
        raw = self.source.text[span.char_start : span.char_end]
        kind: str | None = None
        value: str | int | float | bool | None = None
        if len(tokens) == 1:
            token = tokens[0]
            if token.kind == "string":
                kind, value = "string", token.value
            elif token.kind == "number":
                try:
                    value = float(token.value) if "." in token.value else int(token.value)
                    kind = "number"
                except ValueError:
                    pass
            elif token.folded in ("истина", "ложь"):
                kind, value = "boolean", token.folded == "истина"
            elif token.folded in ("неопределено", "null"):
                kind = "undefined"
            elif token.kind == "date":
                kind, value = "date", token.value
        parts: tuple[str, ...] = ()
        if all(
            token.kind == ("identifier" if index % 2 == 0 else "symbol")
            and (index % 2 == 0 or token.value == ".")
            for index, token in enumerate(tokens)
        ):
            parts = tuple(token.value for token in tokens[::2])
        return Expr(raw, span, kind, value, parts)

    def make_unknown(self, span: SourceSpan, reason: str, owner: str | None) -> None:
        raw = self.source.text[span.char_start : span.char_end]
        self.unknown.append(
            UnknownFragment(
                entity_id=f"{span.file_id}:unknown:{span.char_start}:{len(self.unknown)}",
                kind="unknown",
                name=reason,
                span=span,
                raw_text=raw,
                owner_id=owner,
                reason=reason,
                status=ParseStatus.PARTIAL,
            )
        )

    def statement_unknown(self, statement: Statement, reason: str, owner: str | None) -> None:
        self.make_unknown(statement.span, reason, owner)

    def _mark_silent(self, statement: Statement) -> None:
        self.marks.append((statement.span, Classification.DECLARATIVE))

    def _take_preprocessor(self, statement: Statement, flow: _Flow, owner: str | None) -> bool:
        """Директива кроме области и аннотации — неизвестный фрагмент, код под ней не берётся."""
        kind = _preprocessor_kind(statement)
        if kind is None:
            return False
        if kind == "open":
            flow.depth += 1
        elif kind == "close" and flow.depth:
            flow.depth -= 1
        self.statement_unknown(statement, "директива препроцессора", owner)
        return True

    def _bury_statement(
        self, statement: Statement, flow: _Flow, owner: str | None, reason: str
    ) -> None:
        """Оператор внутри условия, цикла, попытки или после возврата не меняет поля и правила."""
        control = _bsl_control(statement)
        if control is not None:
            kind, _name = control
            if kind == "open":
                flow.depth += 1
            elif kind == "close" and flow.depth:
                flow.depth -= 1
        self.statement_unknown(statement, reason, owner)

    def _open_control(
        self,
        statement: Statement,
        flow: _Flow,
        owner: str | None,
        reasons: dict[str, str],
    ) -> bool:
        """Начинает непрозрачный блок. `reasons` сопоставляет вид блока и текст фрагмента."""
        control = _bsl_control(statement)
        if control is None:
            return False
        kind, name = control
        reason = reasons.get(name, reasons.get("if", "неизвестное условие"))
        if kind == "open":
            flow.depth += 1
        elif kind == "close" and flow.depth:
            flow.depth -= 1
        elif kind == "return":
            flow.returned = True
            reason = reasons.get("return", reason)
        self.statement_unknown(statement, reason, owner)
        return True

    def token_unknown(
        self, tokens: tuple[Token, ...], reason: str, owner: str | None, fallback: SourceSpan
    ) -> None:
        if not tokens:
            self.make_unknown(fallback, reason, owner)
            return
        self.make_unknown(self.source.span(tokens[0].start, tokens[-1].end), reason, owner)

    def read(self) -> RegistrationModuleDocument:
        blocks = self.routine_blocks()
        names: set[str] = set()
        for block in blocks:
            key = block.routine.name.casefold()
            if key in names:
                raise EdFormatError(f"Повторное объявление метода «{block.routine.name}»")
            names.add(key)
            block.kind = self.classify(block)
            role = _ROLES.get(block.kind)
            if role:
                block.routine = replace(block.routine, roles=frozenset({role}))
            self.marks.append((block.routine.span, Classification.DECLARATIVE))
        if not any(block.kind == "init" for block in blocks):
            raise EdFormatError("Не найдена процедура ИнициализацияПравилРегистрации")
        self.filter_helper = any(
            block.kind == "helper"
            and len(block.routine.parameters) == 3
            and all(parameter.name for parameter in block.routine.parameters)
            for block in blocks
        )
        for block in blocks:
            if block.kind == "filter":
                self.parse_filter(block)
        for block in blocks:
            if block.kind == "rule":
                rule = self.parse_rule(block)
                if rule is None:
                    self.make_unknown(
                        block.routine.span,
                        "неизвестное объявление правила",
                        block.routine.entity_id,
                    )
                else:
                    self.rules_by_name[block.routine.name.casefold()] = rule
        ordered: list[RegistrationRule] = []
        for block in blocks:
            if block.kind == "parameters":
                self.parameters = self.parse_parameters(block)
            elif block.kind == "dispatcher":
                self.cases.extend(self.parse_dispatcher(block))
            elif block.kind == "init":
                ordered = self.parse_init(block)
            elif block.kind in {"info", "helper", "forwarder", "handler"}:
                self.marks.append((block.routine.body_span, Classification.OPAQUE_CODE))
            elif block.kind == "unknown":
                self.make_unknown(block.routine.span, "неизвестный метод", block.routine.entity_id)
        rules = _qualify(ordered)
        marks = [*self.marks, *((item.span, Classification.UNKNOWN) for item in self.unknown)]
        coverage = build_coverage(
            self.source,
            self.lexical.tokens,
            marks,
            [block.routine.span for block in blocks],
        )
        status = ParseStatus.PARTIAL if self.unknown else ParseStatus.COMPLETE
        return RegistrationModuleDocument(
            (self.source,),
            tuple(self.parameters),
            rules,
            tuple(block.routine for block in blocks),
            tuple(self.cases),
            tuple(self.filter_list),
            tuple(self.unknown),
            status,
            coverage,
        )

    def routine_blocks(self) -> list[_Block]:
        blocks: list[_Block] = []
        header: Statement | None = None
        body: list[Statement] = []
        for statement in self.lexical.statements:
            if statement.head in ("процедура", "функция"):
                if header is not None:
                    raise EdFormatError("Вложенное объявление метода")
                header, body = statement, []
            elif statement.head in ("конецпроцедуры", "конецфункции"):
                if (
                    header is None
                    or statement.head
                    != {"процедура": "конецпроцедуры", "функция": "конецфункции"}[header.head]
                ):
                    raise EdFormatError("Несогласованный конец метода")
                blocks.append(_Block(self.make_routine(header, statement), header, tuple(body)))
                header = None
            elif header is not None:
                body.append(statement)
            elif statement.tokens[0].kind == "directive":
                self.marks.append((statement.span, Classification.DECLARATIVE))
            else:
                self.statement_unknown(statement, "оператор вне метода", None)
        if header is not None:
            raise EdFormatError("Незакрытый метод")
        for code, span in self.lexical.warnings:
            self.make_unknown(span, code, None)
        return blocks

    def make_routine(self, header: Statement, end: Statement) -> Routine:
        tokens = header.tokens
        if len(tokens) < 4 or tokens[1].kind != "identifier" or tokens[2].value != "(":
            raise EdFormatError("Неверный заголовок метода")
        exported = tokens[-1].folded == "экспорт"
        close = len(tokens) - (2 if exported else 1)
        if tokens[close].value != ")":
            raise EdFormatError("Незакрытые параметры метода")
        params: list[FormalParameter] = []
        for part in split_arguments(tokens[3:close]):
            expression = self.expr(part, header.span)
            by_value = bool(part and part[0].folded == "знач")
            start = 1 if by_value else 0
            name = (
                part[start].value
                if len(part) > start and part[start].kind == "identifier"
                else None
            )
            default = None
            if len(part) > start + 1 and part[start + 1].value == "=":
                default = self.expr(part[start + 2 :], header.span)
            params.append(FormalParameter(name, by_value, default, expression.raw, expression.span))
        span = self.source.span(header.span.char_start, end.span.char_end)
        return Routine(
            entity_id=f"{self.source.file_id}:routine:{header.span.char_start}",
            kind="routine",
            name=tokens[1].value,
            span=span,
            raw_text=self.source.text[span.char_start : span.char_end],
            regions=header.regions,
            tag_ids=header.tag_ids,
            routine_kind="function" if header.head == "функция" else "procedure",
            parameters_raw=self.source.text[tokens[2].end : tokens[close].start],
            parameters=tuple(params),
            exported=exported,
            roles=frozenset(),
            body_span=self.source.span(header.span.char_end, end.span.char_start),
        )

    def classify(self, block: _Block) -> str:
        routine = block.routine
        name = routine.name.casefold()
        if name == "информацияоправилах" and routine.routine_kind == "function":
            return "info"
        if name == "параметрырегистрации" and routine.routine_kind == "function":
            return "parameters"
        if name == "инициализацияправилрегистрации" and routine.routine_kind == "procedure":
            return "init"
        if name in _DISPATCHERS and routine.routine_kind == "procedure":
            return "dispatcher"
        if name == "установитьотборы" and routine.routine_kind == "procedure":
            return "helper"
        if name == "выполнитьправиларегистрациидляобъекта" and routine.routine_kind == "procedure":
            return "forwarder"
        if name.startswith("добавитьпро_") and routine.routine_kind == "procedure":
            return "rule"
        if name.startswith("про_") and routine.routine_kind == "procedure":
            return "handler"
        if self.is_filter(block):
            return "filter"
        return "unknown"

    def is_filter(self, block: _Block) -> bool:
        routine = block.routine
        if routine.routine_kind != "function" or routine.parameters or len(block.body) != 1:
            return False
        statement = block.body[0]
        if statement.head != "возврат":
            return False
        tokens = _bare(statement.tokens)
        return (
            len(tokens) == 2
            and tokens[1].kind == "string"
            and _root_name(tokens[1].value) is not None
        )

    def parse_literal(self, token: Token, routine_name: str, owner: str | None) -> FilterTree:
        span = self.source.span(token.start, token.end)
        root, tree, unknown, error = _read_filter_xml(token.value)
        if error:
            self.make_unknown(span, error, owner)
        return FilterTree(
            root,
            tree,
            token.value,
            span,
            self.source.text[span.char_start : span.char_end],
            _decoded_line_map(self.source, token),
            unknown,
            routine_name,
            error,
        )

    def parse_filter(self, block: _Block) -> None:
        token = _bare(block.body[0].tokens)[1]
        tree = self.parse_literal(token, block.routine.name, block.routine.entity_id)
        self.filters[block.routine.name.casefold()] = tree
        self.filter_list.append(tree)

    def parse_rule(self, block: _Block) -> RegistrationRule | None:
        names = [param.name for param in block.routine.parameters]
        if len(names) != 2 or any(name is None for name in names):
            return None
        collection = str(names[0])
        manager_parameter = str(names[1])
        owner = block.routine.entity_id
        variable: str | None = None
        strings: dict[str, list[Expr]] = defaultdict(list)
        bools: dict[str, list[Expr]] = defaultdict(list)
        manager: list[Expr] = []
        preserved: list[tuple[str, Expr]] = []
        filter_args: tuple[tuple[Token, ...], tuple[Token, ...]] | None = None
        early: list[tuple[SourceSpan, str]] = []
        flow = _Flow()
        rule_reasons = {
            "if": "неизвестное условие правила",
            "loop": "неизвестный цикл правила",
            "try": "неизвестная попытка",
            "return": "возврат прерывает правило",
        }
        for statement in block.body:
            if _silent_directive(statement):
                self._mark_silent(statement)
                continue
            if self._take_preprocessor(statement, flow, owner):
                continue
            if flow.blocked:
                reason = (
                    "оператор после возврата"
                    if flow.returned
                    else "оператор внутри условия, цикла или попытки"
                )
                self._bury_statement(statement, flow, owner, reason)
                continue
            if self._open_control(statement, flow, owner, rule_reasons):
                continue
            if variable is None:
                added = self._added(statement, collection)
                if added:
                    variable = added
                    continue
                early.append((statement.span, "неизвестный оператор правила"))
                continue
            filters = self._filter_call(statement, variable)
            if filters is not None:
                if not self.filter_helper:
                    self.statement_unknown(statement, "внешний помощник УстановитьОтборы", owner)
                    continue
                if filter_args is not None:
                    self.statement_unknown(statement, "повторный вызов УстановитьОтборы", owner)
                else:
                    filter_args = filters
                continue
            field = self._field_assignment(statement, variable)
            if field is None:
                self.statement_unknown(statement, "неизвестный оператор правила", owner)
                continue
            name, right = field
            expression = self.expr(right, statement.span)
            self._take_field(
                statement,
                owner,
                name,
                expression,
                strings,
                bools,
                manager,
                preserved,
                manager_parameter,
            )
        if variable is None:
            return None
        for span, reason in early:
            self.make_unknown(span, reason, owner)
        plan_filter = object_filter = None
        if filter_args is not None:
            plan_filter = self.resolve_filter(
                filter_args[0], owner, block.routine.span, "ОтборПоСвойствамПланаОбмена"
            )
            object_filter = self.resolve_filter(
                filter_args[1], owner, block.routine.span, "ОтборПоСвойствамОбъекта"
            )
        identifier = _string_value(strings["Идентификатор"])
        flag_fields = {
            field_name: _fold_bool(bools[field_name]) for _, field_name in HANDLER_EVENTS
        }
        batch = flag_fields["ПакетноеВыполнениеОбработчиков"]
        return RegistrationRule(
            identifier,
            "",
            "",
            block.routine.name,
            _fold_string(strings["ОбъектМетаданныхИмя"]),
            _fold_string(strings["ИмяПланаОбмена"]),
            _fold_string(strings["ИмяРеквизитаФлага"]),
            _fold_bool(bools["ПравилоПоСвойствамОбъектаПустое"]),
            batch,
            tuple((event, flag_fields[field_name]) for event, field_name in HANDLER_EVENTS),
            _fold_expr(manager, manager_parameter),
            plan_filter,
            object_filter,
            tuple(preserved),
            (block.routine.span,),
            block.routine.span,
            block.routine.raw_text,
        )

    def _take_field(
        self,
        statement: Statement,
        owner: str,
        name: str,
        expression: Expr,
        strings: dict[str, list[Expr]],
        bools: dict[str, list[Expr]],
        manager: list[Expr],
        preserved: list[tuple[str, Expr]],
        manager_parameter: str,
    ) -> None:
        name = _canon_field(name)
        if name == "ИмяМенеджераРегистрации":
            if not _manager_value(expression, manager_parameter):
                self.statement_unknown(statement, "неизвестное присваивание", owner)
            if manager:
                self.statement_unknown(statement, "повторное присваивание", owner)
            manager.append(expression)
            return
        if name in _STRING_FIELDS:
            if expression.literal_type != "string":
                self.statement_unknown(statement, "неизвестное присваивание", owner)
            if strings[name]:
                self.statement_unknown(statement, "повторное присваивание", owner)
            strings[name].append(expression)
            return
        if name in _BOOL_FIELDS:
            if expression.literal_type != "boolean":
                self.statement_unknown(statement, "неизвестное присваивание", owner)
            if bools[name]:
                self.statement_unknown(statement, "повторное присваивание", owner)
            bools[name].append(expression)
            return
        preserved.append((name, expression))
        self.statement_unknown(statement, "неизвестное присваивание", owner)

    def _added(self, statement: Statement, collection: str) -> str | None:
        assign = _assignment(statement)
        if assign is None:
            return None
        variable, right = assign
        if "." in variable:
            return None
        call = _call(right)
        if (
            call is None
            or call[1] != ()
            or call[0].casefold() != f"{collection.casefold()}.добавить"
        ):
            return None
        return variable

    def _field_assignment(
        self, statement: Statement, variable: str
    ) -> tuple[str, tuple[Token, ...]] | None:
        assign = _assignment(statement)
        if assign is None:
            return None
        left, right = assign
        base, dot, field = left.rpartition(".")
        if dot != "." or base.casefold() != variable.casefold() or "." in field or not field:
            return None
        return field, right

    def _filter_call(
        self, statement: Statement, variable: str
    ) -> tuple[tuple[Token, ...], tuple[Token, ...]] | None:
        call = _call(statement.tokens)
        if call is None or "." in call[0] or call[0].casefold() != "установитьотборы":
            return None
        if len(call[1]) != 3:
            return None
        first = call[1][0]
        if (
            len(first) != 1
            or first[0].kind != "identifier"
            or first[0].value.casefold() != variable.casefold()
        ):
            return None
        return call[1][1], call[1][2]

    def resolve_filter(
        self, tokens: tuple[Token, ...], owner: str, fallback: SourceSpan, expected_root: str
    ) -> FilterTree | None:
        """Аргумент `УстановитьОтборы`. Корень XML сверяется с позицией: план, затем объект."""
        if len(tokens) == 1 and tokens[0].kind == "string":
            tree = self._with_expected_root(
                self.parse_literal(tokens[0], "", owner), tokens, owner, fallback, expected_root
            )
            self.filter_list.append(tree)
            return tree
        call = _call(tokens)
        if call is None or "." in call[0] or call[1] != ():
            self.token_unknown(tokens, "вычисляемый или внешний вызов отбора", owner, fallback)
            return None
        found = self.filters.get(call[0].casefold())
        if found is None:
            self.token_unknown(tokens, "функция не возвращает литерал отбора", owner, fallback)
            return None
        return self._with_expected_root(found, tokens, owner, fallback, expected_root)

    def _with_expected_root(
        self,
        tree: FilterTree,
        tokens: tuple[Token, ...],
        owner: str,
        fallback: SourceSpan,
        expected_root: str,
    ) -> FilterTree:
        """Чужой корень в позиции аргумента — ошибка модуля: загрузчик БСП корень не проверяет."""
        if tree.error or tree.root_tag == expected_root:
            return tree
        self.token_unknown(tokens, _ROOT_MISMATCH, owner, fallback)
        return replace(tree, error=_ROOT_MISMATCH)

    def parse_parameters(self, block: _Block) -> list[RegistrationParameter]:
        structure: str | None = None
        literals: dict[str, Expr] = {}
        opaque: set[str] = set()
        found: dict[str, RegistrationParameter] = {}
        order: list[str] = []
        owner = block.routine.entity_id
        flow = _Flow()
        parameter_reasons = {
            "if": "неизвестное условие параметров",
            "loop": "неизвестный цикл параметров",
            "try": "неизвестная попытка",
            "return": "возврат прерывает параметры",
        }
        for statement in block.body:
            if _silent_directive(statement):
                self._mark_silent(statement)
                continue
            if self._take_preprocessor(statement, flow, owner):
                continue
            if flow.blocked:
                assign = _assignment(statement)
                if assign is not None and "." not in assign[0]:
                    opaque.add(assign[0].casefold())
                    literals.pop(assign[0].casefold(), None)
                reason = (
                    "оператор после возврата"
                    if flow.returned
                    else "оператор внутри условия, цикла или попытки"
                )
                self._bury_statement(statement, flow, owner, reason)
                continue
            created = _new_structure(statement)
            if created and structure is None:
                structure = created
                continue
            inserted = _insert(statement, structure) if structure else None
            if inserted is not None:
                key, value_tokens = inserted
                self._take_parameter(
                    statement, owner, key, value_tokens, literals, opaque, found, order
                )
                continue
            assign = _assignment(statement)
            if assign is not None and "." not in assign[0]:
                variable, right = assign
                if _is_opaque_expr(right):
                    opaque.add(variable.casefold())
                    literals.pop(variable.casefold(), None)
                    self.marks.append((statement.span, Classification.OPAQUE_CODE))
                    continue
                if len(right) == 1:
                    expression = self.expr(right, statement.span)
                    if expression.literal_type in ("string", "number", "boolean"):
                        literals[variable.casefold()] = expression
                        opaque.discard(variable.casefold())
                        continue
                date = self._date_expr(right, statement.span)
                if date is not None:
                    literals[variable.casefold()] = date
                    opaque.discard(variable.casefold())
                    continue
                opaque.add(variable.casefold())
                literals.pop(variable.casefold(), None)
                self.statement_unknown(statement, "неизвестный оператор параметров", owner)
                continue
            tokens = _bare(statement.tokens)
            if (
                statement.head == "возврат"
                and structure
                and len(tokens) == 2
                and tokens[1].value == structure
            ):
                flow.returned = True
                continue
            if self._open_control(statement, flow, owner, parameter_reasons):
                continue
            self.statement_unknown(statement, "неизвестный оператор параметров", owner)
        return [found[name] for name in order]

    def _take_parameter(
        self,
        statement: Statement,
        owner: str,
        key: str,
        value_tokens: tuple[Token, ...],
        literals: dict[str, Expr],
        opaque: set[str],
        found: dict[str, RegistrationParameter],
        order: list[str],
    ) -> None:
        expression: Expr | None = None
        presence = "expression"
        opaque_value = False
        if len(value_tokens) == 1 and value_tokens[0].kind == "string":
            expression = self.expr(value_tokens, statement.span)
            presence = "literal"
        else:
            date = self._date_expr(value_tokens, statement.span)
            if date is not None:
                expression = date
                presence = "literal"
            elif len(value_tokens) == 1 and value_tokens[0].kind == "identifier":
                folded = value_tokens[0].folded
                if folded in literals:
                    expression = literals[folded]
                    presence = "literal"
                elif folded in opaque or _is_opaque_expr(value_tokens):
                    expression = self.expr(value_tokens, statement.span)
                    opaque_value = True
                else:
                    self.statement_unknown(statement, "нелитеральный параметр", owner)
                    expression = self.expr(value_tokens, statement.span)
            elif _is_opaque_expr(value_tokens):
                expression = self.expr(value_tokens, statement.span)
                opaque_value = True
            else:
                self.statement_unknown(statement, "нелитеральный параметр", owner)
                expression = self.expr(value_tokens, statement.span)
        if opaque_value:
            self.marks.append((statement.span, Classification.OPAQUE_CODE))
        if expression is None:
            return
        if key in found:
            previous = found[key]
            found[key] = RegistrationParameter(
                key,
                Field("ambiguous", None, (*previous.field.assignments, expression)),
                statement.span,
                statement.raw_text,
            )
            self.statement_unknown(statement, "повторный параметр", owner)
            return
        found[key] = RegistrationParameter(
            key,
            Field(presence, expression if presence == "literal" else None, (expression,)),
            statement.span,
            statement.raw_text,
        )
        order.append(key)

    def _date_expr(self, tokens: tuple[Token, ...], fallback: SourceSpan) -> Expr | None:
        call = _call(tokens)
        if call is None or call[0].casefold() != "дата" or not 3 <= len(call[1]) <= 6:
            return None
        if not all(
            len(arg) == 1 and arg[0].kind == "number" and arg[0].value.isdigit() for arg in call[1]
        ):
            return None
        base = self.expr(tokens, fallback)
        return Expr(base.raw, base.span, "date", None, ("Дата",))

    def parse_dispatcher(self, block: _Block) -> list[DispatcherCase]:
        stack: list[str | None] = []
        cases: list[DispatcherCase] = []
        owner = block.routine.entity_id
        flow = _Flow()
        dispatcher_reasons = {
            "loop": "неизвестный цикл диспетчера",
            "try": "неизвестная попытка",
            "return": "возврат прерывает диспетчер",
        }
        for statement in block.body:
            if _silent_directive(statement):
                self._mark_silent(statement)
                continue
            if self._take_preprocessor(statement, flow, owner):
                continue
            if flow.blocked:
                reason = (
                    "оператор после возврата"
                    if flow.returned
                    else "оператор внутри условия, цикла или попытки"
                )
                self._bury_statement(statement, flow, owner, reason)
                continue
            if statement.head in (
                "попытка",
                "исключение",
                "конецпопытки",
                "для",
                "пока",
                "конеццикла",
            ) or (statement.head == "возврат"):
                self._open_control(statement, flow, owner, dispatcher_reasons)
                continue
            if statement.head in ("если", "иначеесли"):
                literal = _identifier_compare(statement)
                if statement.head == "если":
                    stack.append(literal)
                elif not stack:
                    self.statement_unknown(statement, "ветка без начала диспетчера", owner)
                    continue
                else:
                    stack[-1] = literal
                if literal is None:
                    self.statement_unknown(
                        statement, "ветка диспетчера без литерала ПРО.Идентификатор", owner
                    )
                continue
            if statement.head == "иначе":
                if stack:
                    stack[-1] = None
                else:
                    self.statement_unknown(statement, "ветка без начала диспетчера", owner)
                continue
            if statement.head == "конецесли":
                if stack:
                    stack.pop()
                else:
                    self.statement_unknown(statement, "конец ветки без начала диспетчера", owner)
                continue
            call = _call(statement.tokens)
            if call is not None and "." not in call[0] and stack and stack[-1] is not None:
                tokens = _bare(statement.tokens)
                opening = next(index for index, token in enumerate(tokens) if token.value == "(")
                cases.append(
                    DispatcherCase(
                        entity_id=(
                            f"{self.source.file_id}:case:{statement.span.char_start}:{len(cases)}"
                        ),
                        kind="case",
                        name=stack[-1],
                        span=statement.span,
                        raw_text=statement.raw_text,
                        regions=statement.regions,
                        tag_ids=statement.tag_ids,
                        dispatcher_id=owner,
                        literal_name=stack[-1],
                        target=self.expr(tokens[:opening], statement.span),
                        arguments=tuple(self.expr(arg, statement.span) for arg in call[1]),
                        returns=False,
                    )
                )
                continue
            self.statement_unknown(statement, "неизвестный оператор диспетчера", owner)
        return cases

    def parse_init(self, block: _Block) -> list[RegistrationRule]:
        names = [param.name for param in block.routine.parameters]
        if len(names) != 2 or any(name is None for name in names):
            self.make_unknown(
                block.routine.span, "неизвестная инициализация правил", block.routine.entity_id
            )
            return []
        params = [str(name) for name in names]
        collection, owner = params[0], block.routine.entity_id
        flow = _Flow()
        pending_column = False
        ordered: list[RegistrationRule] = []
        init_reasons = {
            "if": "неизвестное условие инициализации",
            "loop": "цикл генерации правил",
            "try": "неизвестная попытка",
            "return": "возврат прерывает инициализацию",
        }
        for statement in block.body:
            if _silent_directive(statement):
                self._mark_silent(statement)
                continue
            if self._take_preprocessor(statement, flow, owner):
                continue
            if flow.blocked:
                reason = (
                    "оператор после возврата"
                    if flow.returned
                    else "правило внутри цикла или условия"
                )
                self._bury_statement(statement, flow, owner, reason)
                continue
            if pending_column:
                if _is_column_add(statement, collection):
                    continue
                if statement.head == "конецесли":
                    pending_column = False
                    continue
                self.statement_unknown(statement, "неизвестное тело проверки колонки", owner)
                continue
            if _is_column_guard(statement, collection):
                pending_column = True
                continue
            if self._open_control(statement, flow, owner, init_reasons):
                continue
            called = _rule_call(statement, params)
            if called is None:
                self.statement_unknown(statement, "неизвестный оператор инициализации", owner)
                continue
            rule = self.rules_by_name.get(called.casefold())
            if rule is None:
                self.statement_unknown(statement, "вызов не разобран как правило", owner)
                continue
            ordered.append(replace(rule, origins=(statement.span, rule.span)))
        return ordered


def _fold_string(exprs: list[Expr]) -> Field[str]:
    if not exprs:
        return Field()
    if (
        len(exprs) == 1
        and exprs[0].literal_type == "string"
        and isinstance(exprs[0].literal_value, str)
    ):
        return Field("literal", exprs[0].literal_value, tuple(exprs))
    if len(exprs) > 1:
        return Field("ambiguous", None, tuple(exprs))
    return Field("expression", None, tuple(exprs))


def _fold_bool(exprs: list[Expr]) -> Field[bool]:
    if not exprs:
        return Field()
    if (
        len(exprs) == 1
        and exprs[0].literal_type == "boolean"
        and isinstance(exprs[0].literal_value, bool)
    ):
        return Field("literal", exprs[0].literal_value, tuple(exprs))
    if len(exprs) > 1:
        return Field("ambiguous", None, tuple(exprs))
    return Field("expression", None, tuple(exprs))


def _fold_expr(exprs: list[Expr], parameter: str) -> Field[Expr]:
    if not exprs:
        return Field()
    if len(exprs) > 1:
        return Field("ambiguous", None, tuple(exprs))
    if _manager_value(exprs[0], parameter):
        return Field("literal", exprs[0], tuple(exprs))
    return Field("expression", None, tuple(exprs))


def _string_value(exprs: list[Expr]) -> str:
    field = _fold_string(exprs)
    return field.value if field.presence == "literal" and isinstance(field.value, str) else ""


def _manager_value(expression: Expr, parameter: str) -> bool:
    """Литерал либо параметр процедуры правила, а не произвольный идентификатор."""
    if expression.literal_type == "string":
        return True
    return (
        expression.literal_type is None
        and len(expression.reference_parts) == 1
        and expression.reference_parts[0].casefold() == parameter.casefold()
    )


def _is_opaque_expr(tokens: tuple[Token, ...]) -> bool:
    return any(token.kind == "identifier" and token.folded in _OPAQUE_CALLS for token in tokens)


def _new_structure(statement: Statement) -> str | None:
    assign = _assignment(statement)
    if assign is None:
        return None
    variable, right = assign
    if "." in variable or not variable:
        return None
    if (
        len(right) == 4
        and right[0].folded == "новый"
        and right[1].folded == "структура"
        and right[2].value == "("
        and right[3].value == ")"
    ):
        return variable
    if len(right) == 2 and right[0].folded == "новый" and right[1].folded == "структура":
        return variable
    return None


def _insert(statement: Statement, structure: str | None) -> tuple[str, tuple[Token, ...]] | None:
    if not structure:
        return None
    call = _call(statement.tokens)
    if (
        call is None
        or call[0].casefold() != f"{structure.casefold()}.вставить"
        or len(call[1]) != 2
    ):
        return None
    key = call[1][0]
    if len(key) != 1 or key[0].kind != "string":
        return None
    return key[0].value, call[1][1]


def _is_column_guard(statement: Statement, collection: str) -> bool:
    tokens = statement.tokens
    return (
        len(tokens) == 12
        and tokens[0].folded == "если"
        and tokens[1].folded == collection.casefold()
        and tokens[2].value == "."
        and tokens[3].folded == "колонки"
        and tokens[4].value == "."
        and tokens[5].folded == "найти"
        and tokens[6].value == "("
        and tokens[7].kind == "string"
        and tokens[7].value == "Идентификатор"
        and tokens[8].value == ")"
        and tokens[9].value == "="
        and tokens[10].folded == "неопределено"
        and tokens[11].folded == "тогда"
    )


def _is_column_add(statement: Statement, collection: str) -> bool:
    tokens = _bare(statement.tokens)
    return (
        len(tokens) == 8
        and tokens[0].folded == collection.casefold()
        and tokens[1].value == "."
        and tokens[2].folded == "колонки"
        and tokens[3].value == "."
        and tokens[4].folded == "добавить"
        and tokens[5].value == "("
        and tokens[6].kind == "string"
        and tokens[6].value == "Идентификатор"
        and tokens[7].value == ")"
    )


def _rule_call(statement: Statement, params: list[str]) -> str | None:
    call = _call(statement.tokens)
    if call is None or "." in call[0] or not call[0].casefold().startswith("добавитьпро_"):
        return None
    if len(call[1]) != len(params):
        return None
    got: list[str] = []
    for arg in call[1]:
        if len(arg) != 1 or arg[0].kind != "identifier":
            return None
        got.append(arg[0].value)
    if tuple(item.casefold() for item in got) != tuple(item.casefold() for item in params):
        return None
    return call[0]


def _identifier_compare(statement: Statement) -> str | None:
    tokens = statement.tokens
    if statement.head not in ("если", "иначеесли") or len(tokens) != 7:
        return None
    if (
        tokens[1].folded == "про"
        and tokens[2].value == "."
        and tokens[3].folded == "идентификатор"
        and tokens[4].value == "="
        and tokens[5].kind == "string"
        and tokens[6].folded == "тогда"
    ):
        return tokens[5].value
    return None
