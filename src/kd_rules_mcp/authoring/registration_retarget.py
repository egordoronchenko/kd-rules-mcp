"""Перенацеливание готовых правил регистрации на другой план обмена.

Имя плана в файле — атрибут ``Имя`` элемента ``ПланОбмена``
(``reference/kd2-cfg/DataProcessors/ВыгрузкаРегистрации/Ext/ObjectModule.bsl``,
строки 89–99; сам атрибут — строка 93). Текст элемента писатель берёт из
``ПланОбмена.Наименование`` (строка 95). Если в файле там стоит тип
``ПланОбменаСсылка.<старое имя>``, текст заменяется на новое имя плана:
наименования целевого плана у функции нет.

Реквизит узла листа — ``СвойствоПланаОбмена`` (строка 332): шапка или
``[ТабличнаяЧасть].Реквизит``. Хвост после первой точки — разыменование,
его не переименовываем (читатель, ``ЗагрузкаПравилРегистрацииОбъектов``,
строки 471–484). Ключ отображения ``Имя`` — только реквизит шапки,
``[ТЧ]`` — табличная часть, ``[ТЧ].Имя`` — её реквизит. Связанная таблица —
``ТаблицаСвойствПланаОбмена`` (строки 342–343 и 402–420): у табличной части
наименование ``[Имя]``, у реквизита — имя; ``Тип`` и ``Вид`` сохраняются.
``РеквизитРежимаВыгрузки`` (строка 220) переименовывается тем же отображением
и сверяется с шапкой целевого плана: исполнитель подставляет его в запрос узлов
(``ОбменДаннымиСобытия``, ``МассивУзловПоЗначениямСвойств``, строки 2198–2215)
и читает ``Узел[ПРО.ИмяРеквизитаФлага]`` (строка 2884).

Тексты обработчиков и алгоритмы значений не переписываются. Старое имя плана
и старые имена реквизитов узла в коде (не в комментариях) — замечание.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace

from lxml import etree

from kd_rules_mcp.authoring.registration import (
    FilterProperty,
    ObjectFilter,
    _object_filter,
    snapshot_registration,
)
from kd_rules_mcp.errors import (
    DuplicateTargetPropertyError,
    InvalidRegistrationNameError,
    NotRegistrationRulesError,
    PropertyNameClashError,
    RegistrationRetargetError,
    RetargetInvariantError,
)
from kd_rules_mcp.kd2.diff import diff_rules
from kd_rules_mcp.kd2.model import Node, RegistrationRules, RulesDocument
from kd_rules_mcp.kd2.rules_io import dump_rules
from kd_rules_mcp.structures.queries import ObjectCard, ObjectProperty
from kd_rules_mcp.structures.xmldump import (
    EventSubscription,
    plan_subscriptions,
    registration_events,
)
from kd_rules_mcp.validation.address import pro_addresses
from kd_rules_mcp.validation.registration import _split_plan_property

# Буква или «_», дальше буквы, цифры и «_». Так платформа именует метаданные.
_IDENTIFIER = re.compile(r"[^\W\d]\w*")
_MAPPING_KEY = re.compile(r"[^\W\d]\w*|\[([^\W\d]\w*)](?:\.([^\W\d]\w*))?")
_PLAN_TYPE_PREFIX = "ПланОбменаСсылка."
_PLAN_FILTER = "ОтборПоСвойствамПланаОбмена"
_OBJECT_FILTER = "ОтборПоСвойствамОбъекта"
_PLAN_PROPERTY = "СвойствоПланаОбмена"
_PLAN_TABLE = "ТаблицаСвойствПланаОбмена"
_OBJECT_TABLE = "ТаблицаСвойствОбъекта"
_HANDLERS = (
    "ПередОбработкой",
    "ПриОбработке",
    "ПриОбработкеДополнительный",
    "ПослеОбработки",
)
_UNLOAD_MODE = "РеквизитРежимаВыгрузки"
# ОбменДаннымиСобытия:3021–3027: только эти виды проверяются перед выгрузкой.
_DELETION_OBJECT_KINDS = frozenset(
    {
        "Справочник",
        "Документ",
        "ПланВидовХарактеристик",
        "ПланСчетов",
        "ПланВидовРасчета",
        "БизнесПроцесс",
        "Задача",
    }
)
_ALLOWED_DIFF = frozenset({"ПланОбмена.Имя", "ПланОбмена", _PLAN_FILTER, _UNLOAD_MODE})


@dataclass(frozen=True, slots=True)
class RetargetRule:
    """Итог одного правила: адрес и число листьев отбора."""

    address: str
    renamed: int
    untouched: int
    replaced: int = 0
    removed: int = 0


@dataclass(frozen=True, slots=True)
class BooleanReplacement:
    """Явное решение агента о новом булевом условии."""

    name: str
    value: bool


@dataclass(frozen=True, slots=True)
class FilterChange:
    """Замена одного исходного листа или его удаление."""

    address: str
    leaf: str
    before: str
    after: str
    action: str


@dataclass(frozen=True, slots=True)
class RetargetRemark:
    """Реквизит узла после замены не найден у целевого плана."""

    address: str
    leaf: str
    property_name: str
    message: str
    reference: str = ""


@dataclass(frozen=True, slots=True)
class CodeMention:
    """Старое имя плана или реквизита узла осталось в коде. Код не переписан."""

    address: str
    event: str
    line: int
    name: str
    message: str


@dataclass(frozen=True, slots=True)
class RetargetNotice:
    """Проверка использования типа или приведение имени к написанию метаданных."""

    check: str
    address: str
    leaf: str
    reference: str
    message: str
    blocking: bool = False
    requires_acknowledgement: bool = False


@dataclass(frozen=True, slots=True)
class NodeReference:
    """Вид ссылки и найденный реквизит; одинаковы для структуры и выгрузки."""

    tabular: str
    head: str
    tail: str
    canonical: str
    field: ObjectProperty | None
    missing: str | None


@dataclass(frozen=True, slots=True)
class RetargetResult:
    """Новая модель и отчёт замены. Исходный документ не меняется.

    ``code_mentions`` больше нуля — перенос неполон: в обработчиках или
    алгоритмах остались старые имена. Сам код функция не меняет.
    """

    document: RegistrationRules
    rules: tuple[RetargetRule, ...]
    unused: tuple[str, ...]
    remarks: tuple[RetargetRemark, ...]
    only_expected: bool
    code_mentions: int
    mentions: tuple[CodeMention, ...]
    source_rules_hash: str = ""
    notices: tuple[RetargetNotice, ...] = ()
    deletion_mark_filter: bool = False
    deletion_filters: tuple[tuple[str, str], ...] = ()
    changes: tuple[FilterChange, ...] = ()


def retarget_registration(
    rules: RulesDocument,
    *,
    plan_name: str,
    node_properties: Mapping[str, object],
    target_plan: ObjectCard | None = None,
    target_properties: frozenset[str] | None = None,
    deletion_mark_filter: bool = False,
    target_objects: Sequence[ObjectCard] | None = None,
) -> RetargetResult:
    """Копия правил регистрации с новым именем плана и реквизитами узла.

    ``node_properties`` — «имя в старом плане → имя в целевом». Ключ ``Имя``
    относится к реквизиту шапки, ``[ТЧ]`` — к табличной части, ``[ТЧ].Имя`` —
    к реквизиту табличной части. Заменяется голова ``СвойствоПланаОбмена``,
    строки ``ТаблицаСвойствПланаОбмена`` и ``РеквизитРежимаВыгрузки``.
    Строка означает переименование, null — удаление листа, {name, value} —
    замену булевой константы; массив таких решений объединяется по И.
    Меняется только указанный отбор плана. Отборы объекта, обработчики и
    неизвестные узлы остаются как были. Код со старыми именами не переписывается: он попадает в
    ``mentions``, а ключ, встретившийся только там, — не в ``unused``.

    ``target_plan`` — карточка плана из структуры. Нет реквизита или
    табличной части, на которые после замены ссылается лист или режим
    выгрузки, — замечание, не исключение. Вид ссылки, тип использования и
    написание имён сверяются одинаково по карточке структуры и выгрузки.
    Разыменование ссылочного типа требует ручной проверки, простого — запрещено.
    ``target_properties`` — прежний вход только по именам без типов;
    сервис передаёт полную карточку из общего читателя метаданных.
    """
    document = _registration(rules)
    name = _check_identifier(plan_name, "плана обмена")
    if target_plan is not None and target_plan.kind != "ПланОбмена":
        raise RegistrationRetargetError(
            f"Сведения целевого плана относятся к «{target_plan.kind}», а не к плану обмена"
        )
    filter_names, unload_names = _node_names(document)
    mapping, actions = _decisions(node_properties)
    old_plan = document.exchange_plan
    if target_plan is None and target_properties is not None:
        target_plan = _names_card(name, target_properties)
    effective, case_notices = _canonical_mapping(document, mapping, target_plan)
    if actions:
        _check_action_clashes(effective, actions, filter_names | unload_names, target_plan)
    else:
        _check_targets(mapping, effective, filter_names | unload_names)
        _check_clash(effective, filter_names, unload_names)
    cloned = _clone(document)
    _set_plan_name(cloned, name, old_plan)
    stats, applied = _rename_filters(cloned, effective)
    _rename_unload_modes(cloned, effective, applied)
    _assert_only_expected(document, cloned, name, effective)
    changes, action_notices, action_applied = _apply_actions(document, cloned, actions, target_plan)
    applied.update(action_applied)
    stats = [
        replace(
            row,
            replaced=sum(c.address == row.address and c.action == "replaced" for c in changes),
            removed=sum(c.address == row.address and c.action == "removed" for c in changes),
            untouched=row.untouched - sum(c.address == row.address for c in changes),
        )
        for row in stats
    ]
    keys = {key: key for key in node_properties}
    mentions = _code_mentions(cloned, old_plan if old_plan != name else "", keys)
    seen_in_code = _mapping_keys_in_code(keys, mentions)
    result = RetargetResult(
        document=cloned,
        rules=tuple(stats),
        unused=tuple(
            key for key in keys if key.casefold() not in applied and key not in seen_in_code
        ),
        remarks=registration_node_remarks(cloned, name, target_plan, target_properties),
        only_expected=True,
        code_mentions=len(mentions),
        mentions=mentions,
        source_rules_hash=hashlib.sha256(dump_rules(document)).hexdigest(),
        notices=case_notices + action_notices + registration_type_notices(cloned, target_plan),
        changes=changes,
    )
    if deletion_mark_filter:
        if target_objects is None:
            raise RegistrationRetargetError(
                "Для отбора по пометке нужны карточки объектов конфигурации"
            )
        statuses, notices = _deletion_filters(cloned, target_plan, target_objects)
        result = replace(
            result,
            deletion_mark_filter=True,
            deletion_filters=statuses,
            notices=result.notices + notices,
        )
    return result


def deletion_filter_summary(result: RetargetResult) -> dict:
    """Счётчики добавления и причин пропуска; адреса замечаний выдаются постранично."""
    reasons: dict[str, int] = {}
    added = 0
    for _, status in result.deletion_filters:
        if status == "added":
            added += 1
        else:
            reasons[status] = reasons.get(status, 0) + 1
    summary = {
        "enabled": result.deletion_mark_filter,
        "added": added,
        "skipped": sum(reasons.values()),
        "skipped_by_reason": reasons,
    }
    if result.deletion_mark_filter:
        summary["mode_rules"] = sum(n.check == "registration.deletion_mode" for n in result.notices)
    return summary


def registration_rule_active(rule: Node) -> bool:
    """БСП загружает только Валидное=true (ЗПРО:282–292,1065–1085), как rules_validate."""
    return rule.attrs.get("Отключить") is not True and rule.attrs.get("Валидное", False) is True


def registration_plan_notices(
    rules: RegistrationRules,
    plan_name: str,
    content: frozenset[str] | None,
    subscriptions: tuple[EventSubscription, ...] | None = None,
) -> tuple[RetargetNotice, ...]:
    """Состав из той же выгрузки: ОДС:171–188,1389–1414; вне него ПРО не исполняется."""
    if content is None:
        return (
            RetargetNotice(
                "registration.plan_content_unchecked",
                f"ПланОбмена.{plan_name}",
                "СоставПланаОбмена",
                plan_name,
                f"Не удалось прочитать состав плана «{plan_name}»; "
                "проверьте, что объекты правил входят в его состав.",
                requires_acknowledgement=True,
            ),
        )
    members = {name.casefold() for name in content}
    notices = []
    selected = plan_subscriptions(subscriptions, plan_name) if subscriptions is not None else ()
    advice = " Добавьте объектом «только для регистрации» в комплект модуля: " + (
        'ed_authoring_build(scope="manager", registration_objects=["Вид.Имя"]).'
    )
    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        if not registration_rule_active(rule):
            continue
        name = str(rule.get("ОбъектМетаданныхИмя"))
        if name.casefold() not in members:
            check = "registration.plan_membership"
            message = (
                f"{address}: объект «{name}» вне состава плана «{plan_name}»; "
                "правило не исполнится до добавления в состав."
            )
        elif subscriptions is not None:
            kind, _, object_name = name.partition(".")
            missing = [
                event
                for event, source in registration_events(kind, object_name)
                if not any(s.event == event and s.covers(source) for s in selected)
            ]
            if not missing:
                continue
            check = "registration.plan_registration_unsubscribed"
            message = (
                f"{address}: объект «{name}» в составе плана «{plan_name}», "
                f"но вне источников подписок: {', '.join(missing)}; "
                "ПРО не вызывается для этих событий."
            )
        else:
            continue
        notices.append(
            RetargetNotice(
                check,
                address,
                "ОбъектМетаданныхИмя",
                name,
                message + advice,
                requires_acknowledgement=True,
            )
        )
    return tuple(notices)


def _has_deletion_mark(card: ObjectCard) -> bool:
    return any(
        p.path.casefold() == "пометкаудаления"
        and not p.is_group
        and p.kind in {"Свойство", "СтандартныйРеквизит"}
        for p in card.properties
    )


def _deletion_filters(
    rules: RegistrationRules, plan: ObjectCard | None, objects: Sequence[ObjectCard]
) -> tuple[tuple[tuple[str, str], ...], tuple[RetargetNotice, ...]]:
    """Добавляет лист в корень «И», сохраняя группы и обработчики.

    Форма: reference/kd2-cfg/DataProcessors/ВыгрузкаРегистрации/Ext/ObjectModule.bsl:
    362–385,402–420;
    ЗагрузкаПравилРегистрацииОбъектов:514–574 читает тип перед константой.
    ОбменДаннымиСобытия:1833–1847 — ПРОБ И ПРОП,2598–2641 — корень И;
    :1874–1897,2671–2686 — перед отбором код может менять ПРО;
    :2113,2700–2737 — ПриОбработке меняет запрос узлов после ПРОБ;
    :1888–1900,2782–2796 — ПослеОбработки может добавлять получателей.
    :1833,2446–2474 — алгоритм значения исполняется с доступом к ПРО до отбора.
    """
    cards = {alias.casefold(): card for card in objects for alias in (card.name, card.type_name)}
    statuses, notices = [], []
    enabled: dict[str, list[tuple[str, str, bool]]] = {}

    def notice(check: str, address: str, reference: str, message: str, ack: bool = True) -> None:
        notices.append(
            RetargetNotice(
                check, address, _OBJECT_FILTER, reference, message, requires_acknowledgement=ack
            )
        )

    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        raw = str(rule.get("ОбъектМетаданныхИмя"))
        card = cards.get(raw.casefold())
        if rule.attrs.get("Отключить") is True:
            status = "disabled"
        elif not registration_rule_active(rule):
            status = "invalid"
        elif card is None:
            status = "object_unchecked"
            notice(
                "registration.deletion_unchecked",
                address,
                raw,
                f"{address}: объект «{raw}» не найден в структуре; наличие пометки не проверено.",
            )
        elif not _has_deletion_mark(card):
            status = "no_deletion_mark"
        elif card.kind not in _DELETION_OBJECT_KINDS:
            status = "unsupported_kind"
        else:
            existing = any(
                str(item.get("СвойствоОбъекта")).split(".", 1)[0].casefold() == "пометкаудаления"
                for tag in (_OBJECT_FILTER, _PLAN_FILTER)
                for _, item in _leaves(rule, tag)
            )
            status = "existing_filter" if existing else "added"
            guarded = not existing
            if existing:
                guarded = any(
                    len(path) == 1
                    and str(item.get("СвойствоОбъекта")).casefold() == "пометкаудаления"
                    and item.get("ТипСвойстваОбъекта") == "Булево"
                    and item.get("ВидСравнения") == "Равно"
                    and item.get("Вид") in ("", "ЗначениеКонстанты")
                    and str(item.get("ЗначениеКонстанты")).strip() in {"false", "0"}
                    for path, item in _leaves(rule, _OBJECT_FILTER)
                )
                notice(
                    "registration.deletion_existing",
                    address,
                    raw,
                    f"{address}: отбор по пометке уже есть и сохранён без изменений; "
                    "проверьте, что помеченный объект не проходит правило.",
                    ack=not guarded,
                )
            else:
                container = rule.child(_OBJECT_FILTER)
                if container is None:
                    container = Node.new("object_filter", _OBJECT_FILTER)
                    rule.children[_OBJECT_FILTER] = container
                container.items.append(
                    _object_filter(
                        ObjectFilter(
                            object_property="ПометкаУдаления",
                            property_type="Булево",
                            comparison="Равно",
                            element_kind="ЗначениеКонстанты",
                            constant_value="false",
                            object_properties=(
                                FilterProperty("ПометкаУдаления", "Булево", "Свойство"),
                            ),
                        )
                    )
                )
            enabled.setdefault(card.name.casefold(), []).append((address, status, guarded))
            code_sources = [(event, str(rule.get(event))) for event in _HANDLERS]
            code_sources.extend(
                (f"АлгоритмЗначения {_leaf_label(tag, path)}", str(item.get("ЗначениеКонстанты")))
                for tag in (_OBJECT_FILTER, _PLAN_FILTER)
                for path, item in _leaves(rule, tag)
                if item.get("Вид") == "АлгоритмЗначения"
            )
            for event, code in code_sources:
                if any(line.strip() for line in _code_without_comments(code)):
                    notice(
                        "registration.deletion_handler",
                        address,
                        event,
                        f"{address}, {event}: код сохранён. "
                        "Он может менять правило или получателей; "
                        "сервер его не исполняет. Проверьте, что отбор по пометке не обходится.",
                    )
            mode = str(rule.get(_UNLOAD_MODE)).strip()
            if mode:
                notice(
                    "registration.deletion_mode",
                    address,
                    mode,
                    f"{address}: проверьте режим узла в «{mode}». «По условию» или пустой: "
                    "пометка и её снятие передаются с отбором. «При необходимости»: "
                    "пометка уходит удалением и без нашего отбора; "
                    "снятие пометки не регистрируется "
                    "до следующего изменения объекта. После снятия нужно перезаписать объект "
                    "или зарегистрировать его к отправке. "
                    "«Выгружать всегда»: отбор не проверяется. "
                    "«Вручную» и «Не выгружать»: помеченный объект снимается с регистрации "
                    "без отправки удаления. "
                    "При начальной выгрузке помеченные объекты не отправляются, "
                    "кроме узлов с режимом «Выгружать всегда».",
                )
        statuses.append((address, status))
    for name, rows in enabled.items():
        if (
            len(rows) > 1
            and any(s == "added" for _, s, _ in rows)
            and not all(g for _, _, g in rows)
        ):
            notice(
                "registration.deletion_partial",
                rows[0][0],
                name,
                "Отбор добавлен не во все правила объекта: часть уже содержит свой отбор. "
                "Помеченный объект должен не проходить каждое правило: "
                + "; ".join(a for a, _, _ in rows),
            )
    if plan is not None:
        for prop in plan.properties:
            if prop.kind != "ЭлементСоставаПланаОбмена":
                continue
            for type_name in prop.types + prop.unresolved:
                card = cards.get(type_name.casefold())
                if (
                    card is not None
                    and _has_deletion_mark(card)
                    and card.kind in _DELETION_OBJECT_KINDS
                    and card.name.casefold() not in enabled
                ):
                    notice(
                        "registration.deletion_missing_rule",
                        f"ПланОбмена.{rules.exchange_plan}",
                        card.name,
                        f"Объект «{card.name}» входит в состав плана, "
                        "но действующего правила регистрации нет. "
                        "Он выгружается всегда; пометка удаления этим отбором не передастся.",
                    )
    return tuple(statuses), tuple(notices)


def applicable_node_keys(rules: RegistrationRules, mapping: Mapping[str, str]) -> bool:
    """Есть ли структурная ссылка, к которой применим хотя бы один ключ."""
    filters, modes = _node_names(rules)
    return bool({key.casefold() for key in filters | modes} & {key.casefold() for key in mapping})


def _registration(rules: RulesDocument) -> RegistrationRules:
    if isinstance(rules, RegistrationRules):
        return rules
    found = rules.root_tag if isinstance(rules, RulesDocument) else type(rules).__name__
    raise NotRegistrationRulesError(
        f"Ожидались правила регистрации «ПравилаРегистрации», найден корень «{found}»"
    )


def _check_identifier(value: object, what: str) -> str:
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
        return value
    shown = value if isinstance(value, str) else type(value).__name__
    raise InvalidRegistrationNameError(
        f"Имя {what} «{shown}» пустое или недопустимое: "
        "нужна буква или «_», дальше буквы, цифры и «_»"
    )


def _decisions(
    raw: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, tuple[BooleanReplacement, ...] | None]]:
    """Разбирает расширенные решения; опечатки и пустой массив не означают удаление."""
    if not isinstance(raw, Mapping):
        raise InvalidRegistrationNameError("Отображение реквизитов узла должно быть словарём")
    renames: dict[str, str] = {}
    actions: dict[str, tuple[BooleanReplacement, ...] | None] = {}
    seen = set()
    for key, value in raw.items():
        key = _check_mapping_key(key)
        folded = key.casefold()
        if folded in seen:
            raise InvalidRegistrationNameError(
                f"Ключ отображения «{key}» повторяется без учёта регистра"
            )
        seen.add(folded)
        if isinstance(value, str):
            renames[key] = _check_identifier(value, "реквизита узла")
        elif value is None:
            actions[folded] = None
        else:
            values = value if isinstance(value, list) else [value]
            if not values:
                raise InvalidRegistrationNameError(
                    f"«{key}»: пустой массив замен запрещён; удаление — null"
                )
            replacements = []
            targets = set()
            for row in values:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"name", "value"}
                    or not isinstance(row["value"], bool)
                ):
                    raise InvalidRegistrationNameError(
                        f"«{key}»: нужна замена с name и булевым value"
                    )
                name = _check_identifier(row["name"], "реквизита узла")
                if name.casefold() in targets:
                    raise DuplicateTargetPropertyError(f"«{key}»: цель «{name}» повторяется")
                targets.add(name.casefold())
                replacements.append(BooleanReplacement(name, row["value"]))
            actions[folded] = tuple(replacements)
    return renames, actions


def _action_key(raw: str, actions: Mapping[str, object]) -> str | None:
    tabular, head = _node_parts(raw)
    candidates = (f"[{tabular}].{head}", f"[{tabular}]") if tabular else (head,)
    return next((key.casefold() for key in candidates if key.casefold() in actions), None)


def _check_action_clashes(
    renames: Mapping[str, str],
    actions: Mapping[str, tuple[BooleanReplacement, ...] | None],
    occupied: set[str],
    card: ObjectCard | None,
) -> None:
    """Удалённое имя освобождается; новые цели сравниваются до приведения регистра."""
    final: dict[str, str] = {}
    for old in sorted(occupied):
        key = _action_key(old, actions)
        if key is not None:
            targets = tuple(r.name for r in actions[key] or ())
        else:
            targets = (_rename_plan_property(old, renames),)
        for target in targets:
            name = registration_reference(target, card).canonical
            previous = final.setdefault(name.casefold(), old.casefold())
            if previous != old.casefold():
                error = (
                    DuplicateTargetPropertyError
                    if (previous in renames or _action_key(previous, actions) is not None)
                    and (old.casefold() in renames or key is not None)
                    else PropertyNameClashError
                )
                raise error(f"Реквизиты «{previous}» и «{old}» дают одно имя «{name}»")


def _apply_actions(
    source: RegistrationRules,
    target: RegistrationRules,
    actions: Mapping[str, tuple[BooleanReplacement, ...] | None],
    card: ObjectCard | None,
) -> tuple[tuple[FilterChange, ...], tuple[RetargetNotice, ...], set[str]]:
    """Решения применяются к исходным листьям, одновременно с переименованиями.

    Константа ПРОП: ВР:332–343; РЕГ:448–484. Корень И: ОДС:2598–2641;
    массив внутри ИЛИ занимает одну позицию вложенной группой И (ВР:305–320).
    """
    changes: list[FilterChange] = []
    notices: list[RetargetNotice] = []
    applied: set[str] = set()

    def blocked(address: str, leaf: str, raw: str) -> None:
        notices.append(
            RetargetNotice(
                "registration.attribute_type",
                address,
                leaf,
                raw,
                f"{address}, {leaf}: замена «{raw}» допустима только для булевой константы "
                "(ЭтоСтрокаКонстанты=true, ТипСвойстваОбъекта=Булево) "
                "и проверенного булевого реквизита целевого плана; режим выгрузки заменить нельзя.",
                blocking=True,
            )
        )

    def rewrite(left: Node, right: Node, address: str, path: tuple[int, ...] = ()) -> None:
        items = []
        for index, (original, item) in enumerate(zip(left.items, right.items, strict=True)):
            location = (*path, index)
            if original.tag == "Группа":
                rewrite(original, item, address, location)
                if item.items or not original.items:
                    items.append(item)
                continue
            raw = str(original.get(_PLAN_PROPERTY))
            key = _action_key(raw, actions) if original.tag == "ЭлементОтбора" else None
            if key is None:
                items.append(item)
                continue
            applied.add(key)
            replacements = actions[key]
            label = _leaf_label(_PLAN_FILTER, location)
            before = (
                f"{raw} {original.get('ВидСравнения')} {original.get('СвойствоОбъекта')}".strip()
            )
            if replacements is None:
                changes.append(FilterChange(address, label, before, "Элемент удалён", "removed"))
                continue
            refs = [registration_reference(row.name, card) for row in replacements]
            if (
                original.get("ЭтоСтрокаКонстанты") is not True
                or original.get("ТипСвойстваОбъекта") != "Булево"
                or str(original.get("СвойствоОбъекта")).strip() not in {"true", "false", "1", "0"}
                or registration_reference(raw).tail
                or any(
                    ref.field is None
                    or ref.missing is not None
                    or ref.field.types != ("Булево",)
                    or ref.field.unresolved
                    for ref in refs
                )
            ):
                blocked(address, label, raw)
                items.append(item)
                continue
            new_items = []
            for replacement, ref in zip(replacements, refs, strict=True):
                if replacement.name != ref.canonical:
                    notices.append(
                        RetargetNotice(
                            "registration.property_case",
                            address,
                            label,
                            replacement.name,
                            f"{address}, {label}: имя «{replacement.name}» приведено "
                            f"к написанию метаданных «{ref.canonical}».",
                        )
                    )
                new = deepcopy(item)
                new.values[_PLAN_PROPERTY] = ref.canonical
                new.values["СвойствоОбъекта"] = "true" if replacement.value else "false"
                properties = new.child(_PLAN_TABLE)
                if properties is not None:
                    # Цель решения — реквизит шапки; разыменования и ТЧ в старой таблице не нужны.
                    row = (
                        deepcopy(properties.items[-1])
                        if properties.items
                        else Node.new("property_row", "Свойство")
                    )
                    row.values["Наименование"] = ref.canonical
                    row.values["Тип"] = "Булево"
                    properties.items[:] = [row]
                new_items.append(new)
            after = " И ".join(
                f"{row.get(_PLAN_PROPERTY)} {row.get('ВидСравнения')} {row.get('СвойствоОбъекта')}"
                for row in new_items
            )
            changes.append(FilterChange(address, label, before, after, "replaced"))
            if len(new_items) > 1 and right.get("БулевоЗначениеГруппы") == "ИЛИ":
                group = Node.new("plan_filter_group", "Группа")
                group.values["БулевоЗначениеГруппы"] = "И"
                group.items.extend(new_items)
                items.append(group)
            else:
                items.extend(new_items)
        right.items[:] = items

    for address, left, right in zip(
        pro_addresses(source.rules()), source.rules(), target.rules(), strict=True
    ):
        mode = str(left.get(_UNLOAD_MODE)).strip()
        if mode.casefold() in actions:
            applied.add(mode.casefold())
            blocked(address, _UNLOAD_MODE, mode)
        source_tree, target_tree = left.child(_PLAN_FILTER), right.child(_PLAN_FILTER)
        if source_tree is not None and target_tree is not None:
            rewrite(source_tree, target_tree, address)
    return tuple(changes), tuple(notices), applied


def _check_targets(
    mapping: Mapping[str, str], effective: Mapping[str, str], occupied: set[str]
) -> None:
    """Сравнивает приведённые имена только применимых ключей отображения."""
    present = {name.casefold() for name in occupied}
    by_target: dict[str, list[str]] = {}
    for old in mapping:
        if old.casefold() in present:
            new = effective[old.casefold()]
            by_target.setdefault(new.casefold(), []).append(old)
    for sources in by_target.values():
        if len(sources) > 1:
            listed = "», «".join(sources)
            new = effective[sources[0].casefold()]
            raise DuplicateTargetPropertyError(
                f"Реквизиты «{listed}» переименовываются в одно имя «{new}»"
            )


def _check_mapping_key(value: object) -> str:
    if isinstance(value, str) and _MAPPING_KEY.fullmatch(value):
        return value
    shown = value if isinstance(value, str) else type(value).__name__
    raise InvalidRegistrationNameError(
        f"Ключ отображения «{shown}» пустой или недопустимый: "
        "нужно имя реквизита шапки, «[ТабличнаяЧасть]» или «[ТабличнаяЧасть].Реквизит»"
    )


def _check_clash(mapping: dict[str, str], filter_names: set[str], unload_names: set[str]) -> None:
    """Замена одновременная: А→Б и Б→В не схлопывают А и Б в В.

    Режим выгрузки переписывается тем же отображением и занимает уже новое имя.
    """
    final_names: dict[str, str] = {}
    for old in sorted(filter_names | unload_names):
        new = _rename_plan_property(old, mapping)
        previous = final_names.setdefault(new.casefold(), old.casefold())
        if previous != old.casefold():
            raise PropertyNameClashError(
                f"Имя «{new}» уже используется другим реквизитом узла в этих правилах"
            )


def _node_names(rules: RegistrationRules) -> tuple[set[str], set[str]]:
    """Имена реквизитов узла в листьях отбора и в реквизите режима выгрузки."""
    filter_names: set[str] = set()
    unload_names: set[str] = set()
    for rule in rules.rules():
        mode = str(rule.values.get(_UNLOAD_MODE, "")).strip()
        if mode:
            unload_names.add(mode)
        for _path, item in _leaves(rule, _PLAN_FILTER):
            tabular, head = _node_parts(str(item.values.get(_PLAN_PROPERTY, "")))
            if tabular:
                filter_names.add(f"[{tabular}]")
                if head:
                    filter_names.add(f"[{tabular}].{head}")
            elif head:
                filter_names.add(head)
    return filter_names, unload_names


def _clone(rules: RegistrationRules) -> RegistrationRules:
    return RegistrationRules(_copy_node(rules.root), rules.style, rules.origin)


def _copy_node(node: Node) -> Node:
    return Node(
        kind=node.kind,
        tag=node.tag,
        attrs=dict(node.attrs),
        values=dict(node.values),
        children={tag: _copy_node(child) for tag, child in node.children.items()},
        items=[_copy_node(item) for item in node.items],
        text=node.text,
        unknown=[deepcopy(element) for element in node.unknown],
        leading_text=node.leading_text,
        trailing_text=node.trailing_text,
    )


def _set_plan_name(rules: RegistrationRules, plan_name: str, old_name: str) -> None:
    if plan_name == old_name:
        return
    node = rules.root.child("ПланОбмена")
    if node is None:
        node = Node.new("exchange_plan", "ПланОбмена")
        rules.root.children["ПланОбмена"] = node
    node.attrs["Имя"] = plan_name
    text = node.text or ""
    if text.strip() == f"{_PLAN_TYPE_PREFIX}{old_name}":
        node.text = plan_name


def _rename_filters(
    rules: RegistrationRules, mapping: Mapping[str, str]
) -> tuple[list[RetargetRule], set[str]]:
    applied: set[str] = set()
    stats: list[RetargetRule] = []
    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        renamed = 0
        untouched = 0
        for _path, item in _leaves(rule, _PLAN_FILTER):
            if _rename_plan_leaf(item, mapping, applied):
                renamed += 1
            else:
                untouched += 1
        untouched += len(_leaves(rule, _OBJECT_FILTER))
        stats.append(RetargetRule(address, renamed, untouched))
    return stats, applied


def _rename_unload_modes(
    rules: RegistrationRules, mapping: Mapping[str, str], applied: set[str]
) -> None:
    for rule in rules.rules():
        old = str(rule.values.get(_UNLOAD_MODE, "")).strip()
        if not old or old.casefold() not in mapping:
            continue
        applied.add(old.casefold())
        new = mapping[old.casefold()]
        if new != old:
            rule.values[_UNLOAD_MODE] = new


def _rename_plan_leaf(item: Node, mapping: Mapping[str, str], applied: set[str]) -> bool:
    old = str(item.values.get(_PLAN_PROPERTY, ""))
    tabular, head = _node_parts(old)
    if tabular:
        section_key = f"[{tabular}]".casefold()
        if section_key in mapping:
            applied.add(section_key)
        if head:
            attr_key = f"[{tabular}].{head}".casefold()
            if attr_key in mapping:
                applied.add(attr_key)
    elif head.casefold() in mapping:
        applied.add(head.casefold())
    new = _rename_plan_property(old, mapping)
    if new != old and (_PLAN_PROPERTY in item.values or new):
        item.values[_PLAN_PROPERTY] = new
    table = item.child(_PLAN_TABLE)
    if table is not None:
        names = [str(row.values.get("Наименование", "")) for row in table.items]
        renamed = _rename_row_names(old, names, mapping)
        for row, name in zip(table.items, renamed, strict=True):
            if name != row.values.get("Наименование", ""):
                row.values["Наименование"] = name
    return new != old


def _node_parts(raw: str) -> tuple[str, str]:
    """Табличная часть и первый реквизит узла. Разыменование отбрасывается."""
    tabular, attribute = _split_plan_property(raw)
    head = attribute.split(".", 1)[0] if attribute else ""
    return tabular, head


def _rename_plan_property(raw: str, mapping: Mapping[str, str]) -> str:
    tabular, attribute = _split_plan_property(raw)
    if not attribute and not tabular:
        return raw
    segments = attribute.split(".") if attribute else []
    head = segments[0] if segments else ""
    rest = segments[1:]
    if tabular:
        new_tab = mapping.get(f"[{tabular}]".casefold(), tabular)
        new_head = mapping.get(f"[{tabular}].{head}".casefold(), head) if head else ""
    else:
        new_tab = ""
        new_head = mapping.get(head.casefold(), head) if head else ""
    new_attr = ".".join([new_head, *rest]) if segments else ""
    if new_tab:
        return f"[{new_tab}].{new_attr}" if new_attr else f"[{new_tab}]"
    return new_attr


def _rename_row_names(raw: str, names: list[str], mapping: Mapping[str, str]) -> list[str]:
    """Строки таблицы в порядке писателя: ``[ТабличнаяЧасть]``, затем реквизит.

    Дальше идут разыменования — их наименования не меняются. Нет строки
    табличной части — ожидается сразу реквизит.
    """
    tabular, head = _node_parts(raw)
    expected: list[tuple[str, str]] = []
    if tabular:
        expected.append(("tabular", tabular))
    if head:
        expected.append(("attribute", head))
    result = list(names)
    cursor = 0
    for index, current in enumerate(result):
        if cursor >= len(expected):
            break
        kind, name = expected[cursor]
        if kind == "tabular" and current.casefold() in (f"[{name}]".casefold(), name.casefold()):
            new_name = mapping.get(f"[{name}]".casefold(), name)
            bracket = current.startswith("[") and current.endswith("]")
            result[index] = f"[{new_name}]" if bracket else new_name
            cursor += 1
        elif kind == "attribute" and current.casefold() == name.casefold():
            key = f"[{tabular}].{name}" if tabular else name
            result[index] = mapping.get(key.casefold(), name)
            cursor += 1
    return result


def _leaves(rule: Node, tag: str) -> list[tuple[tuple[int, ...], Node]]:
    container = rule.child(tag)
    found: list[tuple[tuple[int, ...], Node]] = []
    if container is None:
        return found

    def visit(node: Node, prefix: tuple[int, ...]) -> None:
        for index, item in enumerate(node.items):
            path = (*prefix, index)
            if item.tag == "Группа":
                visit(item, path)
            else:
                found.append((path, item))

    visit(container, ())
    return found


def _leaf_label(tag: str, path: tuple[int, ...]) -> str:
    return "/".join((tag, *(str(index) for index in path)))


def registration_node_remarks(
    rules: RegistrationRules,
    plan_name: str,
    target_plan: ObjectCard | None,
    target_properties: frozenset[str] | None = None,
) -> tuple[RetargetRemark, ...]:
    """Недостающие поля и недопустимые ссылки, включая ТЧ без поля."""
    card = target_plan
    if card is None and target_properties is not None:
        card = _names_card(plan_name, target_properties)

    remarks: list[RetargetRemark] = []
    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        for path, item in _leaves(rule, _PLAN_FILTER):
            raw = str(item.values.get(_PLAN_PROPERTY, ""))
            reference = registration_reference(raw, card)
            missing = reference.missing
            if missing is None:
                continue
            leaf = _leaf_label(_PLAN_FILTER, path)
            remarks.append(
                RetargetRemark(
                    address=address,
                    leaf=leaf,
                    property_name=missing,
                    reference=raw,
                    message=(
                        f"{address}, лист {leaf}: ссылка «{raw}» указывает на табличную часть "
                        "без поля; БСП не сможет построить условие запроса."
                        if reference.tabular and not reference.head
                        else f"{address}, лист {leaf}: реквизит узла «{missing}» "
                        f"не найден у плана обмена «{plan_name}»"
                    ),
                )
            )
        mode = str(rule.values.get(_UNLOAD_MODE, "")).strip()
        if not mode:
            continue
        missing_mode = registration_reference(mode, card).missing
        if missing_mode is None:
            continue
        remarks.append(
            RetargetRemark(
                address=address,
                leaf=_UNLOAD_MODE,
                property_name=missing_mode,
                reference=mode,
                message=(
                    f"{address}, {_UNLOAD_MODE}: реквизит узла «{missing_mode}» "
                    f"не найден у плана обмена «{plan_name}»"
                ),
            )
        )
    return tuple(remarks)


def own_attribute_remarks(rules: RegistrationRules, names: set[str]) -> tuple[RetargetRemark, ...]:
    """Булев реквизит шапки не заменяет ТЧ, ссылку, иной тип или режим выгрузки.

    Константа и тип значения: ВыгрузкаРегистрации:323–343,
    ЗагрузкаПравилРегистрацииОбъектов:448–484. Режим сравнивается с
    перечислением, а не Булево: ОбменДаннымиСобытия:2206–2212,2884–2888.
    """
    issues = []
    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        leaves = [
            (_leaf_label(_PLAN_FILTER, path), item) for path, item in _leaves(rule, _PLAN_FILTER)
        ]
        for leaf, item in leaves:
            raw = str(item.get(_PLAN_PROPERTY)).strip()
            reference = registration_reference(raw)
            tabular, head = reference.tabular, reference.head
            if not ({tabular.casefold(), head.casefold()} & names):
                continue
            reason = ""
            if tabular:
                reason = (
                    "Расширение добавляет только реквизиты шапки, а не табличные части и их поля."
                )
            elif raw != head:
                reason = "Булев реквизит нельзя разыменовывать: у него нет вложенных полей."
            elif not _boolean_comparison(item):
                reason = (
                    "Собственный булев реквизит подходит только для сравнения с булевым значением; "
                    "тип этого сравнения не подтверждён."
                )
            if reason:
                issues.append(RetargetRemark(address, leaf, head or tabular, reason, raw))
        mode = str(rule.get(_UNLOAD_MODE)).strip()
        if mode.casefold() in names:
            issues.append(
                RetargetRemark(
                    address,
                    _UNLOAD_MODE,
                    mode,
                    "Реквизит режима выгрузки сравнивается с перечислением режимов; "
                    "собственный булев реквизит для него не подходит.",
                    mode,
                )
            )
    return tuple(issues)


def _boolean_comparison(item: Node) -> bool:
    """Не исполняет алгоритм; неизвестное значение не признаётся булевым."""
    extra = {str(n.tag): n.text or "" for n in item.unknown if not len(n) and not n.attrib}
    if extra.get("Вид", "Константа") != "Константа":
        return False
    declared = str(item.get("ТипСвойстваОбъекта")).strip()
    if declared and declared != "Булево":
        return False
    if item.get("ЭтоСтрокаКонстанты") is True:
        return declared == "Булево" and str(item.get("СвойствоОбъекта")).strip().casefold() in {
            "true",
            "false",
            "истина",
            "ложь",
        }
    # Старые/внешние файлы могут хранить эти поля вне известной схемы листа.
    if extra.get("Вид") == "Константа":
        return extra.get("ЗначениеКонстанты", "").strip() in {"true", "false"}
    return declared == "Булево" and bool(item.get("СвойствоОбъекта"))


def own_attribute_covers(remark: RetargetRemark, names: set[str]) -> bool:
    """Имя без скобок и точек обозначает только реквизит шапки."""
    return (
        remark.reference.casefold() in names
        and "." not in remark.reference
        and "[" not in remark.reference
    )


def _names_card(name: str, properties: frozenset[str]) -> ObjectCard:
    """Совместимость старого входа без типов; сервис передаёт полную карточку."""
    sections = {p[1:-1].casefold() for p in properties if p.startswith("[") and p.endswith("]")}
    rows = []
    for raw in sorted(properties):
        tabular, head = _node_parts(raw)
        path = (f"{tabular}.{head}" if head else tabular) if tabular else raw
        group = not head if tabular else raw.casefold() in sections
        rows.append(ObjectProperty(path, "ТабличнаяЧасть" if group else "Реквизит", group, (), ()))
    return ObjectCard(f"ПланОбмена.{name}", f"ПланОбменаСсылка.{name}", "ПланОбмена", tuple(rows))


def registration_reference(raw: str, card: ObjectCard | None = None) -> NodeReference:
    """Проверяет вид ссылки и наличие поля без учёта регистра имён 1С.

    ТЧ без поля запрещена: ЗагрузкаПравилРегистрацииОбъектов:466–484,836–838,894.
    Хвост разыменования остаётся отдельным, его тип нельзя выводить из типа головы.
    """
    tabular, attribute = _split_plan_property(raw)
    head, dot, rest = attribute.partition(".")
    tail = dot + rest
    properties = {p.path.casefold(): p for p in card.properties} if card else {}
    section = properties.get(tabular.casefold()) if tabular else None
    path = f"{tabular}.{head}" if tabular else head
    field = properties.get(path.casefold())
    missing = None
    if tabular:
        if not head:
            missing = f"[{tabular}]"
        elif card and (section is None or section.kind != "ТабличнаяЧасть"):
            missing = tabular
        elif card and (field is None or field.is_group):
            missing = f"[{tabular}].{head}"
    elif raw and card and (field is None or field.is_group):
        missing = head or raw
    canonical = raw
    if field is not None and missing is None:
        if tabular:
            section_name, _, field_name = field.path.partition(".")
            canonical = f"[{section_name}].{field_name}{tail}"
        else:
            canonical = field.path + tail
    return NodeReference(tabular, head, tail, canonical, field, missing)


def _references(rules: RegistrationRules) -> Iterator[tuple[str, str, Node | None, str]]:
    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        for path, item in _leaves(rule, _PLAN_FILTER):
            yield address, _leaf_label(_PLAN_FILTER, path), item, str(item.get(_PLAN_PROPERTY))
        mode = str(rule.get(_UNLOAD_MODE)).strip()
        if mode:
            yield address, _UNLOAD_MODE, None, mode


def _canonical_mapping(
    rules: RegistrationRules, mapping: dict[str, str], card: ObjectCard | None
) -> tuple[dict[str, str], tuple[RetargetNotice, ...]]:
    explicit = {key.casefold(): value for key, value in mapping.items()}
    effective = dict(explicit)
    notices = []
    for address, leaf, _, raw in _references(rules):
        mapped = _rename_plan_property(raw, explicit)
        resolved = registration_reference(mapped, card)
        if resolved.canonical == mapped:
            continue
        old_tabular, old_head = _node_parts(raw)
        new_tabular, new_head = _node_parts(resolved.canonical)
        if old_tabular:
            effective[f"[{old_tabular}]".casefold()] = new_tabular
            if old_head:
                effective[f"[{old_tabular}].{old_head}".casefold()] = new_head
        else:
            effective[old_head.casefold()] = new_head
        notices.append(
            RetargetNotice(
                "registration.property_case",
                address,
                leaf,
                mapped,
                f"{address}, {leaf}: имя «{mapped}» приведено к написанию метаданных "
                f"«{resolved.canonical}».",
            )
        )
    return effective, tuple(notices)


_PRIMITIVE_TYPES = frozenset({"Булево", "Дата", "Число", "Строка"})
_PRIMITIVE_NAMES = frozenset(t.casefold() for t in _PRIMITIVE_TYPES)
_REFERENCE_TYPE = re.compile(r"[^.]+Ссылка\.[^.]+", re.IGNORECASE)
_MODE_TYPE = "ПеречислениеСсылка.РежимыВыгрузкиОбъектовОбмена"


def _known_types(types: tuple[str, ...]) -> bool:
    return bool(types) and all(
        t.casefold() in _PRIMITIVE_NAMES or _REFERENCE_TYPE.fullmatch(t) for t in types
    )


def _comparison_types(item: Node) -> tuple[str, ...]:
    declared = str(item.get("ТипСвойстваОбъекта")).strip()
    if declared:
        return tuple(t.strip() for t in declared.split(",") if t.strip())
    if item.get("ЭтоСтрокаКонстанты") is True:
        return ()
    # Совместимость внешнего синтетического формата; типизированная константа КД
    # без ТипСвойстваОбъекта остаётся неизвестной (ЗагрузкаПравилРегистрацииОбъектов:450).
    extra = {str(n.tag): n.text or "" for n in item.unknown if not len(n) and not n.attrib}
    if extra.get("Вид") != "Константа":
        return ()
    value = extra.get("ЗначениеКонстанты", "").strip()
    if value in {"true", "false"}:
        return ("Булево",)
    if re.fullmatch(r"\d{4}-\d\d-\d\d(?:T[\d:]+)?", value):
        return ("Дата",)
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
        return ("Число",)
    if value.startswith('"') and value.endswith('"'):
        return ("Строка",)
    return ()


def registration_type_notices(
    rules: RegistrationRules, card: ObjectCard | None
) -> tuple[RetargetNotice, ...]:
    """Сверяет тип существующего поля с использованием, не исполняя BSL.

    Тип сравнения: ЗагрузкаПравилРегистрацииОбъектов:448–484. Режим выгрузки:
    ОбменДаннымиСобытия:2206–2212,2884–2888 — перечисление режимов.
    """
    notices = []
    for address, leaf, item, raw in _references(rules):
        ref = registration_reference(raw, card)
        if ref.missing is not None or ref.field is None:
            continue
        node_types = ref.field.types + ref.field.unresolved
        comparison = _comparison_types(item) if item else (_MODE_TYPE,)
        known_node = _known_types(node_types) and not ref.field.unresolved
        reason = ""
        unknown = not known_node or not _known_types(comparison)
        if (
            item is not None
            and item.get("ЭтоСтрокаКонстанты") is True
            and not str(item.get("ТипСвойстваОбъекта")).strip()
        ):
            reason = "У константы не задан тип свойства; БСП не сможет загрузить правила."
        elif item is None and (ref.tabular or ref.tail):
            reason = "Режим выгрузки должен быть реквизитом шапки без разыменования."
        elif ref.tail:
            if known_node and all(t in _PRIMITIVE_TYPES for t in node_types):
                reason = "У простого типа реквизита нет вложенных полей для разыменования."
            else:
                unknown = True
        elif not unknown and not {t.casefold() for t in comparison} & {
            t.casefold() for t in node_types
        }:
            reason = "Тип реквизита узла несовместим с использованием в правиле."
        blocked = bool(reason)
        if not blocked and not unknown:
            continue
        types = (
            f"Тип узла: {', '.join(node_types) or 'неизвестен'}; "
            f"тип сравнения: {', '.join(comparison) or 'неизвестен'}."
        )
        if not reason:
            reason = "Тип сравнения не удалось проверить; перенос неполон: проверьте его вручную."
        notices.append(
            RetargetNotice(
                "registration.attribute_type" if blocked else "registration.type_unchecked",
                address,
                leaf,
                raw,
                f"{address}, {leaf}, «{raw}»: {reason} {types}",
                blocking=blocked,
                requires_acknowledgement=not blocked,
            )
        )
    return tuple(notices)


def _code_mentions(
    rules: RegistrationRules, plan_name: str, mapping: Mapping[str, str]
) -> tuple[CodeMention, ...]:
    """Старые имена в коде обработчиков и алгоритмов. Комментарии не считаются."""
    names = _code_names(plan_name, mapping)
    if not names:
        return ()
    found: list[CodeMention] = []
    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        for event in _HANDLERS:
            text = rule.values.get(event, "")
            if isinstance(text, str) and text:
                found.extend(_mentions_in(address, event, text, names))
        for path, item in _leaves(rule, _OBJECT_FILTER):
            if str(item.values.get("Вид", "")) != "АлгоритмЗначения":
                continue
            text = item.values.get("ЗначениеКонстанты", "")
            if not isinstance(text, str) or not text:
                continue
            leaf = _leaf_label(_OBJECT_FILTER, path)
            found.extend(_mentions_in(address, f"АлгоритмЗначения {leaf}", text, names))
        for path, item in _leaves(rule, _PLAN_FILTER):
            if str(item.values.get("Вид", "")) != "АлгоритмЗначения":
                continue
            text = item.values.get("ЗначениеКонстанты", "")
            if not isinstance(text, str) or not text:
                continue
            leaf = _leaf_label(_PLAN_FILTER, path)
            found.extend(_mentions_in(address, f"АлгоритмЗначения {leaf}", text, names))
    return tuple(found)


def _code_names(plan_name: str, mapping: Mapping[str, str]) -> dict[str, str]:
    """Идентификатор в коде → имя, которое попадёт в замечание."""
    names: dict[str, str] = {}
    if plan_name:
        names[plan_name.casefold()] = plan_name
    for key in mapping:
        token = _key_token(key)
        names.setdefault(token.casefold(), token)
    return names


def _key_token(key: str) -> str:
    """Имя, которое пишут в коде: у ``[ТЧ].Имя`` это ``Имя``, у ``[ТЧ]`` — ``ТЧ``."""
    if key.startswith("[") and key.endswith("]") and "]." not in key:
        return key[1:-1]
    if key.startswith("[") and "]." in key:
        return key.split("].", 1)[1]
    return key


def _mapping_keys_in_code(
    mapping: Mapping[str, str], mentions: tuple[CodeMention, ...]
) -> set[str]:
    found = {item.name.casefold() for item in mentions}
    return {key for key in mapping if _key_token(key).casefold() in found}


def _mentions_in(
    address: str, event: str, text: str, names: Mapping[str, str]
) -> list[CodeMention]:
    patterns = {
        token: re.compile(rf"(?<![\w]){re.escape(token)}(?![\w])", re.IGNORECASE) for token in names
    }
    found: list[CodeMention] = []
    for line_no, line in enumerate(_code_without_comments(text), 1):
        for token, pattern in patterns.items():
            if pattern.search(line) is None:
                continue
            label = names[token]
            found.append(
                CodeMention(
                    address=address,
                    event=event,
                    line=line_no,
                    name=label,
                    message=(
                        f"{address}, {event}, строка {line_no}: в коде осталось имя «{label}»"
                    ),
                )
            )
    return found


def _code_without_comments(text: str) -> list[str]:
    """Строки без комментариев ``//``. В строке так же, если перед ``//`` не идентификатор."""
    lines: list[str] = []
    in_string = False
    for raw in text.splitlines():
        out: list[str] = []
        index = 0
        while index < len(raw):
            if in_string:
                if raw.startswith('""', index):
                    out.append('""')
                    index += 2
                    continue
                if raw[index] == '"':
                    in_string = False
                    out.append('"')
                    index += 1
                    continue
                if raw.startswith("//", index) and _comment_mark(raw, index):
                    break
                out.append(raw[index])
                index += 1
                continue
            if raw.startswith("//", index):
                break
            if raw[index] == '"':
                in_string = True
            out.append(raw[index])
            index += 1
        lines.append("".join(out))
    return lines


def _comment_mark(line: str, index: int) -> bool:
    if index == 0:
        return True
    previous = line[index - 1]
    return not (previous.isalnum() or previous == "_")


def _assert_only_expected(
    source: RegistrationRules,
    result: RegistrationRules,
    plan_name: str,
    mapping: Mapping[str, str],
) -> None:
    problems: list[str] = []
    for change in diff_rules(source, result, include_header=True):
        if change.change != "changed" or change.field not in _ALLOWED_DIFF:
            label = change.field or change.change
            problems.append(f"{change.address or 'заголовок'}: {label}")
        elif change.field == "ПланОбмена.Имя" and change.new != plan_name:
            problems.append(f"имя плана в диффе «{change.new}»")
        elif change.field == "ПланОбмена" and change.new != plan_name:
            problems.append(f"текст плана в диффе «{change.new}»")
        elif change.field == _UNLOAD_MODE:
            previous = change.old or ""
            if change.new != mapping.get(previous.casefold(), previous):
                problems.append(f"режим выгрузки в диффе «{change.new}»")
    problems.extend(_snapshot_problems(source, result, mapping))
    problems.extend(_leaf_problems(source, result, mapping))
    if problems:
        shown = "; ".join(problems[:8])
        raise RetargetInvariantError(
            "Перенацеливание изменило не только имя плана и реквизиты узла: " + shown
        )


def _snapshot_problems(
    source: RegistrationRules, result: RegistrationRules, mapping: Mapping[str, str]
) -> list[str]:
    left = snapshot_registration(source)
    right = snapshot_registration(result)
    if [item["address"] for item in left] != [item["address"] for item in right]:
        return ["снимки правил разошлись по адресам"]
    problems: list[str] = []
    for old, new in zip(left, right, strict=True):
        address = str(old["address"])
        if old["handlers"] != new["handlers"] or old["object"] != new["object"]:
            problems.append(f"{address}: снимок обработчиков или отбора объекта")
        expected = [_rename_leaf_key(str(key), mapping) for key in old["plan"]]
        if expected != list(new["plan"]):
            problems.append(f"{address}: снимок отбора плана")
    return problems


def _rename_leaf_key(key: str, mapping: Mapping[str, str]) -> str:
    parts: list[str] = []
    for part in key.split(";"):
        tag, separator, value = part.partition("=")
        if separator and tag == _PLAN_PROPERTY:
            value = _rename_plan_property(value, mapping)
        parts.append(f"{tag}={value}" if separator else part)
    return ";".join(parts)


def _leaf_problems(
    source: RegistrationRules, result: RegistrationRules, mapping: Mapping[str, str]
) -> Iterator[str]:
    source_rules = source.rules()
    result_rules = result.rules()
    if len(source_rules) != len(result_rules):
        yield "число правил"
        return
    for left, right in zip(source_rules, result_rules, strict=True):
        for name in _HANDLERS:
            if left.values.get(name) != right.values.get(name):
                yield f"{name}: текст обработчика"
        if _unknown_blobs(left) != _unknown_blobs(right):
            yield "неизвестные узлы"
        for tag in (_PLAN_FILTER, _OBJECT_FILTER):
            if _shape(left.child(tag)) != _shape(right.child(tag)):
                yield f"{tag}: группы"
        yield from _compare_tree(left, right, _PLAN_FILTER, mapping)
        yield from _compare_tree(left, right, _OBJECT_FILTER, mapping)


def _compare_tree(left: Node, right: Node, tag: str, mapping: Mapping[str, str]) -> Iterator[str]:
    source_leaves = _leaves(left, tag)
    result_leaves = _leaves(right, tag)
    if len(source_leaves) != len(result_leaves):
        yield f"{tag}: число листьев"
        return
    plan = tag == _PLAN_FILTER
    for (path, source), (_, result) in zip(source_leaves, result_leaves, strict=True):
        label = _leaf_label(tag, path)
        if plan:
            old = str(source.values.get(_PLAN_PROPERTY, ""))
            new = str(result.values.get(_PLAN_PROPERTY, ""))
            if new != _rename_plan_property(old, mapping):
                yield f"{label}: свойство плана"
            if _without(source, _PLAN_PROPERTY) != _without(result, _PLAN_PROPERTY):
                yield f"{label}: поля листа плана"
            yield from _compare_table(source, result, _PLAN_TABLE, label, old, mapping)
            yield from _compare_table(source, result, _OBJECT_TABLE, label, "", None)
        elif source.values != result.values:
            yield f"{label}: поля листа объекта"
        else:
            yield from _compare_table(source, result, _OBJECT_TABLE, label, "", None)


def _without(node: Node, skip: str) -> dict[str, object]:
    return {key: value for key, value in node.values.items() if key != skip}


def _compare_table(
    source: Node,
    result: Node,
    tag: str,
    label: str,
    raw: str,
    mapping: Mapping[str, str] | None,
) -> Iterator[str]:
    if not tag:
        return
    left = source.child(tag)
    right = result.child(tag)
    left_rows = left.items if left is not None else []
    right_rows = right.items if right is not None else []
    if len(left_rows) != len(right_rows):
        yield f"{label}: число строк {tag}"
        return
    names = [str(row.values.get("Наименование", "")) for row in left_rows]
    expected = _rename_row_names(raw, names, mapping) if mapping is not None else names
    for index, (old, new) in enumerate(zip(left_rows, right_rows, strict=True)):
        if str(new.values.get("Наименование", "")) != expected[index]:
            yield f"{label}: наименование строки {tag}"
        if old.values.get("Тип") != new.values.get("Тип"):
            yield f"{label}: тип строки {tag}"
        if old.values.get("Вид") != new.values.get("Вид"):
            yield f"{label}: вид строки {tag}"


def _shape(node: Node | None) -> tuple[object, ...]:
    """Порядок листьев и операторы групп. Сами имена свойств сюда не входят."""
    if node is None:
        return ()
    items: list[object] = []
    for item in node.items:
        if item.tag == "Группа":
            operator = str(item.values.get("БулевоЗначениеГруппы", ""))
            items.append((item.tag, operator, _shape(item)))
        else:
            items.append((item.tag,))
    return tuple(items)


def _unknown_blobs(node: Node) -> tuple[bytes, ...]:
    found = [etree.tostring(element) for element in node.unknown]
    for child in node.children.values():
        found.extend(_unknown_blobs(child))
    for item in node.items:
        found.extend(_unknown_blobs(item))
    return tuple(found)
