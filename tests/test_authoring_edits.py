"""Точечные правки правил (спецификация `rules-authoring`, «Точечные правки правил»)."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from lxml import etree

from kd_rules_mcp.authoring.edits import (
    EditResult,
    create_pko_many,
    create_pko_with_properties,
    create_rule,
    delete_rule,
    find_rule,
    update_rule,
    update_rules,
)
from kd_rules_mcp.errors import (
    AmbiguousAddressError,
    CountMismatchError,
    DanglingReferenceError,
    DuplicateRuleError,
    ObjectNotFoundError,
    PreviewRequiredError,
    RuleEditError,
    RuleNotFoundError,
    UnknownFieldError,
)
from kd_rules_mcp.kd2.canonical import canonical_diff, canonical_form
from kd_rules_mcp.kd2.model import ExchangeRules, Node
from kd_rules_mcp.kd2.rules_io import dump_rules, load_exchange_rules, load_rules
from kd_rules_mcp.server import error_payload
from kd_rules_mcp.service.views import pko_batch_view
from kd_rules_mcp.structures import db
from kd_rules_mcp.structures.xmldump import KINDS
from kd_rules_mcp.validation.address import side_name, walk_pks
from kd_rules_mcp.validation.format import check_format

DATA = Path(__file__).parent / "data"

# (вид, имя, синоним, типы, вложенные свойства) — как в tests/test_authoring_candidates.py.
Prop = tuple[str, str, str, str, Sequence["Prop"]]

_HEAD = "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата></ПравилаОбмена>"


class _Builder:
    """Маленькая структура прямо в SQLite."""

    def __init__(self, path: Path) -> None:
        self.conn = db.create(path)

    def add(
        self,
        kind: str,
        name: str,
        props: Sequence[Prop] = (),
        *,
        synonym: str = "",
        values: Sequence[tuple[str, str]] = (),
    ) -> None:
        prefix = {
            "Документ": "ДокументСсылка",
            "Справочник": "СправочникСсылка",
            "Перечисление": "ПеречислениеСсылка",
        }.get(kind, kind)
        cur = self.conn.execute(
            "INSERT INTO objects (kind, name, type_name, synonym) VALUES (?, ?, ?, ?)",
            (kind, name, f"{prefix}.{name}", synonym),
        )
        object_id = int(cur.lastrowid or 0)
        self._props(object_id, None, "", props)
        for value, value_synonym in values:
            self.conn.execute(
                "INSERT INTO object_values (object_id, name, synonym) VALUES (?, ?, ?)",
                (object_id, value, value_synonym),
            )

    def _props(
        self, object_id: int, parent: int | None, prefix: str, props: Sequence[Prop]
    ) -> None:
        for kind, name, synonym, types, children in props:
            type_set = None
            if types:
                self.conn.execute("INSERT OR IGNORE INTO type_sets (types) VALUES (?)", (types,))
                row = self.conn.execute(
                    "SELECT id FROM type_sets WHERE types = ?", (types,)
                ).fetchone()
                type_set = None if row is None else row[0]
            cur = self.conn.execute(
                "INSERT INTO properties (object_id, parent_id, kind, name, path, synonym, "
                "type_set_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (object_id, parent, kind, name, f"{prefix}{name}", synonym, type_set),
            )
            self._props(object_id, int(cur.lastrowid or 0), f"{prefix}{name}.", children)


def _rules() -> ExchangeRules:
    document = load_rules(_HEAD.encode())
    assert isinstance(document, ExchangeRules)
    return document


@pytest.mark.parametrize("kind", ["pko", "conversion", "unknown"])
@pytest.mark.parametrize("fields", [None, {}])
def test_update_rule_empty_fields_rejected_before_lookup(kind: str, fields: dict | None) -> None:
    rules = _rules()
    before = canonical_form(dump_rules(rules))
    with pytest.raises(
        RuleEditError, match="Нет полей для правки: передайте в fields хотя бы одно поле"
    ):
        update_rule(rules, kind, "", fields)
    assert canonical_form(dump_rules(rules)) == before


@pytest.mark.parametrize("operation", [create_rule, update_rule, delete_rule])
def test_pro_edit_points_to_registration_build(operation) -> None:
    rules = _rules()
    before = canonical_form(dump_rules(rules))
    with pytest.raises(RuleEditError, match=r"registration_build.*rules_get"):
        if operation is delete_rule:
            operation(rules, "pro", "Справочник.Организации")
        else:
            operation(rules, "pro", "Справочник.Организации", {"Комментарий": "правка"})
    assert canonical_form(dump_rules(rules)) == before


def _section_with_groups(section: str, body: str) -> ExchangeRules:
    xml = (
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
        f"<{section}>{body}</{section}></ПравилаОбмена>"
    )
    document = load_rules(xml.encode())
    assert isinstance(document, ExchangeRules)
    return document


def _group_node(rules: ExchangeRules, section: str, path: str) -> Node:
    container = rules.root.children[section]
    for segment in path.split("/"):
        container = next(item for item in container.items if item.is_group and item.code == segment)
    return container


def _assert_sound(rules: ExchangeRules) -> None:
    """Выгрузка читается обратно без потерь, формат без ошибок."""
    raw = dump_rules(rules)
    loaded = load_rules(raw)
    assert canonical_form(raw) == canonical_form(dump_rules(loaded))
    assert check_format(loaded).errors == []


def _side(name: str, kind: str, type_name: str = "") -> dict[str, str]:
    fields = {"Имя": name, "Вид": kind}
    if type_name:
        fields["Тип"] = type_name
    return fields


def _pks(rules: ExchangeRules, code: str) -> Node:
    properties = next(item for item in rules.pko() if item.code == code).child("Свойства")
    assert properties is not None
    return properties


def _by_target(container: Node) -> dict[str, Node]:
    return {side_name(item, "Приемник"): item for item in container.items}


# --- Сценарии спецификации ---------------------------------------------------------------------


def test_mass_pks_follows_kd_autosetup(tmp_path: Path) -> None:
    """Сценарий «Массовое создание ПКС»: точные пары, синоним КД и выключенные без пары."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source_props: list[Prop] = [
        ("Реквизит", "Организация", "", "СправочникСсылка.Организации", []),
        ("Реквизит", "Номер", "", "Строка", []),
        ("Реквизит", "Комментарий", "", "Строка", []),
        ("Реквизит", "Контрагент", "", "Строка", []),
        ("Реквизит", "Касса", "Касса выплаты", "Строка", []),
        ("Реквизит", "Склад", "", "СправочникСсылка.Склады", []),
        ("Реквизит", "ТолькоВИсточнике", "", "Число", []),
        (
            "ТабличнаяЧасть",
            "Зарплата",
            "",
            "",
            [
                ("Реквизит", "Сумма", "", "Число", []),
                ("Реквизит", "Сотрудник", "", "СправочникСсылка.Сотрудники", []),
            ],
        ),
    ]
    target_props: list[Prop] = [
        ("Реквизит", "Организация", "", "СправочникСсылка.Организации", []),
        ("Реквизит", "НомерДок", "", "Строка", []),
        ("Реквизит", "Комментарий", "", "Строка", []),
        ("Реквизит", "Контрагент", "", "СправочникСсылка.Контрагенты", []),
        ("Реквизит", "КассаВыплаты", "касса выплаты", "Строка", []),
        ("Реквизит", "Склад", "", "СправочникСсылка.Склады", []),
        ("Реквизит", "ТолькоВПриемнике", "", "Число", []),
        ("Реквизит", "Подразделение", "", "СправочникСсылка.Подразделения", []),
        (
            "ТабличнаяЧасть",
            "Зарплата",
            "",
            "",
            [
                ("Реквизит", "Сумма", "", "Число", []),
                ("Реквизит", "Сотрудник", "", "СправочникСсылка.Сотрудники", []),
                ("Реквизит", "ТолькоВТЧ", "", "Строка", []),
            ],
        ),
    ]
    source.add("Документ", "Ведомость", source_props, synonym="Ведомость на выплату")
    target.add("Документ", "Ведомость", target_props, synonym="Ведомость")
    rules = _rules()
    org = "СправочникСсылка.Организации"
    create_rule(rules, "pko", "Организации", {"Источник": org, "Приемник": org})
    employee = "СправочникСсылка.Сотрудники"
    create_rule(rules, "pko", "Сотрудники1", {"Источник": employee, "Приемник": employee})
    create_rule(rules, "pko", "Сотрудники2", {"Источник": employee, "Приемник": employee})

    result = create_pko_with_properties(
        rules,
        "Ведомость",
        source.conn,
        target.conn,
        "Документ.Ведомость",
        "Документ.Ведомость",
        {"Комментарий": "черновик"},
    )

    assert result.address == "ПКО «Ведомость»"
    assert result.skipped == []
    pko = next(item for item in rules.pko() if item.code == "Ведомость")
    assert pko.get("Наименование") == "Документ: Ведомость на выплату"
    assert pko.get("Комментарий") == "черновик"
    assert pko.get("Источник") == "ДокументСсылка.Ведомость"
    assert pko.get("Приемник") == "ДокументСсылка.Ведомость"
    assert pko.get("СинхронизироватьПоИдентификатору") is True
    assert any(
        item.startswith("СинхронизироватьПоИдентификатору включено по умолчанию")
        for item in result.warnings
    )
    props = _pks(rules, "Ведомость")
    found = _by_target(props)
    assert list(found) == [
        "Организация",
        "НомерДок",
        "Комментарий",
        "Склад",
        "ТолькоВПриемнике",
        "Подразделение",
        "Зарплата",
    ]
    assert [item.values["Порядок"] for item in props.items] == [0, 50, 100, 150, 200, 250, 300]
    assert found["Организация"].get("КодПравилаКонвертации") == "Организации"
    assert found["Организация"].child("Источник") is not None
    assert side_name(found["Организация"], "Источник") == "Организация"
    assert found["Организация"].child("Приемник") is not None
    receiver = found["Организация"].child("Приемник")
    assert receiver is not None and receiver.attrs["Тип"] == org
    number = found["НомерДок"]
    assert side_name(number, "Источник") == "Номер"
    assert number.get("КодПравилаКонвертации") == ""
    assert "Отключить" not in number.attrs
    assert found["ТолькоВПриемнике"].attrs.get("Отключить") is True
    assert side_name(found["ТолькоВПриемнике"], "Источник") == ""
    assert "ТолькоВИсточнике" not in found
    group = found["Зарплата"]
    assert group.is_group
    group_receiver = group.child("Приемник")
    assert group_receiver is not None
    assert "Тип" not in group_receiver.attrs
    nested = _by_target(group)
    assert list(nested) == ["Сумма", "Сотрудник", "ТолькоВТЧ"]
    assert [item.values["Порядок"] for item in group.items] == [0, 50, 100]
    assert nested["Сотрудник"].get("КодПравилаКонвертации") == ""
    assert nested["ТолькоВТЧ"].attrs.get("Отключить") is True
    assert result.disabled == ["ТолькоВПриемнике", "Подразделение", "Зарплата/ТолькоВТЧ"]
    assert any("Контрагент" in item and "примитив" in item for item in result.not_applied)
    assert any("КассаВыплаты" in item and "по синониму" in item for item in result.not_applied)
    assert all("ТолькоВИсточнике" not in item for item in result.not_applied)
    assert any(item.startswith("Склад:") for item in result.unresolved)
    assert any(item.startswith("Подразделение:") for item in result.unresolved)
    assert any("Зарплата/Сотрудник" in item and "найдено 2" in item for item in result.unresolved)
    assert not any(item.startswith("Организация:") for item in result.unresolved)
    _assert_sound(rules)


def test_unknown_object_raises_object_not_found(tmp_path: Path) -> None:
    """Опечатка в имени объекта источника или приёмника — ObjectNotFoundError с подсказками."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Контрагенты")
    target.add("Справочник", "Контрагенты")
    rules = _rules()
    with pytest.raises(ObjectNotFoundError, match="Контрагент") as source_error:
        create_pko_with_properties(
            rules,
            "Контрагенты",
            source.conn,
            target.conn,
            "Справочник.Контрагент",
            "Справочник.Контрагенты",
        )
    assert source_error.value.suggestions
    assert "Справочник.Контрагенты" in source_error.value.suggestions
    with pytest.raises(ObjectNotFoundError, match="Контрагент") as target_error:
        create_pko_with_properties(
            rules,
            "Контрагенты",
            source.conn,
            target.conn,
            "Справочник.Контрагенты",
            "Справочник.Контрагент",
        )
    assert target_error.value.suggestions
    assert "Справочник.Контрагенты" in target_error.value.suggestions
    assert rules.pko() == []


def test_dangling_pko_reference_lists_rules_of_the_type() -> None:
    """Сценарий «Ссылка на несуществующее ПКО»: отказ и перечень ПКО этого типа."""
    rules = _rules()
    org = "СправочникСсылка.Организации"
    partner = "СправочникСсылка.Контрагенты"
    create_rule(rules, "pko", "Организации", {"Источник": org, "Приемник": org})
    create_rule(rules, "pko", "Контрагенты", {"Источник": partner, "Приемник": partner})
    create_rule(
        rules,
        "pks",
        "Организация",
        {"Приемник": _side("Организация", "Реквизит", org)},
        owner="Организации",
    )
    before = dump_rules(rules)
    with pytest.raises(DanglingReferenceError, match="ПКО для типа") as error:
        update_rule(
            rules,
            "pks",
            "Организация",
            {"КодПравилаКонвертации": "НетТакого"},
            owner="Организации",
        )
    message = str(error.value)
    assert "«Организации»" in message
    assert "СправочникСсылка.Организации" in message
    assert "Контрагенты" not in message
    assert dump_rules(rules) == before

    create_rule(
        rules,
        "pks",
        "Комментарий",
        {"Приемник": _side("Комментарий", "Реквизит")},
        owner="Организации",
    )
    with pytest.raises(DanglingReferenceError, match="Существующие ПКО") as error:
        update_rule(
            rules,
            "pks",
            "Комментарий",
            {"КодПравилаКонвертации": "НетТакого"},
            owner="Организации",
        )
    message = str(error.value)
    assert "«Организации»" in message and "«Контрагенты»" in message

    create_rule(rules, "pvd", "Выгрузка", {"ОбъектВыборки": org})
    with pytest.raises(DanglingReferenceError, match="ПКО для типа") as error:
        update_rule(rules, "pvd", "Выгрузка", {"КодПравилаКонвертации": "НетТакого"})
    message = str(error.value)
    assert "«Организации»" in message
    assert "Контрагенты" not in message

    with pytest.raises(DanglingReferenceError, match="Существующие ПКО") as error:
        create_rule(rules, "parameter", "П1", {"ПравилоКонвертации": "НетТакого"})
    message = str(error.value)
    assert "«Организации»" in message and "«Контрагенты»" in message
    _assert_sound(rules)


def test_one_field_keeps_the_rest_of_the_rule() -> None:
    """Сценарий «Изменение одного поля»: обработчик не трогает остальные поля и вложенные ПКС."""
    rules = _rules()
    created = create_rule(
        rules,
        "pko",
        "Организации",
        {
            "Наименование": "Справочник: Организации",
            "Порядок": 50,
            "Источник": "СправочникСсылка.Организации",
            "Приемник": "СправочникСсылка.Организации",
            "СинхронизироватьПоИдентификатору": True,
        },
    )
    assert created.skipped == [
        "правка применена; проверка по структуре источника не выполнена: структура не передана",
        "правка применена; проверка по структуре приёмника не выполнена: структура не передана",
    ]
    create_rule(
        rules,
        "pks",
        "ИНН",
        {
            "Код": "1",
            "Наименование": "ИНН --> ИНН",
            "Порядок": 50,
            "Источник": _side("ИНН", "Реквизит", "Строка"),
            "Приемник": _side("ИНН", "Реквизит", "Строка"),
        },
        owner="Организации",
    )
    pko = rules.pko()[0]
    properties = pko.child("Свойства")
    assert properties is not None
    nested = properties.items[0]
    before = dict(pko.values)
    update_rule(rules, "pko", "Организации", {"ПередВыгрузкой": "Отказ = Истина;"})
    assert pko.values["ПередВыгрузкой"] == "Отказ = Истина;"
    for key, value in before.items():
        assert pko.values[key] == value
    assert pko.child("Свойства") is properties
    assert properties.items == [nested]
    assert nested.values["Наименование"] == "ИНН --> ИНН"
    assert nested.child("Приемник") is not None
    receiver = nested.child("Приемник")
    assert receiver is not None and receiver.attrs["Тип"] == "Строка"
    _assert_sound(rules)


def test_real_template_one_field_is_the_only_canonical_change() -> None:
    """Правка одного поля макета меняет в каноническом сравнении ровно это поле."""
    path = DATA / "exchange_rules.xml"
    rules = load_exchange_rules(path)
    before = canonical_form(dump_rules(rules))
    update_rule(rules, "pko", "Организации", {"Наименование": "Справочник: Организации*"})
    diff = canonical_diff(before, canonical_form(dump_rules(rules)))
    assert len(diff) == 1
    assert "Наименование" in diff[0]
    assert "Справочник: Организации*" in diff[0]
    _assert_sound(rules)


# --- Создание, изменение, удаление -------------------------------------------------------------


def test_create_update_delete_each_kind() -> None:
    rules = _rules()
    org = "СправочникСсылка.Организации"
    addresses = [
        create_rule(
            rules,
            "pko",
            "Организации",
            {"Наименование": "Справочник: Организации", "Источник": org, "Приемник": org},
        ).address,
        create_rule(
            rules, "pvd", "Выгрузка", {"Наименование": "Организации", "Порядок": 50}
        ).address,
        create_rule(rules, "pod", "Очистка", {"Порядок": 50, "ОбъектВыборки": org}).address,
        create_rule(rules, "algorithm", "Общий", {"Текст": "Возврат 1;"}).address,
        create_rule(rules, "query", "Остатки", {"Текст": "ВЫБРАТЬ 1"}).address,
        create_rule(rules, "parameter", "П1", {"Наименование": "Параметр"}).address,
        create_rule(
            rules,
            "pks_group",
            "Контакты",
            {
                "Источник": _side("Контакты", "ТабличнаяЧасть"),
                "Приемник": _side("Контакты", "ТабличнаяЧасть"),
            },
            owner="Организации",
        ).address,
        create_rule(
            rules,
            "pks",
            "Контакты/Телефон",
            {
                "Источник": _side("Телефон", "Реквизит", "Строка"),
                "Приемник": _side("Телефон", "Реквизит", "Строка"),
            },
            owner="Организации",
        ).address,
        create_rule(
            rules, "pkz", "Начисление", {"Приемник": "Начисление"}, owner="Организации"
        ).address,
    ]
    assert addresses == [
        "ПКО «Организации»",
        "ПВД «Выгрузка»",
        "ПОД «Очистка»",
        "алгоритм «Общий»",
        "запрос «Остатки»",
        "параметр «П1»",
        "ПКО «Организации» / ПКС Контакты",
        "ПКО «Организации» / ПКС Контакты/Телефон",
        "ПКО «Организации» / ПКЗ Начисление",
    ]
    assert create_rule(rules, "algorithm", "Пустой", {"Текст": "Возврат 1;"}).skipped == []

    update_rule(rules, "pko", "Организации", {"Комментарий": "правка"})
    update_rule(rules, "pvd", "Выгрузка", {"Комментарий": "правка"})
    update_rule(rules, "pod", "Очистка", {"Комментарий": "правка"})
    update_rule(rules, "algorithm", "Общий", {"Комментарий": "правка"})
    update_rule(rules, "query", "Остатки", {"Комментарий": "правка"})
    update_rule(rules, "parameter", "П1", {"Наименование": "Параметр 1"})
    update_rule(rules, "pks_group", "Контакты", {"Комментарий": "группа"}, owner="Организации")
    update_rule(rules, "pks", "Контакты/Телефон", {"Комментарий": "телефон"}, owner="Организации")
    update_rule(rules, "pkz", "Начисление", {"Комментарий": "значение"}, owner="Организации")

    pko = rules.pko()[0]
    assert pko.get("Комментарий") == "правка"
    assert pko.get("Источник") == org
    phone = _by_target(_pks(rules, "Организации").items[0])["Телефон"]
    assert phone.get("Комментарий") == "телефон"
    receiver = phone.child("Приемник")
    assert receiver is not None and receiver.attrs == {
        "Имя": "Телефон",
        "Вид": "Реквизит",
        "Тип": "Строка",
    }
    assert rules.algorithms()[0].get("Текст") == "Возврат 1;"
    assert rules.algorithms()[0].get("Комментарий") == "правка"
    _assert_sound(rules)

    delete_rule(rules, "pks", "Контакты/Телефон", owner="Организации")
    delete_rule(rules, "pks_group", "Контакты", owner="Организации")
    delete_rule(rules, "pkz", "Начисление", owner="Организации")
    delete_rule(rules, "pko", "Организации")
    delete_rule(rules, "pvd", "Выгрузка")
    delete_rule(rules, "pod", "Очистка")
    delete_rule(rules, "algorithm", "Общий")
    delete_rule(rules, "algorithm", "Пустой")
    delete_rule(rules, "query", "Остатки")
    delete_rule(rules, "parameter", "П1")
    assert rules.pko() == []
    assert rules.pvd() == []
    assert rules.pod() == []
    assert rules.algorithms() == []
    assert rules.queries() == []
    _assert_sound(rules)


def test_duplicate_code_or_name_is_refused() -> None:
    rules = _rules()
    create_rule(rules, "pko", "Организации", {"Источник": "А", "Приемник": "Б"})
    create_rule(rules, "algorithm", "Общий", {"Текст": "Возврат 1;"})
    before = dump_rules(rules)
    with pytest.raises(DuplicateRuleError, match="кодом «Организации»"):
        create_rule(rules, "pko", "Организации", {"Источник": "А", "Приемник": "Б"})
    with pytest.raises(DuplicateRuleError, match="именем «Общий»"):
        create_rule(rules, "algorithm", "Общий", {"Текст": "Возврат 2;"})
    assert dump_rules(rules) == before


def test_unknown_field_and_missing_address_do_not_change_the_document() -> None:
    rules = _rules()
    create_rule(rules, "pko", "Организации", {"Источник": "А", "Приемник": "Б"})
    before = dump_rules(rules)
    with pytest.raises(UnknownFieldError, match="Чужое"):
        update_rule(rules, "pko", "Организации", {"Чужое": "1"})
    with pytest.raises(UnknownFieldError, match="Наименование"):
        create_rule(rules, "pod", "Очистка", {"Наименование": "лишнее"})
    with pytest.raises(RuleNotFoundError, match="НетТакого"):
        update_rule(rules, "pko", "НетТакого", {"Комментарий": "1"})
    with pytest.raises(RuleNotFoundError, match="не найдено"):
        delete_rule(rules, "pvd", "НетТакого")
    with pytest.raises(RuleEditError, match="Неизвестный вид"):
        create_rule(rules, "foo", "Код", {})
    assert dump_rules(rules) == before


def test_delete_and_rename_of_referenced_pko_are_refused() -> None:
    rules = _rules()
    org = "СправочникСсылка.Организации"
    create_rule(rules, "pko", "Организации", {"Источник": org, "Приемник": org})
    create_rule(
        rules, "pko", "Док", {"Источник": "ДокументСсылка.Док", "Приемник": "ДокументСсылка.Док"}
    )
    create_rule(
        rules,
        "pks",
        "Организация",
        {
            "Приемник": _side("Организация", "Реквизит", org),
            "КодПравилаКонвертации": "Организации",
        },
        owner="Док",
    )
    create_rule(
        rules,
        "pvd",
        "Выгрузка",
        {"КодПравилаКонвертации": "Организации", "ОбъектВыборки": org},
    )
    create_rule(rules, "parameter", "П1", {"ПравилоКонвертации": "Организации"})
    before = dump_rules(rules)
    with pytest.raises(DanglingReferenceError, match="удалить нельзя") as error:
        delete_rule(rules, "pko", "Организации")
    message = str(error.value)
    assert "ПКО «Док» / ПКС Организация" in message
    assert "ПВД «Выгрузка»" in message
    assert "параметр «П1»" in message
    with pytest.raises(DanglingReferenceError, match="изменить нельзя"):
        update_rule(rules, "pko", "Организации", {"Код": "Другой"})
    assert dump_rules(rules) == before

    update_rule(rules, "pks", "Организация", {"КодПравилаКонвертации": ""}, owner="Док")
    update_rule(rules, "pvd", "Выгрузка", {"КодПравилаКонвертации": ""})
    update_rule(rules, "parameter", "П1", {"ПравилоКонвертации": ""})
    delete_rule(rules, "pko", "Организации")
    assert [item.code for item in rules.pko()] == ["Док"]
    _assert_sound(rules)


def test_padded_pko_code_still_blocks_delete_and_rename() -> None:
    """КД дополняет код пробелами; ссылка с другим хвостом всё равно держит ПКО."""
    rules = _rules()
    org = "СправочникСсылка.Организации"
    create_rule(rules, "pko", "Организации", {"Источник": org, "Приемник": org})
    create_rule(
        rules, "pko", "Док", {"Источник": "ДокументСсылка.Док", "Приемник": "ДокументСсылка.Док"}
    )
    create_rule(
        rules,
        "pks",
        "Организация",
        {"Приемник": _side("Организация", "Реквизит", org), "КодПравилаКонвертации": "Организации"},
        owner="Док",
    )
    padded = "Организации".ljust(50)
    pko = next(item for item in rules.pko() if item.code == "Организации")
    pko.values["Код"] = padded
    pks = next(item for item in rules.pko() if item.code == "Док").child("Свойства")
    assert pks is not None
    pks.items[0].values["КодПравилаКонвертации"] = "Организации" + " " * 3
    before = dump_rules(rules)
    with pytest.raises(DanglingReferenceError, match="удалить нельзя"):
        delete_rule(rules, "pko", padded)
    with pytest.raises(DanglingReferenceError, match="изменить нельзя"):
        update_rule(rules, "pko", "Организации", {"Код": "Другой"})
    assert dump_rules(rules) == before
    update_rule(rules, "pko", padded, {"Комментарий": "жив"})
    assert (
        next(item for item in rules.pko() if item.code == "Организации").get("Комментарий") == "жив"
    )


def test_structure_checks_refuse_missing_object_property_and_value(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    props: list[Prop] = [
        ("Реквизит", "Комментарий", "", "Строка", []),
        (
            "ТабличнаяЧасть",
            "Зарплата",
            "",
            "",
            [("Реквизит", "Сумма", "", "Число", [])],
        ),
    ]
    source.add("Документ", "Ведомость", props)
    target.add("Документ", "Ведомость", props)
    source.add("Перечисление", "Виды", values=(("Начисление", ""), ("Удержание", "")))
    target.add("Перечисление", "Виды", values=(("Начисление", ""), ("Удержание", "")))
    rules = _rules()
    before = dump_rules(rules)
    with pytest.raises(DanglingReferenceError, match=r"СправочникСсылка\.Нет"):
        create_rule(
            rules,
            "pko",
            "Нет",
            {"Источник": "СправочникСсылка.Нет", "Приемник": "ДокументСсылка.Ведомость"},
            source=source.conn,
            target=target.conn,
        )
    assert dump_rules(rules) == before

    document = "ДокументСсылка.Ведомость"
    checked = create_rule(
        rules,
        "pko",
        "Ведомость",
        {"Источник": document, "Приемник": document},
        source=source.conn,
        target=target.conn,
    )
    assert checked.skipped == []
    with pytest.raises(DanglingReferenceError, match="НетТакого"):
        create_rule(
            rules,
            "pks",
            "НетТакого",
            {
                "Источник": _side("Комментарий", "Реквизит", "Строка"),
                "Приемник": _side("НетТакого", "Реквизит", "Строка"),
            },
            owner="Ведомость",
            source=source.conn,
            target=target.conn,
        )
    create_rule(
        rules,
        "pks_group",
        "Зарплата",
        {
            "Источник": _side("Зарплата", "ТабличнаяЧасть"),
            "Приемник": _side("Зарплата", "ТабличнаяЧасть"),
        },
        owner="Ведомость",
        source=source.conn,
        target=target.conn,
    )
    create_rule(
        rules,
        "pks",
        "Зарплата/Сумма",
        {
            "Источник": _side("Сумма", "Реквизит", "Число"),
            "Приемник": _side("Сумма", "Реквизит", "Число"),
        },
        owner="Ведомость",
        source=source.conn,
        target=target.conn,
    )
    with pytest.raises(DanglingReferenceError, match=r"Зарплата\.Нет"):
        create_rule(
            rules,
            "pks",
            "Зарплата/Нет",
            {"Приемник": _side("Нет", "Реквизит", "Строка")},
            owner="Ведомость",
            source=source.conn,
            target=target.conn,
        )

    enum = "ПеречислениеСсылка.Виды"
    create_rule(
        rules,
        "pko",
        "Виды",
        {"Источник": enum, "Приемник": enum},
        source=source.conn,
        target=target.conn,
    )
    with pytest.raises(DanglingReferenceError, match="НетЗначения"):
        create_rule(
            rules,
            "pkz",
            "НетЗначения",
            {"Приемник": "Начисление"},
            owner="Виды",
            source=source.conn,
            target=target.conn,
        )
    create_rule(
        rules,
        "pkz",
        "Начисление",
        {"Приемник": "Начисление"},
        owner="Виды",
        source=source.conn,
        target=target.conn,
    )
    with pytest.raises(DanglingReferenceError, match="Объект выборки"):
        create_rule(
            rules,
            "pvd",
            "Чужой",
            {"ОбъектВыборки": "ДокументСсылка.Нет"},
            source=source.conn,
        )
    partial = create_rule(
        rules,
        "pvd",
        "Выгрузка",
        {"ОбъектВыборки": document},
        source=source.conn,
    )
    assert partial.skipped == [
        "правка применена; проверка по структуре приёмника не выполнена: структура не передана"
    ]
    _assert_sound(rules)


def test_edit_uses_the_same_structure_checks_as_the_document(tmp_path: Path) -> None:
    """Одноимённые ТЧ и набор движений, типы и покрытие — те же `structure.*`, что у документа."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    shared: list[Prop] = [
        (
            "ТабличнаяЧасть",
            "Товары",
            "",
            "",
            [("Реквизит", "Номенклатура", "", "Строка", [])],
        ),
        (
            "НаборДвиженийРегистраНакопления",
            "Товары",
            "",
            "",
            [("Реквизит", "Количество", "", "Число", [])],
        ),
        ("Реквизит", "Владелец", "", "СправочникСсылка.Контрагенты", []),
    ]
    source.add("Документ", "Приход", [*shared, ("Реквизит", "Артикул", "", "Строка", [])])
    target.add(
        "Документ",
        "Приход",
        [
            *shared,
            ("Реквизит", "Артикул", "", "СправочникСсылка.Номенклатура", []),
            (
                "НаборДвиженийРегистраНакопления",
                "Остатки",
                "",
                "",
                [("Реквизит", "Сумма", "", "Число", [])],
            ),
        ],
    )
    source.add("Справочник", "Организации")
    source.add("Перечисление", "Виды", values=(("Начисление", ""), ("Удержание", "")))
    target.add("Перечисление", "Виды", values=(("Начисление", ""), ("Удержание", "")))
    rules = _rules()
    document = "ДокументСсылка.Приход"
    create_rule(
        rules,
        "pko",
        "Приход",
        {"Источник": document, "Приемник": document},
        source=source.conn,
        target=target.conn,
    )
    create_rule(
        rules,
        "pks_group",
        "Товары",
        {
            "Источник": _side("Товары", "ТабличнаяЧасть"),
            "Приемник": _side("Товары", "ТабличнаяЧасть"),
        },
        owner="Приход",
        source=source.conn,
        target=target.conn,
    )
    tabular = create_rule(
        rules,
        "pks",
        "Товары/Номенклатура",
        _pks_sides("Номенклатура", type_name="Строка"),
        owner="Приход",
        source=source.conn,
        target=target.conn,
    )
    assert tabular.address == "ПКО «Приход» / ПКС Товары/Номенклатура"

    before = dump_rules(rules)
    with pytest.raises(DanglingReferenceError, match=r"structure\.pks_source") as missing_group:
        create_rule(
            rules,
            "pks_group",
            "Остатки",
            {
                "Источник": _side("Остатки", "НаборДвиженийРегистраНакопления"),
                "Приемник": _side("Остатки", "НаборДвиженийРегистраНакопления"),
            },
            owner="Приход",
            source=source.conn,
            target=target.conn,
        )
    assert "НаборДвиженийРегистраНакопления" in str(missing_group.value)
    assert dump_rules(rules) == before

    # Группа в источнике не найдена: реквизит внутри не даёт замечания источника.
    create_rule(
        rules,
        "pks_group",
        "Остатки",
        {
            "Источник": _side("Остатки", "НаборДвиженийРегистраНакопления"),
            "Приемник": _side("Остатки", "НаборДвиженийРегистраНакопления"),
        },
        owner="Приход",
        target=target.conn,
    )
    nested = create_rule(
        rules,
        "pks",
        "Остатки/Сумма",
        _pks_sides("Сумма", type_name="Число"),
        owner="Приход",
        source=source.conn,
        target=target.conn,
    )
    assert not any(item.startswith("structure.pks_source") for item in nested.warnings)

    disabled = create_rule(
        rules,
        "pks",
        "НетРеквизита",
        {"Отключить": True, **_pks_sides("НетРеквизита", type_name="Строка")},
        owner="Приход",
        source=source.conn,
        target=target.conn,
    )
    assert disabled.warnings == []

    with pytest.raises(DanglingReferenceError, match=r"structure\.pks_type"):
        create_rule(
            rules,
            "pks",
            "Артикул",
            _pks_sides("Артикул"),
            owner="Приход",
            source=source.conn,
            target=target.conn,
        )
    reference = create_rule(
        rules,
        "pks",
        "Владелец",
        _pks_sides("Владелец", kind="Свойство", type_name="СправочникСсылка.Контрагенты"),
        owner="Приход",
        source=source.conn,
        target=target.conn,
    )
    assert any(item.startswith("structure.pko_missing:") for item in reference.warnings)

    enum = "ПеречислениеСсылка.Виды"
    create_rule(
        rules,
        "pko",
        "Виды",
        {"Источник": enum, "Приемник": enum},
        source=source.conn,
        target=target.conn,
    )
    covered = create_rule(
        rules,
        "pkz",
        "Начисление",
        {"Приемник": "Начисление"},
        owner="Виды",
        source=source.conn,
        target=target.conn,
    )
    assert any(item.startswith("structure.pkz_coverage:") for item in covered.warnings)
    assert find_rule(rules, "pkz", "Начисление", owner="Виды").get("Приемник") == "Начисление"

    selection = create_rule(
        rules,
        "pvd",
        "ЧужаяВыборка",
        {
            "ОбъектВыборки": "СправочникСсылка.Организации",
            "КодПравилаКонвертации": "Приход",
        },
        source=source.conn,
        target=target.conn,
    )
    assert any(item.startswith("structure.pvd_pko:") for item in selection.warnings)
    assert find_rule(rules, "pvd", "ЧужаяВыборка").get("ОбъектВыборки") == (
        "СправочникСсылка.Организации"
    )
    _assert_sound(rules)


def test_without_structures_object_check_is_skipped_and_reported() -> None:
    rules = _rules()
    result: EditResult = create_rule(
        rules,
        "pko",
        "Нет",
        {"Источник": "СправочникСсылка.Нет", "Приемник": "СправочникСсылка.Нет"},
    )
    assert result.skipped == [
        "правка применена; проверка по структуре источника не выполнена: структура не передана",
        "правка применена; проверка по структуре приёмника не выполнена: структура не передана",
    ]
    assert rules.pko()[0].get("Источник") == "СправочникСсылка.Нет"


def test_one_structure_checks_only_its_side(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    source.add("Справочник", "Организации")
    rules = _rules()
    result = create_rule(
        rules,
        "pko",
        "Организации",
        {"Источник": "СправочникСсылка.Организации", "Приемник": "СправочникСсылка.Нет"},
        source=source.conn,
    )
    assert result.skipped == [
        "правка применена; проверка по структуре приёмника не выполнена: структура не передана"
    ]
    before = dump_rules(rules)
    with pytest.raises(DanglingReferenceError, match="источника"):
        update_rule(
            rules,
            "pko",
            "Организации",
            {"Источник": "СправочникСсылка.Нет"},
            source=source.conn,
        )
    assert dump_rules(rules) == before


def test_create_pko_inside_existing_group() -> None:
    """ПКО ложится в items группы «Справочники», а не рядом с ней в корне списка."""
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    result = create_rule(
        rules,
        "pko",
        "Контрагенты",
        {"Источник": "СправочникСсылка.Контрагенты", "Приемник": "СправочникСсылка.Контрагенты"},
        group="Справочники",
    )
    assert result.address == "ПКО «Контрагенты»"
    section = rules.root.children["ПравилаКонвертацииОбъектов"]
    catalogs = _group_node(rules, "ПравилаКонвертацииОбъектов", "Справочники")
    assert [item.code for item in catalogs.items] == ["Организации", "ВидыОпераций", "Контрагенты"]
    assert catalogs.items[-1] in rules.pko()
    assert all(item.code != "Контрагенты" for item in section.items)

    raw = dump_rules(rules)
    written = etree.fromstring(raw).find(
        "ПравилаКонвертацииОбъектов/Группа[Код='Справочники']/Правило[Код='Контрагенты']"
    )
    assert written is not None
    parent = written.getparent()
    assert parent is not None and parent.tag == "Группа"
    _assert_sound(rules)


@pytest.mark.parametrize(
    ("kind", "section"),
    [
        ("pko", "ПравилаКонвертацииОбъектов"),
        ("pvd", "ПравилаВыгрузкиДанных"),
        ("pod", "ПравилаОчисткиДанных"),
    ],
)
def test_create_rule_in_nested_group(kind: str, section: str) -> None:
    rules = _section_with_groups(
        section, "<Группа><Код>A</Код><Группа><Код>B</Код></Группа></Группа>"
    )
    create_rule(rules, kind, "Новый", group="A/B")
    outer = _group_node(rules, section, "A")
    inner = _group_node(rules, section, "A/B")
    assert [item.code for item in inner.items] == ["Новый"]
    assert all(item.code != "Новый" for item in outer.items)
    assert any(item.code == "Новый" for item in rules.root.children[section].walk())
    _assert_sound(rules)


def test_missing_group_is_not_created() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    before = dump_rules(rules)
    with pytest.raises(
        RuleNotFoundError, match="Группа «НетТакой» в списке ПравилаКонвертацииОбъектов не найдена"
    ):
        create_rule(rules, "pko", "Новый", group="НетТакой")
    with pytest.raises(
        RuleNotFoundError,
        match="Группа «Справочники/Нет» в списке ПравилаВыгрузкиДанных не найдена",
    ):
        create_rule(rules, "pvd", "Новый", group="Справочники/Нет")
    assert dump_rules(rules) == before


@pytest.mark.parametrize("kind", ["algorithm", "query", "parameter", "pks", "pks_group", "pkz"])
def test_group_rejected_for_kind_without_list_group(kind: str) -> None:
    rules = _rules()
    before = dump_rules(rules)
    with pytest.raises(RuleEditError, match="задаётся только для ПКО, ПВД и ПОД"):
        create_rule(rules, kind, "Код", group="Справочники")
    assert dump_rules(rules) == before


def test_duplicate_code_in_another_group_is_refused() -> None:
    rules = _section_with_groups(
        "ПравилаКонвертацииОбъектов",
        "<Группа><Код>Справочники</Код></Группа>"
        "<Группа><Код>Другая</Код>"
        "<Правило><Код>Организации</Код><Источник>А</Источник><Приемник>Б</Приемник></Правило>"
        "</Группа>",
    )
    before = dump_rules(rules)
    with pytest.raises(DuplicateRuleError, match="кодом «Организации»"):
        create_rule(
            rules,
            "pko",
            "Организации",
            {"Источник": "А", "Приемник": "Б"},
            group="Справочники",
        )
    assert dump_rules(rules) == before


def test_create_pko_with_properties_lands_in_group(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Контрагенты", synonym="Контрагенты")
    target.add("Справочник", "Контрагенты", synonym="Контрагенты")
    rules = _section_with_groups(
        "ПравилаКонвертацииОбъектов",
        "<Группа><Код>Другая</Код>"
        "<Правило><Код>Занято</Код><Источник>А</Источник><Приемник>Б</Приемник></Правило>"
        "</Группа>"
        "<Группа><Код>Справочники</Код></Группа>",
    )
    before = dump_rules(rules)
    with pytest.raises(RuleNotFoundError, match="Группа «Нет»"):
        create_pko_with_properties(
            rules,
            "Контрагенты",
            source.conn,
            target.conn,
            "Справочник.Контрагенты",
            "Справочник.Контрагенты",
            group="Нет",
        )
    assert dump_rules(rules) == before
    result = create_pko_with_properties(
        rules,
        "Контрагенты",
        source.conn,
        target.conn,
        "Справочник.Контрагенты",
        "Справочник.Контрагенты",
        group="Справочники",
    )
    assert result.address == "ПКО «Контрагенты»"
    catalogs = _group_node(rules, "ПравилаКонвертацииОбъектов", "Справочники")
    assert [item.code for item in catalogs.items] == ["Контрагенты"]
    assert catalogs.items[0] in rules.pko()
    with pytest.raises(DuplicateRuleError, match="Занято"):
        create_pko_with_properties(
            rules,
            "Занято",
            source.conn,
            target.conn,
            "Справочник.Контрагенты",
            "Справочник.Контрагенты",
            group="Справочники",
        )


# --- Умолчания КД ------------------------------------------------------------------------------

_HIERARCHY_ATTRS = '{"Иерархический": "true", "ВидИерархии": "ИерархияГруппИЭлементов"}'
_SYNC_NOTE = (
    "СинхронизироватьПоИдентификатору включено по умолчанию: ссылочный приёмник, не перечисление. "
    "Если данные уже заведены в обеих базах, при первой синхронизации поиск только по GUID даст "
    "дубли: нужны поля поиска и ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли"
)


def _mandatory_note(path: str) -> str:
    return f"ПКС «{path}»: Обязательное включено по умолчанию"


def _hierarchical_catalog(path: Path) -> _Builder:
    """Справочник с иерархией групп и элементов: свойства, как их пишет MD83Exp."""
    builder = _Builder(path)
    props: list[Prop] = [
        ("Свойство", "Родитель", "Родитель", "СправочникСсылка.Номенклатура", []),
        ("Свойство", "ЭтоГруппа", "Это группа", "Булево", []),
        ("Свойство", "МоёЭтоГруппа", "", "Булево", []),
        ("Свойство", "Группа", "", "Булево", []),
        ("Реквизит", "Наименование", "", "Строка", []),
    ]
    builder.add("Справочник", "Номенклатура", props, synonym="Номенклатура")
    builder.conn.execute(
        "UPDATE objects SET attrs = ? WHERE name = ?", (_HIERARCHY_ATTRS, "Номенклатура")
    )
    return builder


def test_kd_defaults_for_reference_pko_and_group_property(tmp_path: Path) -> None:
    """Ссылочное ПКО синхронизируется по идентификатору, ПКС «ЭтоГруппа» обязательна.

    Перечисление и регистр флаг не получают. Имя приёмника — как `ПОДОБНО "%ЭтоГруппа%"`:
    подстрока подходит, голое «Группа» — нет. Оба признака переживают запись и чтение.
    """
    source = _hierarchical_catalog(tmp_path / "source.sqlite")
    target = _hierarchical_catalog(tmp_path / "target.sqlite")
    source.add("Перечисление", "Статусы", values=(("Черновик", ""),))
    target.add("Перечисление", "Статусы", values=(("Черновик", ""),))
    source.add("РегистрСведений", "Цены")
    target.add("РегистрСведений", "Цены")
    source.add("Константа", "ВалютаУчета")
    target.add("Константа", "ВалютаУчета")
    rules = _rules()

    result = create_pko_with_properties(
        rules,
        "Номенклатура",
        source.conn,
        target.conn,
        "Справочник.Номенклатура",
        "Справочник.Номенклатура",
    )

    pko = next(item for item in rules.pko() if item.code == "Номенклатура")
    assert pko.get("СинхронизироватьПоИдентификатору") is True
    props = _by_target(_pks(rules, "Номенклатура"))
    assert props["ЭтоГруппа"].attrs.get("Обязательное") is True
    assert props["МоёЭтоГруппа"].attrs.get("Обязательное") is True
    assert "Обязательное" not in props["Группа"].attrs
    assert "Обязательное" not in props["Наименование"].attrs
    assert result.warnings == [
        _SYNC_NOTE,
        _mandatory_note("ЭтоГруппа"),
        _mandatory_note("МоёЭтоГруппа"),
    ]

    raw = dump_rules(rules)
    rule = next(
        item
        for item in etree.fromstring(raw).iter("Правило")
        if item.findtext("Код") == "Номенклатура"
    )
    assert rule.findtext("СинхронизироватьПоИдентификатору") == "true"
    assert _property_element(raw, "Номенклатура", "ЭтоГруппа").get("Обязательное") == "true"
    assert _property_element(raw, "Номенклатура", "МоёЭтоГруппа").get("Обязательное") == "true"
    assert _property_element(raw, "Номенклатура", "Группа").get("Обязательное") is None
    loaded = load_rules(raw)
    assert isinstance(loaded, ExchangeRules)
    again = next(item for item in loaded.pko() if item.code == "Номенклатура")
    assert again.get("СинхронизироватьПоИдентификатору") is True
    again_props = again.child("Свойства")
    assert again_props is not None
    restored = _by_target(again_props)
    assert restored["ЭтоГруппа"].attrs.get("Обязательное") is True
    assert restored["МоёЭтоГруппа"].attrs.get("Обязательное") is True

    enum = create_pko_with_properties(
        rules,
        "Статусы",
        source.conn,
        target.conn,
        "Перечисление.Статусы",
        "Перечисление.Статусы",
    )
    enum_pko = next(item for item in rules.pko() if item.code == "Статусы")
    assert "СинхронизироватьПоИдентификатору" not in enum_pko.values
    assert _SYNC_NOTE not in enum.warnings

    register = create_pko_with_properties(
        rules, "Цены", source.conn, target.conn, "РегистрСведений.Цены", "РегистрСведений.Цены"
    )
    register_pko = next(item for item in rules.pko() if item.code == "Цены")
    assert "СинхронизироватьПоИдентификатору" not in register_pko.values
    assert _SYNC_NOTE not in register.warnings

    constant = create_pko_with_properties(
        rules, "Валюта", source.conn, target.conn, "Константа.ВалютаУчета", "Константа.ВалютаУчета"
    )
    constant_pko = next(item for item in rules.pko() if item.code == "Валюта")
    assert "СинхронизироватьПоИдентификатору" not in constant_pko.values
    assert _SYNC_NOTE not in constant.warnings
    _assert_sound(rules)


def test_explicit_fields_override_kd_defaults(tmp_path: Path) -> None:
    """Явное значение в `fields`, в том числе ложь, не заменяется умолчанием КД."""
    source = _hierarchical_catalog(tmp_path / "source.sqlite")
    target = _hierarchical_catalog(tmp_path / "target.sqlite")
    source.add("Перечисление", "Статусы")
    target.add("Перечисление", "Статусы")
    rules = _rules()

    kept_false = create_pko_with_properties(
        rules,
        "Номенклатура",
        source.conn,
        target.conn,
        "Справочник.Номенклатура",
        "Справочник.Номенклатура",
        {"СинхронизироватьПоИдентификатору": False},
    )
    pko = next(item for item in rules.pko() if item.code == "Номенклатура")
    assert pko.values["СинхронизироватьПоИдентификатору"] is False
    assert _SYNC_NOTE not in kept_false.warnings
    raw = dump_rules(rules)
    rule = next(
        item
        for item in etree.fromstring(raw).iter("Правило")
        if item.findtext("Код") == "Номенклатура"
    )
    assert rule.find("СинхронизироватьПоИдентификатору") is None
    # ПКС «ЭтоГруппа» умолчание всё равно получает: его в fields ПКО не передавали.
    assert _property_element(raw, "Номенклатура", "ЭтоГруппа").get("Обязательное") == "true"

    kept_true = create_pko_with_properties(
        rules,
        "Статусы",
        source.conn,
        target.conn,
        "Перечисление.Статусы",
        "Перечисление.Статусы",
        {"СинхронизироватьПоИдентификатору": True},
    )
    enum_pko = next(item for item in rules.pko() if item.code == "Статусы")
    assert enum_pko.get("СинхронизироватьПоИдентификатору") is True
    assert _SYNC_NOTE not in kept_true.warnings

    created = create_rule(
        rules,
        "pks",
        "ЭТОГРУППА",
        _pks_sides("ЭТОГРУППА", "Свойство", "Булево"),
        owner="Номенклатура",
    )
    assert created.warnings == [_mandatory_note("ЭТОГРУППА")]
    upper = _by_target(_pks(rules, "Номенклатура"))["ЭТОГРУППА"]
    assert upper.attrs.get("Обязательное") is True

    plain = create_rule(
        rules,
        "pks",
        "ГруппаСвоя",
        _pks_sides("ГруппаСвоя", "Реквизит", "Булево"),
        owner="Номенклатура",
    )
    assert _mandatory_note("ГруппаСвоя") not in plain.warnings
    assert "Обязательное" not in _by_target(_pks(rules, "Номенклатура"))["ГруппаСвоя"].attrs

    forced_off = create_rule(
        rules,
        "pks",
        "СуффиксЭтоГруппа",
        {**_pks_sides("СуффиксЭтоГруппа", "Свойство", "Булево"), "Обязательное": False},
        owner="Номенклатура",
    )
    assert _mandatory_note("СуффиксЭтоГруппа") not in forced_off.warnings
    off = _by_target(_pks(rules, "Номенклатура"))["СуффиксЭтоГруппа"]
    assert off.attrs.get("Обязательное") is False

    group = create_rule(
        rules,
        "pks_group",
        "ЭтоГруппаТЧ",
        {
            "Источник": _side("ЭтоГруппаТЧ", "ТабличнаяЧасть"),
            "Приемник": _side("ЭтоГруппаТЧ", "ТабличнаяЧасть"),
        },
        owner="Номенклатура",
    )
    assert not any("Обязательное" in item for item in group.warnings)
    tabular = _by_target(_pks(rules, "Номенклатура"))["ЭтоГруппаТЧ"]
    assert "Обязательное" not in tabular.attrs

    raw = dump_rules(rules)
    assert _property_element(raw, "Номенклатура", "ЭТОГРУППА").get("Обязательное") == "true"
    assert _property_element(raw, "Номенклатура", "СуффиксЭтоГруппа").get("Обязательное") is None
    loaded = load_rules(raw)
    assert isinstance(loaded, ExchangeRules)
    restored = _by_target(_pks(loaded, "Номенклатура"))
    assert restored["ЭТОГРУППА"].attrs.get("Обязательное") is True
    assert restored["ЭтоГруппа"].attrs.get("Обязательное") is True
    _assert_sound(rules)


def test_create_rule_pko_gets_identifier_sync_default() -> None:
    """ПКО, созданное напрямую, получает то же умолчание, что и ПКО из кандидатов.

    КД ставит флаг любому новому ПКО (`ПриемникПриИзмененииПКО`), вид приёмника здесь
    берётся из имени типа: структура стороны может быть не передана.
    """
    rules = _rules()

    def sides(type_name: str) -> dict[str, str]:
        return {"Источник": type_name, "Приемник": type_name}

    catalog = create_rule(rules, "pko", "Валюты", sides("СправочникСсылка.Валюты"))
    assert _SYNC_NOTE in catalog.warnings
    enum = create_rule(rules, "pko", "Статусы", sides("ПеречислениеСсылка.Статусы"))
    assert _SYNC_NOTE not in enum.warnings
    register = create_rule(rules, "pko", "Цены", sides("РегистрСведенийЗапись.Цены"))
    assert _SYNC_NOTE not in register.warnings
    no_target = create_rule(rules, "pko", "БезПриемника", {"Источник": "СправочникСсылка.Валюты"})
    assert _SYNC_NOTE not in no_target.warnings
    forced_off = create_rule(
        rules,
        "pko",
        "Банки",
        {**sides("СправочникСсылка.Банки"), "СинхронизироватьПоИдентификатору": False},
    )
    assert _SYNC_NOTE not in forced_off.warnings

    flags = {item.code: item.get("СинхронизироватьПоИдентификатору") for item in rules.pko()}
    assert flags["Валюты"] is True
    assert not flags["Статусы"]
    assert not flags["Цены"]
    assert not flags["БезПриемника"]
    assert flags["Банки"] is False
    # Правка существующего ПКО умолчание не подставляет: КД делает это только для нового.
    updated = update_rule(rules, "pko", "Статусы", {"Приемник": "СправочникСсылка.Статусы"})
    assert _SYNC_NOTE not in updated.warnings
    assert not next(item for item in rules.pko() if item.code == "Статусы").get(
        "СинхронизироватьПоИдентификатору"
    )


# --- Код и Порядок ПКС -------------------------------------------------------------------------


def _pks_sides(name: str, kind: str = "Реквизит", type_name: str = "") -> dict[str, dict[str, str]]:
    return {"Источник": _side(name, kind, type_name), "Приемник": _side(name, kind, type_name)}


def _property_element(raw: bytes, pko_code: str, target: str) -> etree._Element:
    root = etree.fromstring(raw)
    for rule in root.iter("Правило"):
        if rule.findtext("Код") != pko_code:
            continue
        properties = rule.find("Свойства")
        if properties is None:
            continue
        for prop in properties.iter("Свойство"):
            receiver = prop.find("Приемник")
            if receiver is not None and receiver.get("Имя") == target:
                return prop
    raise AssertionError(f"ПКС «{target}» в ПКО «{pko_code}» не найдено в XML")


def test_pks_code_and_order_follow_neighbors() -> None:
    """У ПКО с ПКС новый код — max+1, порядок — max+50 того же контейнера."""
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    create_rule(rules, "pks", "КПП", _pks_sides("КПП", type_name="Строка"), owner="Организации")
    created = _by_target(_pks(rules, "Организации"))["КПП"]
    # В макете у ИНН код «1» и порядок 50.
    assert created.values["Код"] == "2"
    assert created.values["Порядок"] == 100

    only_order = create_rule(
        rules,
        "pks",
        "ОГРН",
        {**_pks_sides("ОГРН", type_name="Строка"), "Порядок": 10},
        owner="Организации",
    )
    assert only_order.address == "ПКО «Организации» / ПКС ОГРН"
    ogrn = _by_target(_pks(rules, "Организации"))["ОГРН"]
    assert ogrn.values["Код"] == "3"
    assert ogrn.values["Порядок"] == 10
    _assert_sound(rules)


def test_pks_in_empty_properties_gets_first_code_and_order() -> None:
    """Пустой контейнер: код 1, порядок 0 — как первая строка автонастройки."""
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    create_rule(rules, "pks", "Имя", _pks_sides("Имя"), owner="ВидыОпераций")
    created = _pks(rules, "ВидыОпераций").items[0]
    assert created.values["Код"] == "1"
    assert created.values["Порядок"] == 0

    fresh = _rules()
    create_rule(fresh, "pko", "Новый", {"Источник": "А", "Приемник": "Б"})
    create_rule(fresh, "pks", "Поле", _pks_sides("Поле"), owner="Новый")
    assert _pks(fresh, "Новый").items[0].values["Код"] == "1"
    assert _pks(fresh, "Новый").items[0].values["Порядок"] == 0
    _assert_sound(fresh)


def test_pks_in_group_takes_order_from_group_and_code_from_pko() -> None:
    """Порядок — по группе, код — по всему ПКО, нечисловые коды не считаются."""
    rules = _rules()
    create_rule(rules, "pko", "Док", {"Источник": "А", "Приемник": "Б"})
    create_rule(
        rules,
        "pks",
        "Номер",
        {**_pks_sides("Номер"), "Код": "4", "Порядок": 50},
        owner="Док",
    )
    create_rule(
        rules,
        "pks",
        "Комментарий",
        {**_pks_sides("Комментарий"), "Код": "нечисло", "Порядок": 100},
        owner="Док",
    )
    create_rule(
        rules,
        "pks_group",
        "Строки",
        {**_pks_sides("Строки", "ТабличнаяЧасть"), "Код": "9", "Порядок": 150},
        owner="Док",
    )
    create_rule(
        rules,
        "pks",
        "Строки/Товар",
        {**_pks_sides("Товар"), "Код": "2", "Порядок": 50},
        owner="Док",
    )
    create_rule(rules, "pks", "Строки/Количество", _pks_sides("Количество"), owner="Док")
    group = _by_target(_pks(rules, "Док"))["Строки"]
    created = _by_target(group)["Количество"]
    # Коды ПКО: 4, «нечисло» (мимо), 9 у группы, 2 у строки. Порядок группы — 50, не 150 корня.
    assert created.values["Код"] == "10"
    assert created.values["Порядок"] == 100

    create_rule(rules, "pks_group", "Прочее", _pks_sides("Прочее", "ТабличнаяЧасть"), owner="Док")
    extra = _by_target(_pks(rules, "Док"))["Прочее"]
    assert extra.values["Код"] == "11"
    assert extra.values["Порядок"] == 200
    _assert_sound(rules)


def test_explicit_pks_code_and_order_are_kept() -> None:
    """Переданные `Код` и `Порядок` не заменяются соседними; у ПКЗ и ПВД по-прежнему пусто."""
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    create_rule(
        rules,
        "pks",
        "КПП",
        {**_pks_sides("КПП", type_name="Строка"), "Код": 7, "Порядок": 10},
        owner="Организации",
    )
    created = _by_target(_pks(rules, "Организации"))["КПП"]
    assert created.values["Код"] == 7
    assert created.values["Порядок"] == 10

    create_rule(rules, "pkz", "Прочее", {"Приемник": "Прочее"}, owner="Организации")
    values = next(item for item in rules.pko() if item.code == "Организации").child("Значения")
    assert values is not None
    pkz = next(item for item in values.items if str(item.get("Источник")) == "Прочее")
    assert "Код" not in pkz.values
    assert "Порядок" not in pkz.values

    create_rule(rules, "pvd", "Новая", {"Наименование": "Новая"})
    pvd = next(item for item in rules.pvd() if item.code == "Новая")
    assert pvd.values["Код"] == "Новая"
    assert "Порядок" not in pvd.values
    _assert_sound(rules)


def _pks_with_group(rules: ExchangeRules) -> None:
    """К ПКО «Организации» макета: ещё два ПКС и группа с двумя вложенными."""
    create_rule(rules, "pks", "КПП", _pks_sides("КПП", type_name="Строка"), owner="Организации")
    create_rule(
        rules,
        "pks",
        "Наименование",
        _pks_sides("Наименование", type_name="Строка"),
        owner="Организации",
    )
    create_rule(
        rules,
        "pks_group",
        "Контакты",
        {
            "Источник": _side("Контакты", "ТабличнаяЧасть"),
            "Приемник": _side("Контакты", "ТабличнаяЧасть"),
        },
        owner="Организации",
    )
    create_rule(
        rules,
        "pks",
        "Контакты/Телефон",
        _pks_sides("Телефон", type_name="Строка"),
        owner="Организации",
    )
    create_rule(
        rules,
        "pks",
        "Контакты/Почта",
        _pks_sides("Почта", type_name="Строка"),
        owner="Организации",
    )


def _pks_flags(rules: ExchangeRules) -> dict[str, object]:
    return {
        path: node.values.get("НеЗамещать") for path, node in walk_pks(_pks(rules, "Организации"))
    }


def test_update_rules_sets_flag_on_every_pks_except_two() -> None:
    """Все ПКС, кроме двух адресов, получают НеЗамещать; вложенные тоже, группа — нет."""
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    _pks_with_group(rules)
    results = update_rules(
        rules,
        "pks",
        {"НеЗамещать": True},
        owner="Организации",
        except_keys=["ИНН", "Контакты/Телефон"],
    )
    assert [item.address for item in results.results] == [
        "ПКО «Организации» / ПКС КПП",
        "ПКО «Организации» / ПКС Наименование",
        "ПКО «Организации» / ПКС Контакты/Почта",
    ]
    assert _pks_flags(rules) == {
        "ИНН": None,
        "КПП": True,
        "Наименование": True,
        "Контакты": None,
        "Контакты/Телефон": None,
        "Контакты/Почта": True,
    }
    _assert_sound(rules)


def test_update_rules_explicit_keys_follow_document_order() -> None:
    """`keys` берёт ровно перечисленные ПКС, но обрабатывает их в порядке обхода ПКО."""
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    _pks_with_group(rules)
    results = update_rules(
        rules,
        "pks",
        {"НеЗамещать": True},
        owner="Организации",
        keys=["Контакты/Почта", "ИНН"],
    )
    assert [item.address for item in results.results] == [
        "ПКО «Организации» / ПКС ИНН",
        "ПКО «Организации» / ПКС Контакты/Почта",
    ]
    assert _pks_flags(rules)["КПП"] is None
    assert _pks_flags(rules)["Контакты"] is None
    assert _pks_flags(rules)["Контакты/Телефон"] is None


def test_update_rules_unknown_key_changes_nothing() -> None:
    """Неизвестный адрес в keys или except_keys — ошибка до правок."""
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    _pks_with_group(rules)
    before = dump_rules(rules)
    with pytest.raises(RuleNotFoundError):
        update_rules(
            rules,
            "pks",
            {"НеЗамещать": True},
            owner="Организации",
            keys=["ИНН", "НетТакого"],
        )
    with pytest.raises(RuleNotFoundError):
        update_rules(
            rules,
            "pks",
            {"НеЗамещать": True},
            owner="Организации",
            except_keys=["НетТакого"],
        )
    assert dump_rules(rules) == before


def test_update_rules_rolls_back_when_a_later_target_fails(tmp_path: Path) -> None:
    """Структура принимает первые ПКС и отвергает отсутствующий реквизит: флаг не остаётся."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    props = [
        ("Реквизит", "ИНН", "ИНН", "Строка", ()),
        ("Реквизит", "КПП", "КПП", "Строка", ()),
    ]
    source.add("Справочник", "Организации", props)
    target.add("Справочник", "Организации", props)
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    create_rule(rules, "pks", "КПП", _pks_sides("КПП", type_name="Строка"), owner="Организации")
    create_rule(
        rules,
        "pks",
        "НетРеквизита",
        _pks_sides("НетРеквизита", type_name="Строка"),
        owner="Организации",
    )
    before = dump_rules(rules)
    with pytest.raises(DanglingReferenceError, match="НетРеквизита"):
        update_rules(
            rules,
            "pks",
            {"НеЗамещать": True},
            owner="Организации",
            source=source.conn,
            target=target.conn,
        )
    assert dump_rules(rules) == before


def test_update_rules_rejects_top_level_kind() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    before = dump_rules(rules)
    with pytest.raises(RuleEditError, match="rule_update по одному"):
        update_rules(rules, "pod", {"НеЗамещать": False}, owner="Организации")
    assert dump_rules(rules) == before


def test_update_rules_rejects_empty_selection() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    with pytest.raises(RuleEditError, match="Нечего менять"):
        update_rules(
            rules,
            "pks",
            {"НеЗамещать": True},
            owner="Организации",
            except_keys=["ИНН"],
        )


def test_update_rules_group_kind_touches_only_groups() -> None:
    """Вид pks_group меняет группу и не ставит флаг вложенным ПКС."""
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    _pks_with_group(rules)
    results = update_rules(rules, "pks_group", {"НеЗамещать": True}, owner="Организации")
    assert [item.address for item in results.results] == ["ПКО «Организации» / ПКС Контакты"]
    flags = _pks_flags(rules)
    assert flags["Контакты"] is True
    assert flags["Контакты/Телефон"] is None
    assert flags["ИНН"] is None


def test_update_rules_pkz_except_one() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    create_rule(rules, "pkz", "Удержание", {"Приемник": "Удержание"}, owner="ВидыОпераций")
    results = update_rules(
        rules,
        "pkz",
        {"Наименование": "Значение"},
        owner="ВидыОпераций",
        except_keys=["Начисление"],
    )
    assert [item.address for item in results.results] == ["ПКО «ВидыОпераций» / ПКЗ Удержание"]
    kept = find_rule(rules, "pkz", "Начисление", "ВидыОпераций")
    changed = find_rule(rules, "pkz", "Удержание", "ВидыОпераций")
    assert "Наименование" not in kept.values
    assert changed.values["Наименование"] == "Значение"


def test_pks_code_and_order_round_trip() -> None:
    """`<Код>` и `<Порядок>` новой ПКС пишутся в XML и читаются обратно."""
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    create_rule(rules, "pks", "КПП", _pks_sides("КПП", type_name="Строка"), owner="Организации")
    raw = dump_rules(rules)
    element = _property_element(raw, "Организации", "КПП")
    assert element.findtext("Код") == "2"
    assert element.findtext("Порядок") == "100"
    loaded = load_exchange_rules(raw)
    again = _by_target(_pks(loaded, "Организации"))["КПП"]
    assert again.values["Код"] == "2"
    assert again.values["Порядок"] == 100


def _property_xml(name: str, *, search: bool = False) -> str:
    attrs = ' Поиск="true"' if search else ""
    return (
        f"<Свойство{attrs}>"
        f'<Источник Имя="{name}" Вид="Реквизит" Тип="Строка"/>'
        f'<Приемник Имя="{name}" Вид="Реквизит" Тип="Строка"/>'
        "</Свойство>"
    )


def _accounts(*properties: str) -> ExchangeRules:
    """ПКО «БанковскиеСчета» с заданными ПКС. Два свойства с одним именем грузятся из XML."""
    body = "".join(properties)
    xml = (
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
        "<ПравилаКонвертацииОбъектов><Правило><Код>БанковскиеСчета</Код>"
        f"<Свойства>{body}</Свойства></Правило></ПравилаКонвертацииОбъектов></ПравилаОбмена>"
    )
    return load_exchange_rules(xml.encode())


def test_update_by_search_qualifier_changes_only_the_search_pks() -> None:
    """`Владелец[поиск]` меняет поисковую ПКС, а не обычную с тем же именем."""
    rules = _accounts(_property_xml("Владелец", search=True), _property_xml("Владелец"))
    result = update_rule(
        rules,
        "pks",
        "Владелец[поиск]",
        {"Комментарий": "поиск"},
        owner="БанковскиеСчета",
    )
    assert result.address == "ПКО «БанковскиеСчета» / ПКС Владелец[поиск]"
    properties = rules.pko()[0].child("Свойства")
    assert properties is not None
    assert properties.items[0].values["Комментарий"] == "поиск"
    assert "Комментарий" not in properties.items[1].values


def test_bare_pks_name_is_ambiguous_when_search_and_plain_share_it() -> None:
    """Голое имя при двух кандидатах — `ambiguous_address` с обоими адресами."""
    rules = _accounts(_property_xml("Владелец", search=True), _property_xml("Владелец"))
    before = dump_rules(rules)
    with pytest.raises(AmbiguousAddressError) as error:
        update_rule(rules, "pks", "Владелец", {"Комментарий": "нет"}, owner="БанковскиеСчета")
    message = str(error.value)
    assert message == (
        "Адрес «Владелец» подходит нескольким правилам: "
        "ПКО «БанковскиеСчета» / ПКС Владелец[поиск], "
        "ПКО «БанковскиеСчета» / ПКС Владелец"
    )
    assert error_payload(error.value)["code"] == "ambiguous_address"
    assert dump_rules(rules) == before


def test_bare_pks_name_finds_the_only_search_property() -> None:
    """Единственная поисковая ПКС находится по голому имени: адрес без квалификатора."""
    rules = _accounts(_property_xml("Владелец", search=True))
    result = update_rule(rules, "pks", "Владелец", {"Комментарий": "один"}, owner="БанковскиеСчета")
    assert result.address == "ПКО «БанковскиеСчета» / ПКС Владелец"
    with pytest.raises(RuleNotFoundError, match="не найдено") as error:
        update_rule(
            rules, "pks", "Владелец[поиск]", {"Комментарий": "нет"}, owner="БанковскиеСчета"
        )
    assert type(error.value) is RuleNotFoundError


def test_update_rules_key_selects_the_search_pks() -> None:
    """`rule_update_many` с `keys=["Владелец[поиск]"]` меняет только поисковую ПКС."""
    rules = _accounts(_property_xml("Владелец", search=True), _property_xml("Владелец"))
    results = update_rules(
        rules,
        "pks",
        {"НеЗамещать": True},
        owner="БанковскиеСчета",
        keys=["Владелец[поиск]"],
    )
    assert [item.address for item in results.results] == [
        "ПКО «БанковскиеСчета» / ПКС Владелец[поиск]"
    ]
    properties = rules.pko()[0].child("Свойства")
    assert properties is not None
    assert properties.items[0].values["НеЗамещать"] is True
    assert "НеЗамещать" not in properties.items[1].values


# --- Групповая правка ПКО и ПВД и пачка ПКО ----------------------------------------------------

_BULK_XML = """\
<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов>
<Группа><Код>Справочники</Код>
<Правило><Код>Валюты</Код>
<Источник>СправочникСсылка.Валюты</Источник><Приемник>СправочникСсылка.Валюты</Приемник>
</Правило>
<Группа><Код>Прочее</Код>
<Правило><Код>Контрагенты</Код>
<Источник>СправочникСсылка.Контрагенты</Источник><Приемник>СправочникСсылка.Контрагенты</Приемник>
</Правило>
</Группа>
<Правило><Код>Номенклатура</Код>
<Источник>СправочникСсылка.Номенклатура</Источник><Приемник>ДокументСсылка.Номенклатура</Приемник>
</Правило>
</Группа>
<Правило><Код>Поступление</Код>
<Источник>ДокументСсылка.Поступление</Источник><Приемник>ДокументСсылка.Поступление</Приемник>
</Правило>
<Правило><Код>Реализация</Код>
<Источник>ДокументСсылка.Реализация</Источник><Приемник>СправочникСсылка.Реализация</Приемник>
</Правило>
</ПравилаКонвертацииОбъектов>
<ПравилаВыгрузкиДанных>
<Правило><Код>Документы</Код><ОбъектВыборки>ДокументСсылка.Поступление</ОбъектВыборки></Правило>
<Правило><Код>БезВыборки</Код></Правило>
<Правило><Код>СправочникиВыгрузка</Код>
<ОбъектВыборки>СправочникСсылка.Валюты</ОбъектВыборки></Правило>
</ПравилаВыгрузкиДанных>
</ПравилаОбмена>
"""

_CATALOG_CODES = ("Валюты", "Контрагенты", "Номенклатура")
_ALL_PKO = (*_CATALOG_CODES, "Поступление", "Реализация")


def _bulk_rules() -> ExchangeRules:
    return load_exchange_rules(_BULK_XML.encode())


def _addresses(batch) -> list[str]:
    return [item.address for item in batch.results]


def _pko_addresses(codes: Sequence[str]) -> list[str]:
    return [f"ПКО «{code}»" for code in codes]


def _many(rules: ExchangeRules, source: _Builder, target: _Builder, **options):
    return create_pko_many(rules, "source", "target", source.conn, target.conn, **options)


def _pair(source: str, target: str, code: str | None = None) -> dict[str, str]:
    item = {"source": source, "target": target}
    if code is not None:
        item["code"] = code
    return item


def _occupy(rules: ExchangeRules, code: str, number: int) -> None:
    """Занимает код ПКО чужими типами, чтобы пара по типам не стала `exists`."""
    type_name = f"ДокументСсылка.Занятие{number}"
    create_rule(rules, "pko", code, {"Источник": type_name, "Приемник": type_name})


def test_update_many_filter_source_kind_selects_three_catalogs() -> None:
    """Из пяти ПКО фильтр source_kind берёт три справочника и не трогает документы."""
    rules = _bulk_rules()
    batch = update_rules(
        rules,
        "pko",
        {"Комментарий": "справочники"},
        filter={"source_kind": "Справочник"},
        expected_count=3,
    )
    assert _addresses(batch) == _pko_addresses(_CATALOG_CODES)
    assert batch.matched == 3
    assert batch.changed == 3
    for code in _CATALOG_CODES:
        assert find_rule(rules, "pko", code).values["Комментарий"] == "справочники"
    for code in ("Поступление", "Реализация"):
        assert "Комментарий" not in find_rule(rules, "pko", code).values


def test_update_many_filter_target_kind() -> None:
    """target_kind смотрит на Приемник: два справочника и Реализация, чей приёмник — справочник."""
    rules = _bulk_rules()
    batch = update_rules(
        rules, "pko", {"Комментарий": "приёмник"}, filter={"target_kind": "Справочник"}
    )
    assert _addresses(batch) == _pko_addresses(("Валюты", "Контрагенты", "Реализация"))
    assert "Комментарий" not in find_rule(rules, "pko", "Номенклатура").values
    assert "Комментарий" not in find_rule(rules, "pko", "Поступление").values


def test_update_many_filter_group_includes_subgroup() -> None:
    """Группа Справочники включает подгруппу Прочее и не включает ПКО вне группы."""
    rules = _bulk_rules()
    batch = update_rules(rules, "pko", {"Комментарий": "группа"}, filter={"group": "Справочники"})
    assert _addresses(batch) == _pko_addresses(_CATALOG_CODES)
    assert find_rule(rules, "pko", "Контрагенты").values["Комментарий"] == "группа"
    assert "Комментарий" not in find_rule(rules, "pko", "Поступление").values
    assert "Комментарий" not in find_rule(rules, "pko", "Реализация").values


def test_update_many_pvd_by_source_object() -> None:
    """ПВД отбираются по ОбъектВыборки. Правило без объекта выборки не попадает."""
    rules = _bulk_rules()
    batch = update_rules(rules, "pvd", {"Отключить": True}, filter={"source_kind": "Документ"})
    assert _addresses(batch) == ["ПВД «Документы»"]
    flags = {node.code: node.attrs.get("Отключить") for node in rules.pvd()}
    assert flags["Документы"] is True
    assert flags["БезВыборки"] is not True
    assert flags["СправочникиВыгрузка"] is not True


def test_update_many_pvd_target_kind_rejected() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(ValueError, match="target_kind"):
        update_rules(rules, "pvd", {"Отключить": True}, filter={"target_kind": "Справочник"})
    assert dump_rules(rules) == before


def test_update_many_unknown_object_kind_lists_allowed() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(ValueError, match="Справочники") as error:
        update_rules(rules, "pko", {"Комментарий": "x"}, filter={"source_kind": "Справочники"})
    allowed = str(error.value).split("Допустимые: ", 1)[1]
    listed = [item.strip() for item in allowed.split(",")]
    for info in KINDS.values():
        assert info[1] in listed
    assert "Справочники" not in listed
    assert dump_rules(rules) == before


def test_update_many_unknown_filter_key() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(ValueError, match="нет_такого"):
        update_rules(rules, "pko", {"Комментарий": "x"}, filter={"нет_такого": "Справочник"})
    assert dump_rules(rules) == before


def test_update_many_keys_and_filter_together() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(ValueError, match="ровно одно"):
        update_rules(
            rules,
            "pko",
            {"Комментарий": "x"},
            keys=["Валюты"],
            filter={"source_kind": "Справочник"},
        )
    assert dump_rules(rules) == before


def test_update_many_requires_keys_or_filter() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(ValueError, match="ровно одно"):
        update_rules(rules, "pko", {"Комментарий": "x"})
    assert dump_rules(rules) == before


def test_update_many_owner_rejected_for_pko() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(ValueError, match="owner"):
        update_rules(rules, "pko", {"Комментарий": "x"}, owner="Валюты", keys=["Валюты"])
    assert dump_rules(rules) == before


def test_update_many_filter_rejected_for_pks() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    before = dump_rules(rules)
    with pytest.raises(ValueError, match="Фильтр"):
        update_rules(rules, "pks", {"НеЗамещать": True}, owner="Организации", filter={})
    assert dump_rules(rules) == before


def test_update_many_except_keys_unknown_code() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(RuleNotFoundError, match="НетТакого"):
        update_rules(
            rules,
            "pko",
            {"Комментарий": "x"},
            filter={"source_kind": "Справочник"},
            except_keys=["НетТакого"],
        )
    assert dump_rules(rules) == before


def test_update_many_except_keys_with_keys_rejected() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(ValueError, match="except_keys"):
        update_rules(
            rules,
            "pko",
            {"Комментарий": "x"},
            keys=["Валюты"],
            except_keys=["Контрагенты"],
        )
    assert dump_rules(rules) == before


def test_update_many_code_field_rejected() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(RuleEditError, match="Код"):
        update_rules(rules, "pko", {"Код": "Другой"}, keys=["Валюты"])
    assert dump_rules(rules) == before
    assert find_rule(rules, "pko", "Валюты").code == "Валюты"


def test_update_many_empty_filter_selects_every_pko() -> None:
    """Пустой фильтр — все ПКО. Группы списка в выбор не входят."""
    rules = _bulk_rules()
    batch = update_rules(rules, "pko", {"Комментарий": "все"}, filter={})
    assert _addresses(batch) == _pko_addresses(_ALL_PKO)
    assert batch.matched == 5
    section = rules.root.children["ПравилаКонвертацииОбъектов"]
    group = section.items[0]
    assert group.code == "Справочники"
    assert group.is_group
    assert "Комментарий" not in group.values
    subgroup = next(item for item in group.items if item.is_group)
    assert subgroup.code == "Прочее"
    assert "Комментарий" not in subgroup.values


def test_update_many_missing_group() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(RuleNotFoundError, match="НетГруппы"):
        update_rules(rules, "pko", {"Комментарий": "x"}, filter={"group": "НетГруппы"})
    assert dump_rules(rules) == before


def test_update_many_keys_follow_document_order() -> None:
    """Повторы в keys схлопываются, порядок — порядок документа, не порядок списка."""
    rules = _bulk_rules()
    batch = update_rules(
        rules, "pko", {"Комментарий": "ключи"}, keys=["Реализация", "Валюты", "Валюты"]
    )
    assert _addresses(batch) == _pko_addresses(("Валюты", "Реализация"))
    assert batch.matched == 2
    assert batch.changed == 2


def test_update_many_expected_count_mismatch_changes_nothing() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(CountMismatchError) as error:
        update_rules(
            rules,
            "pko",
            {"Комментарий": "нет"},
            filter={"source_kind": "Справочник"},
            expected_count=2,
        )
    assert error.value.details == {"matched": 3}
    payload = error_payload(error.value)
    assert payload["code"] == "count_mismatch"
    assert payload["matched"] == 3
    assert dump_rules(rules) == before


def test_update_many_preview_restores_every_node() -> None:
    rules = _bulk_rules()
    before = dump_rules(rules)
    batch = update_rules(
        rules,
        "pko",
        {"Комментарий": "черновик"},
        filter={"source_kind": "Справочник"},
        preview=True,
        expected_count=3,
    )
    assert dump_rules(rules) == before
    assert batch.matched == 3
    assert batch.changed == 3
    assert batch.results[0].changes["Комментарий"] == {"old": None, "new": "черновик"}
    assert all("Комментарий" not in node.values for node in rules.pko())


def test_update_many_rolls_back_when_last_pko_fails(tmp_path: Path) -> None:
    """Приёмника последнего справочника нет в структуре: правка первых двух тоже снимается."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    for name in ("Валюты", "Контрагенты", "Номенклатура"):
        source.add("Справочник", name)
    target.add("Справочник", "Валюты")
    target.add("Справочник", "Контрагенты")
    rules = _bulk_rules()
    before = dump_rules(rules)
    with pytest.raises(DanglingReferenceError, match="Номенклатура"):
        update_rules(
            rules,
            "pko",
            {"Комментарий": "нет"},
            filter={"source_kind": "Справочник"},
            source=source.conn,
            target=target.conn,
        )
    assert dump_rules(rules) == before


def test_update_many_unchanged_value_is_not_changed() -> None:
    """Уже стоящее значение не попадает в changes и не считается изменённым."""
    rules = _bulk_rules()
    update_rule(rules, "pko", "Валюты", {"НеЗамещать": True})
    batch = update_rules(rules, "pko", {"НеЗамещать": True}, filter={"source_kind": "Справочник"})
    by_address = {item.address: item for item in batch.results}
    assert batch.matched == 3
    assert batch.changed == 2
    assert by_address["ПКО «Валюты»"].changes == {}
    assert by_address["ПКО «Контрагенты»"].changes["НеЗамещать"] == {"old": None, "new": True}
    assert find_rule(rules, "pko", "Валюты").values["НеЗамещать"] is True


def test_update_many_true_string_matches_bool() -> None:
    """Строка true приводится к булеву и при уже стоящем true не даёт changes."""
    rules = _bulk_rules()
    update_rule(rules, "pko", "Валюты", {"НеЗамещать": True})
    batch = update_rules(rules, "pko", {"НеЗамещать": "true"}, keys=["Валюты"])
    assert batch.matched == 1
    assert batch.changed == 0
    assert batch.results[0].changes == {}
    assert find_rule(rules, "pko", "Валюты").values["НеЗамещать"] is True


def test_update_many_pks_side_type_is_in_changes() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    batch = update_rules(
        rules,
        "pks",
        {"Приемник": {"Тип": "Число"}},
        owner="Организации",
        keys=["ИНН"],
    )
    assert batch.matched == 1
    assert batch.changed == 1
    changes = batch.results[0].changes
    assert changes["Приемник.Тип"] == {"old": "Строка", "new": "Число"}
    assert "Приемник.Имя" not in changes
    assert "Приемник.Вид" not in changes
    assert "Источник.Тип" not in changes


def test_create_many_kind_keeps_only_exact_pairs(tmp_path: Path) -> None:
    """kind берёт только auto: «по синониму» и «нет пары» в пачку не входят."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты", synonym="Валюты")
    target.add("Справочник", "Валюты", synonym="Валюты")
    source.add("Справочник", "Номенклатура", synonym="Товары")
    target.add("Справочник", "Товары", synonym="Товары")
    source.add("Справочник", "ТолькоИсточник")
    target.add("Справочник", "ТолькоПриемник")
    rules = _rules()
    before = dump_rules(rules)
    batch = _many(rules, source, target, kind="Справочник")
    assert dump_rules(rules) == before
    assert [(item.status, item.code, item.source, item.target) for item in batch.items] == [
        ("created", "Валюты", "Справочник.Валюты", "Справочник.Валюты")
    ]
    view = pko_batch_view(batch, 0, 50)
    assert view["mode"] == "preview"
    assert view["summary"] == {
        "requested": 1,
        "planned": 1,
        "skipped": 0,
        "skip_reasons": {},
        "pks": 0,
        "unresolved": 0,
        "not_applied": 0,
    }
    assert view["items"][0]["status"] == "planned"
    assert view["items"][0]["code"] == "Валюты"


def test_create_many_explicit_synonym_pair_keeps_only_auto_pks(tmp_path: Path) -> None:
    """Явная пара «по синониму» создаётся. ПКС — только auto; синоним в not_applied."""
    source_props: Sequence[Prop] = (
        ("Реквизит", "Код", "Код", "Строка", ()),
        ("Реквизит", "Артикул", "Код товара", "Строка", ()),
    )
    target_props: Sequence[Prop] = (
        ("Реквизит", "Код", "Код", "Строка", ()),
        ("Реквизит", "КодТовара", "код товара", "Строка", ()),
        ("Реквизит", "Лишний", "Лишний", "Строка", ()),
    )
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Номенклатура", source_props, synonym="Товары")
    target.add("Справочник", "Товары", target_props, synonym="Товары")
    rules = _rules()
    batch = _many(
        rules,
        source,
        target,
        pairs=[_pair("Справочник.Номенклатура", "Справочник.Товары")],
        fields={"Комментарий": "пачка"},
    )
    item = batch.items[0]
    assert item.status == "created"
    assert item.code == "Товары"
    assert item.source == "Справочник.Номенклатура"
    assert item.target == "Справочник.Товары"
    assert item.node is not None and item.node.values["Комментарий"] == "пачка"
    assert item.result is not None
    assert item.result.disabled == ["Лишний"]
    assert len(item.result.not_applied) == 1
    assert "Артикул" in item.result.not_applied[0]
    assert "КодТовара" in item.result.not_applied[0]
    assert "по синониму" in item.result.not_applied[0]
    view = pko_batch_view(batch, 0, 50)["items"][0]
    assert view["pks"] == 2
    assert view["disabled"] == 1
    assert view["not_applied"] == item.result.not_applied
    properties = item.node.child("Свойства")
    assert properties is not None
    assert [side_name(node, "Приемник") for node in properties.items] == ["Код", "Лишний"]


def test_create_many_exists_leaves_the_pko(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _rules()
    create_pko_with_properties(
        rules,
        "Уже",
        source.conn,
        target.conn,
        "Справочник.Валюты",
        "Справочник.Валюты",
        {"Комментарий": "держится"},
    )
    before = dump_rules(rules)
    preview = _many(rules, source, target, pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")])
    assert preview.items[0].status == "skipped"
    assert preview.items[0].reason == "exists"
    assert preview.items[0].existing == "Уже"
    applied = _many(
        rules,
        source,
        target,
        pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")],
        mode="apply",
        expected_preview_hash=preview.preview_hash,
    )
    assert dump_rules(rules) == before
    view = pko_batch_view(applied, 0, 50)
    assert view["summary"]["created"] == 0
    assert view["summary"]["skipped"] == 1
    assert view["summary"]["skip_reasons"] == {"exists": 1}
    assert view["items"][0]["existing"] == "Уже"
    assert find_rule(rules, "pko", "Уже").values["Комментарий"] == "держится"


def test_create_many_repeated_pair_is_exists(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _rules()
    pair = _pair("Справочник.Валюты", "Справочник.Валюты")
    batch = _many(rules, source, target, pairs=[pair, pair])
    assert batch.items[0].status == "created"
    assert batch.items[0].code == "Валюты"
    assert batch.items[1].status == "skipped"
    assert batch.items[1].reason == "exists"
    assert batch.items[1].existing == "Валюты"
    assert rules.pko() == []


def _unit_sides(tmp_path: Path) -> tuple[_Builder, _Builder]:
    props: Sequence[Prop] = (("Реквизит", "Единица", "Единица", "СправочникСсылка.Единицы", ()),)
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Номенклатура", props)
    target.add("Справочник", "Номенклатура", props)
    source.add("Справочник", "Единицы")
    target.add("Справочник", "Единицы")
    return source, target


def test_create_many_pks_sees_later_pko_in_the_batch(tmp_path: Path) -> None:
    """ПКС ссылки заполняется ПКО той же пачки, даже если оно идёт следом."""
    source, target = _unit_sides(tmp_path)
    rules = _rules()
    batch = _many(
        rules,
        source,
        target,
        pairs=[
            _pair("Справочник.Номенклатура", "Справочник.Номенклатура"),
            _pair("Справочник.Единицы", "Справочник.Единицы"),
        ],
    )
    item = batch.items[0]
    assert item.result is not None
    assert item.result.unresolved == []
    assert item.node is not None
    properties = item.node.child("Свойства")
    assert properties is not None
    assert properties.items[0].values["КодПравилаКонвертации"] == "Единицы"
    view = pko_batch_view(batch, 0, 50)["items"][0]
    assert "unresolved" not in view


def test_create_many_single_call_still_leaves_unresolved(tmp_path: Path) -> None:
    """Одиночное создание не ждёт ПКО, которого ещё нет: ссылка остаётся unresolved."""
    source, target = _unit_sides(tmp_path)
    rules = _rules()
    result = create_pko_with_properties(
        rules,
        "Номенклатура",
        source.conn,
        target.conn,
        "Справочник.Номенклатура",
        "Справочник.Номенклатура",
    )
    assert result.unresolved
    assert any("СправочникСсылка.Единицы" in line for line in result.unresolved)
    properties = find_rule(rules, "pko", "Номенклатура").child("Свойства")
    assert properties is not None
    assert "КодПравилаКонвертации" not in properties.items[0].values


def test_create_many_allocates_code_from_base(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    batch = _many(_rules(), source, target, pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")])
    assert batch.items[0].code == "Валюты"
    assert batch.items[0].result is not None
    assert not any("занят" in warning for warning in batch.items[0].result.warnings)


def test_create_many_occupied_base_gets_suffix(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _rules()
    _occupy(rules, "Валюты", 1)
    batch = _many(rules, source, target, pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")])
    assert batch.items[0].code == "Валюты00001"
    assert batch.items[0].result is not None
    assert "код «Валюты» занят — взят «Валюты00001»" in batch.items[0].result.warnings


def test_create_many_occupied_suffixes_skip_to_next(tmp_path: Path) -> None:
    """Заняты основа, …00001 и …00003: берётся …00004, а не дырка 00002."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _rules()
    for number, code in enumerate(("Валюты", "Валюты00001", "Валюты00003"), start=1):
        _occupy(rules, code, number)
    batch = _many(rules, source, target, pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")])
    assert batch.items[0].code == "Валюты00004"


def test_create_many_string_order_trap_picks_a_free_code(tmp_path: Path) -> None:
    """X1 больше X00002 как строка; первый кандидат X00002 занят, свободен X00003."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "X")
    target.add("Справочник", "X")
    rules = _rules()
    for number, code in enumerate(("X", "X1", "X00002"), start=1):
        _occupy(rules, code, number)
    batch = _many(rules, source, target, pairs=[_pair("Справочник.X", "Справочник.X")])
    assert batch.items[0].code == "X00003"
    assert batch.items[0].result is not None
    assert "код «X» занят — взят «X00003»" in batch.items[0].result.warnings


def test_create_many_base_is_truncated_to_45(tmp_path: Path) -> None:
    name = "А" * 50
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", name)
    target.add("Справочник", name)
    batch = _many(
        _rules(), source, target, pairs=[_pair(f"Справочник.{name}", f"Справочник.{name}")]
    )
    assert batch.items[0].code == "А" * 45
    assert len(batch.items[0].code) == 45


def test_create_many_explicit_code_taken_is_casefold(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Контрагенты")
    target.add("Справочник", "Контрагенты")
    rules = _rules()
    _occupy(rules, "Валюты", 1)
    before = dump_rules(rules)
    batch = _many(
        rules,
        source,
        target,
        pairs=[_pair("Справочник.Контрагенты", "Справочник.Контрагенты", "валюты")],
    )
    assert dump_rules(rules) == before
    assert batch.items[0].status == "skipped"
    assert batch.items[0].reason == "code_taken"
    assert batch.items[0].code == "валюты"
    view = pko_batch_view(batch, 0, 50)
    assert view["summary"]["skip_reasons"] == {"code_taken": 1}
    assert view["items"][0]["code"] == "валюты"
    assert "existing" not in view["items"][0]


def test_create_many_exists_wins_over_code_taken(tmp_path: Path) -> None:
    """Та же пара типов — exists, даже если явный код уже занят этим ПКО."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _rules()
    create_pko_with_properties(
        rules, "Валюты", source.conn, target.conn, "Справочник.Валюты", "Справочник.Валюты"
    )
    batch = _many(
        rules,
        source,
        target,
        pairs=[_pair("Справочник.Валюты", "Справочник.Валюты", "Валюты")],
    )
    assert batch.items[0].reason == "exists"
    assert batch.items[0].existing == "Валюты"


def test_create_many_unknown_objects_are_one_error(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _rules()
    before = dump_rules(rules)
    with pytest.raises(ObjectNotFoundError) as error:
        _many(
            rules,
            source,
            target,
            pairs=[
                _pair("Справочник.Валюты", "Справочник.Валюты"),
                _pair("Справочник.НетИсточника", "Справочник.НетПриемника"),
                _pair("Справочник.ЕщёНет", "Справочник.Валюты"),
            ],
        )
    text = str(error.value)
    assert "НетИсточника" in text
    assert "НетПриемника" in text
    assert "ЕщёНет" in text
    assert dump_rules(rules) == before
    assert rules.pko() == []


def test_create_many_unknown_kind_lists_both_sides(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Документ", "Приход")
    rules = _rules()
    before = dump_rules(rules)
    with pytest.raises(ValueError, match="Перечисление") as error:
        _many(rules, source, target, kind="Перечисление")
    text = str(error.value)
    assert "Справочник" in text
    assert "Документ" in text
    assert dump_rules(rules) == before


def test_create_many_identity_fields_rejected(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _rules()
    before = dump_rules(rules)
    with pytest.raises(RuleEditError, match="Источник"):
        _many(
            rules,
            source,
            target,
            pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")],
            fields={"Источник": "СправочникСсылка.Валюты"},
        )
    assert dump_rules(rules) == before


def test_create_many_structure_error_rolls_back_inserts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    source.add("Справочник", "Контрагенты")
    target.add("Справочник", "Контрагенты")
    rules = _rules()
    before = dump_rules(rules)
    calls = {"n": 0}

    def boom(*_args, **_kwargs) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise DanglingReferenceError("проверка второго ПКО")
        return []

    monkeypatch.setattr("kd_rules_mcp.authoring.edits._apply_structure", boom)
    with pytest.raises(DanglingReferenceError, match="второго"):
        _many(
            rules,
            source,
            target,
            pairs=[
                _pair("Справочник.Валюты", "Справочник.Валюты"),
                _pair("Справочник.Контрагенты", "Справочник.Контрагенты"),
            ],
        )
    assert dump_rules(rules) == before
    assert "ПравилаКонвертацииОбъектов" not in rules.root.children


def test_create_many_hash_ignores_page(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    source.add("Справочник", "Контрагенты")
    target.add("Справочник", "Контрагенты")
    rules = _rules()
    first = _many(rules, source, target, kind="Справочник")
    second = _many(rules, source, target, kind="Справочник")
    assert first.preview_hash == second.preview_hash
    page_a = pko_batch_view(first, 0, 1)
    page_b = pko_batch_view(first, 1, 1)
    assert page_a["preview_hash"] == page_b["preview_hash"] == first.preview_hash
    assert page_a["items"][0]["code"] != page_b["items"][0]["code"]
    assert page_a["has_more"] is True
    assert page_a["total"] == 2
    assert page_a["offset"] == 0
    assert page_a["limit"] == 1
    assert rules.pko() == []


def test_create_many_hash_changes_after_manual_pko(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    source.add("Справочник", "Контрагенты")
    target.add("Справочник", "Контрагенты")
    rules = _rules()
    pairs = [
        _pair("Справочник.Валюты", "Справочник.Валюты"),
        _pair("Справочник.Контрагенты", "Справочник.Контрагенты"),
    ]
    original = _many(rules, source, target, pairs=pairs).preview_hash
    create_pko_with_properties(
        rules, "Валюты", source.conn, target.conn, "Справочник.Валюты", "Справочник.Валюты"
    )
    preview = _many(rules, source, target, pairs=pairs)
    assert preview.preview_hash != original
    assert preview.items[0].reason == "exists"
    assert preview.items[0].existing == "Валюты"
    kept = dump_rules(rules)
    with pytest.raises(PreviewRequiredError) as stale:
        _many(rules, source, target, pairs=pairs, mode="apply", expected_preview_hash=original)
    assert stale.value.details == {"preview_hash": preview.preview_hash}
    assert error_payload(stale.value)["code"] == "preview_required"
    assert error_payload(stale.value)["preview_hash"] == preview.preview_hash
    assert dump_rules(rules) == kept
    with pytest.raises(PreviewRequiredError):
        _many(rules, source, target, pairs=pairs, mode="apply")
    assert dump_rules(rules) == kept
    again = _many(rules, source, target, pairs=pairs)
    assert dump_rules(rules) == kept
    assert again.preview_hash == preview.preview_hash
    applied = _many(
        rules,
        source,
        target,
        pairs=pairs,
        mode="apply",
        expected_preview_hash=preview.preview_hash,
    )
    assert applied.preview_hash == preview.preview_hash
    assert [node.code for node in rules.pko()] == ["Валюты", "Контрагенты"]
    view = pko_batch_view(applied, 0, 50)
    assert view["summary"]["created"] == 1
    assert view["summary"]["skipped"] == 1
    assert view["items"][1]["status"] == "created"


def test_create_many_preview_leaves_the_document(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _rules()
    before = dump_rules(rules)
    _many(rules, source, target, pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")])
    assert dump_rules(rules) == before
    assert "ПравилаКонвертацииОбъектов" not in rules.root.children


def test_create_many_group_and_missing_group(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _section_with_groups(
        "ПравилаКонвертацииОбъектов", "<Группа><Код>Справочники</Код></Группа>"
    )
    before = dump_rules(rules)
    with pytest.raises(RuleNotFoundError, match="НетТакой"):
        _many(
            rules,
            source,
            target,
            pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")],
            group="НетТакой",
        )
    assert dump_rules(rules) == before
    preview = _many(
        rules,
        source,
        target,
        pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")],
        group="Справочники",
    )
    assert dump_rules(rules) == before
    _many(
        rules,
        source,
        target,
        pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")],
        group="Справочники",
        mode="apply",
        expected_preview_hash=preview.preview_hash,
    )
    group = _group_node(rules, "ПравилаКонвертацииОбъектов", "Справочники")
    assert [item.code for item in group.items] == ["Валюты"]
    root = rules.root.children["ПравилаКонвертацииОбъектов"]
    assert all(item.code != "Валюты" for item in root.items)


def test_create_many_requires_one_selector(tmp_path: Path) -> None:
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    rules = _rules()
    with pytest.raises(ValueError, match="ровно одно"):
        _many(rules, source, target)
    with pytest.raises(ValueError, match="ровно одно"):
        _many(
            rules,
            source,
            target,
            kind="Справочник",
            pairs=[_pair("Справочник.Валюты", "Справочник.Валюты")],
        )
    with pytest.raises(ValueError, match="Режим"):
        _many(rules, source, target, kind="Справочник", mode="later")
    with pytest.raises(ValueError, match="неизвестные ключи"):
        _many(
            rules,
            source,
            target,
            pairs=[{"source": "Справочник.Валюты", "target": "x", "extra": "y"}],
        )
    batch = _many(rules, source, target, pairs=[])
    assert batch.items == []
    assert pko_batch_view(batch, 0, 50)["summary"]["planned"] == 0


# --- Находки ревью bulk-edits -----------------------------------------------------------------

_DUP_CODE = """<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов>
<Группа><Код>Справочники</Код>
<Правило><Код>Х</Код><Источник>СправочникСсылка.Х</Источник><Приемник>СправочникСсылка.Х</Приемник></Правило>
</Группа>
<Группа><Код>Документы</Код>
<Правило><Код>Х</Код><Источник>ДокументСсылка.Х</Источник><Приемник>ДокументСсылка.Х</Приемник></Правило>
</Группа>
</ПравилаКонвертацииОбъектов></ПравилаОбмена>"""


def test_update_many_duplicate_code_edits_the_filtered_node() -> None:
    """Повтор кода ПКО в разных группах: правится ровно выбранное фильтром правило."""
    rules = load_exchange_rules(_DUP_CODE.encode())
    batch = update_rules(rules, "pko", {"НеЗамещать": True}, filter={"source_kind": "Документ"})
    assert (batch.matched, batch.changed) == (1, 1)
    flags = {str(node.get("Источник")): node.values.get("НеЗамещать") for node in rules.pko()}
    assert flags == {"СправочникСсылка.Х": None, "ДокументСсылка.Х": True}

    rules = load_exchange_rules(_DUP_CODE.encode())
    batch = update_rules(rules, "pko", {"НеЗамещать": True}, filter={})
    assert (batch.matched, batch.changed) == (2, 2)
    assert all(node.values.get("НеЗамещать") is True for node in rules.pko())


def test_update_many_pkz_same_source_in_two_groups_edits_both() -> None:
    """ПКЗ с одним именем значения источника в двух группах значений: правятся обе."""
    xml = """<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов>
<Правило><Код>Виды</Код><Источник>ПеречислениеСсылка.Виды</Источник><Приемник>ПеречислениеСсылка.Виды</Приемник>
<Значения>
<Группа><Код>Г1</Код><Наименование>Г1</Наименование>
<Значение><Код>1</Код><Источник>Товар</Источник><Приемник>Товар</Приемник></Значение>
</Группа>
<Группа><Код>Г2</Код><Наименование>Г2</Наименование>
<Значение><Код>2</Код><Источник>Товар</Источник><Приемник>Услуга</Приемник></Значение>
</Группа>
</Значения>
</Правило>
</ПравилаКонвертацииОбъектов></ПравилаОбмена>"""
    rules = load_exchange_rules(xml.encode())
    batch = update_rules(rules, "pkz", {"Наименование": "x"}, owner="Виды")
    assert (batch.matched, batch.changed) == (2, 2)
    values = find_rule(rules, "pko", "Виды").child("Значения")
    assert values is not None
    assert [node.values.get("Наименование") for node in values.walk()] == ["x", "x"]


def test_update_many_clearing_absent_true_only_flag_is_not_a_change() -> None:
    """Флаг «только истина» без значения и ложь пишутся одинаково — не изменение."""
    xml = """<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов>
<Правило><Код>А</Код><Источник>СправочникСсылка.А</Источник><Приемник>СправочникСсылка.А</Приемник></Правило>
<Правило><Код>Б</Код><НеЗамещать>true</НеЗамещать><Источник>СправочникСсылка.Б</Источник><Приемник>СправочникСсылка.Б</Приемник></Правило>
</ПравилаКонвертацииОбъектов></ПравилаОбмена>"""
    rules = load_exchange_rules(xml.encode())
    batch = update_rules(rules, "pko", {"НеЗамещать": False}, filter={})
    assert (batch.matched, batch.changed) == (2, 1)
    assert [item.changes for item in batch.results] == [
        {},
        {"НеЗамещать": {"old": True, "new": False}},
    ]


def test_create_many_code_of_list_group_is_taken(tmp_path: Path) -> None:
    """Код группы списка ПКО занят, как в КД: новое ПКО получает суффикс."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    source.add("Справочник", "Валюты")
    target.add("Справочник", "Валюты")
    xml = """<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов><Группа><Код>Валюты</Код><Наименование>Валюты</Наименование></Группа>
</ПравилаКонвертацииОбъектов></ПравилаОбмена>"""
    rules = load_exchange_rules(xml.encode())
    batch = _many(rules, source, target, kind="Справочник")
    assert [item.code for item in batch.items] == ["Валюты00001"]


def test_create_many_pks_count_excludes_groups(tmp_path: Path) -> None:
    """`pks` в ответе пачки — число ПКС без групп ПКС (табличных частей)."""
    source = _Builder(tmp_path / "source.sqlite")
    target = _Builder(tmp_path / "target.sqlite")
    for builder in (source, target):
        builder.add(
            "Справочник",
            "А",
            [("ТабличнаяЧасть", "ТЧ", "", "", [("Реквизит", "Р", "", "Строка", ())])],
        )
    view = pko_batch_view(_many(_rules(), source, target, kind="Справочник"), 0, 50)
    assert view["summary"]["pks"] == 1
    assert view["items"][0]["pks"] == 1
