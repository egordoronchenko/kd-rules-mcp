"""Статическая сверка модулей источника и приёмника обмена через универсальный формат.

Источник читается в контексте отправки, приёмник — получения, в одной версии формата. Вывод
«по выгрузкам так»: что корреспондент объявил в базе, знает только живое чтение. Замечание —
только там, где расхождение доказано; обработчик, условие или неполное чтение, которые могут
закрыть расхождение, дают пропуск с причиной. `XDTO:N` — строки `ОбменДаннымиXDTOСервер` БСП 3.1.12.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from kd_rules_mcp.ed import model as ed
from kd_rules_mcp.validation.ed_chain import (
    NOT_APPLICABLE,
    OK,
    PARTIAL,
    SEARCH_BY_FIELDS,
    ChainView,
    LinkState,
    properties,
)
from kd_rules_mcp.validation.report import ValidationReport

SOURCE = "Источник"
TARGET = "Приемник"
_KEYS = "КлючевыеСвойства."
_OPTIONAL_PARENT = ("справочник.", "планвидовхарактеристик.")
# Обработчики приёмника, которые могут заполнить или вернуть значения до переноса в объект:
# конвертация — до поиска (XDTO:1869 → 1899), перед записью — до переноса (XDTO:1975, 1987–1988).
_RECEIVER_FILL = ("ПриКонвертацииДанныхXDTO", "ПередЗаписьюПолученныхДанных")
_RECEIVER_SEARCH = ("ПриКонвертацииДанныхXDTO", "АлгоритмПоиска")

ASSUMPTIONS = (
    "Корреспондент прислал свои настройки: без них база зеркалит свой состав, и пересечение "
    "полное (XDTO:2990–3013); что база знает на самом деле — live_format_support.",
    "Версия узла равна выбранной версии; пересечение исполнитель берёт по версии узла "
    "(XDTO:3878–3885).",
    "Активация и порядок расширений в базах не проверены.",
    "Обработчики не исполняются: расхождение, которое обработчик может закрыть общим кодом, — "
    "пропуск, а не замечание; обработчик ПОД может выключить ПКО (certain: false).",
)

Supplied = bool | None


def normalize(name: str) -> str:
    """Свойство формата в правиле пишут плоско или через `КлючевыеСвойства.` (XDTO:6746–6750)."""
    name = name.strip()
    return name[len(_KEYS) :] if name.startswith(_KEYS) else name


@dataclass(slots=True)
class _Supply:
    """Что источник отправляет для одного типа: доказанно, под вопросом, обработчиком."""

    header: dict[str, str] = field(default_factory=dict)
    maybe: set[str] = field(default_factory=set)
    tables: dict[str, set[str]] = field(default_factory=dict)
    maybe_tables: set[str] = field(default_factory=set)
    literals: set[str] = field(default_factory=set)
    open: bool = False

    def __call__(self, name: str) -> Supplied:
        if name in self.header or name in self.tables or name in self.literals:
            return True
        if name in self.maybe or name in self.maybe_tables or self.open:
            return None
        return False

    def column(self, table: str, name: str) -> Supplied:
        if name in self.tables.get(table, ()) or f"{table}.{name}" in self.literals:
            return True
        return None if self.open or table in self.maybe_tables else False


@dataclass(slots=True)
class _Sender:
    pods: list[ed.ProcessingRule] = field(default_factory=list)
    rules: list[ed.ObjectRule] = field(default_factory=list)
    state: LinkState = field(default_factory=lambda: LinkState(OK))


@dataclass(slots=True)
class _Receiver:
    pod: ed.ProcessingRule
    rules: list[tuple[ed.ObjectRule, str | None]] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    state: LinkState = field(default_factory=lambda: LinkState(OK))


@dataclass(slots=True)
class PairResult:
    report: ValidationReport
    objects: list[dict[str, Any]]
    coverage: Counter[str]
    version: str | None


def _key(view: ChainView, rule: ed.ObjectRule, prop: ed.PropertyRule) -> tuple[bool | None, str]:
    """Ключевое ли свойство по схеме и почему неизвестно: `schema_unavailable`, `missing`, …"""
    if prop.format_property.strip().startswith(_KEYS):
        return True, "resolved"
    if view.profile.schema is None:
        return None, "schema_unavailable"
    found = view.property_schema(rule, None, prop)
    if found.status == "resolved":
        return found.key, "resolved"
    if found.status == "owner_type_unavailable":
        return None, found.reason or found.status
    return None, found.status


def _rule_type(view: ChainView, rule: ed.ObjectRule) -> tuple[str | None, str]:
    """Тип формата ПКО; вне пакета версии ПКО удаляется (XDTO:4146–4158)."""
    typ, status = view.literal(rule.format_object)
    if typ is None:
        return None, status
    if view.profile.schema is not None and view.type_state(rule)[1] == "missing":
        return None, "type_missing"
    return typ, "resolved"


class _Checker:
    def __init__(self, source: ChainView, target: ChainView) -> None:
        self.source = source
        self.target = target
        self.report = ValidationReport()
        self.coverage: Counter[str] = Counter()

    @staticmethod
    def where(side: str, address: str) -> str:
        return f"{side}/{address}"

    def skip(self, check: str, reason: str, side: str, address: str) -> None:
        self.coverage[reason] += 1
        self.report.skip(check, f"{reason}: {self.where(side, address)}")

    # --- источник -------------------------------------------------------------------------

    def senders(self) -> tuple[dict[str, _Sender], set[str]]:
        """Типы на путях ПОД отправки (XDTO:8344, 794–799) и «умею отправить» (XDTO:9443–9463)."""
        view = self.source
        sent: dict[str, _Sender] = defaultdict(_Sender)
        groups: dict[str, list[ed.ProcessingRule]] = defaultdict(list)
        for pod in view.pods():
            if view.state(pod).status == NOT_APPLICABLE:
                continue
            name, status = view.pod_metadata(pod)
            if name is None:
                if pod.configuration_selection.presence == "absent":
                    # Через план обмена ПОД выбирается только по объекту метаданных (XDTO:8344).
                    self.skip("ed.pair", "pod_without_metadata", SOURCE, view.address(pod))
                    continue
                groups[f"?{pod.entity_id}"].append(pod)
                continue
            groups[name.casefold()].append(pod)
        unknown = [view.table_order(pods[0]) for key, pods in groups.items() if key.startswith("?")]
        for key, pods in groups.items():
            first = pods[0]
            state = view.state(first)
            if key.startswith("?"):
                state = state.worst(LinkState(PARTIAL, ("pod_metadata_unknown",)))
            elif any(order < view.table_order(first) for order in unknown):
                state = state.worst(LinkState(PARTIAL, ("shadowed_maybe",)))
            if view.order_unknown(pods):
                state = state.worst(LinkState(PARTIAL, ("layer_order_unknown",)))
            elif len(pods) > 1 and view.state(first).status != OK:
                state = state.worst(LinkState(PARTIAL, ("shadowed_maybe",)))
            if view.has_event(first, "ПриОбработке"):
                # Обработчик может выключить ПКО: уйдёт удаление (XDTO:805–814).
                state = state.worst(LinkState(PARTIAL, ("pod_handler_may_switch",)))
            # Второй ПОД того же объекта не используется (XDTO:8344); при неизвестном
            # порядке слоёв в расчёт идут все.
            chosen = pods if view.order_unknown(pods) or state.status != OK else [first]
            for pod in chosen:
                for ref in pod.used_pko:
                    rule, rule_state = view.pko_first(ref.name)
                    if rule is None:
                        continue
                    typ, status = _rule_type(view, rule)
                    if typ is None:
                        if status not in ("type_missing", "absent", "empty"):
                            self.skip(
                                "ed.pair", f"format_type_{status}", SOURCE, view.address(rule)
                            )
                        continue
                    row = sent[typ]
                    if pod not in row.pods:
                        row.pods.append(pod)
                    if rule not in row.rules:
                        row.rules.append(rule)
                    row.state = row.state.worst(state).worst(rule_state).worst(view.state(rule))
        known: set[str] = set()
        for rule in view.document.pko:
            if view.state(rule).status == NOT_APPLICABLE:
                continue
            typ, _ = _rule_type(view, rule)
            if typ:
                known.add(typ)
        return dict(sent), known

    def references(self, sent: dict[str, _Sender]) -> dict[str, list[tuple[str, ed.ObjectRule]]]:
        """Ссылки из ПКС объектов, которые источник отправляет: ПКО свойства и адрес ПКС.

        Ссылка уходит через ПКО свойства без пересечения (XDTO:1541–1567); ПКС на свойство вне
        типа схемы теряется (XDTO:933–954), ПКО вне пакета удалено (XDTO:4146–4158).
        """
        view = self.source
        result: dict[str, list[tuple[str, ed.ObjectRule]]] = defaultdict(list)
        for sender in sent.values():
            for rule in sender.rules:
                for group, prop in properties(rule):
                    if not prop.conversion_rule.strip() or not prop.format_property.strip():
                        continue
                    if view.state(prop, group or rule).status == NOT_APPLICABLE:
                        continue
                    if view.profile.schema is not None and (
                        view.property_schema(rule, group, prop).status == "missing"
                    ):
                        continue
                    target, _ = view.pko_first(prop.conversion_rule)
                    if target is None:
                        continue
                    typ, _ = _rule_type(view, target)
                    if typ:
                        result[typ].append((view.address(prop), target))
        return dict(result)

    def supply(self, rules: list[ed.ObjectRule], *, keys_only: bool = False) -> _Supply:
        """Свойства формата, которые отправляют ПКО; `keys_only` — только ключевые (ссылка)."""
        view = self.source
        result = _Supply()
        for rule in rules:
            state = view.state(rule)
            if state.status != OK and set(state.reasons) & {"reader_partial", "unknown_fragment"}:
                result.open = True
            if view.has_event(rule, "ПриОтправкеДанных"):
                result.open = True
            for group, prop in properties(rule):
                prop_state = view.state(prop, group or rule)
                if prop_state.status == NOT_APPLICABLE or not prop.format_property.strip():
                    continue
                name = normalize(prop.format_property)
                if keys_only:
                    if group is not None:
                        continue
                    key, _ = _key(view, rule, prop)
                    if key is False:
                        continue
                    if key is None:
                        result.maybe.add(name)
                        continue
                if group is None:
                    if prop_state.status == OK:
                        result.header.setdefault(name, view.address(prop))
                    else:
                        result.maybe.add(name)
                else:
                    table = group.format_property.strip()
                    if not table:
                        continue
                    if prop_state.status == OK and view.state(group, rule).status == OK:
                        result.tables.setdefault(table, set()).add(name)
                    else:
                        result.maybe_tables.add(table)
            for group in rule.groups:
                table = group.format_property.strip()
                if table and view.state(group, rule).status != NOT_APPLICABLE:
                    result.tables.setdefault(table, set())
        result.literals = self.literals(view, rules, "ПриОтправкеДанных", "write")
        if result.literals:
            self.coverage["handler_literal"] += len(result.literals)
        return result

    @staticmethod
    def literals(view: ChainView, rules: list[ed.ObjectRule], event: str, access: str) -> set[str]:
        """Свойства формата, которые обработчик литерально пишет или читает в `ДанныеXDTO`."""
        targets = {b.target_id for r in rules for b in r.events if b.event == event and b.target_id}
        if not targets or view.references is None:
            return set()
        names: set[str] = set()
        for item in view.references.entries:
            if item.owner_id not in targets or item.kind != "format_property" or not item.name:
                continue
            if access == "write" and item.access != "write" and item.form != "insert":
                continue
            names.add(normalize(item.name))
            names.add(normalize(item.name).split(".", 1)[0])
        return names

    # --- приёмник -------------------------------------------------------------------------

    def receivers(self) -> dict[str, _Receiver]:
        """Первый действующий ПОД получения по типу (XDTO:4519) и ПКО его `ИспользуемыеПКО`."""
        view = self.target
        groups: dict[str, list[ed.ProcessingRule]] = defaultdict(list)
        unknown: list[tuple[int, int, int]] = []
        for pod in view.pods():
            if view.state(pod).status == NOT_APPLICABLE:
                continue
            typ, status = view.literal(pod.format_selection)
            if typ is None:
                if status not in ("absent", "empty"):
                    self.skip("ed.pair", f"format_type_{status}", TARGET, view.address(pod))
                    unknown.append(view.table_order(pod))
                continue
            if view.profile.schema is not None and view.profile.find_type(typ)[1] == "missing":
                # ПОД с типом вне пакета удаляется (XDTO:3939–3950).
                continue
            groups[typ].append(pod)
        accepted: dict[str, _Receiver] = {}
        for typ, pods in groups.items():
            first = pods[0]
            state = view.state(first)
            if view.order_unknown(pods):
                state = state.worst(LinkState(PARTIAL, ("layer_order_unknown",)))
            elif (len(pods) > 1 and state.status != OK) or any(
                order < view.table_order(first) for order in unknown
            ):
                state = state.worst(LinkState(PARTIAL, ("shadowed_maybe",)))
            if view.has_event(first, "ПриОбработке"):
                # Обработчик может выключить ПКО (XDTO:7489–7497).
                state = state.worst(LinkState(PARTIAL, ("pod_handler_may_switch",)))
            row = _Receiver(first, state=state)
            for pod in pods if view.order_unknown(pods) else [first]:
                for ref in pod.used_pko:
                    rule, rule_state = view.pko_first(ref.name)
                    if rule is None:
                        # «Не найдено ПКО» — ошибка по объекту (XDTO:503–508, 7455–7475).
                        row.lost.append(f"нет ПКО «{ref.name}»")
                        continue
                    own, status = _rule_type(view, rule)
                    if own is None and status == "type_missing":
                        row.lost.append(f"тип ПКО «{rule.name}» вне пакета версии")
                        continue
                    row.rules.append((rule, own))
                    row.state = row.state.worst(rule_state).worst(view.state(rule))
            accepted[typ] = row
        return accepted

    def reference_rules(self, parents: list[ed.ObjectRule]) -> dict[str, list[ed.ObjectRule]]:
        """ПКО приёмника, которые конвертируют ссылку: правило ПКС-родителя (XDTO:6820–6826).

        Родители — ПКО получения, которые проходят фильтр на путях ПОД приёмника.
        """
        view = self.target
        result: dict[str, list[ed.ObjectRule]] = defaultdict(list)
        for rule in parents:
            for group, prop in properties(rule):
                if not prop.conversion_rule.strip():
                    continue
                if view.state(prop, group or rule).status == NOT_APPLICABLE:
                    continue
                target, _ = view.pko_first(prop.conversion_rule)
                if target is None:
                    continue
                typ, _ = _rule_type(view, target)
                if typ and target not in result[typ]:
                    result[typ].append(target)
        return dict(result)

    # --- сверка ---------------------------------------------------------------------------

    def run(self) -> PairResult:
        sent, known = self.senders()
        accepted = self.receivers()
        passing: dict[str, list[ed.ObjectRule]] = {}
        for typ in sorted(set(sent) & set(accepted)):
            passing[typ] = self.filtered(typ, accepted[typ], set(accepted), known)
        # Ссылки уходят только из объектов, которые выгружаются (XDTO:797–803, 1541–1567).
        referenced = self.references({typ: sent[typ] for typ, rules in passing.items() if rules})
        parents = [rule for rules in passing.values() for rule in rules]
        objects: list[dict[str, Any]] = []
        for typ in sorted(set(sent) | set(accepted)):
            sender, receiver = sent.get(typ), accepted.get(typ)
            row: dict[str, Any] = {"format_object": typ}
            certain = True
            if sender:
                row["source"] = {
                    "pods": [self.source.address(p) for p in sender.pods],
                    "pko": [self.source.address(r) for r in sender.rules],
                    **sender.state.view(),
                }
                certain = certain and sender.state.status == OK
            if receiver:
                row["target"] = {
                    "pod": self.target.address(receiver.pod),
                    "pko": [self.target.address(r) for r, _ in receiver.rules],
                    **receiver.state.view(),
                }
                certain = certain and receiver.state.status == OK
            if sender and receiver:
                if passing[typ]:
                    row["status"] = "exchanged"
                    self.compare(typ, sender, passing[typ])
                else:
                    row["status"] = "not_loaded"
                    address = self.where(TARGET, self.target.address(receiver.pod))
                    if receiver.lost:
                        self.report.warning(
                            "ed.pair.receive_pko_filtered",
                            address,
                            f"У ПОД получения «{receiver.pod.name}» объекта «{typ}» нет "
                            f"действующих ПКО ({'; '.join(receiver.lost)}): объект не загрузится "
                            "(XDTO:503–508, 7455–7475).",
                        )
                    elif not receiver.rules and self.target.has_event(receiver.pod, "ПриОбработке"):
                        # Обработчик может добавить ПКО через `Вставить` (XDTO:7455–7457).
                        self.skip(
                            "ed.pair.receive_pko_filtered",
                            "pod_handler_may_insert",
                            TARGET,
                            self.target.address(receiver.pod),
                        )
                    elif not receiver.rules:
                        self.report.info(
                            "ed.pair.receive_pko_filtered",
                            address,
                            f"ПОД получения «{receiver.pod.name}» объекта «{typ}» без ПКО: тип "
                            "объявлен к получению, источник его отправит, а приёмник молча не "
                            "загрузит (заглушка; XDTO:7455–7475).",
                        )
            elif sender:
                row["status"] = "not_accepted"
                self.report.info(
                    "ed.pair.not_accepted",
                    self.where(SOURCE, self.source.address(sender.pods[0])),
                    f"Источник отправляет «{typ}», а у приёмника нет действующего ПОД получения "
                    "этого типа: объект не входит в пересечение и молча не выгружается "
                    "(XDTO:797–803, 3878–3893, 9465–9484).",
                )
            else:
                assert receiver is not None
                if typ in referenced:
                    row["status"] = "reference_only"
                    message = (
                        f"Приёмник принимает «{typ}», у источника нет ПОД отправки этого типа: "
                        "объект уходит только ссылкой из других объектов (XDTO:1541–1567)."
                    )
                elif typ in known:
                    row["status"] = "not_sent"
                    message = (
                        f"Приёмник принимает «{typ}», у источника есть ПКО этого типа, но нет ни "
                        "ПОД отправки, ни ссылок из отправляемых объектов: объект не уходит."
                    )
                else:
                    row["status"] = "not_sent"
                    message = (
                        f"Приёмник принимает «{typ}», а у источника нет ПКО этого типа: в «умею "
                        "отправить» его нет (XDTO:9443–9463, 7477–7479)."
                    )
                self.report.info(
                    "ed.pair.not_sent",
                    self.where(TARGET, self.target.address(receiver.pod)),
                    message,
                )
            if not certain:
                row["certain"] = False
            objects.append(row)
        self.reference_search(referenced, self.reference_rules(parents))
        if self.source.profile.schema is None or self.target.profile.schema is None:
            self.report.skip(
                "ed.pair",
                "Схема источника или приёмника не передана: типы вне пакета версии не исключались, "
                "ключевые свойства определены только по префиксу «КлючевыеСвойства.»",
            )
        if self.source.profile.format_version is None:
            self.report.skip(
                "ed.pair", "Версия формата не выбрана: условия по версии не вычислялись"
            )
        return PairResult(self.report, objects, self.coverage, self.source.profile.format_version)

    def filtered(
        self, typ: str, receiver: _Receiver, declared: set[str], known: set[str]
    ) -> list[ed.ObjectRule]:
        """ПКО получения, которые пройдут фильтр `ОбъектФорматаПроходитПоФильтруXDTO` (XDTO:7477).

        Тип ПКО должен быть объявлен приёмником (XDTO:9465–9484) и известен источнику
        (XDTO:9443–9463); иначе ПКО молча пропускается.
        """
        passing: list[ed.ObjectRule] = []
        rejected: list[tuple[ed.ObjectRule, str]] = []
        for rule, own in receiver.rules:
            if own is None:
                passing.append(rule)
                self.skip(
                    "ed.pair", "receive_format_type_unknown", TARGET, self.target.address(rule)
                )
            elif own not in declared:
                rejected.append((rule, f"тип «{own}» не объявлен ни одним ПОД получения приёмника"))
            elif own not in known:
                rejected.append((rule, f"у источника нет ПКО типа «{own}»"))
            else:
                passing.append(rule)
        for rule, cause in rejected:
            message = (
                f"ПКО получения в ПОД «{receiver.pod.name}» объекта «{typ}» пропускается: {cause} "
                "(XDTO:7477–7479)."
            )
            if passing:
                self.report.info(
                    "ed.pair.receive_pko_filtered",
                    self.where(TARGET, self.target.address(rule)),
                    message,
                )
            else:
                self.report.warning(
                    "ed.pair.receive_pko_filtered",
                    self.where(TARGET, self.target.address(rule)),
                    message + " Других ПКО у ПОД нет: объект не загрузится.",
                )
        return passing

    def compare(self, typ: str, sender: _Sender, rules: list[ed.ObjectRule]) -> None:
        supply = self.supply(sender.rules)
        view = self.target
        read: set[str] = set()
        for rule in rules:
            rule_state = view.state(rule)
            unread = rule_state.status != OK and bool(
                set(rule_state.reasons) & {"reader_partial", "unknown_fragment"}
            )
            fill = view.has_event(rule, *_RECEIVER_FILL)
            by_configuration: dict[str, list[ed.PropertyRule]] = defaultdict(list)
            for group, prop in properties(rule):
                if group is not None:
                    continue
                prop_state = view.state(prop, rule)
                if prop_state.status == NOT_APPLICABLE:
                    continue
                if not prop.configuration_property.strip() or not prop.format_property.strip():
                    continue
                read.add(normalize(prop.format_property))
                by_configuration[prop.configuration_property.strip().casefold()].append(prop)
            for props in by_configuration.values():
                for prop in props:
                    self.property(typ, rule, prop, props, supply, unread or fill)
            self.tables(typ, rule, supply, unread or fill)
            self.search(typ, rule, by_configuration, supply)
        read |= self.literals(view, rules, "ПриКонвертацииДанныхXDTO", "read")
        receive_handler = any(view.has_event(r, "ПриКонвертацииДанныхXDTO") for r in rules)
        for name, address in supply.header.items():
            if name in read:
                continue
            if receive_handler:
                self.skip("ed.pair.property_unread", "handler_may_read", SOURCE, address)
                continue
            self.report.info(
                "ed.pair.property_unread",
                self.where(SOURCE, address),
                f"Источник отправляет «{name}» объекта «{typ}», у приёмника нет ПКС и обработчика "
                "конвертации, которые его читают: свойство молча игнорируется (XDTO:6601–6616).",
            )

    def property(
        self,
        typ: str,
        rule: ed.ObjectRule,
        prop: ed.PropertyRule,
        same_attribute: list[ed.PropertyRule],
        supply: _Supply,
        receiver_uncertain: bool,
    ) -> None:
        view = self.target
        name = normalize(prop.format_property)
        state = supply(name)
        if state is True:
            return
        address = view.address(prop)
        check = "ed.pair.property_cleared"
        if state is None:
            self.skip(check, "handler_may_supply", TARGET, address)
            return
        if view.state(prop, rule).status != OK:
            self.skip(check, "opaque_condition", TARGET, address)
            return
        if receiver_uncertain:
            self.skip(check, "receiver_handler", TARGET, address)
            return
        if any(
            other is not prop and supply(normalize(other.format_property)) is not False
            for other in same_attribute
        ):
            # Реквизит заполняет другая ПКС с отправляемым свойством.
            self.skip(check, "other_pks_fills", TARGET, address)
            return
        if prop.algorithm_flag:
            self.skip(check, "algorithmic", TARGET, address)
            return
        key, key_status = _key(view, rule, prop)
        if key_status == "missing":
            # Свойства нет в схеме приёмника: в сообщении его не будет ни от кого
            # (ed_validate: ed.schema.property_missing).
            self.skip("ed.pair.property_unsent", "property_not_in_schema", TARGET, address)
            return
        same = name.casefold() == prop.configuration_property.strip().casefold()
        dotted = "." in prop.format_property.strip()
        if same and not dotted and key is False:
            self.report.info(
                "ed.pair.property_unsent",
                self.where(TARGET, address),
                f"Приёмник читает «{name}» объекта «{typ}», источник его не отправляет: у "
                "найденного объекта реквизит сохранится, у нового будет пустым "
                "(XDTO:6753–6765, 7022–7047).",
            )
        elif same and not dotted and key is None:
            unknown = (
                "без схемы приёмника"
                if key_status == "schema_unavailable"
                else f"схема приёмника не ответила ({key_status})"
            )
            self.report.info(
                "ed.pair.property_unsent",
                self.where(TARGET, address),
                f"Приёмник читает «{name}» объекта «{typ}», источник его не отправляет; ключевое "
                f"ли свойство — неизвестно {unknown}: ключевое у найденного объекта очистится "
                "(XDTO:6753–6765).",
            )
            reason = (
                "target_schema_unavailable"
                if key_status == "schema_unavailable"
                else f"schema_{key_status}"
            )
            self.skip(check, reason, TARGET, address)
        else:
            cause = (
                "свойство ключевое"
                if key
                else "имя формата через точку"
                if dotted
                else f"имя формата «{name}» не равно реквизиту «{prop.configuration_property}»"
            )
            self.report.warning(
                check,
                self.where(TARGET, address),
                f"Приёмник читает «{name}» объекта «{typ}», источник его не отправляет, и защита "
                f"отсутствующих свойств не срабатывает ({cause}): у найденного объекта реквизит "
                "перезапишется пустым (XDTO:6748–6765, 7022–7047).",
            )

    def tables(
        self, typ: str, rule: ed.ObjectRule, supply: _Supply, receiver_uncertain: bool
    ) -> None:
        view = self.target
        for group in rule.groups:
            if view.state(group, rule).status == NOT_APPLICABLE:
                continue
            table = group.format_property.strip()
            if not table or not group.configuration_property.strip():
                continue
            address = view.address(group)
            state = supply(table)
            if state is not True:
                if state is None:
                    self.skip("ed.pair.table_unsent", "handler_may_supply", TARGET, address)
                elif view.state(group, rule).status != OK:
                    self.skip("ed.pair.table_unsent", "opaque_condition", TARGET, address)
                elif receiver_uncertain:
                    self.skip("ed.pair.table_unsent", "receiver_handler", TARGET, address)
                else:
                    self.report.warning(
                        "ed.pair.table_unsent",
                        self.where(TARGET, address),
                        f"Приёмник загружает табличную часть «{table}» объекта «{typ}», источник "
                        "её не отправляет: у найденного объекта табличная часть очистится "
                        "(XDTO:7051–7062, 6629–6633).",
                    )
                continue
            for prop in group.properties:
                if view.state(prop, group).status == NOT_APPLICABLE:
                    continue
                if not prop.configuration_property.strip() or not prop.format_property.strip():
                    continue
                column = normalize(prop.format_property)
                column_state = supply.column(table, column)
                if column_state is True:
                    continue
                prop_address = view.address(prop)
                if column_state is None or receiver_uncertain:
                    reason = "handler_may_supply" if column_state is None else "receiver_handler"
                    self.skip("ed.pair.table_column_unsent", reason, TARGET, prop_address)
                    continue
                self.report.info(
                    "ed.pair.table_column_unsent",
                    self.where(TARGET, prop_address),
                    f"Приёмник загружает колонку «{column}» табличной части «{table}» объекта "
                    f"«{typ}», источник её не отправляет: строки перезагружаются при каждом "
                    "обмене, колонка будет пустой (XDTO:7051–7062).",
                )

    def variants(
        self,
        rule: ed.ObjectRule,
        by_configuration: dict[str, list[ed.PropertyRule]],
        supplied,
        *,
        reference: bool,
    ) -> list[tuple[ed.SearchSet, list[str], bool, bool]] | None:
        """Варианты поиска: (вариант, не заполненные отправителем, не заполнено вовсе, неясно).

        Поля поиска — реквизиты приёмника, заполненные его ПКС (XDTO:1856–1885, 6865); вариант с
        пустым полем пропускается, кроме `Родитель` справочника и ПВХ (XDTO:6863–6887).
        """
        view = self.target
        identification, _ = view.literal(rule.identification)
        if identification is None or identification.strip() not in SEARCH_BY_FIELDS:
            if rule.identification.presence not in ("literal", "absent"):
                self.skip("ed.pair", "identification_unknown", TARGET, view.address(rule))
            return None
        metadata, _ = view.pko_metadata(rule)
        optional_parent = bool(metadata and metadata.casefold().startswith(_OPTIONAL_PARENT))
        rows = []
        for variant in rule.search_sets:
            missing: list[str] = []
            unfilled = False
            uncertain = variant.status != ed.ParseStatus.COMPLETE
            for name in variant.fields:
                if optional_parent and name == "Родитель":
                    continue
                if optional_parent and name == "ЭтоГруппа":
                    # Признак группы ставит исполнитель, не ПКС (XDTO:4399–4402).
                    uncertain = True
                    continue
                props = by_configuration.get(name.casefold(), [])
                if not props:
                    unfilled = True
                    continue
                states = []
                for prop in props:
                    if prop.algorithm_flag or view.state(prop, rule).status != OK:
                        states.append(None)
                        continue
                    format_name = normalize(prop.format_property)
                    if reference:
                        key, _ = _key(view, rule, prop)
                        if key is False:
                            states.append(False)
                            continue
                        if key is None:
                            states.append(None)
                            continue
                    states.append(supplied(format_name))
                if True in states:
                    continue
                if None in states:
                    uncertain = True
                else:
                    missing.append(f"{name} ← {normalize(props[0].format_property)}")
            rows.append((variant, missing, unfilled, uncertain))
        return rows

    def search(
        self,
        typ: str,
        rule: ed.ObjectRule,
        by_configuration: dict[str, list[ed.PropertyRule]],
        supply: _Supply,
    ) -> None:
        view = self.target
        rows = self.variants(rule, by_configuration, supply, reference=False)
        if not rows:
            return
        handled = view.has_event(rule, *_RECEIVER_SEARCH)
        # Варианты перебираются по очереди; пропущенный уступает следующему, и дубль возможен,
        # только когда не остаётся ни одного (XDTO:6863–6887, 1984–1985).
        dead = all(missing or unfilled for _, missing, unfilled, uncertain in rows) and not any(
            uncertain for *_, uncertain in rows
        )
        for variant, missing, _, _uncertain in rows:
            if not missing:
                continue
            address = view.address(variant)
            if handled:
                self.skip("ed.pair.search_field_unsent", "receiver_handler", TARGET, address)
                continue
            fields = ", ".join(variant.fields)
            message = (
                f"Вариант поиска «{fields}» объекта «{typ}» всегда пропускается: источник не "
                f"отправляет {', '.join(missing)}. "
            )
            if dead:
                self.report.warning(
                    "ed.pair.search_field_unsent",
                    self.where(TARGET, address),
                    message + "Рабочих вариантов не остаётся: если по УИД объект не найден, "
                    "существующий объект не найдётся, возможен дубль (XDTO:6863–6887, 1984–1985).",
                )
            else:
                self.report.info(
                    "ed.pair.search_field_unsent",
                    self.where(TARGET, address),
                    message + "Поиск идёт по остальным вариантам (XDTO:6863–6887).",
                )
        for variant, missing, _, uncertain in rows:
            if uncertain and not missing:
                self.skip(
                    "ed.pair.search_field_unsent",
                    "handler_may_supply",
                    TARGET,
                    view.address(variant),
                )

    def reference_search(
        self,
        referenced: dict[str, list[tuple[str, ed.ObjectRule]]],
        converters: dict[str, list[ed.ObjectRule]],
    ) -> None:
        """Объект пришёл ссылкой: только ключевые свойства (XDTO:6543–6563, 6738–6743)."""
        view = self.target
        for typ, links in referenced.items():
            supply = self.supply([rule for _, rule in links], keys_only=True)
            for rule in converters.get(typ, []):
                by_configuration: dict[str, list[ed.PropertyRule]] = defaultdict(list)
                for group, prop in properties(rule):
                    if group is not None or view.state(prop, rule).status == NOT_APPLICABLE:
                        continue
                    if prop.configuration_property.strip() and prop.format_property.strip():
                        by_configuration[prop.configuration_property.strip().casefold()].append(
                            prop
                        )
                rows = self.variants(rule, by_configuration, supply, reference=True)
                if not rows:
                    continue
                if view.has_event(rule, *_RECEIVER_SEARCH):
                    for variant, missing, *_ in rows:
                        if missing:
                            self.skip(
                                "ed.pair.search_field_not_key",
                                "receiver_handler",
                                TARGET,
                                view.address(variant),
                            )
                    continue
                dead = all(missing or unfilled for _, missing, unfilled, _ in rows) and not any(
                    uncertain for *_, uncertain in rows
                )
                for variant, missing, _, uncertain in rows:
                    address = view.address(variant)
                    if not missing:
                        if uncertain:
                            self.skip(
                                "ed.pair.search_field_not_key",
                                "handler_may_supply",
                                TARGET,
                                address,
                            )
                        continue
                    fields = ", ".join(variant.fields)
                    message = (
                        f"Источник отправляет «{typ}» ссылкой ({links[0][0]}), а вариант поиска "
                        f"«{fields}» приёмника заполняется не ключевыми свойствами ссылки "
                        f"({', '.join(missing)}): по ссылке приходят только ключевые "
                        "(XDTO:6543–6563), вариант пропускается. "
                    )
                    if dead:
                        self.report.warning(
                            "ed.pair.search_field_not_key",
                            self.where(TARGET, address),
                            message + "Рабочих вариантов не остаётся: если по УИД объект не "
                            "найден, создаётся объект из ключевых свойств (XDTO:1945–1951).",
                        )
                    else:
                        self.report.info(
                            "ed.pair.search_field_not_key",
                            self.where(TARGET, address),
                            message + "Поиск идёт по остальным вариантам (XDTO:6863–6887).",
                        )


def validate_pair(source: ChainView, target: ChainView) -> PairResult:
    """Источник — контекст отправки, приёмник — получения; версия у обоих одна."""
    if source.direction != "send" or target.direction != "receive":
        raise ValueError("Источник читается при отправке, приёмник — при получении")
    if source.profile.format_version != target.profile.format_version:
        raise ValueError("Версии формата источника и приёмника различаются")
    return _Checker(source, target).run()
