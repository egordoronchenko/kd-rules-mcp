"""Схема элементов правил КД 2: какие атрибуты и дочерние теги у каждого вида и в каком порядке.

Схема выписана из писателей КД 2.1.8.2 и задаёт одновременно разбор и вывод:
порядок слотов — порядок вывода, политика слота — когда значение выводится.
Ссылки на строки писателей (от `reference/kd2-cfg/DataProcessors/`):
`ВК:NNN` — `ВыгрузкаКонвертации/Ext/ObjectModule.bsl`,
`ВР:NNN` — `ВыгрузкаРегистрации/Ext/ObjectModule.bsl`.
"""

from dataclasses import dataclass, field
from enum import Enum


class ValueType(Enum):
    """Тип простого значения."""

    STR = "str"
    INT = "int"
    BOOL = "bool"


class Policy(Enum):
    """Когда значение выводится в XML."""

    # Как `ДобавитьЭлемент` (ВК:83-95): пустая строка и 0 не выводятся, булево — всегда.
    ADD = "add"
    # Как `ВыгрузитьБулевоЗначениеТолькоЕслиИстина` (ВК:740-748): только «истина».
    IF_TRUE = "if_true"
    # Атрибут выводится, если он есть в модели (УстановитьАтрибут пишет безусловно, ВК:104-114).
    KEEP = "keep"
    # Вложенный элемент выводится всегда, пустой — как `<Тег/>` (контейнеры списков).
    ALWAYS = "always"
    # Вложенный элемент выводится, только если в нём есть содержимое (ВК:762-765, ВР:402-410).
    IF_NOT_EMPTY = "if_not_empty"


Scalar = str | int | bool


@dataclass(frozen=True, slots=True)
class Attr:
    """Атрибут элемента."""

    name: str
    type: ValueType = ValueType.STR
    policy: Policy = Policy.KEEP
    # Значение нового узла; задано у атрибутов, которые писатель КД выводит всегда.
    default: "Scalar | None" = None


@dataclass(frozen=True, slots=True)
class Leaf:
    """Простой дочерний тег со скалярным значением в тексте."""

    tag: str
    type: ValueType = ValueType.STR
    policy: Policy = Policy.ADD


@dataclass(frozen=True, slots=True)
class Child:
    """Один вложенный элемент своего вида (например, `Источник` ПКС или контейнер `Свойства`)."""

    tag: str
    kind: str
    policy: Policy = Policy.ALWAYS


@dataclass(frozen=True, slots=True)
class Items:
    """Повторяющиеся вложенные элементы без обёртки: тег → вид (правила и группы)."""

    kinds: dict[str, str]


Slot = Leaf | Child | Items


@dataclass(slots=True)
class Kind:
    """Вид элемента правил."""

    name: str
    title: str
    attrs: tuple[Attr, ...] = ()
    slots: tuple[Slot, ...] = ()
    # Тип текста самого элемента (`<Источник ...>Имя</Источник>`), None — текста нет.
    text: ValueType | None = None
    leaves: dict[str, Leaf] = field(init=False)
    children: dict[str, Child] = field(init=False)
    items: dict[str, str] = field(init=False)
    attr_map: dict[str, Attr] = field(init=False)

    def __post_init__(self) -> None:
        self.leaves = {s.tag: s for s in self.slots if isinstance(s, Leaf)}
        self.children = {s.tag: s for s in self.slots if isinstance(s, Child)}
        self.items = {}
        for slot in self.slots:
            if isinstance(slot, Items):
                self.items.update(slot.kinds)
        self.attr_map = {a.name: a for a in self.attrs}


KINDS: dict[str, Kind] = {}


def _kind(name: str, title: str, **kwargs) -> Kind:
    kind = Kind(name, title, **kwargs)
    KINDS[name] = kind
    return kind


def kind(name: str) -> Kind:
    """Вид по имени."""
    return KINDS[name]


def _strs(*tags: str) -> tuple[Leaf, ...]:
    return tuple(Leaf(tag) for tag in tags)


def _flags(*tags: str) -> tuple[Leaf, ...]:
    return tuple(Leaf(tag, ValueType.BOOL, Policy.IF_TRUE) for tag in tags)


def _str_attr(name: str) -> Attr:
    """Строковый атрибут, который писатель выводит всегда (`УстановитьАтрибут`, ВК:104-114)."""
    return Attr(name, ValueType.STR, Policy.KEEP, "")


def _bool_attr(name: str, default: bool = False) -> Attr:
    """Булев атрибут, который писатель выводит всегда, и его значение для нового узла."""
    return Attr(name, ValueType.BOOL, Policy.KEEP, default)


_ORDER = Leaf("Порядок", ValueType.INT)
_BOOL_ATTR = ValueType.BOOL

# --- Общие части -------------------------------------------------------------------------------

# Источник/Приемник правил обмена и Конфигурация правил регистрации (ВК:374-387, ВР:120-131).
_kind(
    "config",
    "конфигурация",
    attrs=(
        _str_attr("ВерсияПлатформы"),
        _str_attr("ВерсияКонфигурации"),
        _str_attr("СинонимКонфигурации"),
    ),
    text=ValueType.STR,
)

# --- ПравилаОбмена -----------------------------------------------------------------------------

# ВерсияФормата с атрибутом РежимСовместимости (ВК:416-427).
_kind("format_version", "версия формата", attrs=(Attr("РежимСовместимости"),), text=ValueType.STR)

# События конвертации — плоский список обработчиков (ВК:437-457).
CONVERSION_EVENTS: tuple[str, ...] = (
    "ПослеЗагрузкиПравилОбмена",
    "ПередПолучениемИзмененныхОбъектов",
    "ПослеПолученияИнформацииОбУзлахОбмена",
    "ПередВыгрузкойДанных",
    "ПослеВыгрузкиДанных",
    "ПередЗагрузкойДанных",
    "ПослеЗагрузкиДанных",
    "ПередВыгрузкойОбъекта",
    "ПередКонвертациейОбъекта",
    "ПослеВыгрузкиОбъекта",
    "ПередЗагрузкойОбъекта",
    "ПослеЗагрузкиОбъекта",
    "ПередОтправкойИнформацииОбУдалении",
    "ПриПолученииИнформацииОбУдалении",
    "ПослеЗагрузкиПараметров",
)

# Параметры: новый формат — вложенные Параметр, старый — строка кодов в тексте (ВК:460-514).
_kind("parameters", "параметры", slots=(Items({"Параметр": "parameter"}),), text=ValueType.STR)
_kind(
    "parameter",
    "параметр",
    attrs=(
        _str_attr("Имя"),
        _str_attr("Наименование"),
        _bool_attr("ИспользуетсяПриЗагрузке"),
        _bool_attr("УстанавливатьВДиалоге"),
        Attr("ТипЗначения"),
        _bool_attr("ПередаватьПараметрПриВыгрузке"),
        Attr("ПравилоКонвертации"),
        Attr("ПослеЗагрузкиПараметра"),
    ),
)

# Обработки: тело — двоичные данные строкой, хранится как есть (ВК:516-553).
_kind("data_processors", "обработки", slots=(Items({"Обработка": "data_processor"}),))
_kind(
    "data_processor",
    "обработка",
    attrs=(
        _str_attr("Имя"),
        _str_attr("Наименование"),
        _str_attr("Параметры"),
        _str_attr("Комментарий"),
        _bool_attr("ИспользуетсяПриВыгрузке"),
        _bool_attr("ИспользуетсяПриЗагрузке"),
        _bool_attr("ЭтоОбработкаНастройки"),
    ),
    text=ValueType.STR,
)

_COMMON = (*_strs("Код", "Наименование"), _ORDER, *_strs("Описание", "Комментарий"))

# ПКО (ВК:792-888).
_PKO_ITEMS = Items({"Правило": "pko", "Группа": "pko_group"})
_kind("pko_list", "правила конвертации объектов", slots=(_PKO_ITEMS,))
_kind(
    "pko",
    "ПКО",
    slots=(
        *_COMMON,
        *_strs(
            "ПередВыгрузкой",
            "ПриВыгрузке",
            "ПослеВыгрузки",
            "ПослеВыгрузкиВФайл",
            "ПередЗагрузкой",
            "ПриЗагрузке",
            "ПослеЗагрузки",
            "ПоследовательностьПолейПоиска",
        ),
        *_flags(
            "НеЗамещать",
            "НеЗапоминатьВыгруженные",
            "СинхронизироватьПоИдентификатору",
            "ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли",
            "НеВыгружатьОбъектыСвойствПоСсылкам",
            "НеСоздаватьЕслиНеНайден",
            "ИспользоватьБыстрыйПоискПриЗагрузке",
            "ГенерироватьНовыйНомерИлиКодЕслиНеУказан",
            "ВыгружатьОбъектТолькоПриНаличииНаНегоСсылки",
            "ПриПереносеОбъектаПоСсылкеУстанавливатьТолькоGIUD",
            "НеЗамещатьОбъектСозданныйВИнформационнойБазеПриемнике",
        ),
        # «Ниже»/«Совпадает»; значение по умолчанию («выше») не выводится (ВК:831-840).
        Leaf("ПриоритетОбъектовОбмена"),
        *_strs("Источник", "Приемник"),
        Child("НастройкаВариантовПоискаОбъектов", "search_variants", Policy.IF_NOT_EMPTY),
        Child("Свойства", "pks_list"),
        Child("Значения", "pkz_list"),
    ),
)
# У группы ПКО писатель 2.1.8.2 атрибут Отключить не выводит (ВК:870-883), но в корпусе он
# встречается — принимаем и выводим только «истину».
_kind(
    "pko_group",
    "группа ПКО",
    attrs=(Attr("Отключить", _BOOL_ATTR, Policy.IF_TRUE),),
    slots=(*_COMMON, _PKO_ITEMS),
)
_kind("search_variants", "варианты поиска", slots=(Items({"ВариантПоиска": "search_variant"}),))
_kind(
    "search_variant",
    "вариант поиска",
    slots=_strs(
        "ИмяНастройкиДляАлгоритма",
        "ИмяНастройкиДляПользователя",
        "ОписаниеНастройкиДляПользователя",
    ),
)

# ПКС (ВК:615-738), дерево — ВК:1243-1290.
_PKS_ITEMS = Items({"Свойство": "pks", "Группа": "pks_group"})
_kind("pks_list", "правила конвертации свойств", slots=(_PKS_ITEMS,))
_kind(
    "pks_side",
    "сторона свойства",
    attrs=(_str_attr("Имя"), _str_attr("Вид"), Attr("Тип")),
)
_kind(
    "pks",
    "ПКС",
    attrs=(
        Attr("Отключить", _BOOL_ATTR, Policy.IF_TRUE),
        Attr("Поиск", _BOOL_ATTR, Policy.IF_TRUE),
        Attr("Обязательное", _BOOL_ATTR, Policy.IF_TRUE),
    ),
    slots=(
        *_flags("НеЗамещать"),
        *_COMMON,
        *_flags("ПолучитьИзВходящихДанных"),
        Child("Источник", "pks_side"),
        Child("Приемник", "pks_side"),
        Leaf("КодПравилаКонвертации"),
        Leaf("ПриводитьКДлине", ValueType.INT),
        Leaf("ИмяПараметраДляПередачи"),
        *_flags("ПоискПоДатеНаРавенство"),
        *_strs("ПередВыгрузкой", "ПриВыгрузке", "ПослеВыгрузки"),
    ),
)
_kind(
    "pks_group",
    "группа ПКС",
    attrs=(Attr("Отключить", _BOOL_ATTR, Policy.IF_TRUE),),
    slots=(
        *_COMMON,
        *_flags("НеЗамещать"),
        Leaf("КодПравилаКонвертации"),
        *_flags("ПолучитьИзВходящихДанных", "ВыгружатьГруппуЧерезФайл"),
        Child("Источник", "pks_side"),
        Child("Приемник", "pks_side"),
        *_strs(
            "ПередОбработкойВыгрузки",
            "ПередВыгрузкой",
            "ПриВыгрузке",
            "ПослеВыгрузки",
            "ПослеОбработкиВыгрузки",
        ),
        _PKS_ITEMS,
    ),
)

# ПКЗ (ВК:563-607).
_PKZ_ITEMS = Items({"Значение": "pkz", "Группа": "pkz_group"})
_kind("pkz_list", "правила конвертации значений", slots=(_PKZ_ITEMS,))
_kind("pkz", "ПКЗ", slots=(*_COMMON, *_strs("Источник", "Приемник")))
_kind("pkz_group", "группа ПКЗ", slots=(*_COMMON, _PKZ_ITEMS))

# ПВД (ВК:896-957).
_PVD_ITEMS = Items({"Правило": "pvd", "Группа": "pvd_group"})
_DISABLE = _bool_attr("Отключить")
_kind("pvd_list", "правила выгрузки данных", slots=(_PVD_ITEMS,))
_kind(
    "pvd",
    "ПВД",
    attrs=(_DISABLE,),
    slots=(
        *_COMMON,
        *_strs("КодПравилаКонвертации", "СпособОтбораДанных", "ОбъектВыборки"),
        *_flags(
            "ВыбиратьДанныеДляВыгрузкиОднимЗапросом", "НеВыгружатьОбъектыСозданныеВБазеПриемнике"
        ),
        *_strs(
            "ПередОбработкойПравила",
            "ПередВыгрузкойОбъекта",
            "ПослеВыгрузкиОбъекта",
            "ПослеОбработкиПравила",
        ),
    ),
)
_kind("pvd_group", "группа ПВД", attrs=(_DISABLE,), slots=(*_COMMON, _PVD_ITEMS))

# ПОД (ВК:965-1018): без Наименование, Непосредственно пишется через ДобавитьЭлемент.
_POD_ITEMS = Items({"Правило": "pod", "Группа": "pod_group"})
_POD_COMMON = (Leaf("Код"), _ORDER, *_strs("Описание", "Комментарий"))
_kind("pod_list", "правила очистки данных", slots=(_POD_ITEMS,))
_kind(
    "pod",
    "ПОД",
    attrs=(_DISABLE,),
    slots=(
        *_POD_COMMON,
        Leaf("Непосредственно", ValueType.BOOL),
        *_strs(
            "СпособОтбораДанных",
            "ОбъектВыборки",
            "ПередОбработкойПравила",
            "ПослеОбработкиПравила",
            "ПередУдалениемОбъекта",
        ),
    ),
)
_kind("pod_group", "группа ПОД", attrs=(_DISABLE,), slots=(*_POD_COMMON, _POD_ITEMS))

# Алгоритмы и запросы (ВК:1026-1110).
_ALG_ITEMS = Items({"Алгоритм": "algorithm", "Группа": "algorithm_group"})
_USED_ON_IMPORT = _bool_attr("ИспользуетсяПриЗагрузке")
_kind("algorithm_list", "алгоритмы", slots=(_ALG_ITEMS,))
_kind(
    "algorithm",
    "алгоритм",
    attrs=(_str_attr("Имя"), _USED_ON_IMPORT),
    slots=_strs("Текст", "Комментарий", "Параметры"),
)
_kind(
    "algorithm_group",
    "группа алгоритмов",
    attrs=(_str_attr("Имя"),),
    slots=(Leaf("Комментарий"), _ALG_ITEMS),
)
_QUERY_ITEMS = Items({"Запрос": "query", "Группа": "query_group"})
_kind("query_list", "запросы", slots=(_QUERY_ITEMS,))
_kind(
    "query",
    "запрос",
    attrs=(_str_attr("Имя"), _USED_ON_IMPORT),
    slots=_strs("Текст", "Комментарий"),
)
_kind(
    "query_group",
    "группа запросов",
    attrs=(_str_attr("Имя"),),
    slots=(Leaf("Комментарий"), _QUERY_ITEMS),
)

# Корень ПравилаОбмена (ВК:1449-1497, реквизиты ВК:394-414, события ВК:435-555).
EXCHANGE_RULES = _kind(
    "exchange_rules",
    "правила обмена",
    slots=(
        Child("ВерсияФормата", "format_version"),
        *_strs("Ид", "Наименование", "ДатаВремяСоздания"),
        Child("Источник", "config"),
        Child("Приемник", "config"),
        *_flags("УдалятьСопоставленныеОбъектыВПриемникеПриИхУдаленииВИсточнике"),
        Leaf("Комментарий"),
        *_strs(*CONVERSION_EVENTS),
        Child("Параметры", "parameters"),
        Child("Обработки", "data_processors"),
        Child("ПравилаКонвертацииОбъектов", "pko_list"),
        Child("ПравилаВыгрузкиДанных", "pvd_list"),
        Child("ПравилаОчисткиДанных", "pod_list"),
        Child("Алгоритмы", "algorithm_list"),
        Child("Запросы", "query_list"),
    ),
)

# --- ПравилаРегистрации ------------------------------------------------------------------------

_kind("exchange_plan", "план обмена", attrs=(_str_attr("Имя"),), text=ValueType.STR)  # ВР:89-101
_kind("plan_content", "состав плана обмена", slots=(Items({"Элемент": "plan_content_item"}),))
_kind(
    "plan_content_item",
    "элемент состава плана обмена",
    slots=(Leaf("Тип"), Leaf("Авторегистрация", ValueType.BOOL)),  # ВР:103-118
)

# Правила регистрации объектов и их группы (ВР:197-279).
_PRO_ITEMS = Items({"Правило": "pro", "Группа": "pro_group"})
_kind("pro_list", "правила регистрации объектов", slots=(_PRO_ITEMS,))
_kind(
    "pro",
    "ПРО",
    attrs=(_DISABLE, _bool_attr("Валидное", default=True)),
    slots=(
        *_strs(
            "Код",
            "Наименование",
            "Описание",
            "Комментарий",
            "ОбъектНастройки",
            "ОбъектМетаданныхИмя",
            "ОбъектМетаданныхТип",
            "РеквизитРежимаВыгрузки",
        ),
        Child("ОтборПоСвойствамПланаОбмена", "plan_filter"),
        Child("ОтборПоСвойствамОбъекта", "object_filter"),
        *_strs("ПередОбработкой", "ПриОбработке", "ПриОбработкеДополнительный", "ПослеОбработки"),
    ),
)
_kind(
    "pro_group",
    "группа ПРО",
    attrs=(_DISABLE,),
    slots=(*_strs("Код", "Наименование", "Описание", "Комментарий", "ТипГруппы"), _PRO_ITEMS),
)

# Отборы по свойствам плана обмена (ВР:323-360) и объекта (ВР:362-400).
_PLAN_FILTER_ITEMS = Items({"ЭлементОтбора": "plan_filter_item", "Группа": "plan_filter_group"})
_OBJECT_FILTER_ITEMS = Items(
    {"ЭлементОтбора": "object_filter_item", "Группа": "object_filter_group"}
)
_kind("plan_filter", "отбор по свойствам плана обмена", slots=(_PLAN_FILTER_ITEMS,))
_kind("object_filter", "отбор по свойствам объекта", slots=(_OBJECT_FILTER_ITEMS,))
_PROPERTY_TABLE = "property_table"
_kind(
    "plan_filter_item",
    "элемент отбора по плану обмена",
    slots=(
        Leaf("ЭтоСтрокаКонстанты", ValueType.BOOL),
        *_strs("ТипСвойстваОбъекта", "СвойствоПланаОбмена", "ВидСравнения", "СвойствоОбъекта"),
        Child("ТаблицаСвойствОбъекта", _PROPERTY_TABLE, Policy.IF_NOT_EMPTY),
        Child("ТаблицаСвойствПланаОбмена", _PROPERTY_TABLE, Policy.IF_NOT_EMPTY),
    ),
)
_kind(
    "plan_filter_group",
    "группа отбора по плану обмена",
    slots=(Leaf("БулевоЗначениеГруппы"), _PLAN_FILTER_ITEMS),
)
_kind(
    "object_filter_item",
    "элемент отбора по объекту",
    slots=(
        *_strs("ТипСвойстваОбъекта", "ВидСравнения", "СвойствоОбъекта", "Вид", "ЗначениеКонстанты"),
        Child("ТаблицаСвойствОбъекта", _PROPERTY_TABLE, Policy.IF_NOT_EMPTY),
    ),
)
_kind(
    "object_filter_group",
    "группа отбора по объекту",
    slots=(Leaf("БулевоЗначениеГруппы"), _OBJECT_FILTER_ITEMS),
)
_kind("property_table", "таблица свойств", slots=(Items({"Свойство": "property_row"}),))
_kind("property_row", "свойство", slots=_strs("Наименование", "Тип", "Вид"))  # ВР:402-420

# Корень ПравилаРегистрации (ВР:67-87, реквизиты ВР:135-153).
REGISTRATION_RULES = _kind(
    "registration_rules",
    "правила регистрации",
    slots=(
        *_strs("ВерсияФормата", "Ид", "Наименование", "ДатаВремяСоздания"),
        Child("ПланОбмена", "exchange_plan"),
        Child("Конфигурация", "config"),
        Leaf("Комментарий"),
        Child("СоставПланаОбмена", "plan_content"),
        Child("ПравилаРегистрацииОбъектов", "pro_list"),
    ),
)

ROOT_KINDS: dict[str, Kind] = {
    "ПравилаОбмена": EXCHANGE_RULES,
    "ПравилаРегистрации": REGISTRATION_RULES,
}
