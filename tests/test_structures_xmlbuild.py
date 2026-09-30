"""Сборка структуры из синтетической XML-выгрузки (спецификация metadata-structures,
требование «Сборка структуры из XML-выгрузки», и правила MD83Exp, которые повторяет xmlbuild).

Выгрузка: `tests/data/xmldump/main` — конфигурация, `ext` — перечисляемое расширение,
`ext_unlisted` — выгрузка, которую в сборку не передают.
"""

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.structures.queries import (
    NotFound,
    Page,
    describe_object,
    exchange_plan_content,
    object_values,
)
from kd2_rules_mcp.structures.store import LoadResult, StructureStore

DUMP = Path(__file__).parent / "data" / "xmldump"
MAIN = DUMP / "main"
EXT = DUMP / "ext"
UNLISTED = DUMP / "ext_unlisted"

SAMPLES = "СправочникСсылка.ОбразцыТипов"
NOMENCLATURE = "СправочникСсылка.Номенклатура"
CONTRACTORS = "СправочникСсылка.Контрагенты"
RECEIPT = "ДокументСсылка.Приход"
EXPENSE = "ДокументСсылка.Расход"
BALANCES = "РегистрНакопленияЗапись.Остатки"
RATES = "РегистрСведенийЗапись.Курсы"
ACCOUNTING = "РегистрБухгалтерииЗапись.Хозрасчетный"
SUBCONTO = "ПланВидовХарактеристикСсылка.Субконто"
ACCRUALS = "ПланВидовРасчетаСсылка.Начисления"
DEDUCTIONS = "ПланВидовРасчетаСсылка.Удержания"
PROCESS = "БизнесПроцессСсылка.Согласование"
TASK = "ЗадачаСсылка.ЗадачаИсполнителя"

CATALOG_REFS = sorted(
    [
        "СправочникСсылка.Бригады",
        "СправочникСсылка.Контрагенты",
        "СправочникСсылка.Номенклатура",
        "СправочникСсылка.ОбразцыТипов",
    ]
)
CHARACTERISTIC_REFS = sorted(
    [
        "СправочникСсылка.Контрагенты",
        "СправочникСсылка.Номенклатура",
    ]
)
DEFINED_TYPE_REFS = sorted(
    [
        "ДокументСсылка.Приход",
        "СправочникСсылка.Номенклатура",
        "Строка",
    ]
)
ANY_REFS = sorted(
    [
        *CATALOG_REFS,
        "ДокументСсылка.Приход",
        "ДокументСсылка.Расход",
        "ПеречислениеСсылка.Виды",
        "ПланВидовРасчетаСсылка.Начисления",
        "ПланВидовРасчетаСсылка.Удержания",
        "ПланВидовХарактеристикСсылка.Субконто",
        "ПланОбменаСсылка.Обмен",
        "ПланСчетовСсылка.Хозрасчетный",
        "БизнесПроцессСсылка.Согласование",
        "ТочкаМаршрутаБизнесПроцессаСсылка.Согласование",
        "ЗадачаСсылка.ЗадачаИсполнителя",
    ]
)
CALCULATION_PLANS = sorted(
    [
        "ПланВидовРасчетаСсылка.Начисления",
        "ПланВидовРасчетаСсылка.Удержания",
    ]
)
RECORDERS = sorted(["ДокументСсылка.Приход", "ДокументСсылка.Расход"])


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[StructureStore, LoadResult]:
    """Структура из main и перечисленного расширения ext."""
    store = StructureStore(tmp_path_factory.mktemp("cache"))
    result = store.load_xml("synth", MAIN, [EXT])
    return store, result


@pytest.fixture
def store(built: tuple[StructureStore, LoadResult]) -> StructureStore:
    return built[0]


@pytest.fixture
def result(built: tuple[StructureStore, LoadResult]) -> LoadResult:
    return built[1]


@pytest.fixture
def conn(store: StructureStore) -> Iterator[sqlite3.Connection]:
    connection = store.open("synth")
    yield connection
    connection.close()


def property_row(connection: sqlite3.Connection, type_name: str, path: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT p.kind, p.name, p.path, p.synonym, p.comment, p.is_group, p.number_length,"
        " p.number_precision, p.number_nonnegative, p.string_length, p.string_fixed,"
        " p.date_parts, p.unresolved, ts.types AS types"
        " FROM properties AS p JOIN objects AS o ON o.id = p.object_id"
        " LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id"
        " WHERE o.type_name = ? AND p.path = ?",
        (type_name, path),
    ).fetchone()
    assert row is not None, f"нет свойства {type_name} / {path}"
    return row


def property_paths(connection: sqlite3.Connection, type_name: str) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT p.path FROM properties AS p JOIN objects AS o ON o.id = p.object_id"
            " WHERE o.type_name = ? ORDER BY p.id",
            (type_name,),
        )
    ]


def split_types(value: str | None) -> list[str]:
    return value.split("\n") if value else []


def assert_qualifiers(row: sqlite3.Row, **overrides: object) -> None:
    expected: dict[str, object] = {
        "number_length": 0,
        "number_precision": 0,
        "number_nonnegative": 0,
        "string_length": 0,
        "string_fixed": 0,
        "date_parts": "Дата и время",
    }
    expected.update(overrides)
    for key, value in expected.items():
        assert row[key] == value, f"{row['path']}.{key}: {row[key]!r} != {value!r}"


@pytest.mark.parametrize(
    ("path", "types", "qualifiers"),
    [
        pytest.param(
            "СтрокаФикс", ["Строка"], {"string_length": 10, "string_fixed": 1}, id="string-fixed"
        ),
        pytest.param("СтрокаПерем", ["Строка"], {"string_length": 25}, id="string-variable"),
        pytest.param(
            "ЧислоНеотр",
            ["Число"],
            {"number_length": 15, "number_precision": 3, "number_nonnegative": 1},
            id="decimal-nonnegative",
        ),
        pytest.param("ДатаДата", ["Дата"], {"date_parts": "Дата"}, id="date"),
        pytest.param("ДатаВремяСуток", ["Дата"], {"date_parts": "Время"}, id="time"),
        pytest.param("ДатаДатаВремя", ["Дата"], {"date_parts": "Дата и время"}, id="datetime"),
        pytest.param("Булево", ["Булево"], {}, id="boolean-without-date"),
        pytest.param("Хранилище", ["ХранилищеЗначения"], {}, id="value-storage"),
        pytest.param("Уид", ["УникальныйИдентификатор"], {}, id="uuid"),
    ],
)
def test_primitive_type_translation(
    conn: sqlite3.Connection,
    path: str,
    types: list[str],
    qualifiers: dict[str, object],
) -> None:
    """Примитивы с квалификаторами; у свойства без даты части даты — «Дата и время»."""
    row = property_row(conn, SAMPLES, path)
    assert row["kind"] == "Реквизит"
    assert split_types(row["types"]) == types
    assert row["unresolved"] is None
    assert_qualifiers(row, **qualifiers)


@pytest.mark.parametrize(
    ("path", "type_name"),
    [
        ("СсылкаСправочник", "СправочникСсылка.Номенклатура"),
        ("СсылкаДокумент", "ДокументСсылка.Приход"),
        ("СсылкаПеречисление", "ПеречислениеСсылка.Виды"),
        ("СсылкаПланСчетов", "ПланСчетовСсылка.Хозрасчетный"),
        ("СсылкаПВХ", "ПланВидовХарактеристикСсылка.Субконто"),
        ("СсылкаПВР", "ПланВидовРасчетаСсылка.Начисления"),
        ("СсылкаПланОбмена", "ПланОбменаСсылка.Обмен"),
        ("СсылкаБизнесПроцесс", "БизнесПроцессСсылка.Согласование"),
        ("СсылкаТочкаМаршрута", "ТочкаМаршрутаБизнесПроцессаСсылка.Согласование"),
        ("СсылкаЗадача", "ЗадачаСсылка.ЗадачаИсполнителя"),
    ],
)
def test_reference_type_translation(conn: sqlite3.Connection, path: str, type_name: str) -> None:
    """Ссылочные типы всех видов из REFS."""
    row = property_row(conn, SAMPLES, path)
    assert split_types(row["types"]) == [type_name]
    assert row["unresolved"] is None
    assert_qualifiers(row)


def test_defined_type_expands_nested_defined_type(conn: sqlite3.Connection) -> None:
    """TypeSet определяемого типа раскрывается, в том числе определяемый внутри определяемого.

    Документная ссылка добавлена заимствованным определяемым типом расширения.
    """
    row = property_row(conn, SAMPLES, "Определяемый")
    assert split_types(row["types"]) == DEFINED_TYPE_REFS
    assert_qualifiers(row, string_length=20)
    assert row["unresolved"] is None


def test_characteristic_type_is_chart_value_type(conn: sqlite3.Connection) -> None:
    """cfg:Characteristic.X — тип плана видов характеристик."""
    row = property_row(conn, SAMPLES, "Характеристика")
    assert split_types(row["types"]) == CHARACTERISTIC_REFS
    assert row["unresolved"] is None


@pytest.mark.parametrize("path", ["ЛюбаяСсылка", "ЛюбаяСсылкаКонфигурации"])
def test_any_ref_includes_every_reference(conn: sqlite3.Connection, path: str) -> None:
    """cfg:AnyIBRef и cfg:AnyRef — все ссылочные типы конфигурации с наложенным расширением."""
    row = property_row(conn, SAMPLES, path)
    assert split_types(row["types"]) == ANY_REFS
    assert row["unresolved"] is None


def test_catalog_ref_type_set_includes_every_catalog(conn: sqlite3.Connection) -> None:
    """TypeSet cfg:CatalogRef — ссылки на все справочники, включая собственный объект расширения."""
    row = property_row(conn, SAMPLES, "ВсеСправочники")
    assert split_types(row["types"]) == CATALOG_REFS
    assert "СправочникСсылка.Лишний" not in split_types(row["types"])


def test_unresolved_type_does_not_stop_build(
    conn: sqlite3.Connection, result: LoadResult, store: StructureStore
) -> None:
    """Неизвестный тип: сборка завершается, тип помечен у свойства, в итоге и в метаданных."""
    assert result.reused is False
    assert result.counts["objects"] > 0
    assert property_paths(conn, NOMENCLATURE)

    missing = property_row(conn, SAMPLES, "НетОпределяемого")
    period = property_row(conn, SAMPLES, "СтандартныйПериод")
    assert split_types(missing["types"]) == []
    assert split_types(period["types"]) == []
    assert missing["unresolved"] == "DefinedType.Отсутствует"
    assert period["unresolved"] == "StandardPeriod"

    described = describe_object(conn, "Справочник.ОбразцыТипов", limit=50)
    if isinstance(described, NotFound):
        raise AssertionError(described.message)
    by_path = {item["path"]: item for item in described["properties"].items}
    assert by_path["НетОпределяемого"]["unresolved"] == ["DefinedType.Отсутствует"]
    assert by_path["НетОпределяемого"]["types"] == []
    assert by_path["СтандартныйПериод"]["unresolved"] == ["StandardPeriod"]

    expected = {
        "DefinedType.Отсутствует": 1,
        "StandardPeriod": 1,
        "Последовательность.ДокументыОрганизаций": 1,
    }
    assert result.unresolved == expected
    assert json.loads(store.meta("synth")["unresolved"]) == expected
    assert result.counts["unresolved_types"] == len(expected)


def test_hierarchical_catalog_with_owner_and_numeric_code(conn: sqlite3.Connection) -> None:
    """Справочник: группы и элементы, владелец, числовой код."""
    assert property_paths(conn, NOMENCLATURE)[:6] == [
        "ПометкаУдаления",
        "Код",
        "Наименование",
        "Родитель",
        "ЭтоГруппа",
        "Владелец",
    ]
    deletion = property_row(conn, NOMENCLATURE, "ПометкаУдаления")
    assert split_types(deletion["types"]) == ["Булево"]
    assert deletion["synonym"] == "Пометка удаления"
    code = property_row(conn, NOMENCLATURE, "Код")
    assert split_types(code["types"]) == ["Число"]
    assert_qualifiers(code, number_length=9, number_nonnegative=1)
    name = property_row(conn, NOMENCLATURE, "Наименование")
    assert split_types(name["types"]) == ["Строка"]
    assert_qualifiers(name, string_length=100)
    parent = property_row(conn, NOMENCLATURE, "Родитель")
    assert split_types(parent["types"]) == [NOMENCLATURE]
    group = property_row(conn, NOMENCLATURE, "ЭтоГруппа")
    assert split_types(group["types"]) == ["Булево"]
    assert group["synonym"] == "Это группа"
    owner = property_row(conn, NOMENCLATURE, "Владелец")
    assert split_types(owner["types"]) == [CONTRACTORS]


def test_document_with_posting_and_number(conn: sqlite3.Connection) -> None:
    """Документ с проведением и номером."""
    assert property_paths(conn, RECEIPT)[:4] == ["ПометкаУдаления", "Номер", "Дата", "Проведен"]
    number = property_row(conn, RECEIPT, "Номер")
    assert split_types(number["types"]) == ["Строка"]
    assert_qualifiers(number, string_length=11)
    posted_date = property_row(conn, RECEIPT, "Дата")
    assert split_types(posted_date["types"]) == ["Дата"]
    assert_qualifiers(posted_date)
    assert split_types(property_row(conn, RECEIPT, "Проведен")["types"]) == ["Булево"]


def test_periodic_information_register(conn: sqlite3.Connection) -> None:
    """Периодический регистр сведений: период и регистратор, без вида движения."""
    assert property_paths(conn, RATES)[:3] == ["Активность", "Регистратор", "Период"]
    assert "ВидДвижения" not in property_paths(conn, RATES)
    assert split_types(property_row(conn, RATES, "Период")["types"]) == ["Дата"]
    assert split_types(property_row(conn, RATES, "Активность")["types"]) == ["Булево"]
    assert split_types(property_row(conn, RATES, "Регистратор")["types"]) == [RECEIPT]


def test_accumulation_balance_has_movement_kind(conn: sqlite3.Connection) -> None:
    """Регистр накопления остатков: период и ВидДвижения с пустым типом."""
    assert property_paths(conn, BALANCES)[:4] == [
        "Активность",
        "Регистратор",
        "Период",
        "ВидДвижения",
    ]
    kind = property_row(conn, BALANCES, "ВидДвижения")
    assert kind["kind"] == "Свойство"
    assert kind["synonym"] == "Вид движения"
    assert kind["comment"] == "Вид движения"
    assert kind["types"] is None
    assert kind["unresolved"] is None


def test_accounting_register_correspondence_and_offbalance_dimension(
    conn: sqlite3.Connection,
) -> None:
    """Регистр бухгалтерии с корреспонденцией: счета, субконто и пара Дт/Кт."""
    paths = property_paths(conn, ACCOUNTING)
    assert paths[:7] == [
        "Активность",
        "Регистратор",
        "Период",
        "СчетДт",
        "СчетКт",
        "СубконтоДт",
        "СубконтоКт",
    ]
    assert "ВидДвижения" not in paths
    assert "Подразделение" not in paths
    assert "ПодразделениеДт" in paths
    assert "ПодразделениеКт" in paths
    assert "Контрагент" in paths
    account = "ПланСчетовСсылка.Хозрасчетный"
    assert split_types(property_row(conn, ACCOUNTING, "СчетДт")["types"]) == [account]
    assert split_types(property_row(conn, ACCOUNTING, "СчетКт")["types"]) == [account]
    for path in ("СубконтоДт", "СубконтоКт"):
        row = property_row(conn, ACCOUNTING, path)
        assert row["kind"] == "ВидыСубконтоСчета"
        assert split_types(row["types"]) == CHARACTERISTIC_REFS
    for path in ("ПодразделениеДт", "ПодразделениеКт", "Контрагент"):
        row = property_row(conn, ACCOUNTING, path)
        assert row["kind"] == "Измерение"
        assert split_types(row["types"]) == [CONTRACTORS]


def test_hierarchical_chart_of_characteristic_types(conn: sqlite3.Connection) -> None:
    """Иерархический план видов характеристик: родитель и «это группа» без HierarchyType."""
    assert property_paths(conn, SUBCONTO)[:5] == [
        "ПометкаУдаления",
        "Код",
        "Наименование",
        "Родитель",
        "ЭтоГруппа",
    ]
    parent = property_row(conn, SUBCONTO, "Родитель")
    assert split_types(parent["types"]) == [SUBCONTO]
    group = property_row(conn, SUBCONTO, "ЭтоГруппа")
    assert split_types(group["types"]) == ["Булево"]
    assert group["synonym"] == "Это группа"
    code = property_row(conn, SUBCONTO, "Код")
    assert split_types(code["types"]) == ["Строка"]
    assert_qualifiers(code, string_length=5, string_fixed=1)


def test_calculation_type_predefined_tabular_sections(conn: sqlite3.Connection) -> None:
    """План видов расчета: период действия, базовые виды и предопределённые табличные части."""
    assert property_paths(conn, ACCRUALS) == [
        "ПометкаУдаления",
        "Код",
        "Наименование",
        "ПериодДействияБазовый",
        "БазовыеВидыРасчета",
        "БазовыеВидыРасчета.ВидРасчета",
        "ВедущиеВидыРасчета",
        "ВедущиеВидыРасчета.ВидРасчета",
        "ВытесняющиеВидыРасчета",
        "ВытесняющиеВидыРасчета.ВидРасчета",
    ]
    assert split_types(property_row(conn, ACCRUALS, "ПериодДействияБазовый")["types"]) == ["Булево"]
    base = property_row(conn, ACCRUALS, "БазовыеВидыРасчета")
    assert base["kind"] == "ТабличнаяЧасть"
    assert base["is_group"] == 1
    assert base["synonym"] == "Базовые виды расчета"
    assert base["comment"] == "Предопределенный объект"
    assert split_types(property_row(conn, ACCRUALS, "БазовыеВидыРасчета.ВидРасчета")["types"]) == [
        DEDUCTIONS
    ]
    assert (
        split_types(property_row(conn, ACCRUALS, "ВедущиеВидыРасчета.ВидРасчета")["types"])
        == CALCULATION_PLANS
    )
    assert split_types(
        property_row(conn, ACCRUALS, "ВытесняющиеВидыРасчета.ВидРасчета")["types"]
    ) == [ACCRUALS]
    assert property_row(conn, ACCRUALS, "БазовыеВидыРасчета.ВидРасчета")["synonym"] == "Вид расчета"

    deductions = property_paths(conn, DEDUCTIONS)
    assert "ПериодДействияБазовый" not in deductions
    assert "БазовыеВидыРасчета" not in deductions
    assert "ВытесняющиеВидыРасчета" not in deductions
    assert (
        split_types(property_row(conn, DEDUCTIONS, "ВедущиеВидыРасчета.ВидРасчета")["types"])
        == CALCULATION_PLANS
    )


def test_business_process_and_task_addressing(conn: sqlite3.Connection) -> None:
    """Бизнес-процесс и задача с реквизитом адресации."""
    assert property_paths(conn, PROCESS) == ["Стартован", "Завершен", "ВедущаяЗадача"]
    assert split_types(property_row(conn, PROCESS, "Стартован")["types"]) == ["Булево"]
    assert split_types(property_row(conn, PROCESS, "Завершен")["types"]) == ["Булево"]
    assert split_types(property_row(conn, PROCESS, "ВедущаяЗадача")["types"]) == [TASK]
    assert property_paths(conn, TASK) == [
        "Исполнитель",
        "БизнесПроцесс",
        "Выполнена",
        "ТочкаМаршрута",
    ]
    addressing = property_row(conn, TASK, "Исполнитель")
    assert addressing["kind"] == "Свойство"
    assert addressing["synonym"] == "Исполнитель"
    assert split_types(addressing["types"]) == [CONTRACTORS]
    assert split_types(property_row(conn, TASK, "БизнесПроцесс")["types"]) == [PROCESS]
    assert split_types(property_row(conn, TASK, "Выполнена")["types"]) == ["Булево"]
    assert split_types(property_row(conn, TASK, "ТочкаМаршрута")["types"]) == [
        "ТочкаМаршрутаБизнесПроцессаСсылка.Согласование"
    ]


def test_document_movements_and_recorders(conn: sqlite3.Connection) -> None:
    """Наборы движений документа и регистратор: все документы, которые пишут в регистр."""
    receipt = property_paths(conn, RECEIPT)
    assert "Остатки" in receipt
    assert "Курсы" in receipt
    assert "Хозрасчетный" in receipt
    assert "Остатки.Регистратор" in receipt
    assert "Хозрасчетный.СчетДт" in receipt
    balances = property_row(conn, RECEIPT, "Остатки")
    assert balances["kind"] == "НаборДвиженийРегистраНакопления"
    assert balances["is_group"] == 1
    assert property_row(conn, RECEIPT, "Курсы")["kind"] == "НаборДвиженийРегистраСведений"
    assert property_row(conn, RECEIPT, "Хозрасчетный")["kind"] == "НаборДвиженийРегистраБухгалтерии"
    assert split_types(property_row(conn, RECEIPT, "Остатки.Регистратор")["types"]) == RECORDERS

    expense = property_paths(conn, EXPENSE)
    assert "Остатки" in expense
    assert "Курсы" not in expense
    assert "Хозрасчетный" not in expense

    assert split_types(property_row(conn, BALANCES, "Регистратор")["types"]) == RECORDERS
    assert split_types(property_row(conn, RATES, "Регистратор")["types"]) == [RECEIPT]
    assert split_types(property_row(conn, ACCOUNTING, "Регистратор")["types"]) == [RECEIPT]


def test_common_attributes_use_auto_and_data_separation(conn: sqlite3.Connection) -> None:
    """Общий реквизит: Use, Auto при AutoUse = Use; DataSeparation отбрасывает реквизит."""
    nomenclature = property_paths(conn, NOMENCLATURE)
    assert "РеквизитЯвно" in nomenclature
    assert "РеквизитАвто" in nomenclature
    explicit = property_row(conn, NOMENCLATURE, "РеквизитЯвно")
    assert explicit["kind"] == "Реквизит"
    assert split_types(explicit["types"]) == ["Строка"]
    assert_qualifiers(explicit, string_length=40)
    automatic = property_row(conn, NOMENCLATURE, "РеквизитАвто")
    assert_qualifiers(automatic, string_length=20)

    assert "РеквизитЯвно" not in property_paths(conn, CONTRACTORS)
    assert "РеквизитЯвно" not in property_paths(conn, RECEIPT)
    assert "РеквизитАвто" not in property_paths(conn, RECEIPT)
    assert "РеквизитАвто" in property_paths(conn, EXPENSE)

    separated = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE name = 'РеквизитРазделение'"
    ).fetchone()
    assert separated is not None
    assert separated[0] == 0


def test_enum_values_predefined_items_and_route_points(conn: sqlite3.Connection) -> None:
    """Значения перечисления, предопределённые с ChildItems и точки маршрута."""
    enums = object_values(conn, "Перечисление.Виды", limit=20)
    if isinstance(enums, NotFound):
        raise AssertionError(enums.message)
    assert [(item["name"], item["synonym"]) for item in enums.items] == [
        ("Приход", "Поступление"),
        ("Расход", "Списание"),
        ("Корректировка", "Корректировка"),
    ]
    assert all(item["predefined"] for item in enums.items)

    accounts = object_values(conn, "ПланСчетов.Хозрасчетный", limit=20)
    if isinstance(accounts, NotFound):
        raise AssertionError(accounts.message)
    assert [(item["name"], item["synonym"]) for item in accounts.items] == [
        ("ОсновныеСредства", "Основные средства"),
        ("ОСвОрганизации", "Основные средства в организации"),
        ("Материалы", "Материалы"),
    ]

    points = object_values(conn, "ТочкаМаршрутаБизнесПроцесса.Согласование", limit=20)
    if isinstance(points, NotFound):
        raise AssertionError(points.message)
    assert [(item["name"], item["synonym"]) for item in points.items] == [
        ("Старт", "Старт"),
        ("Выполнить", "Выполнить"),
    ]
    assert "Линия" not in [item["name"] for item in points.items]
    assert "Надпись" not in [item["name"] for item in points.items]


def test_exchange_plan_content(conn: sqlite3.Connection) -> None:
    """Состав плана обмена: авторегистрация, без константы, последовательность не разрешена."""
    content = exchange_plan_content(conn, "ПланОбмена.Обмен", limit=20)
    if isinstance(content, NotFound):
        raise AssertionError(content.message)
    assert isinstance(content, Page)
    assert [
        (item["name"], item["types"], item["autoregistration"], item.get("unresolved"))
        for item in content.items
    ] == [
        ("Номенклатура", ["СправочникСсылка.Номенклатура"], True, None),
        ("Приход", ["ДокументСсылка.Приход"], False, None),
        ("ДокументыОрганизаций", [], False, ["Последовательность.ДокументыОрганизаций"]),
    ]
    assert "ВалютаУчета" not in [item["name"] for item in content.items]


def test_primitive_stubs_and_constants_set(conn: sqlite3.Connection) -> None:
    """Заглушки примитивов и набор констант, у которого константы — реквизиты."""
    rows = conn.execute(
        "SELECT kind, name, type_name, synonym FROM objects"
        " WHERE type_name IN (?, ?, ?, ?, ?, ?) ORDER BY name",
        ("Булево", "Дата", "Строка", "УникальныйИдентификатор", "ХранилищеЗначения", "Число"),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("Булево", "Булево", "Булево", "Булево"),
        ("Дата", "Дата", "Дата", "Дата"),
        ("Строка", "Строка", "Строка", "Строка"),
        (
            "УникальныйИдентификатор",
            "УникальныйИдентификатор",
            "УникальныйИдентификатор",
            "Уникальный идентификатор",
        ),
        ("ХранилищеЗначения", "ХранилищеЗначения", "ХранилищеЗначения", "Хранилище значения"),
        ("Число", "Число", "Число", "Число"),
    ]
    hidden = describe_object(conn, "Строка.Строка")
    assert isinstance(hidden, NotFound)

    described = describe_object(conn, "НаборКонстант.КонстантыНабор", limit=10)
    if isinstance(described, NotFound):
        raise AssertionError(described.message)
    assert described["kind"] == "НаборКонстант"
    assert described["type_name"] == "КонстантыНабор"
    assert described["synonym"] == "Набор констант"
    props = described["properties"].items
    assert [(item["path"], item["kind"], item["types"]) for item in props] == [
        ("ВалютаУчета", "Реквизит", ["СправочникСсылка.Контрагенты"]),
        ("ЗаголовокСистемы", "Реквизит", ["Строка"]),
    ]
    assert props[1]["qualifiers"]["string_length"] == 100


def test_extension_adds_own_object_and_borrowed_parts(conn: sqlite3.Connection) -> None:
    """Свой объект расширения, новые части заимствованного и значение перечисления."""
    row = conn.execute(
        "SELECT kind, name FROM objects WHERE type_name = 'СправочникСсылка.Бригады'"
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("Справочник", "Бригады")

    paths = property_paths(conn, NOMENCLATURE)
    assert "КомментарийРасш" in paths
    assert "Спецификация" in paths
    assert "Спецификация.Содержание" in paths
    assert "Товары.Номенклатура" in paths
    assert "Товары.Аналитика" in paths
    assert paths.count("Артикул") == 1
    added = property_row(conn, NOMENCLATURE, "КомментарийРасш")
    assert added["kind"] == "Реквизит"
    assert split_types(added["types"]) == ["Строка"]
    section = property_row(conn, NOMENCLATURE, "Спецификация")
    assert section["kind"] == "ТабличнаяЧасть"
    assert section["is_group"] == 1
    column = property_row(conn, NOMENCLATURE, "Товары.Аналитика")
    assert column["kind"] == "Реквизит"
    assert_qualifiers(column, string_length=20)

    values = object_values(conn, "Перечисление.Виды", limit=20)
    if isinstance(values, NotFound):
        raise AssertionError(values.message)
    names = [item["name"] for item in values.items]
    assert names == ["Приход", "Расход", "Корректировка"]
    assert names.count("Приход") == 1


def test_extension_extend_value_widens_qualifiers(conn: sqlite3.Connection) -> None:
    """ExtendValue расширяет число и строку; неотрицательность остаётся, только если она у обоих."""
    quantity = property_row(conn, BALANCES, "Количество")
    assert split_types(quantity["types"]) == ["Число"]
    assert_qualifiers(quantity, number_length=20, number_precision=8)
    kept = property_row(conn, BALANCES, "СуммаНеотр")
    assert_qualifiers(kept, number_length=10, number_precision=2, number_nonnegative=1)
    dropped = property_row(conn, BALANCES, "СуммаЗнак")
    assert_qualifiers(dropped, number_length=8, number_precision=2)
    article = property_row(conn, NOMENCLATURE, "Артикул")
    assert split_types(article["types"]) == ["Строка"]
    assert_qualifiers(article, string_length=16)


def test_extension_supplements_borrowed_defined_type(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Заимствованный определяемый тип дополнен типами расширения, вложенный состав сохранён:
    без расширения документной ссылки в типе нет."""
    main_only = StructureStore(tmp_path)
    main_only.load_xml("main-only", MAIN)
    connection = main_only.open("main-only")
    try:
        own = split_types(property_row(connection, SAMPLES, "Определяемый")["types"])
    finally:
        connection.close()
    assert own == ["СправочникСсылка.Номенклатура", "Строка"]
    assert split_types(property_row(conn, SAMPLES, "Определяемый")["types"]) == DEFINED_TYPE_REFS


def test_unlisted_extension_is_ignored(conn: sqlite3.Connection) -> None:
    """Расширение не перечислено: его объекты, реквизиты и значения в структуру не попадают."""
    names = {str(row[0]) for row in conn.execute("SELECT kind || '.' || name FROM objects")}
    assert "Справочник.Лишний" not in names
    assert "Справочник.Бригады" in names
    secret = conn.execute(
        "SELECT 1 FROM properties AS p JOIN objects AS o ON o.id = p.object_id"
        " WHERE o.type_name = ? AND p.name = 'СекретныйРеквизит'",
        (NOMENCLATURE,),
    ).fetchone()
    assert secret is None
    values = object_values(conn, "Перечисление.Виды", limit=20)
    if isinstance(values, NotFound):
        raise AssertionError(values.message)
    assert "Чужая" not in [item["name"] for item in values.items]


def test_meta_lists_extensions_in_overlay_order(store: StructureStore) -> None:
    """В метаданных — имена перечисленных расширений в порядке наложения."""
    assert json.loads(store.meta("synth")["extensions"]) == ["Расширение"]
    assert store.meta("synth")["config_name"] == "Тестовая"
    assert store.meta("synth")["config_version"] == "1.0.0.1"


def test_cache_reuses_same_input_and_rebuilds_when_dump_changes(tmp_path: Path) -> None:
    """Повтор того же входа берётся из кэша; изменение файла выгрузки собирает структуру заново."""
    dump = tmp_path / "dump"
    shutil.copytree(MAIN, dump / "main")
    shutil.copytree(EXT, dump / "ext")
    cache = StructureStore(tmp_path / "cache")
    first = cache.load_xml("cache-case", dump / "main", [dump / "ext"])
    assert first.reused is False
    second = cache.load_xml("cache-case", dump / "main", [dump / "ext"])
    assert second.reused is True
    assert second.unresolved == first.unresolved

    config = dump / "main" / "Configuration.xml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("1.0.0.1", "9.9.9.9"),
        encoding="utf-8",
    )
    third = cache.load_xml("cache-case", dump / "main", [dump / "ext"])
    assert third.reused is False
    assert cache.meta("cache-case")["config_version"] == "9.9.9.9"


def test_dump_with_config_dump_info_is_fingerprinted_by_markers(tmp_path: Path) -> None:
    """С ConfigDumpInfo.xml отпечаток — по нему и Configuration.xml; force собирает заново."""
    dump = tmp_path / "main"
    shutil.copytree(MAIN, dump)
    info = dump / "ConfigDumpInfo.xml"
    info.write_text('<ConfigDumpInfo configVersion="1"/>', encoding="utf-8")
    cache = StructureStore(tmp_path / "cache")
    assert cache.load_xml("markers", dump).reused is False

    # Правка файла объекта без повторной выгрузки Конфигуратором отпечаток не меняет.
    catalog = next((dump / "Catalogs").glob("*.xml"))
    catalog.write_text(catalog.read_text(encoding="utf-8") + "<!-- правка -->", encoding="utf-8")
    assert cache.load_xml("markers", dump).reused is True
    assert cache.load_xml("markers", dump, force=True).reused is False

    info.write_text('<ConfigDumpInfo configVersion="2"/>', encoding="utf-8")
    assert cache.load_xml("markers", dump).reused is False
    assert cache.load_xml("markers", dump).reused is True


def test_missing_configuration_xml_raises(tmp_path: Path) -> None:
    """Нет Configuration.xml — Kd2Error, структура не собирается."""
    root = tmp_path / "empty"
    root.mkdir()
    cache = StructureStore(tmp_path / "cache")
    with pytest.raises(Kd2Error, match=r"Configuration\.xml"):
        cache.load_xml("missing", root)
