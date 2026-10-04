"""Правила регистрации из состава плана обмена (спецификация `rules-authoring`, design.md Д9).

Писатель КД — `reference/kd2-cfg/DataProcessors/ВыгрузкаРегистрации/Ext/ObjectModule.bsl`:
заголовок (135–153), план обмена (89–101), конфигурация (120–131), состав (103–116, 660–677),
ПРО (197–256), отборы (323–400), таблица свойств (402–420), обработчики только если непусты
(597–605). Версия формата — «2.01» (686). Читатель БСП
(`DataProcessors/ЗагрузкаПравилРегистрацииОбъектов/Ext/ObjectModule.bsl`) блок
`СоставПланаОбмена` пропускает (222–226), но КД его пишет — поэтому пишем и мы (Д9).
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules
from kd2_rules_mcp.kd2.xmlstyle import KD_STYLE
from kd2_rules_mcp.structures.db import read_meta
from kd2_rules_mcp.structures.queries import (
    MAX_LIMIT,
    NotFound,
    exchange_plan_content,
    find_object,
)

_FORMAT_VERSION = "2.01"  # ВыгрузкаРегистрации:686


@dataclass(frozen=True, slots=True)
class FilterProperty:
    """Строка таблицы свойств отбора (ВыгрузкаРегистрации:411–418).

    `name` — `Наименование`, `type_name` — `Тип`, `kind` — `Вид`.
    У табличной части `Тип` пустой: писатель его не выводит (`ДобавитьЭлемент`, 435–439).
    """

    name: str
    type_name: str = ""
    kind: str = ""


@dataclass(frozen=True, slots=True)
class PlanFilter:
    """Элемент отбора по свойствам плана обмена (ВыгрузкаРегистрации:323–345)."""

    plan_property: str = ""
    object_property: str = ""
    property_type: str = ""
    comparison: str = ""
    constant: bool = False
    object_properties: tuple[FilterProperty, ...] = ()
    plan_properties: tuple[FilterProperty, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanFilterGroup:
    """Группа отбора по плану обмена (ВыгрузкаРегистрации:347–356).

    `operator` — `БулевоЗначениеГруппы` (`И` или `ИЛИ`).
    """

    operator: str
    items: tuple[PlanFilter | PlanFilterGroup, ...] = ()


@dataclass(frozen=True, slots=True)
class ObjectFilter:
    """Элемент отбора по свойствам объекта (ВыгрузкаРегистрации:362–385).

    `element_kind` — тег `Вид`: писатель выводит его, только если у дерева есть колонка
    `ВидЭлементаОтбора` (373–377). Пустая строка — тег не пишется.
    `constant_value` — `ЗначениеКонстанты` (`ДобавитьЭлементСПустымЗначением`, 379–380, 455–459).
    """

    object_property: str = ""
    property_type: str = ""
    comparison: str = ""
    element_kind: str = ""
    constant_value: str = ""
    object_properties: tuple[FilterProperty, ...] = ()


@dataclass(frozen=True, slots=True)
class ObjectFilterGroup:
    """Группа отбора по объекту (ВыгрузкаРегистрации:387–398)."""

    operator: str
    items: tuple[ObjectFilter | ObjectFilterGroup, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistrationObject:
    """ПРО, которое задаёт агент. Имена полей соответствуют тегам писателя.

    `metadata_name` — `Справочник.Контрагенты` или имя типа КД (`СправочникСсылка.Контрагенты`).
    `name` — `Наименование` (пустое — синоним объекта, `глНаименованиеПРО`,
    `ОбщегоНазначения:125–127`). `code` — `Код` (пустой — очередной код длины 9,
    `ПравилаРегистрацииОбъектов.xml`, `CodeLength`). `unload_mode` — `РеквизитРежимаВыгрузки`.
    `disabled` — атрибут `Отключить`, `valid` — `Валидное` (204).
    Обработчики: `before_processing` — `ПередОбработкой`, `on_processing` — `ПриОбработке`,
    `on_processing_extra` — `ПриОбработкеДополнительный`, `after_processing` — `ПослеОбработки`
    (253–256, пишутся только непустые, 597–605).
    """

    metadata_name: str
    code: str = ""
    name: str = ""
    description: str = ""
    comment: str = ""
    unload_mode: str = ""
    disabled: bool = False
    valid: bool = True
    # None — список не передавали (при правке проекта отбор не меняется).
    # Пустой кортеж — отбора нет; при правке проекта такой отбор снимается.
    plan_filters: tuple[PlanFilter | PlanFilterGroup, ...] | None = None
    object_filters: tuple[ObjectFilter | ObjectFilterGroup, ...] | None = None
    before_processing: str = ""
    on_processing: str = ""
    on_processing_extra: str = ""
    after_processing: str = ""


@dataclass(slots=True)
class RegistrationBuild:
    """Документ правил регистрации и предупреждения сборки."""

    document: RegistrationRules
    warnings: list[str]


# Имена перечисления ВидыСравнения (reference/kd2-cfg/Enums/ВидыСравнения.xml).
# Читатель неизвестное значение оставляет оператором «=»
# (ЗагрузкаПравилРегистрацииОбъектов:993-1004).
_COMPARISONS = frozenset(
    {"Равно", "НеРавно", "Больше", "БольшеИлиРавно", "Меньше", "МеньшеИлиРавно"}
)
# Имена перечисления БулевыОперации (reference/kd2-cfg/Enums/БулевыОперации.xml).
_OPERATORS = frozenset({"И", "ИЛИ"})


def parse_registration_object(item: Mapping[str, Any]) -> RegistrationObject:
    """Объект правил регистрации из словаря `registration_build`.

    Плоский список отборов — прежнее соединение через «И» (корень отбора у читателя
    всегда «И», ЗагрузкаПравилРегистрацииОбъектов:1203). Группа — `operator` «И» или
    «ИЛИ» и `items`. Читатель группы рекурсивен и глубину не ограничивает
    (ЗагрузкаПравилРегистрацииОбъектов:597-599, :638-640). Неизвестный вид сравнения
    и неизвестный вид группы — `ValueError`: иначе читатель молча подменит оператор.
    """
    name = str(item.get("metadata_name", "")).strip()
    if not name:
        raise ValueError("У объекта правил регистрации нет «metadata_name»")
    return RegistrationObject(
        metadata_name=name,
        code=str(item.get("code", "")),
        name=str(item.get("name", "")),
        comment=str(item.get("comment", "")),
        unload_mode=str(item.get("unload_mode", "")),
        plan_filters=_filter_list(item, "plan_filters", _plan_entry),
        object_filters=_filter_list(item, "object_filters", _object_entry),
    )


def _filter_list[T](
    item: Mapping[str, Any], key: str, parse: Callable[[Mapping[str, Any]], T]
) -> tuple[T, ...] | None:
    """Ключа нет — None (отбор не задан). Пустой список и null — снять отбор."""
    if key not in item:
        return None
    raw = item[key]
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"«{key}» должен быть списком условий и групп")
    return tuple(parse(_as_mapping(entry, key)) for entry in raw)


def _as_mapping(entry: object, where: str) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise ValueError(f"Элемент «{where}» должен быть объектом")
    return entry


def _plan_entry(entry: Mapping[str, Any]) -> PlanFilter | PlanFilterGroup:
    if "operator" in entry:
        return _group(entry, "plan_filters", _plan_entry, PlanFilterGroup)
    comparison = str(entry.get("comparison", ""))
    _check_comparison(comparison, "отбора по свойствам плана обмена")
    constant = _flag(entry.get("constant", False), "constant")
    return PlanFilter(
        plan_property=str(entry.get("plan_property", "")),
        object_property=str(entry.get("object_property", "")),
        property_type=str(entry.get("property_type", "")),
        comparison=comparison,
        constant=constant,
    )


def _object_entry(entry: Mapping[str, Any]) -> ObjectFilter | ObjectFilterGroup:
    if "operator" in entry:
        return _group(entry, "object_filters", _object_entry, ObjectFilterGroup)
    comparison = str(entry.get("comparison", ""))
    _check_comparison(comparison, "отбора по свойствам объекта")
    return ObjectFilter(
        object_property=str(entry.get("object_property", "")),
        property_type=str(entry.get("property_type", "")),
        comparison=comparison,
        constant_value=str(entry.get("constant_value", "")),
    )


def _group[T](
    entry: Mapping[str, Any],
    where: str,
    parse: Callable[[Mapping[str, Any]], T],
    make: Callable[[str, tuple[T, ...]], T],
) -> T:
    operator = str(entry.get("operator", ""))
    _check_operator(operator)
    raw = entry.get("items")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"Группа отбора «{operator}» в «{where}» без элементов не принимается")
    items = tuple(parse(_as_mapping(child, where)) for child in raw)
    return make(operator, items)


def _flag(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"«{name}» должен быть логическим значением")


def _check_comparison(value: str, where: str) -> None:
    """Пустой вид писатель не выводит; неизвестный читатель превращает в «=»."""
    if value and value not in _COMPARISONS:
        allowed = ", ".join(sorted(_COMPARISONS))
        raise ValueError(
            f"Вид сравнения «{value}» {where} не принимается: читатель запишет его"
            f" в запрос как «=». Допустимы: {allowed}"
        )


def _check_operator(value: str) -> None:
    """«И» и «ИЛИ» — имена БулевыОперации. Иное слово попадает в текст запроса плана
    (ЗагрузкаПравилРегистрацииОбъектов:744) либо считается «ИЛИ» у отбора объекта (:646).
    """
    if value not in _OPERATORS:
        shown = value or "пусто"
        raise ValueError(
            f"Вид группы отбора «{shown}» не принимается: нужно «И» или «ИЛИ». "
            "Другое значение читатель отбора по плану подставит в запрос как есть, "
            "а читатель отбора по объекту сочтёт «ИЛИ»"
        )


@dataclass(frozen=True, slots=True)
class _Meta:
    """Объект структуры: полное имя `Вид.Имя`, имя типа КД, вид, имя, синоним."""

    full_name: str
    type_name: str
    kind: str
    name: str
    synonym: str


def build_registration_rules(
    connection: sqlite3.Connection,
    exchange_plan: str,
    *,
    exchange_rules: ExchangeRules | None = None,
    objects: Sequence[RegistrationObject] | None = None,
    name: str = "",
    comment: str = "",
    created_at: datetime | None = None,
    identifier: str = "",
) -> RegistrationBuild:
    """Собирает правила регистрации плана обмена `exchange_plan` по структуре источника.

    `exchange_plan` — имя плана (`ОбменЗарплата3Бухгалтерия3`), `ПланОбмена.Имя` или имя типа КД.
    `objects` — явный выбор агента. Без него берутся объекты ПВД `exchange_rules`:
    тип — `ОбъектВыборки`, а если его нет — `Источник` ПКО по `КодПравилаКонвертации`
    (ВыгрузкаКонвертации:919–921; при создании ПВД из ПКО объект выборки равен источнику,
    `СозданиеПравилВыгрузкиДанных:108`, `ПравилаВыгрузкиДанных:68`).
    Объект, указанный явно, но отсутствующий в составе, попадает в ПРО с предупреждением.
    Объект из выбора по умолчанию вне состава и объект, которого нет в структуре, в ПРО
    не попадают — о них предупреждение.
    """
    if connection.row_factory is not sqlite3.Row:
        connection.row_factory = sqlite3.Row
    warnings: list[str] = []
    plan = _find_plan(connection, exchange_plan)
    if plan is None:
        warnings.append(f"План обмена «{exchange_plan}» не найден в структуре")
    content = _plan_content(connection, plan.full_name) if plan is not None else []
    content_types = {type_name for type_name, _flag in content}
    specs, from_rules = _selection(exchange_rules, objects, warnings)
    rules = _rules(
        connection,
        specs,
        from_rules,
        content_types,
        plan.name if plan else exchange_plan,
        warnings,
        plan,
        _PropertyTables(connection),
    )
    document = _document(
        connection, plan, exchange_plan, content, rules, name, comment, created_at, identifier
    )
    return RegistrationBuild(document, warnings)


def _selection(
    exchange_rules: ExchangeRules | None,
    objects: Sequence[RegistrationObject] | None,
    warnings: list[str],
) -> tuple[list[RegistrationObject], bool]:
    """Явный список или объекты ПВД. Второй флаг — выбор по правилам, а не агентом."""
    if objects is not None:
        return list(objects), False
    if exchange_rules is None:
        warnings.append(
            "Не переданы объекты и правила обмена, правила регистрации объектов не созданы"
        )
        return [], True
    return _from_rules(exchange_rules, warnings), True


def _from_rules(rules: ExchangeRules, warnings: list[str]) -> list[RegistrationObject]:
    """Объекты ПВД в порядке правил, без повторов.

    `ОбъектВыборки` — наименование объекта-источника (ВыгрузкаКонвертации:921).
    Пустое — `Источник` ПКО с тем же кодом (919 и заполнение ПВД из ПКО, 108 / 68).
    """
    sources = _pko_sources(rules)
    seen: set[str] = set()
    chosen: list[RegistrationObject] = []
    unresolved: list[str] = []
    for rule in rules.pvd():
        type_name = str(rule.get("ОбъектВыборки")).strip()
        if not type_name:
            code = str(rule.get("КодПравилаКонвертации")).strip()
            type_name = sources.get(code, "")
            if not type_name:
                unresolved.append(_unresolved_pvd(rule.code.strip(), code))
                continue
        if type_name in seen:
            continue
        seen.add(type_name)
        chosen.append(RegistrationObject(type_name))
    if unresolved:
        warnings.append(
            "Не удалось определить объект правила выгрузки, правило регистрации не создано: "
            + ", ".join(unresolved)
        )
    return chosen


def _pko_sources(rules: ExchangeRules) -> dict[str, str]:
    """Код ПКО → `Источник` (наименование типа, ВыгрузкаКонвертации:843)."""
    sources: dict[str, str] = {}
    for rule in rules.pko():
        code = rule.code.strip()
        source = str(rule.get("Источник")).strip()
        if code and source and code not in sources:
            sources[code] = source
    return sources


def _unresolved_pvd(code: str, conversion_code: str) -> str:
    shown = code or "без кода"
    if conversion_code:
        return f"ПВД «{shown}» (нет ПКО «{conversion_code}»)"
    return f"ПВД «{shown}» (нет объекта выборки)"


def _rules(
    connection: sqlite3.Connection,
    specs: Sequence[RegistrationObject],
    from_rules: bool,
    content_types: set[str],
    plan_name: str,
    warnings: list[str],
    plan: _Meta | None,
    tables: _PropertyTables,
) -> list[Node]:
    missing: list[str] = []
    outside_default: list[str] = []
    outside_explicit: list[str] = []
    seen: set[str] = set()
    nodes: list[Node] = []
    codes = _Codes([spec.code for spec in specs if spec.code])
    for spec in specs:
        meta = _find_object(connection, spec.metadata_name)
        if meta is None:
            missing.append(spec.metadata_name)
            continue
        if meta.full_name in seen:
            continue
        seen.add(meta.full_name)
        if meta.type_name not in content_types:
            if from_rules:
                outside_default.append(meta.full_name)
                continue
            outside_explicit.append(meta.full_name)
        nodes.append(_pro(_with_tables(tables, spec, meta, plan), meta, codes.next(spec.code)))
    _warn_lists(warnings, missing, outside_default, outside_explicit, plan_name)
    return nodes


def _warn_lists(
    warnings: list[str],
    missing: list[str],
    outside_default: list[str],
    outside_explicit: list[str],
    plan_name: str,
) -> None:
    if missing:
        warnings.append("Нет в структуре, правила регистрации не созданы: " + ", ".join(missing))
    if outside_default:
        warnings.append(
            f"Не входят в состав плана обмена «{plan_name}», правила регистрации не созданы: "
            + ", ".join(outside_default)
        )
    if outside_explicit:
        listed = ", ".join(f"«{name}»" for name in outside_explicit)
        subject = "Объект" if len(outside_explicit) == 1 else "Объекты"
        warnings.append(
            f"{subject} {listed} нужно добавить в состав плана обмена «{plan_name}» в конфигурации"
        )


class _Codes:
    """Очередные коды ПРО длины 9, не пересекающиеся с уже заданными."""

    def __init__(self, used: list[str]) -> None:
        self._used = set(used)
        self._number = 1

    def next(self, explicit: str) -> str:
        if explicit:
            return explicit
        while True:
            code = f"{self._number:09d}"
            self._number += 1
            if code not in self._used:
                self._used.add(code)
                return code


def _pro(spec: RegistrationObject, meta: _Meta, code: str) -> Node:
    """ПРО в порядке тегов писателя (ВыгрузкаРегистрации:197–256)."""
    rule = Node.new("pro", "Правило")
    rule.attrs["Отключить"] = spec.disabled
    rule.attrs["Валидное"] = spec.valid
    _put(rule, "Код", code)
    _put(rule, "Наименование", spec.name or meta.synonym or meta.name)
    _put(rule, "Описание", spec.description)
    _put(rule, "Комментарий", spec.comment)
    _put(rule, "ОбъектНастройки", meta.type_name)
    _put(rule, "ОбъектМетаданныхИмя", meta.full_name)
    _put(rule, "ОбъектМетаданныхТип", meta.kind)
    _put(rule, "РеквизитРежимаВыгрузки", spec.unload_mode)
    _attach(
        rule,
        "plan_filter",
        "ОтборПоСвойствамПланаОбмена",
        spec.plan_filters,
        _plan_filter,
    )
    _attach(
        rule,
        "object_filter",
        "ОтборПоСвойствамОбъекта",
        spec.object_filters,
        _object_filter,
    )
    _put(rule, "ПередОбработкой", spec.before_processing)
    _put(rule, "ПриОбработке", spec.on_processing)
    _put(rule, "ПриОбработкеДополнительный", spec.on_processing_extra)
    _put(rule, "ПослеОбработки", spec.after_processing)
    return rule


def _attach[T](
    rule: Node,
    kind_name: str,
    tag: str,
    items: Sequence[T] | None,
    build: Callable[[T], Node],
) -> None:
    """Вложенный отбор пишется, только если агент задал элементы.

    None и пустой список — контейнер не заполняется. Писатель пустое дерево всё равно
    открывает: сериализатор выводит тег по политике ALWAYS.
    """
    if not items:
        return
    node = Node.new(kind_name, tag)
    node.items.extend(build(item) for item in items)
    rule.children[tag] = node


def _plan_filter(item: PlanFilter | PlanFilterGroup) -> Node:
    if isinstance(item, PlanFilterGroup):
        _check_operator(item.operator)
        if not item.items:
            raise ValueError("Группа отбора без элементов не принимается")
        group = Node.new("plan_filter_group", "Группа")
        _put(group, "БулевоЗначениеГруппы", item.operator)
        group.items.extend(_plan_filter(child) for child in item.items)
        return group
    _check_comparison(item.comparison, "отбора по свойствам плана обмена")
    node = Node.new("plan_filter_item", "ЭлементОтбора")
    node.values["ЭтоСтрокаКонстанты"] = item.constant
    _put(node, "ТипСвойстваОбъекта", item.property_type)
    _put(node, "СвойствоПланаОбмена", item.plan_property)
    _put(node, "ВидСравнения", item.comparison)
    _put(node, "СвойствоОбъекта", item.object_property)
    _property_table(node, "ТаблицаСвойствОбъекта", item.object_properties)
    _property_table(node, "ТаблицаСвойствПланаОбмена", item.plan_properties)
    return node


def _object_filter(item: ObjectFilter | ObjectFilterGroup) -> Node:
    if isinstance(item, ObjectFilterGroup):
        _check_operator(item.operator)
        if not item.items:
            raise ValueError("Группа отбора без элементов не принимается")
        group = Node.new("object_filter_group", "Группа")
        _put(group, "БулевоЗначениеГруппы", item.operator)
        group.items.extend(_object_filter(child) for child in item.items)
        return group
    _check_comparison(item.comparison, "отбора по свойствам объекта")
    node = Node.new("object_filter_item", "ЭлементОтбора")
    _put(node, "ТипСвойстваОбъекта", item.property_type)
    _put(node, "ВидСравнения", item.comparison)
    _put(node, "СвойствоОбъекта", item.object_property)
    _put(node, "Вид", item.element_kind)
    _put(node, "ЗначениеКонстанты", item.constant_value)
    _property_table(node, "ТаблицаСвойствОбъекта", item.object_properties)
    return node


def _property_table(node: Node, tag: str, rows: Sequence[FilterProperty]) -> None:
    """Таблица свойств пишется, только если в ней есть строки (ВыгрузкаРегистрации:402–406)."""
    if not rows:
        return
    table = Node.new("property_table", tag)
    for row in rows:
        prop = Node.new("property_row", "Свойство")
        _put(prop, "Наименование", row.name)
        _put(prop, "Тип", row.type_name)
        _put(prop, "Вид", row.kind)
        table.items.append(prop)
    node.children[tag] = table


def _put(node: Node, tag: str, value: str) -> None:
    """Непустая строка. Пустую писатель не выводит (`ДобавитьЭлемент`, 435–439)."""
    if value:
        node.values[tag] = value


def _document(
    connection: sqlite3.Connection,
    plan: _Meta | None,
    requested_plan: str,
    content: list[tuple[str, bool]],
    rules: list[Node],
    name: str,
    comment: str,
    created_at: datetime | None,
    identifier: str,
) -> RegistrationRules:
    """Корень `ПравилаРегистрации` в порядке писателя (67–87, реквизиты 135–153)."""
    meta = read_meta(connection)
    config_name = meta.get("config_name", "")
    plan_synonym = plan.synonym if plan is not None else ""
    root = Node.new("registration_rules", "ПравилаРегистрации")
    root.values["ВерсияФормата"] = _FORMAT_VERSION
    root.values["Ид"] = identifier or str(uuid.uuid4())
    root.values["Наименование"] = name or _registration_name(config_name, plan_synonym)
    root.values["ДатаВремяСоздания"] = (created_at or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
    root.children["ПланОбмена"] = _exchange_plan(plan, requested_plan)
    root.children["Конфигурация"] = _configuration(
        config_name, meta.get("config_version", ""), meta.get("config_synonym", "")
    )
    _put(root, "Комментарий", comment)
    root.children["СоставПланаОбмена"] = _content(content)
    listed = Node.new("pro_list", "ПравилаРегистрацииОбъектов")
    listed.items.extend(rules)
    root.children["ПравилаРегистрацииОбъектов"] = listed
    return RegistrationRules(root, KD_STYLE)


def _registration_name(config_name: str, plan_synonym: str) -> str:
    """Имя регистрации по умолчанию (`глНаименованиеРегистрации`, ОбщегоНазначения:153–159)."""
    if config_name:
        return f"{config_name}: {plan_synonym}"
    return plan_synonym


def _exchange_plan(plan: _Meta | None, requested: str) -> Node:
    """`<ПланОбмена Имя="Имя">Наименование</ПланОбмена>` (89–101).

    Наименование объекта плана в КД — имя типа (`ПланОбменаСсылка.Имя`).
    """
    node = Node.new("exchange_plan", "ПланОбмена")
    node.attrs["Имя"] = plan.name if plan is not None else _bare_plan_name(requested)
    node.text = plan.type_name if plan is not None else ""
    return node


def _configuration(name: str, version: str, synonym: str) -> Node:
    """Конфигурация (120–131). Версия платформы в структуре не хранится.

    Писатель берёт её из перечисления `Приложения` и для неизвестного значения пишет пустую
    строку (643–650). Атрибут выводится всегда (`УстановитьАтрибут`, 475–485).
    """
    node = Node.new("config", "Конфигурация")
    node.attrs["ВерсияПлатформы"] = ""
    node.attrs["ВерсияКонфигурации"] = version
    node.attrs["СинонимКонфигурации"] = synonym
    node.text = name
    return node


def _content(items: list[tuple[str, bool]]) -> Node:
    """Состав плана: `Тип` и `Авторегистрация` (103–116).

    Ложь тоже пишется: схема выводит булево всегда.
    """
    node = Node.new("plan_content", "СоставПланаОбмена")
    for type_name, flag in items:
        item = Node.new("plan_content_item", "Элемент")
        _put(item, "Тип", type_name)
        item.values["Авторегистрация"] = flag
        node.items.append(item)
    return node


def _plan_content(connection: sqlite3.Connection, full_name: str) -> list[tuple[str, bool]]:
    """Типы состава в порядке структуры и признак авторегистрации. Повтор типа не пишется."""
    result: list[tuple[str, bool]] = []
    seen: set[str] = set()
    offset = 0
    while True:
        page = exchange_plan_content(connection, full_name, offset=offset, limit=MAX_LIMIT)
        if isinstance(page, NotFound):
            return result
        for item in page.items:
            flag = bool(item["autoregistration"])
            for type_name in _content_types(item):
                if type_name in seen:
                    continue
                seen.add(type_name)
                result.append((type_name, flag))
        if not page.has_more:
            return result
        offset += len(page.items)


def _content_types(item: dict[str, Any]) -> list[str]:
    """Имена типов элемента состава; неразрешённый тип тоже пишется (как читает проверка 5.3)."""
    types = item.get("types")
    names = [str(name) for name in types] if isinstance(types, list) and types else []
    if names:
        return names
    unresolved = item.get("unresolved")
    if isinstance(unresolved, list):
        return [str(name) for name in unresolved]
    return []


def _find_plan(connection: sqlite3.Connection, name: str) -> _Meta | None:
    """План по полному имени, имени типа или голому имени метаданных."""
    found = _find_object(connection, name)
    if found is not None and found.kind == "ПланОбмена":
        return found
    row = connection.execute(
        "SELECT kind, name, type_name, synonym FROM objects WHERE is_group = 0 "
        "AND kind = 'ПланОбмена' AND name = ?",
        (_bare_plan_name(name),),
    ).fetchone()
    return _meta(row) if row is not None else None


def _bare_plan_name(name: str) -> str:
    for prefix in ("ПланОбменаСсылка.", "ПланОбмена."):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _find_object(connection: sqlite3.Connection, name: str) -> _Meta | None:
    """Объект по `Вид.Имя` или по имени типа КД — тот же поиск, что у запросов структуры."""
    row = find_object(connection, name)
    return None if isinstance(row, NotFound) else _meta(row)


def _meta(row: sqlite3.Row) -> _Meta:
    kind = str(row["kind"])
    name = str(row["name"])
    return _Meta(f"{kind}.{name}", str(row["type_name"]), kind, name, str(row["synonym"]))


class _PropertyTables:
    """Таблицы свойств отбора, как их пишет КД (ВыгрузкаРегистрации:339-343, :382-383).

    Читатель БСП таблицы пропускает, но типовой макет их содержит: без них построенная
    группа не совпадёт с макетом побайтово. Строка берётся из структуры, если агент
    её не задал. Неразрешённый путь таблицу не получает — условие при этом пишется.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._props: dict[str, dict[str, tuple[str, bool, tuple[str, ...]]]] = {}
        self._by_type: dict[str, str | None] = {}

    def rows(self, full_name: str, raw: str, *, plan: bool) -> tuple[FilterProperty, ...]:
        """Цепочка `Наименование/Тип/Вид` по пути свойства. Пусто — путь не разобран."""
        if not raw:
            return ()
        tabular = ""
        attribute = raw
        if plan:
            tabular, attribute = _split_plan_property(raw)
        built: list[FilterProperty] = []
        current = full_name
        prefix = ""
        if tabular:
            field = self._props_of(current).get(tabular)
            if field is None or field[0] != "ТабличнаяЧасть":
                return ()
            # Имя табличной части в макете — в скобках, без типа
            # (ДобавитьЭлемент пустой Тип не пишет).
            built.append(FilterProperty(f"[{tabular}]", "", "ТабличнаяЧасть"))
            prefix = tabular
        segments = [part for part in attribute.split(".") if part]
        if not segments:
            return ()
        for index, segment in enumerate(segments):
            path = f"{prefix}.{segment}" if prefix else segment
            field = self._props_of(current).get(path)
            if field is None:
                return ()
            kind, is_group, types = field
            type_name = ""
            if not is_group:
                if len(types) != 1:
                    return ()
                type_name = types[0]
            built.append(FilterProperty(segment, type_name, kind))
            if index == len(segments) - 1:
                break
            if is_group:
                prefix = path
                continue
            owner = self._full_by_type(types[0])
            if owner is None:
                return ()
            current = owner
            prefix = ""
        return tuple(built)

    def _props_of(self, full_name: str) -> dict[str, tuple[str, bool, tuple[str, ...]]]:
        cached = self._props.get(full_name)
        if cached is not None:
            return cached
        kind, _, name = full_name.partition(".")
        rows = self._connection.execute(
            "SELECT p.path, p.kind, p.is_group, IFNULL(ts.types, '') AS types "
            "FROM properties AS p JOIN objects AS o ON o.id = p.object_id "
            "LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id "
            "WHERE o.kind = ? AND o.name = ?",
            (kind, name),
        ).fetchall()
        loaded: dict[str, tuple[str, bool, tuple[str, ...]]] = {}
        for row in rows:
            types = tuple(part for part in str(row["types"]).split("\n") if part)
            loaded[str(row["path"])] = (str(row["kind"]), bool(row["is_group"]), types)
        self._props[full_name] = loaded
        return loaded

    def _full_by_type(self, type_name: str) -> str | None:
        if type_name not in self._by_type:
            row = self._connection.execute(
                "SELECT kind, name FROM objects WHERE type_name = ? AND is_group = 0",
                (type_name,),
            ).fetchone()
            self._by_type[type_name] = None if row is None else f"{row['kind']}.{row['name']}"
        return self._by_type[type_name]


def _split_plan_property(raw: str) -> tuple[str, str]:
    """`[Организации].Организация` → (`Организации`, `Организация`); без скобок — шапка.

    Как читатель (ЗагрузкаПравилРегистрацииОбъектов:471-484): имя табличной части
    между скобками, реквизит — после `].`.
    """
    open_at = raw.find("[")
    if open_at < 0:
        return "", raw
    close_at = raw.find("]", open_at + 1)
    if close_at < 0:
        return "", raw
    return raw[open_at + 1 : close_at], raw[close_at + 2 :]


def _with_tables(
    tables: _PropertyTables, spec: RegistrationObject, meta: _Meta, plan: _Meta | None
) -> RegistrationObject:
    """Дописывает пустые таблицы свойств из структуры. Заданные агентом не трогает."""
    plan_name = plan.full_name if plan is not None else ""
    plan_filters = (
        _fill_plan(tables, meta.full_name, plan_name, spec.plan_filters)
        if spec.plan_filters
        else spec.plan_filters
    )
    object_filters = (
        _fill_object(tables, meta.full_name, spec.object_filters)
        if spec.object_filters
        else spec.object_filters
    )
    if plan_filters is spec.plan_filters and object_filters is spec.object_filters:
        return spec
    return replace(spec, plan_filters=plan_filters, object_filters=object_filters)


def _fill_plan(
    tables: _PropertyTables,
    object_name: str,
    plan_name: str,
    items: tuple[PlanFilter | PlanFilterGroup, ...],
) -> tuple[PlanFilter | PlanFilterGroup, ...]:
    filled: list[PlanFilter | PlanFilterGroup] = []
    for item in items:
        if isinstance(item, PlanFilterGroup):
            filled.append(
                PlanFilterGroup(
                    item.operator, _fill_plan(tables, object_name, plan_name, item.items)
                )
            )
            continue
        plan_props = item.plan_properties
        if not plan_props and plan_name and item.plan_property:
            plan_props = tables.rows(plan_name, item.plan_property, plan=True)
        object_props = item.object_properties
        # Константа — литерал в СвойствоОбъекта, таблицы свойств объекта у неё нет (макеты).
        if not item.constant and not object_props and item.object_property:
            object_props = tables.rows(object_name, item.object_property, plan=False)
        if plan_props != item.plan_properties or object_props != item.object_properties:
            item = replace(item, plan_properties=plan_props, object_properties=object_props)
        filled.append(item)
    return tuple(filled)


def _fill_object(
    tables: _PropertyTables,
    object_name: str,
    items: tuple[ObjectFilter | ObjectFilterGroup, ...],
) -> tuple[ObjectFilter | ObjectFilterGroup, ...]:
    filled: list[ObjectFilter | ObjectFilterGroup] = []
    for item in items:
        if isinstance(item, ObjectFilterGroup):
            filled.append(
                ObjectFilterGroup(item.operator, _fill_object(tables, object_name, item.items))
            )
            continue
        props = item.object_properties
        if not props and item.object_property:
            props = tables.rows(object_name, item.object_property, plan=False)
        if props != item.object_properties:
            item = replace(item, object_properties=props)
        filled.append(item)
    return tuple(filled)


def replace_registration_filters(
    document: RegistrationRules,
    built: RegistrationRules,
    specs: Sequence[RegistrationObject],
) -> None:
    """Заменяет отборы названных объектов. Остальные правила и заголовок не трогает.

    Объекта ещё нет в проекте — правило из сборки добавляется в конец списка.
    `plan_filters`/`object_filters` со значением None у описания не меняют свой отбор;
    пустой кортеж снимает его.
    """
    by_name = {str(rule.get("ОбъектМетаданныхИмя")): rule for rule in document.rules()}
    section = document.section("ПравилаРегистрацииОбъектов")
    used = {rule.code for rule in document.rules()}
    for rule in built.rules():
        full_name = str(rule.get("ОбъектМетаданныхИмя"))
        spec = _spec_for(specs, full_name, str(rule.get("ОбъектНастройки")))
        current = by_name.get(full_name)
        if current is None:
            _avoid_code_clash(rule, used)
            section.items.append(rule)
            by_name[full_name] = rule
            continue
        if spec is None or spec.plan_filters is not None:
            _set_filter(
                current, "ОтборПоСвойствамПланаОбмена", rule.child("ОтборПоСвойствамПланаОбмена")
            )
        if spec is None or spec.object_filters is not None:
            _set_filter(current, "ОтборПоСвойствамОбъекта", rule.child("ОтборПоСвойствамОбъекта"))


def _spec_for(
    specs: Sequence[RegistrationObject], full_name: str, type_name: str
) -> RegistrationObject | None:
    for spec in specs:
        if spec.metadata_name in (full_name, type_name):
            return spec
    return None


def _avoid_code_clash(rule: Node, used: set[str]) -> None:
    if rule.code not in used:
        used.add(rule.code)
        return
    number = 1
    while f"{number:09d}" in used:
        number += 1
    code = f"{number:09d}"
    used.add(code)
    rule.values["Код"] = code


def _set_filter(rule: Node, tag: str, child: Node | None) -> None:
    if child is None:
        rule.children.pop(tag, None)
    else:
        rule.children[tag] = child
