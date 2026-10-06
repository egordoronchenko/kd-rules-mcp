"""Сборка структуры метаданных из XML-выгрузки конфигурации и расширений в стиле MD83Exp.

Результат — та же схема SQLite, что даёт загрузчик MD83Exp (`db.py`), с теми же именами типов,
свойств и значений, чтобы две структуры сравнивались по именам. Поведение повторяет обработку
`reference/kd2-dist-src/MD83Exp/ВыгрузкаМетаданных/Ext/ObjectModule.bsl` (далее «MD83Exp:строка»).
"""

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from kd2_rules_mcp.structures.xmldump import (
    KINDS,
    REGISTER_TAGS,
    ConfigDump,
    Field,
    MetaObject,
    Named,
    Tabular,
    TypeDesc,
)

# Примитивные типы XML → имена КД (MD83Exp:2021-2054).
PRIMITIVES = {
    "string": "Строка",
    "decimal": "Число",
    "boolean": "Булево",
    "dateTime": "Дата",
    "ValueStorage": "ХранилищеЗначения",
    "UUID": "УникальныйИдентификатор",
}
# Ссылочные типы XML → префиксы КД (MD83Exp:2037-2048) и тег вида метаданных.
REFS = {
    "CatalogRef": ("СправочникСсылка", "Catalog"),
    "DocumentRef": ("ДокументСсылка", "Document"),
    "EnumRef": ("ПеречислениеСсылка", "Enum"),
    "ChartOfAccountsRef": ("ПланСчетовСсылка", "ChartOfAccounts"),
    "ChartOfCharacteristicTypesRef": ("ПланВидовХарактеристикСсылка", "ChartOfCharacteristicTypes"),
    "ChartOfCalculationTypesRef": ("ПланВидовРасчетаСсылка", "ChartOfCalculationTypes"),
    "ExchangePlanRef": ("ПланОбменаСсылка", "ExchangePlan"),
    "BusinessProcessRef": ("БизнесПроцессСсылка", "BusinessProcess"),
    "BusinessProcessRoutePointRef": ("ТочкаМаршрутаБизнесПроцессаСсылка", "BusinessProcess"),
    "TaskRef": ("ЗадачаСсылка", "Task"),
}
# Префиксы ссылок, из которых состоит «любая ссылка» (AnyIBRef; AnyRef — то же в пределах
# конфигурации: в выгрузке ДО у реквизитов БСП вида ОчередьОбновленияПравДоступа.Объект).
ANY_REF = tuple(REFS)
DATE_PARTS = {"Date": "Дата", "Time": "Время", "DateTime": "Дата и время"}
DEFAULT_DATE_PARTS = "Дата и время"  # КвалификаторыДаты по умолчанию у ОписаниеТипов
# Примитивные объекты-заглушки MD83Exp (MD83Exp:125-135): (Имя/Description, Тип, Синоним).
STUBS = (
    ("Число", "Число", "Число"),
    ("Строка", "Строка", "Строка"),
    ("Дата", "Дата", "Дата"),
    ("Булево", "Булево", "Булево"),
    ("ХранилищеЗначения", "ХранилищеЗначения", "Хранилище значения"),
    ("УникальныйИдентификатор", "УникальныйИдентификатор", "Уникальный идентификатор"),
    ("КонстантыНабор", "НаборКонстант", "Набор констант"),
)
# Значения перечислений XML → имена значений 1С, как их пишет `Строка(…)` в MD83Exp.
ENUM_TEXT = {
    "HierarchyFoldersAndItems": "ИерархияГруппИЭлементов",
    "HierarchyOfItems": "ИерархияЭлементов",
    "WholeCatalog": "ВоВсемСправочнике",
    "WithinSubordination": "ВПределахПодчинения",
    "WithinOwnerSubordination": "ВПределахПодчиненияВладельцу",
    "WholeCharacteristicKind": "ВоВсемПланеВидовХарактеристик",
    "WholeChartOfAccounts": "ВоВсемПланеСчетов",
    "Nonperiodical": "Непериодический",
    "Second": "Секунда",
    "Day": "День",
    "Month": "Месяц",
    "Quarter": "Квартал",
    "Year": "Год",
    "RecorderPosition": "ПозицияРегистратора",
    "ForItem": "ДляЭлемента",
    "ForFolder": "ДляГруппы",
    "ForFolderAndItem": "ДляГруппыИЭлемента",
}
MOVEMENT_KINDS = {
    "InformationRegister": "НаборДвиженийРегистраСведений",
    "AccumulationRegister": "НаборДвиженийРегистраНакопления",
    "AccountingRegister": "НаборДвиженийРегистраБухгалтерии",
    "CalculationRegister": "НаборДвиженийРегистраРасчета",
}
# Имя вида 1С → тег XML (для ссылок `Справочник.Валюты` из состава, движений, владельцев).
TAG_BY_RU = {info[1]: tag for tag, info in KINDS.items()}


@dataclass(slots=True)
class Resolved:
    """Разрешённый тип свойства: имена типов КД и квалификаторы."""

    names: list[str] = field(default_factory=list)
    string_length: int = 0
    string_fixed: bool = False
    number_length: int = 0
    number_precision: int = 0
    number_nonnegative: bool = False
    date_parts: str = DEFAULT_DATE_PARTS


def simple_type(*names: str, **qualifiers: object) -> Resolved:
    """Тип из готовых имён и квалификаторов (для системных свойств)."""
    result = Resolved(names=list(names))
    for key, value in qualifiers.items():
        setattr(result, key, value)
    return result


def _ref_type(full_name: str) -> str:
    """`Справочник.Валюты` → `СправочникСсылка.Валюты`."""
    ru_kind, _, name = full_name.partition(".")
    tag = TAG_BY_RU.get(ru_kind)
    return f"{KINDS[tag][4]}{name}" if tag else full_name


def _ru_full(xml_ref: str) -> str:
    """`ChartOfAccounts.Хозрасчетный` → `ПланСчетов.Хозрасчетный`."""
    tag, _, name = xml_ref.partition(".")
    return f"{KINDS[tag][1]}.{name}" if tag in KINDS else xml_ref


class Metadata:
    """Объединённые метаданные конфигурации с наложенными расширениями."""

    def __init__(self, main: ConfigDump, extensions: Iterable[ConfigDump] = ()) -> None:
        self.main = main
        self.objects: dict[str, list[MetaObject]] = {
            tag: list(items) for tag, items in main.objects.items()
        }
        self.index: dict[tuple[str, str], MetaObject] = {
            (obj.tag, obj.name): obj for items in self.objects.values() for obj in items
        }
        self.extensions: list[str] = []
        for extension in extensions:
            self._overlay(extension)
            self.extensions.append(extension.name)

    def _overlay(self, extension: ConfigDump) -> None:
        """Накладывает расширение: свои объекты добавляются, у заимствованных — новые части."""
        for tag, items in extension.objects.items():
            for obj in items:
                existing = self.index.get((tag, obj.name))
                if existing is None:
                    self.objects.setdefault(tag, []).append(obj)
                    self.index[(tag, obj.name)] = obj
                elif obj.adopted:
                    _merge(existing, obj)

    def of(self, tag: str) -> list[MetaObject]:
        """Объекты вида в порядке выгрузки."""
        return self.objects.get(tag, [])

    def get(self, full_name: str) -> MetaObject | None:
        """Объект по полному имени `Справочник.Валюты`."""
        ru_kind, _, name = full_name.partition(".")
        tag = TAG_BY_RU.get(ru_kind)
        return self.index.get((tag, name)) if tag else None


def _merge_fields(target: list[Field], source: list[Field]) -> None:
    """Новые поля расширения добавляются, у заимствованных расширяется тип (`ExtendValue`)."""
    by_name = {f.name: f for f in target}
    for item in source:
        existing = by_name.get(item.name)
        if existing is None:
            if not item.adopted:
                target.append(item)
                by_name[item.name] = item
        elif item.adopted and item.type.extends:
            existing.type.widen(item.type)


def _merge(target: MetaObject, source: MetaObject) -> None:
    """Добавляет к заимствованному объекту то, что расширение создало или расширило."""
    for part in ("attributes", "dimensions", "resources", "addressing"):
        _merge_fields(getattr(target, part), getattr(source, part))
    tabulars = {t.name: t for t in target.tabulars}
    for tabular in source.tabulars:
        existing = tabulars.get(tabular.name)
        if existing is None:
            if not tabular.adopted:
                target.tabulars.append(tabular)
            continue
        _merge_fields(existing.fields, tabular.fields)
    known_values = {v.name for v in target.enum_values}
    target.enum_values.extend(
        v for v in source.enum_values if not v.adopted and v.name not in known_values
    )
    known_predefined = {v.name for v in target.predefined}
    target.predefined.extend(v for v in source.predefined if v.name not in known_predefined)
    for name, items in source.lists.items():
        merged = target.lists.setdefault(name, [])
        merged.extend(i for i in items if i not in merged)
    known_content = {name for name, _ in target.content}
    target.content.extend(c for c in source.content if c[0] not in known_content)
    if source.type is not None and source.type.extends:
        if target.type is None:
            target.type = TypeDesc()
        target.type.widen(source.type)


class TypeResolver:
    """Перевод описаний типов выгрузки в имена типов КД с разворачиванием наборов."""

    def __init__(self, metadata: Metadata) -> None:
        self.metadata = metadata
        self.defined = {obj.name: obj.type for obj in metadata.of("DefinedType") if obj.type}
        self.characteristics = {
            obj.name: obj.type for obj in metadata.of("ChartOfCharacteristicTypes") if obj.type
        }
        self._all_refs: dict[str, list[str]] = {}

    def all_refs(self, xml_ref: str) -> list[str]:
        """Все ссылочные типы вида (`CatalogRef` без имени — любой справочник)."""
        found = self._all_refs.get(xml_ref)
        if found is None:
            prefix, tag = REFS[xml_ref]
            found = [f"{prefix}.{obj.name}" for obj in self.metadata.of(tag)]
            self._all_refs[xml_ref] = found
        return found

    def resolve(self, desc: TypeDesc | None) -> Resolved:
        """Разрешённый тип; неизвестные виды остаются как есть и отсеиваются при записи."""
        result = Resolved()
        names: list[str] = []
        self._collect(desc, names, result, frozenset())
        result.names = sorted(set(names))
        return result

    def _collect(
        self, desc: TypeDesc | None, names: list[str], result: Resolved, seen: frozenset[str]
    ) -> None:
        if desc is None:
            return
        if desc.string_length is not None:
            result.string_length = desc.string_length
            result.string_fixed = desc.string_fixed
        if desc.number_length is not None:
            result.number_length = desc.number_length
            result.number_precision = desc.number_precision
            result.number_nonnegative = desc.number_nonnegative
        date_parts = desc.date_parts
        if date_parts:
            result.date_parts = DATE_PARTS.get(date_parts) or date_parts
        for kind, value in desc.entries:
            head, _, name = value.partition(".")
            if kind == "Type":
                if value in PRIMITIVES:
                    names.append(PRIMITIVES[value])
                elif head in REFS and name:
                    names.append(f"{REFS[head][0]}.{name}")
                else:
                    names.append(value)  # не ссылка и не примитив — останется неразрешённым
            elif head == "DefinedType":
                if name in self.defined and name not in seen:
                    self._collect(self.defined[name], names, result, seen | {name})
                elif name not in self.defined:
                    names.append(value)
            elif head == "Characteristic":
                if name in self.characteristics:
                    self._collect(self.characteristics[name], names, result, seen)
                else:
                    names.append(value)
            elif value in ("AnyIBRef", "AnyRef"):
                for ref in ANY_REF:
                    names.extend(self.all_refs(ref))
            elif head in REFS and not name:
                names.extend(self.all_refs(head))
            else:
                names.append(value)


@dataclass(slots=True)
class Prop:
    """Свойство для записи: имя, вид, тип, признаки."""

    name: str
    kind: str
    type: Resolved | None = None
    synonym: str = ""
    comment: str = ""
    is_group: bool = False
    usage: str = ""
    indexing: bool = False
    autoregistration: bool = False
    children: list["Prop"] = field(default_factory=list)
    fill_checking: str = ""


def _field_props(item: Field, kind: str, resolver: TypeResolver) -> Iterator[Prop]:
    """Реквизит, измерение, ресурс; небалансовые у регистра бухгалтерии — парой Дт/Кт.

    MD83Exp:1226-1256.
    """
    resolved = resolver.resolve(item.type)
    indexing = item.indexing in ("Index", "IndexWithAdditionalOrder")
    usage = ENUM_TEXT.get(item.usage) or item.usage
    names = [item.name] if item.balance else [f"{item.name}Дт", f"{item.name}Кт"]
    for name in names:
        yield Prop(
            name,
            kind,
            resolved,
            item.synonym,
            item.comment,
            usage=usage,
            indexing=indexing,
            fill_checking=item.fill_checking,
        )


AddProp = Callable[..., None]


class Builder:
    """Свойства объектов в порядке и по правилам MD83Exp."""

    def __init__(self, metadata: Metadata) -> None:
        self.metadata = metadata
        self.resolver = TypeResolver(metadata)
        self.common: dict[str, list[Field]] = self._common_attributes()
        self.recorders: dict[str, list[str]] = self._recorders()

    def _common_attributes(self) -> dict[str, list[Field]]:
        """Общие реквизиты без разделения данных по объектам (MD83Exp:2173-2219)."""
        result: dict[str, list[Field]] = {}
        for attr in self.metadata.of("CommonAttribute"):
            if attr.props.get("DataSeparation", "DontUse") != "DontUse":
                continue
            auto = attr.props.get("AutoUse") == "Use"
            item = Field(attr.name, attr.synonym, attr.comment, attr.type or TypeDesc())
            item.indexing = attr.props.get("Indexing", "")
            item.fill_checking = attr.props.get("FillChecking", "DontCheck")
            for full_name, use in attr.common_content:
                if use == "Use" or (use == "Auto" and auto):
                    result.setdefault(full_name, []).append(item)
        return result

    def _recorders(self) -> dict[str, list[str]]:
        """Регистр → типы документов-регистраторов (MD83Exp:1854-1936)."""
        result: dict[str, list[str]] = {}
        for document in self.metadata.of("Document"):
            for register in document.lists.get("RegisterRecords", []):
                result.setdefault(register, []).append(f"ДокументСсылка.{document.name}")
        return result

    def main_properties(self, obj: MetaObject) -> list[Prop]:
        """Системные свойства: цепочка `ВыгрузитьОсновныеСвойства` (MD83Exp:408-1150).

        MD83Exp проверяет наличие свойства метаданных (`Попытка … Исключение Перейти`); здесь
        то же наличие проверяется по элементам `Properties` выгрузки.
        """
        props = obj.props
        out: list[Prop] = []
        prefix = KINDS[obj.tag][4] if obj.tag in KINDS else ""

        def add(name: str, resolved: Resolved, kind: str = "Свойство", synonym: str = "") -> None:
            standard_name = {
                "Код": "Code",
                "Наименование": "Description",
                "Номер": "Number",
                "Дата": "Date",
                "Владелец": "Owner",
                "Родитель": "Parent",
            }.get(name, "")
            out.append(
                Prop(
                    name,
                    kind,
                    resolved,
                    synonym or name,
                    synonym or name,
                    fill_checking=props.get(f"FillChecking.{standard_name}", ""),
                )
            )

        if "BasedOn" in props:
            add("ПометкаУдаления", simple_type("Булево"), synonym="Пометка удаления")
        if "CodeLength" in props:
            length = int(props["CodeLength"] or 0)
            if length > 0:
                if props.get("CodeType", "String") == "Number":
                    code = simple_type("Число", number_length=length, number_nonnegative=True)
                else:
                    code = simple_type("Строка", string_length=length, string_fixed=True)
                add("Код", code)
        if "DescriptionLength" in props:
            length = int(props["DescriptionLength"] or 0)
            if length > 0:
                add("Наименование", simple_type("Строка", string_length=length))
        hierarchical = props.get("Hierarchical") == "true"
        if "Hierarchical" in props:
            if hierarchical:
                add("Родитель", simple_type(f"{prefix}{obj.name}"))
            if "HierarchyType" in props:
                if hierarchical and props["HierarchyType"] == "HierarchyFoldersAndItems":
                    add("ЭтоГруппа", simple_type("Булево"), synonym="Это группа")
            elif "CharacteristicExtValues" in props and hierarchical:
                add("ЭтоГруппа", simple_type("Булево"), synonym="Это группа")
        if "NumberLength" in props:
            length = int(props["NumberLength"] or 0)
            if length > 0:
                if props.get("NumberType", "String") == "Number":
                    number = simple_type("Число", number_length=length, number_nonnegative=True)
                else:
                    number = simple_type("Строка", string_length=length)
                add("Номер", number)
        if "Posting" in props or "NumberType" in props:
            add("Дата", simple_type("Дата"))
            if props.get("Posting") == "Allow":
                add("Проведен", simple_type("Булево"))
        owners = obj.lists.get("Owners")
        if owners:
            add("Владелец", simple_type(*[_ref_type(o) for o in owners]))
        if obj.tag in REGISTER_TAGS:
            add("Активность", simple_type("Булево"))
            recorders = self.recorders.get(obj.full_name)
            if recorders:
                add("Регистратор", simple_type(*recorders))
        periodicity = props.get("InformationRegisterPeriodicity")
        if periodicity is not None and periodicity != "Nonperiodical":
            add("Период", simple_type("Дата"))
        if "RegisterType" in props:
            add("Период", simple_type("Дата"))
            if props["RegisterType"] == "Balance":
                add("ВидДвижения", simple_type(), synonym="Вид движения")
        if obj.tag == "AccountingRegister":
            self._accounting(obj, add)
        if obj.tag == "CalculationRegister":
            self._calculation(obj, add)
        if obj.tag == "ChartOfCalculationTypes" and props.get("ActionPeriodUse") == "true":
            add("ПериодДействияБазовый", simple_type("Булево"))
        if obj.tag == "BusinessProcess":
            add("Стартован", simple_type("Булево"))
            add("Завершен", simple_type("Булево"))
            tasks = [f"ЗадачаСсылка.{t.name}" for t in self.metadata.of("Task")]
            add("ВедущаяЗадача", simple_type(*tasks))
        if obj.tag == "Task":
            for item in obj.addressing:
                resolved = self.resolver.resolve(item.type)
                out.append(Prop(item.name, "Свойство", resolved, item.synonym, item.comment))
            processes = self.metadata.of("BusinessProcess")
            add("БизнесПроцесс", simple_type(*[f"БизнесПроцессСсылка.{p.name}" for p in processes]))
            add("Выполнена", simple_type("Булево"))
            points = [f"ТочкаМаршрутаБизнесПроцессаСсылка.{p.name}" for p in processes]
            add("ТочкаМаршрута", simple_type(*points))
        return out

    def _accounting(self, obj: MetaObject, add: AddProp) -> None:
        """Регистр бухгалтерии: период, счета, субконто (MD83Exp:775-826)."""
        add("Период", simple_type("Дата"))
        chart_name = obj.props.get("ChartOfAccounts", "")
        chart = self.metadata.get(_ru_full(chart_name)) if chart_name else None
        account = simple_type(f"ПланСчетовСсылка.{chart.name}") if chart else None
        subconto = None
        if chart is not None and chart.props.get("ExtDimensionTypes"):
            kinds = self.metadata.get(_ru_full(chart.props["ExtDimensionTypes"]))
            subconto = self.resolver.resolve(kinds.type if kinds else None)
        if obj.props.get("Correspondence") == "true":
            if account is not None:
                add("СчетДт", account)
                add("СчетКт", account)
                if subconto is not None:
                    add("СубконтоДт", subconto, "ВидыСубконтоСчета")
                    add("СубконтоКт", subconto, "ВидыСубконтоСчета")
        else:
            add("ВидДвижения", simple_type(), synonym="Вид движения")
            if account is not None:
                add("Счет", account)
                add("Субконто", subconto or simple_type(), "ВидыСубконтоСчета")

    def _calculation(self, obj: MetaObject, add: AddProp) -> None:
        """Регистр расчета (MD83Exp:827-861)."""
        plan = obj.props.get("ChartOfCalculationTypes", "").partition(".")[2]
        add("ВидРасчета", simple_type(f"ПланВидовРасчетаСсылка.{plan}"))
        add("ПериодРегистрации", simple_type("Дата"))
        if obj.props.get("BasePeriod") == "true":
            add("БазовыйПериодНачало", simple_type("Дата"))
            add("БазовыйПериодКонец", simple_type("Дата"))
        if obj.props.get("ActionPeriod") == "true":
            add("ПериодДействияНачало", simple_type("Дата"))
            add("ПериодДействияКонец", simple_type("Дата"))
        add("Сторно", simple_type("Булево"))

    def own_properties(self, obj: MetaObject) -> list[Prop]:
        """Измерения, ресурсы, реквизиты и общие реквизиты (MD83Exp:1152-1224)."""
        out: list[Prop] = []
        for items, kind in (
            (obj.dimensions, "Измерение"),
            (obj.resources, "Ресурс"),
            (obj.attributes, "Реквизит"),
            (self.common.get(obj.full_name, []), "Реквизит"),
        ):
            for item in items:
                out.extend(_field_props(item, kind, self.resolver))
        return out

    def tabular_sections(self, tabulars: list[Tabular]) -> list[Prop]:
        """Табличные части с реквизитами (MD83Exp:1423-1469)."""
        return [
            Prop(
                t.name,
                "ТабличнаяЧасть",
                None,
                t.synonym,
                t.comment,
                is_group=True,
                children=[p for f in t.fields for p in _field_props(f, "Реквизит", self.resolver)],
            )
            for t in tabulars
        ]

    def movements(self, obj: MetaObject) -> list[Prop]:
        """Наборы движений документа (MD83Exp:1545-1627)."""
        out: list[Prop] = []
        for full_name in obj.lists.get("RegisterRecords", []):
            register = self.metadata.get(full_name)
            if register is None:
                continue
            out.append(
                Prop(
                    register.name,
                    MOVEMENT_KINDS[register.tag],
                    None,
                    register.synonym,
                    register.comment,
                    is_group=True,
                    children=self.main_properties(register) + self.own_properties(register),
                )
            )
        return out

    def calculation_tabulars(self, obj: MetaObject) -> list[Prop]:
        """Предопределённые табличные части плана видов расчета (MD83Exp:2094-2171)."""
        plans = [
            f"ПланВидовРасчетаСсылка.{p.name}" for p in self.metadata.of("ChartOfCalculationTypes")
        ]
        sections: list[tuple[str, str, list[str]]] = []
        base = obj.lists.get("BaseCalculationTypes", [])
        if base:
            names = [f"ПланВидовРасчетаСсылка.{b.partition('.')[2]}" for b in base]
            sections.append(("БазовыеВидыРасчета", "Базовые виды расчета", names))
        sections.append(("ВедущиеВидыРасчета", "Ведущие виды расчета", plans))
        if obj.props.get("ActionPeriodUse") == "true":
            own = [f"ПланВидовРасчетаСсылка.{obj.name}"]
            sections.append(("ВытесняющиеВидыРасчета", "Вытесняющие виды расчета", own))
        return [
            Prop(
                name,
                "ТабличнаяЧасть",
                None,
                synonym,
                "Предопределенный объект",
                is_group=True,
                children=[Prop("ВидРасчета", "Реквизит", simple_type(*types), "Вид расчета")],
            )
            for name, synonym, types in sections
        ]

    def plan_content(self, obj: MetaObject) -> Prop:
        """Состав плана обмена (MD83Exp:1329-1384, 1471-1503): константы не выводятся."""
        items: list[Prop] = []
        for full_name, auto in obj.content:
            ru_kind, _, name = full_name.partition(".")
            if ru_kind == "Константа":
                continue
            target = self.metadata.get(full_name)
            if target is not None:
                type_name, synonym = _ref_type(full_name), target.synonym
            elif ru_kind == "Последовательность":
                type_name, synonym = full_name, ""  # MD83Exp последовательности не выгружает
            else:
                continue
            items.append(
                Prop(
                    name,
                    "ЭлементСоставаПланаОбмена",
                    simple_type(type_name),
                    synonym,
                    autoregistration=auto,
                )
            )
        return Prop(
            "{Состав}", "СоставПланаОбмена", None, "{Состав}", is_group=True, children=items
        )

    def object_properties(self, obj: MetaObject) -> list[Prop]:
        """Все свойства объекта в порядке `ВыгрузитьОбъекты` (MD83Exp:217-243)."""
        props = self.main_properties(obj) + self.own_properties(obj)
        props += self.tabular_sections(obj.tabulars)
        props += self.movements(obj)
        if obj.tag == "ChartOfCalculationTypes":
            props += self.calculation_tabulars(obj)
        if obj.tag == "ExchangePlan":
            props.append(self.plan_content(obj))
        return props

    def object_attrs(self, obj: MetaObject) -> dict[str, str]:
        """Настраиваемые свойства объекта (MD83Exp:275-406)."""
        props = obj.props
        periodicity = (
            props.get("NumberPeriodicity")
            or props.get("InformationRegisterPeriodicity")
            or props.get("Periodicity")
            or ""
        )
        return {
            "Иерархический": "true" if props.get("Hierarchical") == "true" else "false",
            "ВидИерархии": ENUM_TEXT.get(props.get("HierarchyType", ""), ""),
            "ОграничиватьКоличествоУровней": props.get("LimitLevelCount") or "false",
            "КоличествоУровней": props.get("LevelCount") or "0",
            "СерииКодов": ENUM_TEXT.get(props.get("CodeSeries", ""), ""),
            "КонтрольУникальности": props.get("CheckUnique") or "false",
            "АвтоНумерация": props.get("Autonumbering") or "false",
            "Периодичность": ENUM_TEXT.get(periodicity, periodicity),
            "Подчиненный": "true" if obj.lists.get("Owners") else "false",
        }


class _Writer:
    """Запись объектов, свойств и значений в схему `db.py`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.pending: list[tuple[int, list[str]]] = []  # (id свойства, имена типов)

    def add_object(
        self,
        kind: str,
        name: str,
        type_name: str,
        synonym: str,
        comment: str,
        group_id: int | None = None,
        attrs: dict[str, str] | None = None,
        is_group: bool = False,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO objects (kind, name, type_name, synonym, comment, is_group, group_id,"
            " attrs) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                name,
                type_name,
                synonym,
                comment,
                int(is_group),
                group_id,
                json.dumps(attrs or {}, ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid or 0)

    def add_props(
        self,
        object_id: int,
        props: list[Prop],
        parent_id: int | None = None,
        parent_path: str = "",
    ) -> None:
        for prop in props:
            path = f"{parent_path}.{prop.name}" if parent_path else prop.name
            resolved = prop.type or Resolved(date_parts="")
            date_parts = "" if prop.kind == "ЭлементСоставаПланаОбмена" else resolved.date_parts
            cursor = self.connection.execute(
                "INSERT INTO properties (object_id, parent_id, kind, name, path, synonym, comment,"
                " is_group, code, number_length, number_precision, number_nonnegative,"
                " string_length, string_fixed, date_parts, usage, indexing, autoregistration,"
                " fill_checking)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    object_id,
                    parent_id,
                    prop.kind,
                    prop.name,
                    path,
                    prop.synonym,
                    prop.comment,
                    int(prop.is_group),
                    resolved.number_length,
                    resolved.number_precision,
                    int(resolved.number_nonnegative),
                    resolved.string_length,
                    int(resolved.string_fixed),
                    date_parts,
                    prop.usage,
                    int(prop.indexing),
                    int(prop.autoregistration),
                    prop.fill_checking,
                ),
            )
            row_id = int(cursor.lastrowid or 0)
            if prop.type is not None:
                self.pending.append((row_id, prop.type.names))
            if prop.children:
                self.add_props(object_id, prop.children, row_id, path)

    def add_values(self, object_id: int, values: Iterable[Named]) -> None:
        self.connection.executemany(
            "INSERT INTO object_values (object_id, name, synonym, comment, code, predefined)"
            " VALUES (?, ?, ?, ?, '0', 1)",
            [(object_id, v.name, v.synonym, v.comment) for v in values],
        )

    def resolve_types(self) -> dict[str, int]:
        """Наборы типов с дедупликацией; тип без объекта в структуре — неразрешённый.

        Так же ведёт себя MD83Exp: GUID типа разрешается, только если выгружен объект с этим
        именем типа (MD83Exp:2079-2092).
        """
        query = self.connection.execute("SELECT type_name FROM objects")
        known = {row[0] for row in query}
        sets: dict[str, int] = {}
        unresolved: dict[str, int] = {}
        updates: list[tuple[int | None, str | None, int]] = []
        for row_id, names in self.pending:
            good = sorted({n for n in names if n in known})
            bad = sorted({n for n in names if n not in known})
            for name in bad:
                unresolved[name] = unresolved.get(name, 0) + 1
            set_id = None
            if good:
                key = "\n".join(good)
                set_id = sets.get(key)
                if set_id is None:
                    cursor = self.connection.execute(
                        "INSERT INTO type_sets (types) VALUES (?)", (key,)
                    )
                    set_id = int(cursor.lastrowid or 0)
                    sets[key] = set_id
                    self.connection.executemany(
                        "INSERT INTO type_set_items VALUES (?, ?)", [(set_id, n) for n in good]
                    )
            updates.append((set_id, "\n".join(bad) or None, row_id))
        self.connection.executemany(
            "UPDATE properties SET type_set_id = ?, unresolved = ? WHERE id = ?", updates
        )
        return unresolved


@dataclass(slots=True)
class BuildReport:
    """Итог сборки."""

    config_name: str = ""
    config_synonym: str = ""
    config_version: str = ""
    extensions: list[str] = field(default_factory=list)
    objects: int = 0
    properties: int = 0
    values: int = 0
    type_sets: int = 0
    unresolved_types: dict[str, int] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        """Счётчики для метаданных структуры."""
        return {
            "objects": self.objects,
            "properties": self.properties,
            "values": self.values,
            "type_sets": self.type_sets,
            "unresolved_types": len(self.unresolved_types),
            "orphans": 0,
        }


def build(metadata: Metadata, connection: sqlite3.Connection) -> BuildReport:
    """Записывает структуру объединённых метаданных в пустую базу `connection`."""
    builder = Builder(metadata)
    writer = _Writer(connection)
    stub_attrs = builder.object_attrs(MetaObject(tag="", name=""))
    for name, kind, synonym in STUBS:
        object_id = writer.add_object(kind, name, name, synonym, synonym, attrs=stub_attrs)
        if kind == "НаборКонстант":
            constants = [
                Field(c.name, c.synonym, c.comment, c.type or TypeDesc())
                for c in metadata.of("Constant")
            ]
            props = [p for c in constants for p in _field_props(c, "Реквизит", builder.resolver)]
            writer.add_props(object_id, props)
    for tag, (_, ru_kind, group_desc, group_name, prefix) in KINDS.items():
        objects = metadata.of(tag)
        if not objects:
            continue
        group_id = writer.add_object("", group_name, group_desc, group_desc, "", is_group=True)
        for obj in objects:
            attrs = builder.object_attrs(obj)
            object_id = writer.add_object(
                ru_kind, obj.name, f"{prefix}{obj.name}", obj.synonym, obj.comment, group_id, attrs
            )
            writer.add_props(object_id, builder.object_properties(obj))
            writer.add_values(object_id, [*obj.enum_values, *obj.predefined])
            if tag == "BusinessProcess":
                point_id = writer.add_object(
                    "ТочкаМаршрутаБизнесПроцесса",
                    obj.name,
                    f"ТочкаМаршрутаБизнесПроцессаСсылка.{obj.name}",
                    obj.synonym,
                    obj.comment,
                    group_id,
                    attrs,
                )
                writer.add_values(
                    point_id, [Named(p.name, p.name, p.name) for p in obj.route_points]
                )
    report = BuildReport(
        config_name=metadata.main.name,
        config_synonym=metadata.main.synonym,
        config_version=metadata.main.version,
        extensions=list(metadata.extensions),
    )
    report.unresolved_types = writer.resolve_types()
    count = connection.execute
    report.objects = count("SELECT COUNT(*) FROM objects").fetchone()[0]
    report.properties = count("SELECT COUNT(*) FROM properties").fetchone()[0]
    report.values = count("SELECT COUNT(*) FROM object_values").fetchone()[0]
    report.type_sets = count("SELECT COUNT(*) FROM type_sets").fetchone()[0]
    return report
