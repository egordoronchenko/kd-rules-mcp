"""Два прохода чтения менеджера: декларации, затем связи и роли методов."""

import hashlib
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from . import forms
from .coverage import build_coverage
from .errors import EdFormatError, EdReadError, EdResourceLimitError
from .lexer import Lexed, Statement, Token, lex, normalized, split_arguments, tokenize
from .model import (
    Classification,
    Conversion,
    Diagnostic,
    DispatcherCase,
    EdDocument,
    Entity,
    Expr,
    Field,
    FormalParameter,
    Guard,
    HandlerBinding,
    ObjectRule,
    Parameter,
    ParseStatus,
    PredefinedRule,
    ProcessingRule,
    PropertyGroup,
    PropertyRule,
    Routine,
    RuleRef,
    RuleUse,
    SearchSet,
    SourceFile,
    SourceSpan,
    UnknownFragment,
    ValueMapping,
    VersionMention,
)

MAX_BYTES = 32 * 1024 * 1024
MAX_LINES = 1_000_000


def read_manager(path: str | Path) -> EdDocument:
    """Читает один UTF-8 файл, не обнаруживая соседние зависимости."""
    path = Path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except OSError as error:
        raise EdReadError("Файл менеджера недоступен") from error
    if len(raw) > MAX_BYTES:
        raise EdResourceLimitError("Размер менеджера превышает 32 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise EdReadError("Ожидался UTF-8 файл менеджера") from error
    return _read(text, "module", str(path), raw.startswith(b"\xef\xbb\xbf"), raw)


def read_manager_text(
    text: str, *, file_id: str = "module", path: str | Path = "<memory>"
) -> EdDocument:
    """Создаёт неизменяемый снимок строки без обращения к файловой системе."""
    try:
        raw = text.encode("utf-8")
    except UnicodeError as error:
        raise EdReadError("Некорректный Unicode в тексте менеджера") from error
    return _read(text.removeprefix("\ufeff"), file_id, str(path), text.startswith("\ufeff"), raw)


def _read(text: str, file_id: str, path: str, bom: bool, raw: bytes) -> EdDocument:
    if len(raw) > MAX_BYTES:
        raise EdResourceLimitError("Размер менеджера превышает 32 MiB")
    if not file_id:
        raise ValueError("Пустой идентификатор файла")
    lines = text.splitlines(keepends=True)
    if len(lines) > MAX_LINES:
        raise EdResourceLimitError("Менеджер превышает миллион строк")
    if not text.strip():
        raise EdFormatError("Пустой менеджер")
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
    return "".join(t.value for t in tokens)


def _assignment(statement: Statement) -> tuple[str, tuple[Token, ...]] | None:
    tokens = _bare(statement.tokens)
    for i, token in enumerate(tokens):
        if token.value == "=" and token.kind == "symbol":
            if all(t.kind == "identifier" or t.value == "." for t in tokens[:i]):
                return _name(tokens[:i]), tokens[i + 1 :]
            break
    return None


def _call(tokens: tuple[Token, ...]) -> tuple[str, tuple[tuple[Token, ...], ...]] | None:
    tokens = _bare(tokens)
    if not tokens or tokens[-1].value != ")":
        return None
    for i, token in enumerate(tokens):
        if token.value == "(" and token.kind == "symbol":
            if not tokens[:i] or not all(
                t.kind == "identifier" or t.value == "." for t in tokens[:i]
            ):
                return None
            depth = 0
            for index in range(i, len(tokens)):
                current = tokens[index]
                if current.kind == "symbol":
                    depth += (current.value == "(") - (current.value == ")")
                    if depth == 0 and index != len(tokens) - 1:
                        return None
            return _name(tokens[:i]), split_arguments(tokens[i + 1 : -1])
    return None


@dataclass
class _RoutineBlock:
    routine: Routine
    header: Statement
    body: tuple[Statement, ...]


class _Reader:
    def __init__(self, source: SourceFile, lexical: Lexed):
        self.source = source
        self.lexical = lexical
        self.unknown: list[UnknownFragment] = []
        self.diagnostics: list[Diagnostic] = []
        self.marks: list[tuple[SourceSpan, Classification]] = []
        self.guards: list[Guard] = []
        self.contexts: dict[int, tuple[str, ...]] = {}
        self.bindings: list[HandlerBinding] = []
        self.uses: list[RuleUse] = []
        self.cases: list[DispatcherCase] = []
        self.version: int | None = None

    def common(self, kind: str, name: str, item: Statement | Entity | SourceSpan) -> dict[str, Any]:
        span = item if isinstance(item, SourceSpan) else item.span
        return {
            "entity_id": f"{span.file_id}:{kind}:{span.char_start}",
            "kind": kind,
            "name": name,
            "span": span,
            "raw_text": self.source.text[span.char_start : span.char_end],
            "regions": getattr(item, "regions", ()),
            "tag_ids": getattr(item, "tag_ids", ()),
            "guards": self.contexts.get(span.char_start, ()),
        }

    def diagnostic(self, code: str, item: Statement | Entity | SourceSpan) -> None:
        common = self.common("diagnostic", code, item)
        common["entity_id"] += ":" + code
        if any(d.entity_id == common["entity_id"] for d in self.diagnostics):
            return
        self.diagnostics.append(
            Diagnostic(
                **common,
                code=code,
                severity="warning",
                message=code,
            )
        )

    def unknown_block(
        self, item: Statement | Entity, reason: str, owner: str | None = None
    ) -> None:
        self.unknown.append(
            UnknownFragment(
                **self.common("unknown", reason, item),
                owner_id=owner,
                reason=reason,
                status=ParseStatus.PARTIAL,
            )
        )
        self.marks.append((item.span, Classification.UNKNOWN))

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
            t.kind == ("identifier" if i % 2 == 0 else "symbol") and (i % 2 == 0 or t.value == ".")
            for i, t in enumerate(tokens)
        ):
            parts = tuple(t.value for t in tokens[::2])
        return Expr(raw, span, kind, value, parts, self.contexts.get(fallback.char_start, ()))

    def read(self) -> EdDocument:
        blocks = self.routine_blocks()
        if not any(
            b.routine.name.casefold() in {n.casefold() for n in forms.ENTRYPOINTS} for b in blocks
        ):
            raise EdFormatError("Не найдены точки заполнения менеджера")
        self.read_guards()
        for block in blocks:
            block.routine = replace(
                block.routine, guards=self.contexts.get(block.header.span.char_start, ())
            )
        for code, span in self.lexical.warnings:
            self.diagnostic(code, span)
        for block in blocks:
            if block.routine.name.casefold() == forms.VERSION_ROUTINE.casefold():
                returns = [st for st in block.body if st.head == "возврат"]
                if len(returns) == 1:
                    value = self.expr(_bare(returns[0].tokens)[1:], returns[0].span)
                    if value.literal_type == "string" and str(value.literal_value).isdigit():
                        self.version = int(str(value.literal_value))
        if self.version not in forms.MANAGER_VERSIONS:
            self.diagnostic("unsupported_manager_version", blocks[0].routine)
        pko: list[ObjectRule] = []
        pod: list[ProcessingRule] = []
        pkpd: list[PredefinedRule] = []
        parameters: list[Parameter] = []
        routines: list[Routine] = []
        conversion_events: list[HandlerBinding] = []
        for block in blocks:
            routine = block.routine
            roles = set(routine.roles)
            lower = routine.name.casefold()
            if lower.startswith(("добавитьпко_", "добавитьпод_")):
                form = (
                    "pod"
                    if lower.startswith("добавитьпод_")
                    else ("pko_v3" if self.version == 3 else "pko_old")
                )
                expected = tuple(p.casefold() for p in forms.RULE_PARAMETERS[form])
                actual_params = tuple((p.name or "").casefold() for p in routine.parameters)
                if actual_params != expected or routine.routine_kind != "procedure":
                    self.unknown_block(
                        block.header, "unsupported_rule_signature", routine.entity_id
                    )
            if lower.startswith("добавитьпко_"):
                pko.append(self.object_rule(block))
            elif lower.startswith("добавитьпод_"):
                pod.append(self.processing_rule(block))
            elif lower == "заполнитьправилаконвертациипредопределенныхданных":
                pkpd.extend(self.predefined_rules(block))
            elif lower == "заполнитьпараметрыконвертации":
                parameters.extend(self.parameters(block))
            elif "dispatcher" in roles:
                self.read_dispatcher(block)
            elif lower in {name.casefold() for name in forms.ENTRYPOINTS}:
                self.read_entrypoint(block)
            elif "event" in roles or "algorithm" in roles or "handler" in roles:
                self.marks.append((routine.body_span, Classification.OPAQUE_CODE))
            elif "support" in roles:
                self.marks.append((routine.body_span, Classification.OPAQUE_CODE))
                if lower in ("добавитьпкс", "добавитьпктч"):
                    actual = normalized(
                        tuple(t for t in tokenize(routine.raw_text) if t.kind != "comment")
                    )
                    variants = [
                        normalized(tokenize(text))
                        for text in forms.helper_forms(routine.name, self.version)
                    ]
                    if actual not in variants:
                        self.diagnostic("helper_semantics_unverified", routine)
            else:
                self.unknown_block(routine, "unknown_routine")
            if "event" in roles:
                binding = HandlerBinding(
                    **self.common("binding", routine.name, routine),
                    owner_id="conversion",
                    event=routine.name,
                    target_name=routine.name,
                )
                conversion_events.append(binding)
                self.bindings.append(binding)
            routines.append(routine)
        # Второй проход не зависит от порядка объявления вызываемых методов.
        by_name: dict[str, list[Routine]] = defaultdict(list)
        for routine in routines:
            by_name[routine.name.casefold()].append(routine)
        resolved: dict[str, HandlerBinding] = {}
        callback_ids: set[str] = set()
        for binding in self.bindings:
            matches = by_name.get(binding.target_name.casefold(), [])
            routed = [
                case
                for case in self.cases
                if case.literal_name.casefold() == binding.target_name.casefold()
            ]
            if not matches and len(routed) == 1:
                matches = by_name.get(routed[0].target.raw.casefold(), [])
            state = "resolved" if len(matches) == 1 else "ambiguous" if matches else "missing"
            target_id = matches[0].entity_id if len(matches) == 1 else None
            # Отсутствие и неоднозначность ветки — проверка связности, не дефект чтения.
            resolved[binding.entity_id] = replace(binding, target_id=target_id, resolution=state)
            if target_id:
                callback_ids.add(target_id)
        # Непрефиксный обработчик тоже становится известным после разрешения ссылки.
        for case in self.cases:
            matches = by_name.get(case.target.raw.casefold(), [])
            if len(matches) == 1:
                callback_ids.add(matches[0].entity_id)
        for routine in routines:
            if routine.entity_id in callback_ids and not routine.roles:
                self.unknown = [
                    u
                    for u in self.unknown
                    if not (u.reason == "unknown_routine" and u.span == routine.span)
                ]
                self.marks.append((routine.span, Classification.DECLARATIVE))
                self.marks.append((routine.body_span, Classification.OPAQUE_CODE))
        routines = [
            replace(r, roles=r.roles | {"callback", "handler"})
            if r.entity_id in callback_ids and not r.roles
            else replace(r, roles=r.roles | {"callback"})
            if r.entity_id in callback_ids
            else r
            for r in routines
        ]
        pko = [replace(r, events=tuple(resolved[b.entity_id] for b in r.events)) for r in pko]
        pod = [replace(r, events=tuple(resolved[b.entity_id] for b in r.events)) for r in pod]
        pko_by_name: dict[str, list[ObjectRule]] = defaultdict(list)
        for object_rule in pko:
            pko_by_name[object_rule.name.casefold()].append(object_rule)
        for index, processing_rule in enumerate(pod):
            refs: list[RuleRef] = []
            for ref in processing_rule.used_pko:
                matches_pko = pko_by_name.get(ref.name.casefold(), [])
                refs.append(
                    replace(
                        ref,
                        target_id=matches_pko[0].entity_id if len(matches_pko) == 1 else None,
                        resolution="resolved"
                        if len(matches_pko) == 1
                        else "ambiguous"
                        if matches_pko
                        else "missing",
                    )
                )
            pod[index] = replace(processing_rule, used_pko=tuple(refs))
        rule_names: dict[str, list[Entity]] = defaultdict(list)
        for rule in (*pko, *pod):
            rule_names[rule.procedure_name.casefold()].append(rule)
        uses: list[RuleUse] = []
        for use in self.uses:
            targets = rule_names.get(use.target_name.casefold(), [])
            uses.append(replace(use, rule_id=targets[0].entity_id if len(targets) == 1 else None))
        mentions = self.version_mentions()
        whole = self.source.span(0, len(self.source.text))
        # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:951.
        title = generated_at = None
        prefix = "// Менеджер обмена через универсальный формат ("
        for token in self.lexical.tokens:
            if token.kind != "comment":
                break
            if token.value.startswith(prefix) and token.value.endswith(")"):
                title, separator, generated_at = token.value[len(prefix) : -1].rpartition(" от ")
                if not separator:
                    title, generated_at = token.value[len(prefix) : -1], None
                break
        conversion = Conversion(
            **self.common("conversion", "", whole),
            title=title,
            generated_at_raw=generated_at,
            events=tuple(resolved[b.entity_id] for b in conversion_events),
            entrypoints=tuple(
                b.routine.entity_id
                for b in blocks
                if b.routine.name.casefold() in {s.casefold() for s in forms.ENTRYPOINTS}
            ),
            format_version_mentions=tuple(mentions),
        )
        # Unknown имеет приоритет над охватывающими диапазонами сущностей.
        marks = [*self.marks, *((u.span, Classification.UNKNOWN) for u in self.unknown)]
        spans = [r.span for r in routines if r.roles]
        coverage = build_coverage(self.source, self.lexical.tokens, marks, spans)
        status = ParseStatus.PARTIAL if self.unknown or self.diagnostics else ParseStatus.COMPLETE
        problems = [e.span for e in (*self.unknown, *self.diagnostics)]

        def entity_status(entity: Entity) -> ParseStatus:
            return (
                ParseStatus.PARTIAL
                if any(
                    entity.span.char_start <= span.char_start < entity.span.char_end
                    for span in problems
                )
                else ParseStatus.COMPLETE
            )

        routines = [replace(r, status=entity_status(r)) for r in routines]
        pko = [replace(r, status=entity_status(r)) for r in pko]
        pod = [replace(r, status=entity_status(r)) for r in pod]
        pkpd = [replace(r, status=entity_status(r)) for r in pkpd]
        conversion = replace(conversion, status=status)
        return EdDocument(
            (self.source,),
            conversion,
            self.version,
            tuple(routines),
            tuple(pko),
            tuple(pod),
            tuple(pkpd),
            tuple(parameters),
            tuple(self.guards),
            tuple(uses),
            tuple(self.cases),
            tuple(self.unknown),
            tuple(self.diagnostics),
            coverage,
            status,
            tags=self.lexical.tags,
        )

    def routine_blocks(self) -> list[_RoutineBlock]:
        blocks: list[_RoutineBlock] = []
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
                routine = self.make_routine(header, statement)
                blocks.append(_RoutineBlock(routine, header, tuple(body)))
                self.marks.append((routine.span, Classification.DECLARATIVE))
                header = None
            elif header is not None:
                body.append(statement)
            elif statement.tokens[0].kind == "directive":
                self.marks.append((statement.span, Classification.DECLARATIVE))
            else:
                self.unknown_block(statement, "module_statement")
        if header is not None:
            raise EdFormatError("Незакрытый метод")
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
        name = tokens[1].value
        lower = name.casefold()
        roles: set[str] = set()
        if "алгоритмы" in {region.casefold() for region in header.regions}:
            roles.add("algorithm")
        event = forms.event_name(name)
        if event:
            signatures = {
                tuple(n.casefold() for n in signature)
                for signature in forms.EVENT_SIGNATURES[event]
            }
            actual = tuple((p.name or "").casefold() for p in params)
            expected = "функция" if event == "ВыборкаДанных" else "процедура"
            if actual in signatures and header.head == expected:
                roles.add("handler")
            else:
                self.diagnostic("handler_signature_unverified", header)
        if lower in {n.casefold() for n in forms.CONVERSION_EVENTS}:
            roles.add("event")
        if lower in {n.casefold() for n in forms.DISPATCHERS}:
            roles.add("dispatcher")
        if lower.startswith(("добавитьпко_", "добавитьпод_")):
            roles.add("rule")
        if lower in {n.casefold() for n in forms.ENTRYPOINTS} | {
            forms.VERSION_ROUTINE.casefold(),
            "добавитьпкс",
            "добавитьпктч",
            "версияформатачислом",
            "условиеприменениявыполняется",
            "подключаемый_идентификатормодуля",
        }:
            roles.add("support")
        span = self.source.span(header.span.char_start, end.span.char_end)
        return Routine(
            **{
                **self.common("routine", name, header),
                "span": span,
                "raw_text": self.source.text[span.char_start : span.char_end],
            },
            routine_kind="function" if header.head == "функция" else "procedure",
            parameters_raw=self.source.text[tokens[2].end : tokens[close].start],
            parameters=tuple(params),
            exported=exported,
            roles=frozenset(roles),
            body_span=self.source.span(header.span.char_end, end.span.char_start),
        )

    def read_guards(self) -> None:
        stack: list[int] = []
        for st in self.lexical.statements:
            directive = st.tokens[0].kind == "directive"
            word = st.raw_text.split()[0].casefold() if directive else st.head
            is_start = word in ("если", "#если")
            is_branch = word in ("иначеесли", "иначе", "#иначеесли", "#иначе")
            is_end = word in ("конецесли", "#конецесли")
            if is_branch or is_end:
                if not stack:
                    raise EdFormatError("Ветка без начала условия")
                old = stack.pop()
                guard = self.guards[old]
                span = self.source.span(guard.span.char_start, st.span.char_start)
                self.guards[old] = replace(
                    guard, span=span, raw_text=self.source.text[span.char_start : span.char_end]
                )
            if is_start or is_branch:
                expression = (
                    self.expr(st.tokens[1:-1], st.span)
                    if not directive
                    else Expr(st.raw_text, st.span)
                )
                direction = None
                if not directive:
                    items = st.tokens
                    if (
                        len(items) >= 5
                        and items[1].folded == "направлениеобмена"
                        and (items[2].value == "=" and items[3].kind == "string")
                    ):
                        direction = {"Отправка": "send", "Получение": "receive"}.get(items[3].value)
                kind = "preprocessor" if directive else "direction" if direction else "opaque"
                if "толькозаголовки" in expression.raw.casefold():
                    kind = "headers_only"
                if "условиеприменениявыполняется" in expression.raw.casefold():
                    kind = "condition_name"
                guard = Guard(
                    **self.common("guard", "", st),
                    expression_raw=expression.raw,
                    branch="if" if is_start else "else" if word.endswith("иначе") else "elseif",
                    parent_id=self.guards[stack[-1]].entity_id if stack else None,
                    known_direction=direction,
                    guard_kind=kind,
                )
                # kind сущности остаётся guard, подвид хранится отдельно.
                guard = replace(guard, name=kind)
                stack.append(len(self.guards))
                self.guards.append(guard)
            self.contexts[st.span.char_start] = tuple(self.guards[n].entity_id for n in stack)
        if stack:
            raise EdFormatError("Незакрытое условие BSL")

    def structural(self, st: Statement) -> bool:
        return st.tokens[0].kind == "directive" or st.head in (
            "если",
            "иначеесли",
            "иначе",
            "конецесли",
        )

    def binding(self, owner: str, event: str, expression: Expr, st: Statement) -> HandlerBinding:
        binding = HandlerBinding(
            **self.common("binding", event, st),
            owner_id=owner,
            event=event,
            target_name=str(expression.literal_value),
        )
        self.bindings.append(binding)
        return binding

    def read_fields(
        self, statements: tuple[Statement, ...], prefix: str, fields: dict[str, str]
    ) -> tuple[dict[str, Field[Any]], set[int]]:
        result: dict[str, Field[Any]] = {}
        consumed: set[int] = set()
        lookup = {name.casefold(): name for name in fields}
        for st in statements:
            assignment = _assignment(st)
            if assignment is None:
                continue
            left, right = assignment
            base, _, name = left.rpartition(".")
            if base.casefold() != prefix.casefold() or name.casefold() not in lookup:
                continue
            name = lookup[name.casefold()]
            expression = self.expr(right, st.span)
            if fields[name] != "expression" and expression.literal_type != fields[name]:
                self.unknown_block(st, "nonliteral_field")
                old = result.get(name, Field())
                result[name] = Field(
                    "ambiguous" if old.assignments else "expression",
                    None,
                    (*old.assignments, expression),
                )
                consumed.add(st.span.char_start)
                continue
            old = result.get(name, Field())
            assignments = (*old.assignments, expression)
            value = expression if fields[name] == "expression" else expression.literal_value
            result[name] = Field(
                "ambiguous"
                if old.assignments
                else "expression"
                if fields[name] == "expression"
                else "literal",
                None if old.assignments else value,
                assignments,
            )
            consumed.add(st.span.char_start)
            if old.assignments:
                self.diagnostic("repeated_assignment", st)
        return result, consumed

    def object_rule(self, block: _RoutineBlock) -> ObjectRule:
        routine = block.routine
        values, consumed = self.read_fields(block.body, "ПравилоКонвертации", forms.PKO_FIELDS)
        name = cast(str | None, values.get("ИмяПКО", Field()).value)
        key = name if name is not None else routine.name.removeprefix("ДобавитьПКО_")
        common = self.common("pko", key, routine)
        owner = common["entity_id"]
        events: list[HandlerBinding] = []
        properties: list[PropertyRule] = []
        groups: list[PropertyGroup] = []
        searches: list[SearchSet] = []
        extensions: list[str] = []
        group: PropertyGroup | None = None
        for st in block.body:
            if st.span.char_start in consumed:
                assignment = _assignment(st)
                if assignment:
                    event = assignment[0].split(".")[-1]
                    canonical = next(
                        (e for e in forms.EVENT_SIGNATURES if e.casefold() == event.casefold()),
                        None,
                    )
                    value = self.expr(assignment[1], st.span)
                    if canonical and value.literal_type == "string" and value.literal_value:
                        events.append(self.binding(owner, canonical, value, st))
                continue
            assignment = _assignment(st)
            call = _call(assignment[1] if assignment else st.tokens)
            if call and call[0].casefold() in ("добавитьпкс", "добавитьпктч"):
                is_group = call[0].casefold() == "добавитьпктч"
                if not self.valid_property(call[1], is_group):
                    self.unknown_block(st, "unsupported_property_arguments", owner)
                    continue
                args = [self.expr(part, st.span) for part in call[1]]
                strings = [
                    str(arg.literal_value) if arg.literal_type == "string" else "" for arg in args
                ]
                strings += [""] * (7 - len(strings))
                if is_group:
                    if not assignment or assignment[0].casefold() != "свойстватч":
                        self.unknown_block(st, "unknown_property_parent", owner)
                        continue
                    group = PropertyGroup(
                        **self.common("pktch", strings[2] or strings[1], st),
                        owner_id=owner,
                        configuration_property=strings[1],
                        format_property=strings[2],
                        namespace=strings[3],
                        condition_name=strings[4],
                    )
                    groups.append(group)
                else:
                    parent = _name(call[1][0]).casefold()
                    in_group = parent == "свойстватч"
                    if in_group and (
                        group is None or group.guards != self.contexts.get(st.span.char_start, ())
                    ):
                        self.unknown_block(st, "ambiguous_property_parent", owner)
                        continue
                    prop = PropertyRule(
                        **self.common("pks", strings[2] or strings[1], st),
                        owner_id=owner,
                        group_id=group.entity_id if in_group and group else None,
                        configuration_property=strings[1],
                        format_property=strings[2],
                        algorithm_flag=int(args[3].literal_value or 0) if len(args) > 3 else 0,
                        conversion_rule=strings[4],
                        namespace=strings[5],
                        condition_name=strings[6],
                        argument_presence=tuple(bool(part) for part in call[1]),
                        raw_arguments=tuple(args),
                    )
                    if in_group and group:
                        group = replace(group, properties=(*group.properties, prop))
                        groups[-1] = group
                    else:
                        properties.append(prop)
                continue
            if (
                call
                and call[0].casefold() == "правилоконвертации.поляпоиска.добавить"
                and len(call[1]) == 1
                and len(call[1][0]) == 1
                and call[1][0][0].kind == "string"
            ):
                raw = call[1][0][0].value
                searches.append(
                    SearchSet(
                        **self.common("search", str(len(searches) + 1), st),
                        owner_id=owner,
                        value_raw=raw,
                        fields=tuple(p.strip() for p in raw.split(",")),
                        ordinal=len(searches) + 1,
                    )
                )
                if any(not part for part in searches[-1].fields):
                    self.diagnostic("empty_search_field", st)
                continue
            if (
                call
                and call[0]
                .casefold()
                .endswith(".инициализироватьрасширениеправилаконвертацииобъекта")
                and len(call[1]) == 2
                and len(call[1][1]) == 1
                and call[1][1][0].kind == "string"
            ):
                extensions.append(call[1][1][0].value)
                continue
            if self.structural(st) or self.is_initialization(st, "pko"):
                continue
            self.unknown_block(st, "unsupported_pko_statement", owner)
        identification = values.get("ВариантИдентификации", Field())
        if identification.value and identification.value not in forms.IDENTIFICATIONS:
            self.diagnostic("unknown_identification", routine)
        if name is None:
            self.diagnostic("missing_rule_name", routine)
        return ObjectRule(
            **common,
            procedure_name=routine.name,
            declared_name=name,
            configuration_object=values.get("ОбъектДанных", Field()),
            format_object=values.get("ОбъектФормата", Field()),
            group_flag=values.get("ПравилоДляГруппыСправочника", Field()),
            identification=identification,
            events=tuple(events),
            properties=tuple(properties),
            groups=tuple(groups),
            search_sets=tuple(searches),
            extensions=tuple(extensions),
        )

    def valid_property(self, args: tuple[tuple[Token, ...], ...], group: bool) -> bool:
        maximum = (5 if group else 7) if self.version == 3 else (4 if group else 6)
        if not 3 <= len(args) <= maximum:
            return False
        parents = ("правилоконвертации",) if group else ("свойствашапки", "свойстватч")
        if _name(args[0]).casefold() not in parents:
            return False
        for i, part in enumerate(args[1:], 1):
            if i <= 2 and not part:
                return False
            if not part:
                continue
            if not group and i == 3:
                if len(part) != 1 or part[0].kind != "number" or part[0].value not in ("0", "1"):
                    return False
            elif len(part) != 1 or part[0].kind != "string":
                return False
        return True

    def is_initialization(self, st: Statement, kind: str) -> bool:
        tokens = normalized(st.tokens)
        expected = {
            "pko": (
                "ПравилоКонвертации = ОбменДаннымиXDTOСервер."
                "ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);",
                "СвойстваШапки = ПравилоКонвертации.Свойства;",
                "Возврат;",
            ),
            "pod": ("ПравилоОбработки = ПравилаОбработкиДанных.Добавить();",),
            "pkpd": ("ПравилоКонвертации = ПравилаКонвертации.Добавить();",),
        }
        # Сопоставление одного уже выделенного оператора, а не исходного файла.
        joined = "".join(tokens)
        return any(joined == value.replace(" ", "").casefold() for value in expected[kind])

    def processing_rule(self, block: _RoutineBlock) -> ProcessingRule:
        values, consumed = self.read_fields(block.body, "ПравилоОбработки", forms.POD_FIELDS)
        routine = block.routine
        name = cast(str | None, values.get("Имя", Field()).value)
        key = name if name is not None else routine.name.removeprefix("ДобавитьПОД_")
        common = self.common("pod", key, routine)
        events: list[HandlerBinding] = []
        used: list[RuleRef] = []
        for st in block.body:
            if st.span.char_start in consumed:
                assignment = _assignment(st)
                if assignment:
                    event = assignment[0].split(".")[-1]
                    canonical = next(
                        (
                            e
                            for e in ("ПриОбработке", "ВыборкаДанных")
                            if e.casefold() == event.casefold()
                        ),
                        None,
                    )
                    value = self.expr(assignment[1], st.span)
                    if canonical and value.literal_type == "string" and value.literal_value:
                        events.append(self.binding(common["entity_id"], canonical, value, st))
                continue
            call = _call(st.tokens)
            if (
                call
                and call[0].casefold() == "правилообработки.используемыепко.добавить"
                and len(call[1]) == 1
                and len(call[1][0]) == 1
                and call[1][0][0].kind == "string"
            ):
                used.append(RuleRef(call[1][0][0].value, st.span))
                continue
            if not self.structural(st) and not self.is_initialization(st, "pod"):
                self.unknown_block(st, "unsupported_pod_statement", common["entity_id"])
        return ProcessingRule(
            **common,
            procedure_name=routine.name,
            declared_name=name,
            configuration_selection=values.get("ОбъектВыборкиМетаданные", Field()),
            format_selection=values.get("ОбъектВыборкиФормат", Field()),
            clear_data=values.get("ОчисткаДанных", Field()),
            events=tuple(events),
            used_pko=tuple(used),
        )

    def predefined_rules(self, block: _RoutineBlock) -> list[PredefinedRule]:
        result: list[PredefinedRule] = []
        starts = [i for i, st in enumerate(block.body) if self.is_initialization(st, "pkpd")]
        if not starts:
            # Процедура без единого ПКПД (пустая или с одной преамбулой) — допустимая форма.
            return result
        ends = [*starts[1:], len(block.body)]
        for start, end in zip(starts, ends, strict=True):
            statements = block.body[start:end]
            values, consumed = self.read_fields(statements, "ПравилоКонвертации", forms.PKPD_FIELDS)
            name = cast(str | None, values.get("ИмяПКПД", Field()).value)
            span = self.source.span(statements[0].span.char_start, statements[-1].span.char_end)
            key = name if name is not None else f"@{span.line_start}"
            common = self.common("pkpd", key, statements[0])
            common.update(span=span, raw_text=self.source.text[span.char_start : span.char_end])
            mappings: list[ValueMapping] = []
            for st in statements:
                if (
                    st.span.char_start in consumed
                    or self.structural(st)
                    or self.is_initialization(st, "pkpd")
                ):
                    continue
                call = _call(st.tokens)
                if (
                    call
                    and call[0].casefold()
                    in (
                        "значениядляотправки.вставить",
                        "значениядляполучения.вставить",
                    )
                    and len(call[1]) == 2
                ):
                    sending = "отправки" in call[0].casefold()
                    first, second = (self.expr(arg, st.span) for arg in call[1])
                    mappings.append(
                        ValueMapping(
                            **self.common("value", str(len(mappings) + 1), st),
                            configuration_value=first if sending else second,
                            format_value=second if sending else first,
                            direction="send" if sending else "receive",
                            ordinal=len(mappings) + 1,
                        )
                    )
                    continue
                assignment = _assignment(st)
                if assignment:
                    left, right = assignment
                    norm = normalized(right)
                    if left.casefold() in (
                        "значениядляотправки",
                        "значениядляполучения",
                    ) and norm == ("новый", "соответствие"):
                        continue
                    if left.casefold() in (
                        "правилоконвертации.конвертациизначенийприотправке",
                        "правилоконвертации.конвертациизначенийприполучении",
                    ) and _name(right).casefold() in (
                        "значениядляотправки",
                        "значениядляполучения",
                    ):
                        continue
                self.unknown_block(st, "unsupported_pkpd_statement", common["entity_id"])
            result.append(
                PredefinedRule(
                    **common,
                    declared_name=name,
                    configuration_type=values.get("ТипДанных", Field()),
                    format_type=values.get("ТипXDTO", Field()),
                    mappings=tuple(mappings),
                )
            )
        for st in block.body[: starts[0] if starts else len(block.body)]:
            if not self.structural(st):
                self.unknown_block(st, "pkpd_preamble", block.routine.entity_id)
        return result

    def parameters(self, block: _RoutineBlock) -> list[Parameter]:
        result: list[Parameter] = []
        for st in block.body:
            call = _call(st.tokens)
            if call and call[0].casefold() == "параметрыконвертации.вставить" and len(call[1]) == 1:
                name = self.expr(call[1][0], st.span)
                if name.literal_type == "string":
                    result.append(
                        Parameter(**self.common("parameter", str(name.literal_value), st))
                    )
                    continue
            if not self.structural(st):
                self.unknown_block(st, "unsupported_parameter_statement", block.routine.entity_id)
        return result

    def read_entrypoint(self, block: _RoutineBlock) -> None:
        directions = {g.entity_id: g for g in self.guards}
        for st in block.body:
            call = _call(st.tokens)
            if (
                call
                and "." in call[0]
                and call[0].split(".")[-1].casefold() in {n.casefold() for n in forms.ENTRYPOINTS}
            ):
                self.diagnostic("split_module_required", st)
                self.unknown_block(st, "external_rule_call", block.routine.entity_id)
                continue
            if call and call[0].casefold().split(".")[-1].startswith(
                ("добавитьпко_", "добавитьпод_")
            ):
                if "." in call[0]:
                    self.diagnostic("split_module_required", st)
                    self.unknown_block(st, "external_rule_call", block.routine.entity_id)
                    continue
                is_pod = call[0].casefold().startswith("добавитьпод_")
                form = "pod" if is_pod else "pko_v3" if self.version == 3 else "pko_old"
                actual_args = tuple(_name(arg).casefold() for arg in call[1])
                expected_args = tuple(p.casefold() for p in forms.RULE_PARAMETERS[form])
                if actual_args != expected_args:
                    self.unknown_block(st, "unsupported_rule_arguments", block.routine.entity_id)
                    continue
                guards = self.contexts.get(st.span.char_start, ())
                known = [directions[g].known_direction for g in guards]
                direction = known[-1] if known and all(known) else None if guards else "both"
                self.uses.append(
                    RuleUse(
                        **self.common("use", call[0], st),
                        rule_id=None,
                        target_name=call[0],
                        direction=direction,
                    )
                )
                continue
            if self.structural(st):
                continue
            assignment = _assignment(st)
            if (
                assignment
                and assignment[0].casefold()
                in (
                    "направлениеобмена",
                    "версияформатаобмена",
                )
                and _name(assignment[1]).casefold().startswith("компонентыобмена.")
            ):
                continue
            if normalized(st.tokens) == normalized(tokenize(forms.POD_COLUMN_STATEMENT)):
                continue
            self.unknown_block(st, "unsupported_entrypoint_statement", block.routine.entity_id)

    def read_dispatcher(self, block: _RoutineBlock) -> None:
        guard_lookup = {g.entity_id: g for g in self.guards}
        for st in block.body:
            tokens = _bare(st.tokens)
            returns = st.head == "возврат"
            call = _call(tokens[1:] if returns else tokens)
            if call:
                guard_ids = self.contexts.get(st.span.char_start, ())
                literal = None
                for guard_id in guard_ids:
                    guard = guard_lookup[guard_id]
                    # Выражение уже выделено лексером; литерал извлекается из заголовка ветки.
                    header = next(
                        (s for s in block.body if s.span.char_start == guard.span.char_start), None
                    )
                    if (
                        header
                        and len(header.tokens) >= 5
                        and header.tokens[1].folded in ("имяпроцедуры", "имяфункции")
                        and header.tokens[2].value == "="
                        and header.tokens[3].kind == "string"
                    ):
                        literal = header.tokens[3].value
                if literal is not None:
                    opening = next(i for i, t in enumerate(tokens) if t.value == "(")
                    target_tokens = tokens[1:opening] if returns else tokens[:opening]
                    self.cases.append(
                        DispatcherCase(
                            **self.common("case", literal, st),
                            dispatcher_id=block.routine.entity_id,
                            literal_name=literal,
                            target=self.expr(target_tokens, st.span),
                            arguments=tuple(self.expr(arg, st.span) for arg in call[1]),
                            returns=returns,
                        )
                    )
                    continue
            if not self.structural(st):
                self.unknown_block(st, "unsupported_dispatcher_statement", block.routine.entity_id)

    def version_mentions(self) -> list[VersionMention]:
        result: list[VersionMention] = []
        for st in self.lexical.statements:
            if not any(t.kind == "identifier" and "версияформата" in t.folded for t in st.tokens):
                continue
            for token in st.tokens:
                if (
                    token.kind == "string"
                    and "." in token.value
                    and all(part.isdigit() for part in token.value.split("."))
                ):
                    result.append(
                        VersionMention(
                            **self.common(
                                "version", token.value, self.source.span(token.start, token.end)
                            ),
                            value=token.value,
                            context=st.raw_text,
                        )
                    )
        return result
