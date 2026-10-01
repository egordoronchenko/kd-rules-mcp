"""Точечные правки правил (спецификация `rules-authoring`, «Точечные правки правил»)."""

from collections.abc import Sequence
from pathlib import Path

import pytest

from kd2_rules_mcp.authoring.edits import (
    EditResult,
    create_pko_with_properties,
    create_rule,
    delete_rule,
    update_rule,
)
from kd2_rules_mcp.errors import (
    DanglingReferenceError,
    DuplicateRuleError,
    ObjectNotFoundError,
    RuleEditError,
    RuleNotFoundError,
    UnknownFieldError,
)
from kd2_rules_mcp.kd2.canonical import canonical_diff, canonical_form
from kd2_rules_mcp.kd2.model import ExchangeRules, Node
from kd2_rules_mcp.kd2.rules_io import dump_rules, load_exchange_rules, load_rules
from kd2_rules_mcp.structures import db
from kd2_rules_mcp.validation.address import side_name
from kd2_rules_mcp.validation.format import check_format

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
