"""Перевод снимка читателя в авторский оригинал без исполнения и потери BSL."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import replace
from pathlib import PureWindowsPath
from typing import Any, Literal, cast

from . import model as reader
from .address import build_addresses
from .canonical import model_addresses
from .forms import ENTRYPOINTS, POD_COLUMN_STATEMENT, VERSION_ROUTINE
from .lexer import lex, tokenize
from .refs import build_references
from .writer_model import (
    CodeUnit,
    Direction,
    DispatcherCase,
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
    logical_id,
    partition_report,
    text_hash,
    validate_model,
)


def import_expression(expression: reader.Expr | None) -> Value:
    if expression is None or not expression.raw:
        return Value()
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
        self.directions: dict[str, tuple] = {}
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
        guards = {g.entity_id: g for g in document.guards}
        while pending:
            parent = guards[pending.pop()].parent_id
            if parent and parent not in self.guard_ids:
                self.guard_ids.add(parent)
                pending.append(parent)

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

    def rule_unsafe(self, rule: reader.ObjectRule | reader.ProcessingRule) -> bool:
        if self.unsafe(rule):
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
            if field and any(set(e.guards) - set(rule.guards) for e in field.assignments):
                return True
        if isinstance(rule, reader.ObjectRule) and any(
            set(s.guards) - set(rule.guards) for s in rule.search_sets
        ):
            return True
        for statement in self.rule_statements(rule):
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
        for binding in bindings:
            self.retain(binding, self.ids.get(binding.owner_id))
        return tuple(
            Event(
                **self.common(e, n, "retained"),
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
            if kind == "direct"
            and not in_group
            and not unsafe
            and not self.unsafe(item)
            and not self.unverified_pks
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
        result = Property(
            **self.common(item, ordinal, state),
            configuration_property=item.configuration_property,
            format_property=item.format_property,
            property_kind=cast(Literal["direct", "reference", "algorithm"], kind),
            algorithm_flag=item.algorithm_flag,
            conversion=conversion,
            namespace=item.namespace,
            condition_name=item.condition_name,
            argument_presence=item.argument_presence,
            argument_values=tuple(import_expression(e) for e in item.raw_arguments[1:]),
        )
        if state != "editable":
            self.retain(item, owner)
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
            groups.append(
                PropertyGroup(
                    **self.common(group, n, "retained"),
                    configuration_property=group.configuration_property,
                    format_property=group.format_property,
                    namespace=group.namespace,
                    condition_name=group.condition_name,
                    properties=tuple(
                        self.property(p, i, self.ids[group.entity_id], in_group=True)
                        for i, p in enumerate(group.properties, 1)
                    ),
                )
            )
            self.retain(group, key)
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
        """Разбиение строится сверху вниз; вложенные представления не выводятся."""
        doc = self.document
        members = {m.logical_id: m for m in model.members()}
        mapped = {e.logical_id: e for e in model.source_map}
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
            return SourceSlice(file_id, left, right, left == 0 and self.sources[file_id].bom)

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
                    raise ValueError("Перекрытие элементов раскладки")
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
            uses = {u.span.char_start for u in uses_by_routine[routine.entity_id]}
            scaffold = {s.span.char_start for group in column_groups(routine) for s in group}
            rows = [
                s
                for s in self.rule_statements(routine)
                if routine.body_span.char_start <= s.span.char_start < routine.body_span.char_end
            ]
            return all(
                s.span.char_start in uses
                or s.span.char_start in scaffold
                or s.head == "конецесли"
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
            "объектвыборкиформат": "format_selection",
            "очисткаданных": "clear_data",
            "используемыепко": "used_pko",
            "инициализироватьрасширениеправилаконвертацииобъекта": "extensions",
        }

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
            columns = column_groups(routine) if rule is None else ()
            for column in columns:
                start, end = line_range(
                    file_id, column[0].span.char_start, column[-1].span.char_end
                )
                candidates.append((start, end, text_leaf(file_id, start, end, key, "scaffold")))
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
                            text_leaf(file_id, start, end, key, "binding", child.logical_id),
                        )
                    )
                    consumed.add(entry.char_start)
            else:
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
                if element.entity_id and element.source:
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
                is_entry = routine.name.casefold() in {
                    "заполнитьправилаконвертацииобъектов",
                    "заполнитьправилаобработкиданных",
                }
                if (rule and rule.state == "editable" and not has_else) or (
                    is_entry and exact_entry(routine) and not has_else
                ):
                    candidates.append(editable_body(routine, rule))
                else:
                    left, right = line_range(
                        source.file_id, routine.span.char_start, routine.span.char_end
                    )
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
                    if self.ids[routine.entity_id] in {u.logical_id for u in model.code_units}:
                        element = replace(
                            element, kind="entity", entity_id=self.ids[routine.entity_id]
                        )
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
            replace(c, owner_id=parents[c.logical_id]) if c.kind == "conditional" else c
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
            if leaf_id and not isinstance(member, CodeUnit | Event):
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
        self.entries = [replace(e, state=states.get(e.logical_id, e.state)) for e in self.entries]
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
        pkpd = []
        for n, rule in enumerate(doc.pkpd, 1):
            pkpd.append(
                PredefinedRule(
                    **self.common(rule, n, "retained"),
                    configuration_type=import_field(rule.configuration_type),
                    format_type=import_field(rule.format_type),
                    mappings=tuple(
                        ValueMapping(
                            **self.common(v, i, "retained"),
                            configuration_value=import_expression(v.configuration_value),
                            format_value=import_expression(v.format_value),
                            direction=cast(Direction, v.direction),
                        )
                        for i, v in enumerate(rule.mappings, 1)
                    ),
                )
            )
            self.retain(rule)
        parameters = []
        for n, item in enumerate(doc.parameters, 1):
            parameters.append(
                Parameter(
                    **self.common(item, n, "retained"),
                    default=import_expression(item.default),
                    default_source=cast(Literal["implicit", "explicit"], item.default_source),
                )
            )
            self.retain(item)
        references = build_references(doc)
        code = []
        excluded = {s.casefold() for s in ENTRYPOINTS} | {VERSION_ROUTINE.casefold()}
        for routine in doc.routines:
            if "rule" in routine.roles or routine.name.casefold() in excluded:
                if routine.name.casefold() in (
                    "заполнитьправилаконвертациипредопределенныхданных",
                    "заполнитьпараметрыконвертации",
                ):
                    self.retain(routine, reason="retained_declaration_routine")
                continue
            source = self.sources[routine.span.file_id]
            body = source.text[routine.body_span.char_start : routine.body_span.char_end]
            empty_event = "event" in routine.roles and not any(
                t.kind != "comment" for t in tokenize(body)
            )
            state: ImportState = "editable" if empty_event else "retained"
            deps = []
            for ref in references.entries:
                if ref.owner_id != routine.entity_id:
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
            code.append(
                CodeUnit(
                    **self.common(routine, len(code) + 1, state),
                    signature=import_signature(routine),
                    body=body,
                    sha256=text_hash(body),
                    roles=tuple(sorted(routine.roles)),
                    dependencies=tuple(deps),
                    file_id=routine.body_span.file_id,
                    body_start=routine.body_span.char_start,
                    body_end=routine.body_span.char_end,
                    helper_verified=(not self.unverified_pks)
                    if routine.name.casefold() == "добавитьпкс"
                    else None,
                )
            )
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
                    u, n, "editable" if u.direction and not self.unsafe(u) else "retained"
                ),
                rule=self.reference("rule", u.rule_id, u.target_name),
                direction=cast(Direction | None, u.direction),
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
                **self.common(c, n, "retained"),
                dispatcher=self.reference("code_unit", c.dispatcher_id, ""),
                target=self.reference(
                    "code_unit",
                    next((r.entity_id for r in doc.routines if r.name == c.target.raw), None),
                    c.target.raw,
                ),
                arguments=tuple(import_expression(a) for a in c.arguments),
                returns=c.returns,
            )
            for n, c in enumerate(doc.dispatcher_cases, 1)
        )
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
            tuple(self.entries), tuple((d.code, self.address(d)) for d in doc.diagnostics)
        )
        source = doc.files[0]
        model = ManagerModel(
            project_id=self.project_id,
            header=Header(
                manager_name=name,
                interface_version=doc.manager_version,
                title=Value("string", doc.conversion.title)
                if doc.conversion.title is not None
                else Value(),
                generated_at=Value("string", doc.conversion.generated_at_raw)
                if doc.conversion.generated_at_raw is not None
                else Value(),
                text_style=TextStyle(
                    newline=cast(Literal["\n", "\r\n", "mixed"], source.newline), bom=source.bom
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
        )
        model = replace(model, import_report=report).with_revision()
        validate_model(model)
        return model, report
