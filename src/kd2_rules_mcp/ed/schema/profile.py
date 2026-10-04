"""Профиль и трёхзначная применимость деклараций, без исполнения BSL."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType

from kd2_rules_mcp.ed import model as ed
from kd2_rules_mcp.ed.lexer import Token, tokenize

from .model import EdSchema, QName, ResolvedProperty, SchemaProperty, SchemaType
from .resolver import effective_properties, resolve_property

Truth = bool | None


def conjunction(values: tuple[Truth, ...]) -> Truth:
    """Неизвестное И ложь = ложь."""
    return False if False in values else None if None in values else True


def disjunction(values: tuple[Truth, ...]) -> Truth:
    """Неизвестное ИЛИ истина = истина."""
    return True if True in values else None if None in values else False


@lru_cache(maxsize=8192)
def _condition_tokens(raw: str) -> tuple[Token, ...]:
    """Лексемы условия без комментариев. Один и тот же текст разбирается много раз."""
    return tuple(token for token in tokenize(raw) if token.kind not in ("comment", "directive"))


def evaluate_condition(
    raw: str, version: str | None, direction: str, numeric_helper_verified: bool = False
) -> Truth:
    """Только сравнения версии, направления и логические операции. XDTO:4153."""
    tokens = _condition_tokens(raw)
    position = 0

    def take(value: str) -> bool:
        nonlocal position
        if position < len(tokens) and tokens[position].folded == value:
            position += 1
            return True
        return False

    def operand() -> int | str | None:
        nonlocal position
        if take("версияформатачислом"):
            if not numeric_helper_verified:
                raise ValueError
            if not take("("):
                raise ValueError
            result = operand()
            if not take(")") or not isinstance(result, str):
                raise ValueError
            if result in ("", "1.0.beta"):
                return 0
            components = re.fullmatch(r"(\d+)\.(\d+)", result)
            if components is None:
                raise ValueError
            # Число получается только из сверенного helper, не из строки версии.
            return int(components[1]) * 10000 + int(components[2]) * 100
        if take("компонентыобмена"):
            if not take(".") or not take("версияформатаобмена"):
                raise ValueError
            return version
        if take("направлениеобмена"):
            return "Отправка" if direction == "send" else "Получение"
        if position < len(tokens) and tokens[position].kind == "string":
            value = tokens[position].value
            position += 1
            return value
        raise ValueError

    def atom() -> Truth:
        nonlocal position
        if take("не"):
            value = atom()
            return None if value is None else not value
        if take("("):
            value = expression()
            if not take(")"):
                raise ValueError
            return value
        start = position
        try:
            left = operand()
            if position >= len(tokens):
                raise ValueError
            operator = tokens[position].value
            position += 1
            right = operand()
            if left is None or right is None or type(left) is not type(right):
                return None
            if isinstance(left, str) and isinstance(right, str):
                if operator not in ("=", "<>"):
                    return None
                comparison = (left > right) - (left < right)
            elif isinstance(left, int) and isinstance(right, int):
                comparison = (left > right) - (left < right)
            else:
                return None
            return {
                "=": comparison == 0,
                "<>": comparison != 0,
                "<": comparison < 0,
                ">": comparison > 0,
                "<=": comparison <= 0,
                ">=": comparison >= 0,
            }.get(operator)
        except ValueError:
            position = start
            depth = 0
            while position < len(tokens):
                value = tokens[position].folded
                if depth == 0 and value in ("и", "или", ")"):
                    break
                depth += (value == "(") - (value == ")")
                position += 1
            return None

    def term() -> Truth:
        result = atom()
        while take("и"):
            result = conjunction((result, atom()))
        return result

    def expression() -> Truth:
        result = term()
        while take("или"):
            result = disjunction((result, term()))
        return result

    try:
        result = expression()
        return result if position == len(tokens) else None
    except (ValueError, TypeError):
        return None


def numeric_helper_verified(document: ed.EdDocument) -> bool:
    """Сверенная форма helper, а не произвольная функция с тем же именем.

    Множители 10000/100 воспроизводят сравнение двух числовых компонент.
    Текст исключения не влияет на допустимые версии из двух разрядов.
    Эталон: reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/
    ШаблоныТекстовМодулей/Ext/Template.txt:201–222.
    """
    expected = """Функция ВерсияФорматаЧислом(СтрокаВерсии)
    Если Не ЗначениеЗаполнено(СтрокаВерсии) Или СтрокаВерсии = "1.0.beta" Тогда
        Возврат 0;
    КонецЕсли;
    ВерсияФорматаЧислом = 0;
    РазрядыВерсии = СтрРазделить(СтрокаВерсии, ".");
    Если РазрядыВерсии.Количество() <> 2 Тогда
        ВызватьИсключение "Ошибка";
    КонецЕсли;
    МножительРазряда = 10000;
    Для ИндексРазрядаОбратный = 0 По 1 Цикл
        ВерсияФорматаЧислом = ВерсияФорматаЧислом +
            Число(РазрядыВерсии[ИндексРазрядаОбратный])*МножительРазряда;
        МножительРазряда = МножительРазряда / 100;
    КонецЦикла;
    Возврат ВерсияФорматаЧислом;
    КонецФункции"""

    def signature(raw: str) -> tuple[tuple[str, str], ...]:
        result = []
        throwing = False
        for token in tokenize(raw):
            if token.kind in ("comment", "directive"):
                continue
            if token.folded == "экспорт":
                continue
            if token.folded == "вызватьисключение":
                throwing = True
            elif throwing and token.value != ";":
                continue
            elif throwing:
                throwing = False
            result.append((token.kind, token.folded if token.kind == "identifier" else token.value))
        return tuple(result)

    candidates = [r for r in document.routines if r.name.casefold() == "версияформатачислом"]
    return len(candidates) == 1 and signature(candidates[0].raw_text) == signature(expected)


@dataclass(frozen=True, slots=True)
class ValidationProfile:
    """Одна версия и активные URI; индексы принадлежат только снимку."""

    schema: EdSchema | None
    format_version: str | None
    direction: str
    active_namespaces: tuple[str, ...]
    effective: Mapping[str, tuple[tuple[SchemaProperty, tuple[QName, ...]], ...]]
    properties: Mapping[str, SchemaProperty]

    @classmethod
    def build(
        cls,
        schema: EdSchema | None,
        version: str | None = None,
        direction: str = "both",
    ) -> "ValidationProfile":
        if direction not in ("send", "receive", "both"):
            raise ValueError("Направление: send, receive или both")
        return cls(
            schema,
            version,
            direction,
            (schema.base_namespace, *schema.extension_namespaces) if schema else (),
            MappingProxyType(
                {t.id: effective_properties(schema, t) for t in schema.by_id.values()}
                if schema
                else {}
            ),
            MappingProxyType(
                {p.id: p for t in schema.by_id.values() for p in t.properties} if schema else {}
            ),
        )

    @property
    def directions(self) -> tuple[str, ...]:
        return ("send", "receive") if self.direction == "both" else (self.direction,)

    def find_type(
        self, name: str, namespace: str = "", dependency: bool = False
    ) -> tuple[SchemaType | None, str]:
        if self.schema is None:
            return None, "schema_unavailable"
        match = re.fullmatch(r"\{([^}]*)\}(.+)", name)
        if match:
            namespace, name = match.groups()
        if namespace and not dependency and namespace not in self.active_namespaces:
            return None, "inactive_namespace"
        uris = (
            (namespace,)
            if namespace
            else tuple(p.namespace for p in self.schema.packages)
            if dependency
            else self.active_namespaces
        )
        found = [
            self.schema.types[q] for uri in uris if (q := QName(uri, name)) in self.schema.types
        ]
        if len(found) > 1:
            return None, "ambiguous"
        if not found:
            return None, "missing"
        return found[0], "resolved" if found[0].status == "complete" else "partial_schema"

    def resolve(self, owner: SchemaType, path: str, table: str | None = None) -> ResolvedProperty:
        assert self.schema is not None
        return resolve_property(
            self.schema,
            owner.id,
            path,
            table=table,
            effective_index=self.effective,
            property_index=self.properties,
        )

    def owner_type(
        self, rule: ed.ObjectRule, direction: str, applicability: "Applicability"
    ) -> tuple[SchemaType | None, str]:
        """База, затем расширения самого ПКО по порядку (XDTO:4146–4156, 4838–4853)."""
        field = rule.format_object
        state = applicability.field(field, direction)
        if state is None:
            return None, "opaque_condition"
        if state is False:
            return None, "not_applicable"
        if field.presence not in ("literal", "absent"):
            return None, "dynamic_format_type"
        if not field.value:
            return None, "empty_format_side"
        if self.schema is None:
            return None, "schema_unavailable"
        for namespace in (self.schema.base_namespace, *rule.extensions):
            if namespace not in self.active_namespaces:
                return None, "partial_schema"
            typ = self.schema.types.get(QName(namespace, field.value))
            if typ is not None:
                return typ, "resolved" if typ.status == "complete" else "partial_schema"
        return None, "missing"


@dataclass(frozen=True, slots=True)
class Applicability:
    """Условия веток учитывают все предшествующие elseif одной цепочки."""

    profile: ValidationProfile
    guard_values: Mapping[tuple[str, str], Truth]
    parents: Mapping[str, ed.ObjectRule]
    use_values: Mapping[tuple[str, str], Truth]

    @classmethod
    def build(cls, document: ed.EdDocument, profile: ValidationProfile) -> "Applicability":
        previous: dict[str, tuple[str, ...]] = {}
        chains: dict[str | None, list[str]] = {}
        for guard in document.guards:
            chain = chains.setdefault(guard.parent_id, [])
            if guard.branch == "if":
                chain.clear()
            previous[guard.entity_id] = tuple(chain)
            chain.append(guard.entity_id)
        uses: dict[str, list[ed.RuleUse]] = {}
        for use in document.rule_uses:
            if use.rule_id:
                uses.setdefault(use.rule_id, []).append(use)
        verified = numeric_helper_verified(document)
        expressions = {
            (g.entity_id, direction): evaluate_condition(
                g.expression_raw, profile.format_version, direction, verified
            )
            for g in document.guards
            for direction in profile.directions
            if g.branch != "else"
        }
        guard_values: dict[tuple[str, str], Truth] = {}
        for guard in document.guards:
            for direction in profile.directions:
                value = expressions.get((guard.entity_id, direction), True)
                earlier = disjunction(
                    tuple(
                        expressions.get((ident, direction)) for ident in previous[guard.entity_id]
                    )
                )
                guard_values[(guard.entity_id, direction)] = conjunction(
                    (
                        value,
                        None if earlier is None else not earlier,
                        guard_values.get((guard.parent_id, direction)) if guard.parent_id else True,
                    )
                )
        use_values = {
            (ident, direction): disjunction(
                tuple(
                    conjunction(
                        (
                            True
                            if use.guards
                            else use.direction in (direction, "both")
                            if use.direction
                            else None,
                            *(guard_values.get((g, direction)) for g in use.guards),
                        )
                    )
                    for use in paths
                )
            )
            for ident, paths in uses.items()
            for direction in profile.directions
        }
        return cls(
            profile,
            MappingProxyType(guard_values),
            MappingProxyType({g.entity_id: r for r in document.pko for g in r.groups}),
            MappingProxyType(use_values),
        )

    def guard(self, ident: str, direction: str) -> Truth:
        return self.guard_values.get((ident, direction))

    def evaluate(self, entity: ed.Entity, direction: str, parent: ed.Entity | None = None) -> Truth:
        conditions: list[Truth] = [self.guard(g, direction) for g in entity.guards]
        if getattr(entity, "condition_name", ""):
            conditions.append(None)
        namespace = getattr(entity, "namespace", "")
        if namespace and self.profile.schema and namespace not in self.profile.active_namespaces:
            conditions.append(False)
        if isinstance(entity, ed.ValueMapping) and entity.direction != direction:
            conditions.append(False)
        if parent is not None:
            grandparent = self.parents.get(parent.entity_id)
            conditions.append(self.evaluate(parent, direction, grandparent))
        if isinstance(entity, (ed.ObjectRule, ed.ProcessingRule)):
            conditions.append(self.use_values.get((entity.entity_id, direction)))
        return conjunction(tuple(conditions))

    def field(self, field: ed.Field, direction: str) -> Truth:
        if field.presence == "ambiguous":
            return None
        return (
            disjunction(
                tuple(
                    conjunction(tuple(self.guard(g, direction) for g in a.guards))
                    for a in field.assignments
                )
            )
            if field.assignments
            else True
        )
