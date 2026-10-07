"""Чтение XML-выгрузки конфигурации (и расширений) в лёгкую модель метаданных.

Из каждого файла объекта берётся только то, что нужно для структуры в стиле MD83Exp: свойства
объекта, реквизиты, измерения, ресурсы, табличные части, значения перечислений, состав плана
обмена, предопределённые элементы. Деревья lxml после разбора файла отбрасываются — в памяти
остаются небольшие dataclass-ы.
"""

from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

# Виды объектов, которые выгружает MD83Exp, в порядке `ВыполнитьВыгрузку` (MD83Exp, строки 28–104).
# Тег XML → (каталог выгрузки, вид КД, Description группы, Имя группы, префикс имени типа).
KINDS: dict[str, tuple[str, str, str, str, str]] = {
    "ExchangePlan": (
        "ExchangePlans",
        "ПланОбмена",
        "ПланыОбмена",
        "Планы обмена",
        "ПланОбменаСсылка.",
    ),
    "Catalog": ("Catalogs", "Справочник", "Справочники", "Справочники", "СправочникСсылка."),
    "Document": ("Documents", "Документ", "Документы", "Документы", "ДокументСсылка."),
    "Enum": ("Enums", "Перечисление", "Перечисления", "Перечисления", "ПеречислениеСсылка."),
    "ChartOfCharacteristicTypes": (
        "ChartsOfCharacteristicTypes",
        "ПланВидовХарактеристик",
        "ПланыВидовХарактеристик",
        "Планы видов характеристик",
        "ПланВидовХарактеристикСсылка.",
    ),
    "ChartOfAccounts": (
        "ChartsOfAccounts",
        "ПланСчетов",
        "ПланыСчетов",
        "Планы счетов",
        "ПланСчетовСсылка.",
    ),
    "ChartOfCalculationTypes": (
        "ChartsOfCalculationTypes",
        "ПланВидовРасчета",
        "ПланыВидовРасчета",
        "Планы видов расчета",
        "ПланВидовРасчетаСсылка.",
    ),
    "InformationRegister": (
        "InformationRegisters",
        "РегистрСведений",
        "РегистрыСведений",
        "Регистры сведений",
        "РегистрСведенийЗапись.",
    ),
    "AccumulationRegister": (
        "AccumulationRegisters",
        "РегистрНакопления",
        "РегистрыНакопления",
        "Регистры накопления",
        "РегистрНакопленияЗапись.",
    ),
    "AccountingRegister": (
        "AccountingRegisters",
        "РегистрБухгалтерии",
        "РегистрыБухгалтерии",
        "Регистры бухгалтерии",
        "РегистрБухгалтерииЗапись.",
    ),
    "CalculationRegister": (
        "CalculationRegisters",
        "РегистрРасчета",
        "РегистрыРасчета",
        "Регистры расчета",
        "РегистрРасчетаЗапись.",
    ),
    "BusinessProcess": (
        "BusinessProcesses",
        "БизнесПроцесс",
        "БизнесПроцессы",
        "Бизнес-процессы",
        "БизнесПроцессСсылка.",
    ),
    "Task": ("Tasks", "Задача", "Задачи", "Задачи", "ЗадачаСсылка."),
}
# Вспомогательные виды: читаются для разрешения типов и общих реквизитов.
AUX_KINDS: dict[str, str] = {
    "DefinedType": "DefinedTypes",
    "Constant": "Constants",
    "CommonAttribute": "CommonAttributes",
    "Sequence": "Sequences",
}
# Русское имя вида по тегу XML — для полных имён вида `Справочник.Валюты` и ссылок `xr:MDObjectRef`.
RU_KIND = {tag: info[1] for tag, info in KINDS.items()} | {
    "Constant": "Константа",
    "Sequence": "Последовательность",
    "DefinedType": "ОпределяемыйТип",
    "CommonAttribute": "ОбщийРеквизит",
}
REGISTER_TAGS = frozenset(
    {"InformationRegister", "AccumulationRegister", "AccountingRegister", "CalculationRegister"}
)


def local(element: etree._Element) -> str:
    """Имя тега без пространства имён.

    `QName` на каждом теге заметно дороже среза: у элемента lxml тег уже в форме `{uri}Имя`.
    """
    tag = element.tag
    if isinstance(tag, str):
        return tag[tag.rfind("}") + 1 :]
    return etree.QName(element).localname


def child(element: etree._Element | None, name: str) -> etree._Element | None:
    """Первый дочерний элемент с локальным именем `name`."""
    if element is None:
        return None
    for item in element:
        if isinstance(item.tag, str) and local(item) == name:
            return item
    return None


def children(element: etree._Element | None, name: str) -> list[etree._Element]:
    """Все дочерние элементы с локальным именем `name`."""
    if element is None:
        return []
    return [i for i in element if isinstance(i.tag, str) and local(i) == name]


def text(element: etree._Element | None, name: str, default: str = "") -> str:
    """Текст дочернего элемента `name`."""
    found = child(element, name)
    return (found.text or "").strip() if found is not None else default


def ru_text(element: etree._Element | None) -> str:
    """Русский текст многоязычного значения (`Synonym`, `Comment`…)."""
    if element is None:
        return ""
    for item in children(element, "item"):
        if text(item, "lang") == "ru":
            return text(item, "content")
    return (element.text or "").strip()


@dataclass(slots=True)
class TypeDesc:
    """Описание типа из XML: записи `Type`/`TypeSet` и квалификаторы."""

    entries: list[tuple[str, str]] = field(default_factory=list)  # (Type|TypeSet, значение)
    string_length: int | None = None
    string_fixed: bool = False
    number_length: int | None = None
    number_precision: int = 0
    number_nonnegative: bool = False
    date_parts: str | None = None  # Date / Time / DateTime
    # Тип заимствованного объекта в расширении: `ExtendValue` дополняет тип основной конфигурации.
    extends: bool = False

    def widen(self, other: "TypeDesc") -> None:
        """Дополняет тип расширением: объединение типов и расширение квалификаторов.

        Правило сверено с MD83Exp базы с расширениями: Число(15,3) + Число(15,8) → Число(20,8)
        (целая и дробная части — по максимуму), неотрицательность — только если у обоих,
        Строка(10) + Строка(16) → Строка(16), неограниченная длина (0) побеждает.
        """
        self.entries.extend(e for e in other.entries if e not in self.entries)
        if other.number_length is not None:
            if self.number_length is None:
                self.number_length = other.number_length
                self.number_precision = other.number_precision
                self.number_nonnegative = other.number_nonnegative
            else:
                whole = max(
                    self.number_length - self.number_precision,
                    other.number_length - other.number_precision,
                )
                self.number_precision = max(self.number_precision, other.number_precision)
                self.number_length = whole + self.number_precision
                self.number_nonnegative = self.number_nonnegative and other.number_nonnegative
        if other.string_length is not None:
            if self.string_length is None:
                self.string_length = other.string_length
                self.string_fixed = other.string_fixed
            else:
                unlimited = 0 in (self.string_length, other.string_length)
                self.string_length = (
                    0 if unlimited else max(self.string_length, other.string_length)
                )
                self.string_fixed = self.string_fixed and other.string_fixed
        if other.date_parts:
            if not self.date_parts:
                self.date_parts = other.date_parts
            elif self.date_parts != other.date_parts:
                self.date_parts = "DateTime"


def read_type(element: etree._Element | None) -> TypeDesc:
    """Описание типа из элемента `Type` выгрузки (у заимствованных — из `ExtendValue`)."""
    result = TypeDesc()
    if element is None:
        return result
    extend = child(element, "ExtendValue")
    if extend is not None:
        result = read_type(extend)
        result.extends = True
        return result
    for item in element:
        if not isinstance(item.tag, str):
            continue
        name = local(item)
        if name in ("Type", "TypeSet"):
            value = (item.text or "").strip()
            result.entries.append((name, value.split(":", 1)[-1]))
        elif name == "StringQualifiers":
            result.string_length = int(text(item, "Length", "0") or 0)
            result.string_fixed = text(item, "AllowedLength") == "Fixed"
        elif name == "NumberQualifiers":
            result.number_length = int(text(item, "Digits", "0") or 0)
            result.number_precision = int(text(item, "FractionDigits", "0") or 0)
            result.number_nonnegative = text(item, "AllowedSign") == "Nonnegative"
        elif name == "DateQualifiers":
            result.date_parts = text(item, "DateFractions")
    return result


@dataclass(slots=True)
class Field:
    """Реквизит, измерение, ресурс, константа, реквизит адресации, реквизит табличной части."""

    name: str
    synonym: str = ""
    comment: str = ""
    type: TypeDesc = field(default_factory=TypeDesc)
    usage: str = ""  # Use: ForItem / ForFolder / ForFolderAndItem
    indexing: str = ""  # Indexing: Index / IndexWithAdditionalOrder / DontIndex
    balance: bool = True  # Balance у измерений и ресурсов регистра бухгалтерии
    adopted: bool = False  # заимствован расширением
    fill_checking: str = ""  # FillChecking; пусто — сведения отсутствуют


@dataclass(slots=True)
class Tabular:
    """Табличная часть."""

    name: str
    synonym: str = ""
    comment: str = ""
    fields: list[Field] = field(default_factory=list)
    adopted: bool = False


@dataclass(slots=True)
class Named:
    """Значение перечисления, предопределённый элемент, точка маршрута."""

    name: str
    synonym: str = ""
    comment: str = ""
    adopted: bool = False


@dataclass(slots=True)
class MetaObject:
    """Объект метаданных из выгрузки (только нужное для структуры)."""

    tag: str
    name: str
    synonym: str = ""
    comment: str = ""
    props: dict[str, str] = field(default_factory=dict)  # простые свойства: имя → текст
    lists: dict[str, list[str]] = field(default_factory=dict)  # Owners, RegisterRecords…
    type: TypeDesc | None = None  # тип ПВХ, определяемого типа, константы, общего реквизита
    dimensions: list[Field] = field(default_factory=list)
    resources: list[Field] = field(default_factory=list)
    attributes: list[Field] = field(default_factory=list)
    addressing: list[Field] = field(default_factory=list)
    tabulars: list[Tabular] = field(default_factory=list)
    enum_values: list[Named] = field(default_factory=list)
    predefined: list[Named] = field(default_factory=list)
    route_points: list[Named] = field(default_factory=list)
    content: list[tuple[str, bool]] = field(default_factory=list)  # (Вид.Имя, авторегистрация)
    common_content: list[tuple[str, str]] = field(default_factory=list)  # (Вид.Имя, Use)
    adopted: bool = False

    @property
    def full_name(self) -> str:
        """Полное имя в нотации 1С: `Справочник.Валюты`."""
        return f"{RU_KIND.get(self.tag, self.tag)}.{self.name}"


# Элементы Properties, которые являются списками ссылок на метаданные.
_LIST_PROPS = ("Owners", "RegisterRecords", "BaseCalculationTypes")


def _field(element: etree._Element) -> Field:
    props = child(element, "Properties")
    return Field(
        name=text(props, "Name"),
        synonym=ru_text(child(props, "Synonym")),
        comment=ru_text(child(props, "Comment")),
        type=read_type(child(props, "Type")),
        usage=text(props, "Use"),
        indexing=text(props, "Indexing"),
        balance=text(props, "Balance", "true") != "false",
        adopted=text(props, "ObjectBelonging") == "Adopted",
        fill_checking=text(props, "FillChecking", "DontCheck"),
    )


def _md_ref(value: str) -> str:
    """`Catalog.Валюты` → `Справочник.Валюты`."""
    tag, _, name = value.partition(".")
    return f"{RU_KIND.get(tag, tag)}.{name}"


def _predefined(items: list[etree._Element], out: list[Named]) -> None:
    for item in items:
        out.append(Named(text(item, "Name"), text(item, "Description"), text(item, "Description")))
        _predefined(children(child(item, "ChildItems"), "Item"), out)


def read_object(path: Path, tag: str) -> MetaObject:
    """Читает файл объекта выгрузки и связанные файлы `Ext/`."""
    root = etree.parse(str(path)).getroot()
    element = child(root, tag)
    if element is None:
        raise ValueError(f"В файле {path} нет элемента {tag}")
    props = child(element, "Properties")
    result = MetaObject(
        tag=tag,
        name=text(props, "Name"),
        synonym=ru_text(child(props, "Synonym")),
        comment=ru_text(child(props, "Comment")),
        adopted=text(props, "ObjectBelonging") == "Adopted",
    )
    for item in props if props is not None else []:
        if not isinstance(item.tag, str):
            continue
        name = local(item)
        if name in _LIST_PROPS:
            result.lists[name] = [_md_ref((i.text or "").strip()) for i in item if i.text]
        elif name == "Type":
            result.type = read_type(item)
        elif name == "StandardAttributes":
            for attribute in children(item, "StandardAttribute"):
                result.props[f"FillChecking.{attribute.get('name', '')}"] = text(
                    attribute, "FillChecking", "DontCheck"
                )
        elif name == "Content" and tag == "CommonAttribute":
            result.common_content = [
                (_md_ref(text(i, "Metadata")), text(i, "Use")) for i in children(item, "Item")
            ]
        elif len(item) == 0:
            result.props[name] = (item.text or "").strip()
        else:
            # Составное свойство (например, непустой BasedOn): важен сам факт наличия —
            # по нему MD83Exp решает, есть ли у объекта свойство (MD83Exp:410-414).
            result.props[name] = ""
    objects = child(element, "ChildObjects")
    for item in objects if objects is not None else []:
        if not isinstance(item.tag, str):
            continue
        name = local(item)
        if name == "Attribute":
            result.attributes.append(_field(item))
        elif name == "Dimension":
            result.dimensions.append(_field(item))
        elif name == "Resource":
            result.resources.append(_field(item))
        elif name == "AddressingAttribute":
            result.addressing.append(_field(item))
        elif name == "TabularSection":
            tab_props = child(item, "Properties")
            result.tabulars.append(
                Tabular(
                    name=text(tab_props, "Name"),
                    synonym=ru_text(child(tab_props, "Synonym")),
                    comment=ru_text(child(tab_props, "Comment")),
                    fields=[_field(f) for f in children(child(item, "ChildObjects"), "Attribute")],
                    adopted=text(tab_props, "ObjectBelonging") == "Adopted",
                )
            )
        elif name == "EnumValue":
            value_props = child(item, "Properties")
            result.enum_values.append(
                Named(
                    text(value_props, "Name"),
                    ru_text(child(value_props, "Synonym")),
                    ru_text(child(value_props, "Comment")),
                    adopted=text(value_props, "ObjectBelonging") == "Adopted",
                )
            )
    ext = path.with_suffix("") / "Ext"
    predefined = ext / "Predefined.xml"
    if predefined.is_file():
        _predefined(children(etree.parse(str(predefined)).getroot(), "Item"), result.predefined)
    content = ext / "Content.xml"
    if tag == "ExchangePlan" and content.is_file():
        result.content = [
            (_md_ref(text(i, "Metadata")), text(i, "AutoRecord") == "Allow")
            for i in children(etree.parse(str(content)).getroot(), "Item")
        ]
    flowchart = ext / "Flowchart.xml"
    if tag == "BusinessProcess" and flowchart.is_file():
        result.route_points = _route_points(etree.parse(str(flowchart)).getroot())
    return result


# Элементы карты маршрута, которые не являются точками маршрута.
_NOT_ROUTE_POINTS = frozenset({"ConnectionLine", "Decoration"})


def _route_points(root: etree._Element) -> list[Named]:
    """Точки маршрута — элементы `Items` карты, кроме линий и оформления."""
    items = child(root, "Items")
    return [
        Named(text(child(item, "Properties"), "Name"))
        for item in (items if items is not None else [])
        if isinstance(item.tag, str) and local(item) not in _NOT_ROUTE_POINTS
    ]


@dataclass(slots=True)
class ConfigDump:
    """Выгрузка конфигурации или расширения: имя, версия и объекты по видам в порядке выгрузки."""

    root: Path
    name: str = ""
    version: str = ""
    synonym: str = ""
    is_extension: bool = False
    objects: dict[str, list[MetaObject]] = field(default_factory=dict)
    subscriptions: list["EventSubscription"] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EventSubscription:
    """Подписка: типы источников без префикса cfg; UUID основной подписки сохраняется."""

    name: str
    uuid: str
    event: str
    handler: str
    sources: tuple[str, ...]
    extended_uuid: str = ""

    def covers(self, source: str) -> bool:
        """Учитывает конкретный тип и общий тип объектов данного вида."""
        names = {s.casefold() for s in self.sources}
        return source.casefold() in names or source.partition(".")[0].casefold() in names


def read_subscription(path: Path) -> EventSubscription:
    """Форма основной подписки и заимствования расширением: опись ed-writer-plan-content-2026-10."""
    obj = child(etree.parse(str(path)).getroot(), "EventSubscription")
    if obj is None:
        raise ValueError(f"Нет EventSubscription в {path}")
    props = child(obj, "Properties")
    return EventSubscription(
        text(props, "Name"),
        obj.get("uuid", ""),
        text(props, "Event"),
        text(props, "Handler"),
        tuple(value for _, value in read_type(child(props, "Source")).entries),
        text(props, "ExtendedConfigurationObject")
        if text(props, "ObjectBelonging") == "Adopted"
        else "",
    )


def plan_subscriptions(
    subscriptions: tuple[EventSubscription, ...] | list[EventSubscription], plan_name: str
) -> tuple[EventSubscription, ...]:
    """Критерий БСП по Handler, а не по имени подписки; описание — docs/checks.md."""
    result = []
    for subscription in subscriptions:
        parts = subscription.handler.split(".")
        if (
            len(parts) == 3
            and parts[0].casefold() == "commonmodule"
            and parts[1].casefold().startswith("обменданнымисобытия")
            and parts[2].casefold().startswith(plan_name.casefold())
            and subscription.event in {"BeforeWrite", "BeforeDelete"}
        ):
            result.append(subscription)
    return tuple(sorted(result, key=lambda s: s.name))


def registration_events(kind: str, name: str) -> tuple[tuple[str, str], ...]:
    """События и тип источника: ссылочные объекты и наборы записей (опись формата)."""
    tag = next((tag for tag, info in KINDS.items() if info[1].casefold() == kind.casefold()), "")
    if not tag or tag == "Enum":
        return ()
    source = tag + ("RecordSet." if tag in REGISTER_TAGS else "Object.") + name
    events = ("BeforeWrite",) if tag in REGISTER_TAGS else ("BeforeWrite", "BeforeDelete")
    return tuple((event, source) for event in events)


def registration_source_object(source: str) -> tuple[str, str] | None:
    """Тег XML и имя объекта по типу источника подписки — обратное `registration_events`."""
    type_name, _, name = source.partition(".")
    for tag in KINDS:
        suffix = "RecordSet" if tag in REGISTER_TAGS else "Object"
        if name and tag != "Enum" and type_name == tag + suffix:
            return tag, name
    return None


def subscription_accepts(subscription: EventSubscription, event: str, source: str) -> bool:
    """Разные сигнатуры BeforeWrite: документ, набор записей, прочий ссылочный объект."""

    def family(value: str) -> str:
        tag = value.partition(".")[0]
        return (
            "recordset"
            if tag.endswith("RecordSet")
            else "document"
            if tag == "DocumentObject"
            else "object"
        )

    return subscription.event == event and any(
        family(value) == family(source) or event == "BeforeDelete" for value in subscription.sources
    )


def read_dump(root: Path) -> ConfigDump:
    """Читает `Configuration.xml` и все объекты нужных видов."""
    config = etree.parse(str(root / "Configuration.xml")).getroot()
    configuration = child(config, "Configuration")
    props = child(configuration, "Properties")
    dump = ConfigDump(
        root=root,
        name=text(props, "Name"),
        version=text(props, "Version"),
        synonym=ru_text(child(props, "Synonym")),
        is_extension=child(props, "ConfigurationExtensionPurpose") is not None,
    )
    directories = {tag: info[0] for tag, info in KINDS.items()} | AUX_KINDS
    child_objects = child(configuration, "ChildObjects")
    for item in child_objects if child_objects is not None else []:
        if not isinstance(item.tag, str):
            continue
        tag = local(item)
        if tag == "EventSubscription":
            path = root / "EventSubscriptions" / f"{(item.text or '').strip()}.xml"
            dump.subscriptions.append(read_subscription(path))
            continue
        directory = directories.get(tag)
        if directory is None:
            continue
        path = root / directory / f"{(item.text or '').strip()}.xml"
        dump.objects.setdefault(tag, []).append(read_object(path, tag))
    return dump
