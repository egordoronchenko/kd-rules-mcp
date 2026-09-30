"""Правила регистрации из состава плана обмена (спецификация `rules-authoring`, design.md Д9).

Писатель КД — `reference/kd2-cfg/DataProcessors/ВыгрузкаРегистрации/Ext/ObjectModule.bsl`:
заголовок (135–153), план обмена (89–101), конфигурация (120–131), состав (103–116, 660–677),
ПРО (197–256), отборы (323–400), таблица свойств (402–420), обработчики только если непусты
(597–605). Версия формата — «2.01» (686). Читатель БСП
(`DataProcessors/ЗагрузкаПравилРегистрацииОбъектов/Ext/ObjectModule.bsl`) блок
`СоставПланаОбмена` пропускает (222–226), но КД его пишет — поэтому пишем и мы (Д9).
"""

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
    items: tuple["PlanFilter | PlanFilterGroup", ...] = ()


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
    items: tuple["ObjectFilter | ObjectFilterGroup", ...] = ()


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
    plan_filters: tuple[PlanFilter | PlanFilterGroup, ...] = ()
    object_filters: tuple[ObjectFilter | ObjectFilterGroup, ...] = ()
    before_processing: str = ""
    on_processing: str = ""
    on_processing_extra: str = ""
    after_processing: str = ""


@dataclass(slots=True)
class RegistrationBuild:
    """Документ правил регистрации и предупреждения сборки."""

    document: RegistrationRules
    warnings: list[str]


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
        connection, specs, from_rules, content_types, plan.name if plan else exchange_plan, warnings
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
        nodes.append(_pro(spec, meta, codes.next(spec.code)))
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
    rule: Node, kind_name: str, tag: str, items: Sequence[T], build: Callable[[T], Node]
) -> None:
    """Вложенный отбор пишется, только если агент задал элементы."""
    if not items:
        return
    node = Node.new(kind_name, tag)
    node.items.extend(build(item) for item in items)
    rule.children[tag] = node


def _plan_filter(item: PlanFilter | PlanFilterGroup) -> Node:
    if isinstance(item, PlanFilterGroup):
        group = Node.new("plan_filter_group", "Группа")
        _put(group, "БулевоЗначениеГруппы", item.operator)
        group.items.extend(_plan_filter(child) for child in item.items)
        return group
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
        group = Node.new("object_filter_group", "Группа")
        _put(group, "БулевоЗначениеГруппы", item.operator)
        group.items.extend(_object_filter(child) for child in item.items)
        return group
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
