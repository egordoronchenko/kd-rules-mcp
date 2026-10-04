"""Производный индекс литеральных ссылок в телах обработчиков EnterpriseData.

Сканер смотрит только токены лексора: строки и комментарии кодом не считаются.
Имена не вычисляются. Вычисляемое выражение в известной позиции даёт запись с
`name=None`. Направление берётся из RuleUse владельцев привязки; в проекции оно
распространяется на проверенных помощников и доказанные прежние вызовы.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from .forms import EVENT_INVOCATIONS
from .layer_model import EffectiveDocument
from .lexer import Token, lex, split_arguments, tokenize
from .model import DispatcherCase, EdDocument, Expr, Routine, SourceFile, SourceSpan

ReferenceKind = Literal[
    "pko_lookup",
    "instruction_rule",
    "pod_use",
    "additional_key",
    "parameter",
    "format_property",
    "received_property",
]
ReferenceForm = Literal[
    "lookup",
    "find",
    "constructor",
    "insert",
    "assignment",
    "member",
    "call",
    "index",
]
ReferenceAccess = Literal["read", "write"]
ReferenceDirection = Literal["send", "receive", "both"]

KINDS: tuple[ReferenceKind, ...] = (
    "pko_lookup",
    "instruction_rule",
    "pod_use",
    "additional_key",
    "parameter",
    "format_property",
    "received_property",
)
_INDEXED_ROLES = frozenset({"handler", "handler_helper", "algorithm", "event", "callback"})
# Наиболее специальный корень побеждает, чтобы не учитывать цепочку дважды.
_FAMILIES: tuple[tuple[str, ReferenceKind], ...] = (
    ("дополнительныесвойства", "additional_key"),
    ("параметрыконвертации", "parameter"),
    ("использованиепко", "pod_use"),
    ("данныеxdto", "format_property"),
    ("полученныеданные", "received_property"),
)
_CALL_ACCESS = {
    "вставить": "write",
    "удалить": "write",
    "свойство": "read",
    "получить": "read",
}
_PROCEDURE_DISPATCHER = "выполнитьпроцедурумодуляменеджера"
_DEFERRED_EVENT = "ПослеЗагрузкиВсехДанных"
# Писатель кладёт в отложенный вызов только эти пути от Параметры.
ALLOWED_DEFERRED: frozenset[str] = frozenset(
    {
        "параметры.объект",
        "параметры.компонентыобмена",
        "параметры.объектмодифицирован",
        "параметры.компонентыобмена.параметрыконвертации",
    }
)


@dataclass(frozen=True, slots=True)
class EdReference:
    """Одно литеральное или вычисляемое упоминание в теле метода."""

    kind: ReferenceKind
    name: str | None
    span: SourceSpan
    owner_id: str
    form: ReferenceForm
    access: ReferenceAccess
    direction: ReferenceDirection | None
    raw: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class ReferenceIndex:
    """Ссылки одного снимка. Пустая строка — известное имя, `None` — не разобрано."""

    entries: tuple[EdReference, ...]
    known_by_kind: Mapping[ReferenceKind, int]
    unparsed_by_kind: Mapping[ReferenceKind, int]
    known_by_owner: Mapping[str, int]
    unparsed_by_owner: Mapping[str, int]
    deferred_argument_unparsed: int

    @property
    def known(self) -> int:
        return sum(self.known_by_kind.values())

    @property
    def unparsed(self) -> int:
        return sum(self.unparsed_by_kind.values())


@dataclass(slots=True)
class _Draft:
    kind: ReferenceKind
    name: str | None
    span: SourceSpan
    owner_id: str
    form: ReferenceForm
    access: ReferenceAccess
    direction: ReferenceDirection | None
    raw: str


def norm_name(value: str) -> str:
    """Сравнение имён правил: крайние пробелы и регистр не различаются."""
    return value.strip().casefold()


def classify_deferred_argument(argument: Expr) -> Literal["ok", "invalid", "unparsed"]:
    """Простой путь от Параметры либо допустим, либо предупреждение; иное — не разобрано."""
    parts = argument.reference_parts
    if parts and parts[0].casefold() == "параметры" and len(parts) >= 2:
        folded = "".join(token.value for token in tokenize(argument.raw)).casefold()
        if folded in ALLOWED_DEFERRED:
            return "ok"
        return "invalid"
    return "unparsed"


def deferred_cases(document: EdDocument) -> tuple[DispatcherCase, ...]:
    """Ветки процедурного диспетчера, привязанные к ПослеЗагрузкиВсехДанных."""
    names = {
        norm_name(binding.target_name)
        for rule in document.pko
        for binding in rule.events
        if binding.event == _DEFERRED_EVENT and binding.target_name.strip()
    }
    if not names:
        return ()
    dispatcher_ids = {
        routine.entity_id
        for routine in document.routines
        if routine.name.casefold() == _PROCEDURE_DISPATCHER
    }
    return tuple(
        case
        for case in document.dispatcher_cases
        if case.dispatcher_id in dispatcher_ids and norm_name(case.literal_name) in names
    )


def build_references(document: EdDocument) -> ReferenceIndex:
    """Индексирует тела handler, algorithm, event и callback.

    Диспетчер и служебные методы пропускает.
    """
    directions = _directions(document)
    sources = {source.file_id: source for source in document.files}
    starts: dict[str, set[int]] = {}
    drafts: list[_Draft] = []
    aliases: dict[str, dict[str, str]] = defaultdict(dict)
    routines = {r.entity_id: r for r in document.routines}
    for rule in (*document.pko, *document.pod):
        for binding in rule.events:
            routine = routines.get(binding.target_id or "")
            signature = EVENT_INVOCATIONS.get(binding.event)
            if (
                routine
                and signature
                and document.files
                and routine.span.file_id != document.files[0].file_id
            ):
                case = next(
                    (
                        case
                        for case in document.dispatcher_cases
                        if case.literal_name == binding.target_name
                        and case.span.file_id == routine.span.file_id
                    ),
                    None,
                )
                keys = (
                    tuple(arg.reference_parts[-1] for arg in case.arguments if arg.reference_parts)
                    if case
                    else ()
                )
                for formal, key in zip(routine.parameters, keys, strict=False):
                    if formal.name:
                        canonical = key.casefold()
                        if canonical == "объектобработки":
                            canonical = (
                                "данныеxdto"
                                if directions.get(routine.entity_id) == "receive"
                                else "данныеиб"
                            )
                        aliases[routine.entity_id][formal.name.casefold()] = canonical
    previous = document.previous_calls if isinstance(document, EffectiveDocument) else ()
    helpers = {
        (r.span.file_id, r.name.casefold()): r
        for r in document.routines
        if "handler_helper" in r.roles
    }
    changed = bool(helpers or previous)
    while changed:
        size = sum(len(values) for values in aliases.values())
        for call in previous:
            target = routines.get(call.target_id)
            signature = EVENT_INVOCATIONS.get(call.event)
            if (
                target
                and signature
                and document.files
                and target.span.file_id != document.files[0].file_id
            ):
                for formal, argument in zip(target.parameters, call.arguments, strict=False):
                    if formal.name:
                        key = argument.raw.casefold()
                        aliases[target.entity_id][formal.name.casefold()] = aliases[
                            call.routine_id
                        ].get(key, key)
        for routine in document.routines:
            if routine.span.file_id not in {file_id for file_id, _ in helpers}:
                continue
            tokens = tokenize(routine.raw_text)
            pairs = _bracket_pairs(tokens)
            for index, token in enumerate(tokens):
                target = helpers.get((routine.span.file_id, token.folded))
                if (
                    not target
                    or index + 1 not in pairs
                    or tokens[index + 1].value != "("
                    or (index and tokens[index - 1].value == ".")
                ):
                    continue
                arguments = split_arguments(tokens[index + 2 : pairs[index + 1]])
                for formal, arg in zip(target.parameters, arguments, strict=False):
                    if formal.name and len(arg) == 1:
                        canonical = aliases[routine.entity_id].get(arg[0].folded, arg[0].folded)
                        if canonical in {name for name, _ in _FAMILIES}:
                            aliases[target.entity_id][formal.name.casefold()] = canonical
        changed = sum(len(values) for values in aliases.values()) != size
    for routine in document.routines:
        if not routine.roles & _INDEXED_ROLES:
            continue
        source = sources[routine.span.file_id]
        if source.file_id not in starts:
            starts[source.file_id] = {
                statement.span.char_start for statement in lex(source).statements
            }
        drafts.extend(
            _scan_routine(
                source,
                routine,
                directions.get(routine.entity_id),
                starts[source.file_id],
                aliases.get(routine.entity_id, {}),
            )
        )
    drafts.sort(
        key=lambda item: (
            item.span.file_id,
            item.span.char_start,
            item.kind,
            item.form,
            item.raw,
        )
    )
    entries = tuple(
        EdReference(
            item.kind,
            item.name,
            item.span,
            item.owner_id,
            item.form,
            item.access,
            item.direction,
            item.raw,
            ordinal,
        )
        for ordinal, item in enumerate(drafts, 1)
    )
    known_by_kind = dict.fromkeys(KINDS, 0)
    unparsed_by_kind = dict.fromkeys(KINDS, 0)
    known_by_owner: dict[str, int] = {}
    unparsed_by_owner: dict[str, int] = {}
    for entry in entries:
        if entry.name is None:
            unparsed_by_kind[entry.kind] += 1
            unparsed_by_owner[entry.owner_id] = unparsed_by_owner.get(entry.owner_id, 0) + 1
        else:
            known_by_kind[entry.kind] += 1
            known_by_owner[entry.owner_id] = known_by_owner.get(entry.owner_id, 0) + 1
    deferred_unparsed = sum(
        classify_deferred_argument(argument) == "unparsed"
        for case in deferred_cases(document)
        for argument in case.arguments
    )
    return ReferenceIndex(
        entries,
        MappingProxyType(known_by_kind),
        MappingProxyType(unparsed_by_kind),
        MappingProxyType(known_by_owner),
        MappingProxyType(unparsed_by_owner),
        deferred_unparsed,
    )


def binding_owners(document: EdDocument) -> Mapping[str, tuple[str, ...]]:
    """Владельцы тел; вызовы распространяются только по проверенным связям проекции."""
    owners: dict[str, set[str]] = defaultdict(set)
    for rule in (*document.pko, *document.pod):
        for binding in rule.events:
            if binding.target_id:
                owners[binding.target_id].add(rule.entity_id)
    helpers = {
        (r.span.file_id, r.name.casefold()): r
        for r in document.routines
        if "handler_helper" in r.roles
    }
    previous = document.previous_calls if isinstance(document, EffectiveDocument) else ()
    helper_files = {file_id for file_id, _ in helpers}
    changed = bool(helpers or previous)
    while changed:
        changed = False
        for call in previous:
            before = len(owners[call.target_id])
            owners[call.target_id].update(owners.get(call.routine_id, ()))
            changed |= len(owners[call.target_id]) != before
        for routine in document.routines:
            if not owners.get(routine.entity_id) or routine.span.file_id not in helper_files:
                continue
            tokens = tokenize(routine.raw_text)
            for index, token in enumerate(tokens):
                target = helpers.get((routine.span.file_id, token.folded))
                if (
                    target
                    and index + 1 < len(tokens)
                    and tokens[index + 1].value == "("
                    and (not index or tokens[index - 1].value != ".")
                ):
                    before = len(owners[target.entity_id])
                    owners[target.entity_id].update(owners[routine.entity_id])
                    changed |= len(owners[target.entity_id]) != before
    return MappingProxyType({key: tuple(sorted(value)) for key, value in owners.items()})


def _directions(document: EdDocument) -> dict[str, ReferenceDirection]:
    """Объединение направлений RuleUse правил, к которым привязан метод."""
    owners = binding_owners(document)
    uses: dict[str, set[str]] = defaultdict(set)
    for use in document.rule_uses:
        if use.rule_id and use.direction == "both":
            uses[use.rule_id].update(("send", "receive"))
        elif use.rule_id and use.direction in ("send", "receive"):
            uses[use.rule_id].add(use.direction)
    result: dict[str, ReferenceDirection] = {}
    for routine_id, rule_ids in owners.items():
        found: set[str] = set()
        for rule_id in rule_ids:
            found.update(uses.get(rule_id, ()))
        if found == {"send", "receive"}:
            result[routine_id] = "both"
        elif found == {"send"}:
            result[routine_id] = "send"
        elif found == {"receive"}:
            result[routine_id] = "receive"
    return result


def _scan_routine(
    source: SourceFile,
    routine: Routine,
    direction: ReferenceDirection | None,
    statement_starts: set[int],
    aliases: Mapping[str, str] = MappingProxyType({}),
) -> list[_Draft]:
    base = routine.body_span.char_start
    tokens = tuple(
        token
        for token in tokenize(source.text[base : routine.body_span.char_end])
        if token.kind != "comment"
    )
    pairs = _bracket_pairs(tokens)
    found: list[_Draft] = []

    def add(
        kind: ReferenceKind,
        name: str | None,
        start: int,
        end: int,
        form: ReferenceForm,
        access: ReferenceAccess = "read",
    ) -> None:
        span = source.span(start, end)
        found.append(
            _Draft(
                kind,
                name,
                span,
                routine.entity_id,
                form,
                access,
                direction,
                source.text[span.char_start : span.char_end],
            )
        )

    def add_expression(
        kind: ReferenceKind,
        expression: tuple[Token, ...],
        form: ReferenceForm,
        access: ReferenceAccess,
        fallback: int,
    ) -> None:
        literal = _one_string(expression)
        if literal is not None:
            add(kind, literal.value, base + literal.start, base + literal.end, form, access)
            return
        if expression:
            add(
                kind,
                None,
                base + expression[0].start,
                base + expression[-1].end,
                form,
                access,
            )
            return
        add(kind, None, fallback, fallback, form, access)

    for index, token in enumerate(tokens):
        if token.kind != "identifier" or (index and tokens[index - 1].value == "."):
            continue
        identifiers = [token]
        cursor = index + 1
        while (
            cursor + 1 < len(tokens)
            and tokens[cursor].value == "."
            and tokens[cursor + 1].kind == "identifier"
        ):
            identifiers.append(tokens[cursor + 1])
            cursor += 2
        folded = [
            aliases.get(item.folded, item.folded) if i == 0 else item.folded
            for i, item in enumerate(identifiers)
        ]
        arguments = (
            split_arguments(tokens[cursor + 1 : pairs[cursor]])
            if cursor in pairs and tokens[cursor].value == "("
            else None
        )
        chain_start = base + identifiers[0].start
        if arguments is not None and folded == ["обменданнымиxdtoсервер", "пкопоимени"]:
            if len(arguments) >= 2:
                add_expression("pko_lookup", arguments[1], "lookup", "read", chain_start)
            else:
                add("pko_lookup", None, chain_start, chain_start, "lookup")
        if (
            arguments is not None
            and folded[-1] == "найти"
            and "правилаконвертацииобъектов" in folded
            and len(arguments) >= 2
            and _folded_string(arguments[1]) == "имяпко"
        ):
            add_expression("pko_lookup", arguments[0], "find", "read", chain_start)
        # Без аргумента-значения имени правила нет вовсе: это не вычисляемая ссылка.
        if (
            arguments is not None
            and folded[-1] == "вставить"
            and len(arguments) >= 2
            and _folded_string(arguments[0]) == "имяпко"
        ):
            add_expression("instruction_rule", arguments[1], "insert", "write", chain_start)
        if (
            arguments is not None
            and folded == ["структура"]
            and index
            and tokens[index - 1].folded == "новый"
        ):
            fields = _one_string(arguments[0]) if arguments else None
            if fields is not None:
                for number, field in enumerate(fields.value.split(","), 1):
                    if field.strip().casefold() != "имяпко":
                        continue
                    # Конструктор только со списком ключей значения не задаёт: имя правила
                    # появится отдельным присваиванием (форма assignment), здесь ссылки нет.
                    if number < len(arguments):
                        add_expression(
                            "instruction_rule",
                            arguments[number],
                            "constructor",
                            "write",
                            chain_start,
                        )
        if (
            folded[-1] == "имяпко"
            and cursor < len(tokens)
            and tokens[cursor].value == "="
            and chain_start in statement_starts
        ):
            end = next(
                (
                    position
                    for position in range(cursor + 1, len(tokens))
                    if tokens[position].value == ";"
                ),
                len(tokens),
            )
            add_expression(
                "instruction_rule",
                tokens[cursor + 1 : end],
                "assignment",
                "write",
                base + tokens[cursor].end,
            )
        _add_family(
            add,
            add_expression,
            identifiers,
            folded,
            cursor,
            tokens,
            pairs,
            arguments,
            chain_start,
            statement_starts,
            base,
        )
    return found


def _add_family(
    add,
    add_expression,
    identifiers: list[Token],
    folded: list[str],
    cursor: int,
    tokens: tuple[Token, ...],
    pairs: dict[int, int],
    arguments: tuple[tuple[Token, ...], ...] | None,
    chain_start: int,
    statement_starts: set[int],
    base: int,
) -> None:
    for root, kind in _FAMILIES:
        if root not in folded:
            continue
        position = folded.index(root)
        if position + 1 < len(identifiers):
            member = identifiers[position + 1]
            if position + 2 == len(identifiers) and arguments is not None:
                access = _CALL_ACCESS.get(member.folded)
                if access is not None:
                    if arguments:
                        add_expression(kind, arguments[0], "call", access, chain_start)
                    else:
                        add(kind, None, chain_start, chain_start, "call", access)
            else:
                writing = (
                    cursor < len(tokens)
                    and tokens[cursor].value == "="
                    and chain_start in statement_starts
                )
                add(
                    kind,
                    member.value,
                    base + member.start,
                    base + member.end,
                    "member",
                    "write" if writing else "read",
                )
        elif cursor in pairs and tokens[cursor].value == "[":
            inner = tokens[cursor + 1 : pairs[cursor]]
            closing = pairs[cursor]
            writing = closing + 1 < len(tokens) and tokens[closing + 1].value == "="
            add_expression(kind, inner, "index", "write" if writing else "read", chain_start)
        break


def _bracket_pairs(tokens: tuple[Token, ...]) -> dict[int, int]:
    pairs: dict[int, int] = {}
    stack: list[int] = []
    opener = {"(": ")", "[": "]"}
    for index, token in enumerate(tokens):
        if token.kind != "symbol":
            continue
        if token.value in opener:
            stack.append(index)
        elif token.value in (")", "]") and stack:
            start = stack.pop()
            if opener[tokens[start].value] == token.value:
                pairs[start] = index
    return pairs


def _one_string(tokens: tuple[Token, ...]) -> Token | None:
    if len(tokens) == 1 and tokens[0].kind == "string":
        return tokens[0]
    return None


def _folded_string(tokens: tuple[Token, ...]) -> str | None:
    literal = _one_string(tokens)
    if literal is None:
        return None
    return literal.value.strip().casefold()
