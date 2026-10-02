"""Проверка правил обмена по структурам метаданных источника и приёмника (спецификация
`rules-validation`, «Проверка по структурам метаданных»).

Ссылки «Исп:N» — строки исполнителя БСП
`DataProcessors\\КонвертацияОбъектовИнформационныхБаз\\Ext\\ObjectModule.bsl` (БСП 3.1.12),
«УО:N» — `reference/kd2-dist-src/V8Exchan83/УниверсальныйОбменДаннымиXML/Ext/ObjectModule.bsl`.

Имена типов и видов свойств в правилах и в структуре — в одной нотации КД (`СправочникСсылка.Имя`,
`Реквизит`, `ТабличнаяЧасть` …), поэтому сравниваются напрямую.
"""

import sqlite3
from dataclasses import dataclass, field

from kd2_rules_mcp.kd2.model import ExchangeRules, Node
from kd2_rules_mcp.kd2.schema import CONVERSION_EVENTS
from kd2_rules_mcp.validation.address import (
    pks_address,
    pks_segment,
    pkz_address,
    rule_address,
    side_name,
)
from kd2_rules_mcp.validation.report import ValidationReport

SOURCE = "Источник"
TARGET = "Приемник"
SIDE_TITLES = {SOURCE: "источника", TARGET: "приёмника"}
# Префиксы ссылочных типов: значение такого типа выгружается только через ПКО (БСП:13152-13161).
REF_PREFIXES = (
    "СправочникСсылка.",
    "ДокументСсылка.",
    "ПеречислениеСсылка.",
    "ПланВидовХарактеристикСсылка.",
    "ПланСчетовСсылка.",
    "ПланВидовРасчетаСсылка.",
    "ПланОбменаСсылка.",
    "БизнесПроцессСсылка.",
    "ТочкаМаршрутаБизнесПроцессаСсылка.",
    "ЗадачаСсылка.",
)
# Обработчики ПКС: значение может быть вычислено или заменено кодом, типы не проверяются.
PKS_HANDLERS = ("ПередВыгрузкой", "ПриВыгрузке", "ПослеВыгрузки")
PKO_HANDLERS = (
    "ПередВыгрузкой",
    "ПриВыгрузке",
    "ПослеВыгрузки",
    "ПослеВыгрузкиВФайл",
    "ПередЗагрузкой",
    "ПриЗагрузке",
    "ПослеЗагрузки",
)
# Тексты, где упоминание кода ПКО считается вызовом: обработчики ПКО, ПКС, ПВД, ПОД
# и события конвертации. Текст алгоритма смотрится отдельно — у запроса тег тоже `Текст`.
_CALL_TEXTS = frozenset(
    {
        *PKO_HANDLERS,
        "ПоследовательностьПолейПоиска",
        *PKS_HANDLERS,
        "ПередОбработкойВыгрузки",
        "ПослеОбработкиВыгрузки",
        "ПередОбработкойПравила",
        "ПередВыгрузкойОбъекта",
        "ПослеВыгрузкиОбъекта",
        "ПослеОбработкиПравила",
        "ПередУдалениемОбъекта",
        "ПослеЗагрузкиПараметра",
        *CONVERSION_EVENTS,
    }
)


def is_ref(type_name: str) -> bool:
    """Ссылочный тип (нужен ПКО)."""
    return type_name.startswith(REF_PREFIXES)


@dataclass(slots=True)
class Property:
    """Свойство объекта структуры."""

    kind: str
    types: tuple[str, ...]


@dataclass(slots=True)
class StructureObject:
    """Объект структуры с лениво загружаемыми свойствами и значениями."""

    id: int
    kind: str
    type_name: str
    # (путь, вид родителя) → свойства: у документа бывают одноимённые ТЧ и набор движений.
    properties: dict[tuple[str, str], list[Property]] | None = None
    values: set[str] | None = None


@dataclass(slots=True)
class Structure:
    """Индекс структуры для проверок: объекты по имени типа, свойства по пути."""

    connection: sqlite3.Connection
    objects: dict[str, StructureObject] = field(default_factory=dict)

    @classmethod
    def load(cls, connection: sqlite3.Connection) -> "Structure":
        """Читает список объектов; свойства и значения — при первом обращении."""
        structure = cls(connection)
        rows = connection.execute("SELECT id, kind, type_name FROM objects WHERE is_group = 0")
        for object_id, kind, type_name in rows:
            structure.objects[type_name] = StructureObject(object_id, kind, type_name)
        return structure

    def get(self, type_name: str) -> StructureObject | None:
        """Объект по имени типа."""
        return self.objects.get(type_name)

    def properties(self, obj: StructureObject) -> dict[tuple[str, str], list[Property]]:
        """Свойства объекта по пути (`ТЧ.Реквизит`) и виду родителя (`""` у свойств объекта)."""
        if obj.properties is None:
            rows = self.connection.execute(
                "SELECT p.path, p.kind, ts.types, COALESCE(parent.kind, '') FROM properties AS p"
                " LEFT JOIN properties AS parent ON parent.id = p.parent_id"
                " LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id WHERE p.object_id = ?",
                (obj.id,),
            )
            index: dict[tuple[str, str], list[Property]] = {}
            for path, kind, types, parent_kind in rows:
                prop = Property(kind, tuple(types.split("\n")) if types else ())
                index.setdefault((path, parent_kind), []).append(prop)
            obj.properties = index
        return obj.properties

    def find(
        self, obj: StructureObject, path: str, parent_kind: str, kind: str = ""
    ) -> Property | None:
        """Свойство по пути и виду родителя; при заданном виде — только этого вида."""
        candidates = self.properties(obj).get((path, parent_kind), [])
        if kind:
            candidates = [prop for prop in candidates if prop.kind == kind]
        return candidates[0] if candidates else None

    def values(self, obj: StructureObject) -> set[str]:
        """Имена значений объекта (перечисления, предопределённые)."""
        if obj.values is None:
            rows = self.connection.execute(
                "SELECT name FROM object_values WHERE object_id = ?", (obj.id,)
            )
            obj.values = {name for (name,) in rows}
        return obj.values


@dataclass(slots=True)
class _Context:
    """Состояние проверки одного файла правил."""

    rules: ExchangeRules
    report: ValidationReport
    sides: dict[str, Structure | None]
    # ПКО по коду и типы источника, для которых есть ПКО (поиск ПКО по типу, Исп:7080-7085).
    pko_by_code: dict[str, Node] = field(default_factory=dict)
    pko_sources: set[str] = field(default_factory=set)


def check_structures(
    rules: ExchangeRules,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
) -> ValidationReport:
    """Проверяет ПКО, ПКС, ПКЗ, ПВД и ПОД против структур источника и приёмника.

    Без структуры стороны проверки по ней не выполняются и перечисляются в отчёте отдельно.
    """
    context = _prepare(rules, source, target)
    for pko in rules.pko():
        _check_pko(context, pko)
    for pvd in rules.pvd():
        _check_pvd(context, pvd)
    for pod in rules.pod():
        _check_pod(context, pod)
    _check_unreachable_pko(context)
    return context.report


def check_rule(
    rules: ExchangeRules,
    node: Node,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
) -> ValidationReport:
    """Проверяет один узел, уже стоящий в дереве `rules`.

    Владелец-ПКО и группы-предки ищутся обходом документа: у `Node` нет ссылки на родителя.
    Предки задают путь и вид родителя и сами замечаний не дают. Соседи и вложенные правила,
    которых эта правка не меняла, молчат. `structure.pko_unreachable` считает весь документ
    и здесь не выполняется.
    """
    context = _prepare(rules, source, target)
    place = _locate(rules, node)
    if place is None:
        return context.report
    if place.kind == "pko":
        _check_pko_sides(context, node)
    elif place.kind == "pks" and place.pko is not None:
        _check_one_pks(context, place.pko, node, place.ancestors, place.path)
    elif place.kind == "pkz" and place.pko is not None:
        _check_one_pkz(context, place.pko, node)
    elif place.kind == "pvd":
        _check_pvd(context, node)
    elif place.kind == "pod":
        _check_pod(context, node)
    return context.report


def _prepare(
    rules: ExchangeRules,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
) -> _Context:
    """Стороны и индекс ПКО. Без структуры стороны проверки по ней пропускаются."""
    report = ValidationReport()
    sides: dict[str, Structure | None] = {
        SOURCE: Structure.load(source) if source is not None else None,
        TARGET: Structure.load(target) if target is not None else None,
    }
    for side, structure in sides.items():
        if structure is None:
            report.skip(
                f"structure.{'source' if side == SOURCE else 'target'}",
                f"структура {SIDE_TITLES[side]} не загружена",
            )
    context = _Context(rules, report, sides)
    for pko in rules.pko():
        context.pko_by_code.setdefault(pko.code, pko)
        source_type = str(pko.get(SOURCE))
        if source_type:
            context.pko_sources.add(source_type)
    return context


@dataclass(slots=True)
class _Place:
    """Место узла в документе: вид проверки, ПКО-владелец, группы над ним и путь ПКС."""

    kind: str
    pko: Node | None
    ancestors: list[Node]
    path: str


def _locate(rules: ExchangeRules, node: Node) -> _Place | None:
    """Ищет узел среди ПКО, ПКС, ПКЗ, ПВД и ПОД. Группы-предки ПКС — от корня свойств."""
    for pko in rules.pko():
        if pko is node:
            return _Place("pko", pko, [], "")
        properties = pko.child("Свойства")
        if properties is not None:
            found = _find_pks(properties, node, "", [])
            if found is not None:
                return _Place("pks", pko, found[0], found[1])
        values = pko.child("Значения")
        if values is not None and _contains(values, node):
            return _Place("pkz", pko, [], "")
    for pvd in rules.pvd():
        if pvd is node:
            return _Place("pvd", None, [], "")
    for pod in rules.pod():
        if pod is node:
            return _Place("pod", None, [], "")
    return None


def _find_pks(
    container: Node, target: Node, prefix: str, ancestors: list[Node]
) -> tuple[list[Node], str] | None:
    """Группы над `target` и его путь `группа/…/свойство`. Сам узел в предки не входит."""
    for index, item in enumerate(container.items):
        path = f"{prefix}{pks_segment(item, index)}"
        if item is target:
            return ancestors, path
        if item.is_group:
            found = _find_pks(item, target, f"{path}/", [*ancestors, item])
            if found is not None:
                return found
    return None


def _contains(container: Node, target: Node) -> bool:
    for item in container.items:
        if item is target:
            return True
        if item.is_group and _contains(item, target):
            return True
    return False


# --- ПКО ---------------------------------------------------------------------------------------


def _object(context: _Context, side: str, type_name: str) -> StructureObject | None:
    structure = context.sides[side]
    return structure.get(type_name) if structure is not None and type_name else None


def _pko_objects(context: _Context, pko: Node) -> dict[str, StructureObject | None]:
    """Объекты сторон ПКО без замечаний: пустой тип и ненайденный объект — `None`."""
    return {side: _object(context, side, str(pko.get(side))) for side in (SOURCE, TARGET)}


def _check_pko_sides(context: _Context, pko: Node) -> dict[str, StructureObject | None]:
    """Типы ПКО на сторонах. Пустой источник — выгрузка из обработчиков, объекта нет."""
    address = rule_address(pko)
    objects = _pko_objects(context, pko)
    for side in (SOURCE, TARGET):
        type_name = str(pko.get(side))
        # Пустой источник — ПКО для выгрузки из обработчиков, объекта нет (22 ПКО в корпусе).
        if type_name and context.sides[side] is not None and objects[side] is None:
            context.report.error(
                f"structure.pko_{'source' if side == SOURCE else 'target'}",
                address,
                f"Тип {SIDE_TITLES[side]} «{type_name}» не найден в структуре",
            )
    return objects


def _check_pko(context: _Context, pko: Node) -> None:
    objects = _check_pko_sides(context, pko)
    properties = pko.child("Свойства")
    if properties is not None:
        empty = {SOURCE: "", TARGET: ""}
        _check_pks_list(context, pko, properties, objects, empty, empty, "")
    values = pko.child("Значения")
    if values is not None:
        _check_pkz(context, pko, values, objects)


# --- ПКС ---------------------------------------------------------------------------------------


def _check_pks_list(
    context: _Context,
    pko: Node,
    container: Node,
    objects: dict[str, StructureObject | None],
    prefixes: dict[str, str],
    parent_kinds: dict[str, str],
    address_prefix: str,
) -> None:
    """ПКС контейнера: пути свойств на каждой стороне собираются из имён групп (ТЧ, наборов)."""
    for index, item in enumerate(container.items):
        path = f"{address_prefix}{pks_segment(item, index)}"
        if item.attrs.get("Отключить") is True:
            continue
        found = _check_pks_sides(context, pko, item, objects, prefixes, parent_kinds, path)
        if not item.is_group:
            _check_pks_types(context, pko, item, found, path)
            continue
        # Свойства группы проверяются на той стороне, где найдена сама группа.
        sub_objects, sub_prefixes, sub_kinds = _child_scope(item, objects, prefixes, found)
        _check_pks_list(context, pko, item, sub_objects, sub_prefixes, sub_kinds, f"{path}/")


def _resolve_side(
    context: _Context,
    node: Node,
    side: str,
    obj: StructureObject | None,
    prefix: str,
    parent_kind: str,
) -> tuple[Property | None, str, str, str] | None:
    """Свойство стороны: (свойство или None, путь, вид группы, тип объекта).

    `None` — сторону не с чем сверять (нет объекта, структуры или имени).
    Вид для поиска задан только у группы: по нему выбирается коллекция.
    """
    structure = context.sides[side]
    name = side_name(node, side)
    if obj is None or structure is None or not name:
        return None
    child = node.child(side)
    group_kind = str(child.attrs.get("Вид", "")) if child is not None and node.is_group else ""
    full_path = f"{prefix}{name}"
    prop = structure.find(obj, full_path, parent_kind, group_kind)
    return prop, full_path, group_kind, obj.type_name


def _check_pks_sides(
    context: _Context,
    pko: Node,
    pks: Node,
    objects: dict[str, StructureObject | None],
    prefixes: dict[str, str],
    parent_kinds: dict[str, str],
    path: str,
) -> dict[str, Property | None]:
    """Существование свойства на сторонах; возвращает найденные свойства.

    Вид исполнитель смотрит только у групп: по нему выбирается коллекция — табличная часть
    или набор движений (Исп:11752-11766); у свойства вид не используется.
    """
    found: dict[str, Property | None] = {SOURCE: None, TARGET: None}
    for side in (SOURCE, TARGET):
        resolved = _resolve_side(
            context, pks, side, objects[side], prefixes[side], parent_kinds[side]
        )
        if resolved is None:
            continue
        prop, full_path, group_kind, type_name = resolved
        if prop is None:
            what = f"«{group_kind}» " if group_kind else ""
            context.report.error(
                f"structure.pks_{'source' if side == SOURCE else 'target'}",
                pks_address(pko.code, path),
                f"Свойства {what}{SIDE_TITLES[side]} «{full_path}» нет у {type_name}",
            )
            continue
        found[side] = prop
    return found


def _child_scope(
    group: Node,
    objects: dict[str, StructureObject | None],
    prefixes: dict[str, str],
    found: dict[str, Property | None],
) -> tuple[dict[str, StructureObject | None], dict[str, str], dict[str, str]]:
    """Контекст детей группы. Вид родителя — у найденного свойства, не у атрибута `Вид`."""
    sub_objects: dict[str, StructureObject | None] = {}
    sub_prefixes: dict[str, str] = {}
    sub_kinds: dict[str, str] = {}
    for side in (SOURCE, TARGET):
        prop = found[side]
        sub_objects[side] = objects[side] if prop is not None else None
        sub_prefixes[side] = f"{prefixes[side]}{side_name(group, side)}."
        sub_kinds[side] = prop.kind if prop is not None else ""
    return sub_objects, sub_prefixes, sub_kinds


def _scope_at(
    context: _Context, pko: Node, ancestors: list[Node]
) -> tuple[dict[str, StructureObject | None], dict[str, str], dict[str, str]]:
    """Объекты, префиксы и виды родителей на уровне узла. Предки замечаний не дают."""
    objects = _pko_objects(context, pko)
    prefixes = {SOURCE: "", TARGET: ""}
    parent_kinds = {SOURCE: "", TARGET: ""}
    for group in ancestors:
        found: dict[str, Property | None] = {SOURCE: None, TARGET: None}
        for side in (SOURCE, TARGET):
            resolved = _resolve_side(
                context, group, side, objects[side], prefixes[side], parent_kinds[side]
            )
            if resolved is not None:
                found[side] = resolved[0]
        objects, prefixes, parent_kinds = _child_scope(group, objects, prefixes, found)
    return objects, prefixes, parent_kinds


def _check_one_pks(
    context: _Context, pko: Node, node: Node, ancestors: list[Node], path: str
) -> None:
    """ПКС или группа ПКС. Выключенное правило пропускается, вложенные не обходятся."""
    if node.attrs.get("Отключить") is True:
        return
    objects, prefixes, parent_kinds = _scope_at(context, pko, ancestors)
    found = _check_pks_sides(context, pko, node, objects, prefixes, parent_kinds, path)
    if not node.is_group:
        _check_pks_types(context, pko, node, found, path)


def _has_handlers(pks: Node) -> bool:
    return any(pks.get(handler) for handler in PKS_HANDLERS)


def _pko_handlers_mention(pko: Node, name: str) -> bool:
    """Обработчик ПКО обращается к свойству `.Имя` (например, `Объект.ВидОперации = …`)."""
    return bool(name) and any(f".{name}" in str(pko.get(handler)) for handler in PKO_HANDLERS)


def _check_pks_types(
    context: _Context,
    pko: Node,
    pks: Node,
    found: dict[str, Property | None],
    path: str,
) -> None:
    """Совместимость типов ПКС и наличие ПКО для ссылочных типов источника."""
    address = pks_address(pko.code, path)
    source, target = found[SOURCE], found[TARGET]
    if _has_handlers(pks):
        return
    code = str(pks.get("КодПравилаКонвертации"))
    if code:
        rule = context.pko_by_code.get(code)
        if rule is None:
            return  # висячая ссылка — проверка формата
        rule_source, rule_target = str(rule.get(SOURCE)), str(rule.get(TARGET))
        if source is not None and rule_source and rule_source not in source.types:
            context.report.warning(
                "structure.pks_type",
                address,
                f"ПКО «{code}» конвертирует «{rule_source}», а у свойства источника типы:"
                f" {_types(source)}",
            )
        if target is not None and rule_target and rule_target not in target.types:
            context.report.warning(
                "structure.pks_type",
                address,
                f"ПКО «{code}» даёт «{rule_target}», а у свойства приёмника типы: {_types(target)}",
            )
        return
    if source is None:
        return
    source_refs = [name for name in source.types if is_ref(name)]
    missing = [name for name in source_refs if name not in context.pko_sources]
    if missing:
        # Исп:13152-13161: ссылка без ПКО своего типа молча не выгружается.
        whole = len(missing) == len(source_refs) == len(source.types)
        message = (
            f"Нет ПКО для типов {', '.join(missing)}: значения этих типов не выгрузятся"
            " (свойство пропускается) — создайте ПКО или укажите правило конвертации в ПКС"
        )
        mentioned = _pko_handlers_mention(pko, side_name(pks, TARGET))
        if mentioned:
            message += "; свойство упомянуто в обработчике ПКО — возможно, значение задаёт он"
        if whole and not mentioned:
            context.report.error("structure.pko_missing", address, message)
        else:
            context.report.warning("structure.pko_missing", address, message)
    if target is not None and source.types and target.types:
        primitive_source = not source_refs
        target_refs_only = all(is_ref(name) for name in target.types)
        if primitive_source and target_refs_only:
            context.report.error(
                "structure.pks_type",
                address,
                f"Примитив → объектная ссылка: источник {_types(source)},"
                f" приёмник {_types(target)}",
            )


def _types(prop: Property) -> str:
    return ", ".join(prop.types) or "не заданы"


# --- ПКЗ ---------------------------------------------------------------------------------------


def _check_pkz(
    context: _Context, pko: Node, container: Node, objects: dict[str, StructureObject | None]
) -> None:
    pkz_list = list(container.walk())
    _check_pkz_values(context, pko, pkz_list, objects)
    _check_pkz_coverage(context, pko, objects, pkz_list)


def _check_one_pkz(context: _Context, pko: Node, node: Node) -> None:
    """Одно ПКЗ и покрытие перечисления всего ПКО. Соседние значения сами не проверяются."""
    objects = _pko_objects(context, pko)
    values = pko.child("Значения")
    pkz_list = list(values.walk()) if values is not None else [node]
    _check_pkz_values(context, pko, [node], objects)
    _check_pkz_coverage(context, pko, objects, pkz_list)


def _check_pkz_values(
    context: _Context,
    pko: Node,
    pkz_list: list[Node],
    objects: dict[str, StructureObject | None],
) -> None:
    for side in (SOURCE, TARGET):
        obj, structure = objects[side], context.sides[side]
        if obj is None or structure is None:
            continue
        known = structure.values(obj)
        for pkz in pkz_list:
            name = str(pkz.get(side))
            if name and name not in known:
                context.report.error(
                    f"structure.pkz_{'source' if side == SOURCE else 'target'}",
                    pkz_address(pko.code, str(pkz.get(SOURCE))),
                    f"Значения {SIDE_TITLES[side]} «{name}» нет у {obj.type_name}",
                )


def _check_pkz_coverage(
    context: _Context,
    pko: Node,
    objects: dict[str, StructureObject | None],
    pkz_list: list[Node],
) -> None:
    source, source_structure = objects[SOURCE], context.sides[SOURCE]
    target_type = str(pko.get(TARGET))
    # Исп:740-772: при непустом соответствии значений значение перечисления без ПКЗ
    # выгружается пустой ссылкой с ошибкой 71 в протоколе. Без ПКЗ соответствие не строится.
    if (
        not pkz_list
        or source is None
        or source_structure is None
        or source.kind != "Перечисление"
        or not target_type.startswith("ПеречислениеСсылка.")
    ):
        return
    covered = {str(pkz.get(SOURCE)) for pkz in pkz_list}
    uncovered = sorted(source_structure.values(source) - covered)
    if uncovered:
        context.report.warning(
            "structure.pkz_coverage",
            rule_address(pko),
            f"Значения без ПКЗ (выгрузятся пустой ссылкой): {', '.join(uncovered)}",
        )


# --- ПВД и ПОД ---------------------------------------------------------------------------------


def _check_pvd(context: _Context, pvd: Node) -> None:
    if pvd.attrs.get("Отключить") is True:
        return
    address = rule_address(pvd)
    selection = str(pvd.get("ОбъектВыборки"))
    if (
        selection
        and context.sides[SOURCE] is not None
        and _object(context, SOURCE, selection) is None
    ):
        context.report.error(
            "structure.pvd_object",
            address,
            f"Объект выборки «{selection}» не найден в структуре источника",
        )
    code = str(pvd.get("КодПравилаКонвертации"))
    rule = context.pko_by_code.get(code) if code else None
    if rule is not None and selection and str(rule.get(SOURCE)) not in ("", selection):
        context.report.warning(
            "structure.pvd_pko",
            address,
            f"Объект выборки «{selection}», а ПКО «{code}» конвертирует «{rule.get(SOURCE)}»",
        )


def _check_pod(context: _Context, pod: Node) -> None:
    """ПОД выполняются при загрузке до чтения данных (УО:12022-12023): объект — в приёмнике."""
    if pod.attrs.get("Отключить") is True or context.sides[TARGET] is None:
        return
    selection = str(pod.get("ОбъектВыборки"))
    if selection and _object(context, TARGET, selection) is None:
        context.report.error(
            "structure.pod_object",
            rule_address(pod),
            f"Объект выборки «{selection}» не найден в структуре приёмника",
        )


def _disabled(node: Node) -> bool:
    """`Отключить`: у ПВД булево, у ПКО атрибута нет в схеме — остаётся строка `true`."""
    flag = node.attrs.get("Отключить")
    return flag is True or flag == "true"


def _conversion_code(node: Node) -> str:
    """Код ПКО из `КодПравилаКонвертации` (ПКС, группа ПКС, ПКЗ, ПВД)."""
    return str(node.get("КодПравилаКонвертации")).strip()


def _referenced_pko_codes(rules: ExchangeRules) -> set[str]:
    """Коды ПКО, на которые есть ссылка из включённого ПВД, ПКС, группы ПКС или ПКЗ."""
    codes: set[str] = set()
    for pvd in rules.pvd():
        if _disabled(pvd):
            continue
        code = _conversion_code(pvd)
        if code:
            codes.add(code)
    for pko in rules.pko():
        for container_tag in ("Свойства", "Значения"):
            container = pko.child(container_tag)
            if container is None:
                continue
            # `walk()` раскрывает группы и не возвращает их; `walk_all` отдаёт и группы,
            # у которых поле тоже `КодПравилаКонвертации`.
            for node in container.walk_all():
                code = _conversion_code(node)
                if code:
                    codes.add(code)
    return codes


def _handler_corpus(rules: ExchangeRules) -> str:
    """Тексты обработчиков и алгоритмов: подстрока кода ПКО считается вызовом."""
    chunks: list[str] = []
    for node in rules.root.walk_all():
        if node.kind.name == "algorithm":
            text = node.get("Текст")
            if text:
                chunks.append(str(text))
        for tag, value in node.values.items():
            if tag in _CALL_TEXTS and value:
                chunks.append(str(value))
        loaded = node.attrs.get("ПослеЗагрузкиПараметра")
        if loaded:
            chunks.append(str(loaded))
    return "\n".join(chunks)


def _check_unreachable_pko(context: _Context) -> None:
    """ПКО, которое ничем не вызывается.

    Исполнитель берёт ПКО из ПВД, по коду у свойства или подбором по типу значения
    (БСП:13152-13161). Подбор по типу ещё возможен, поэтому это предупреждение.
    Пустой источник — выгрузка из обработчиков; выключенное ПКО не исполняется.
    От загруженных структур не зависит.
    """
    called = _referenced_pko_codes(context.rules)
    handlers = _handler_corpus(context.rules)
    for pko in context.rules.pko():
        if _disabled(pko) or not str(pko.get(SOURCE)).strip():
            continue
        code = pko.code.strip()
        if code and (code in called or code in handlers):
            continue
        context.report.warning(
            "structure.pko_unreachable",
            rule_address(pko),
            f"ПКО «{pko.code}» не вызывается ни из ПВД, ни из ПКС: проверьте состав"
            " плана обмена (structure_plan_content) и добавьте ПВД или ссылку из ПКС",
        )
