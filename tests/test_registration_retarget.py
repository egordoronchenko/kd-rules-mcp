"""Перенацеливание правил регистрации: синтетика, отказы, концы строк."""

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from kd_rules_mcp.authoring.registration_retarget import (
    retarget_registration,
)
from kd_rules_mcp.errors import (
    DuplicateTargetPropertyError,
    InvalidRegistrationNameError,
    NotRegistrationRulesError,
    PropertyNameClashError,
    RegistrationRetargetError,
)
from kd_rules_mcp.kd2.diff import diff_rules
from kd_rules_mcp.kd2.model import Node, RegistrationRules
from kd_rules_mcp.kd2.rules_io import dump_rules, load_exchange_rules, load_registration_rules
from kd_rules_mcp.kd2.xmlstyle import BOM, preserve_line_endings
from kd_rules_mcp.structures.queries import ObjectCard, ObjectProperty

FIXTURE = Path(__file__).parent / "data" / "registration" / "retarget.xml"
EXCHANGE = Path(__file__).parent / "data" / "exchange_rules.xml"
PLAN = "НовыйПлан"
MAPPING = {
    "ДатаНачала": "ДатаНовая",
    "[Организации]": "Фирмы",
    "[Организации].Организация": "Фирма",
    "ФлагПерсональных": "ФлагНовый",
    "ИНН": "Код",
    "НетТакогоРеквизита": "ТожеНет",
    "РежимВыгрузки": "РежимДругой",
}
_HANDLERS = (
    "ПередОбработкой",
    "ПриОбработке",
    "ПриОбработкеДополнительный",
    "ПослеОбработки",
)


def _load() -> RegistrationRules:
    return load_registration_rules(FIXTURE)


def _rule(metadata: str, plan_property: str, *, unload: str = "") -> str:
    mode = f"<РеквизитРежимаВыгрузки>{unload}</РеквизитРежимаВыгрузки>" if unload else ""
    return (
        '<Правило Отключить="false" Валидное="true">'
        "<Код>1</Код>"
        f"<ОбъектМетаданныхИмя>{metadata}</ОбъектМетаданныхИмя>"
        f"{mode}"
        "<ОтборПоСвойствамПланаОбмена><ЭлементОтбора>"
        f"<СвойствоПланаОбмена>{plan_property}</СвойствоПланаОбмена>"
        "</ЭлементОтбора></ОтборПоСвойствамПланаОбмена>"
        "</Правило>"
    )


def _tiny(body: str) -> RegistrationRules:
    text = (
        "<ПравилаРегистрации><ВерсияФормата>2.01</ВерсияФормата>"
        '<ПланОбмена Имя="СтарыйПлан">Старый</ПланОбмена>'
        f"<ПравилаРегистрацииОбъектов>{body}</ПравилаРегистрацииОбъектов>"
        "</ПравилаРегистрации>"
    )
    return load_registration_rules(text.encode())


def _card(*rows: tuple[str, str, bool]) -> ObjectCard:
    return ObjectCard(
        "ПланОбмена.НовыйПлан",
        "ПланОбменаСсылка.НовыйПлан",
        "ПланОбмена",
        tuple(ObjectProperty(path, kind, group, (), ()) for path, kind, group in rows),
    )


def _plan_leaf(rule: Node, index: int) -> Node:
    container = rule.child("ОтборПоСвойствамПланаОбмена")
    assert container is not None
    return container.items[index]


def _names(table: Node | None) -> list[str]:
    assert table is not None
    return [str(row.get("Наименование")) for row in table.items]


def test_bare_tabular_reference_is_invalid_even_without_card() -> None:
    result = retarget_registration(
        _tiny(_rule("Документ.Test", "[Rows]")), plan_name=PLAN, node_properties={}
    )
    assert result.remarks[0].reference == "[Rows]"
    assert "без поля" in result.remarks[0].message


def test_same_plan_keeps_header_text_and_does_not_report_plan_in_code() -> None:
    source = _tiny(_rule("Документ.Test", "Old"))
    plan = source.root.child("ПланОбмена")
    assert plan is not None
    plan.text = "ПланОбменаСсылка.СтарыйПлан"
    source.rules()[0].values["ПриОбработке"] = "Узел = ПланыОбмена.СтарыйПлан.НайтиПоКоду(Код);"
    result = retarget_registration(source, plan_name="СтарыйПлан", node_properties={"Old": "New"})
    target = result.document.root.child("ПланОбмена")
    assert target is not None
    assert target.attrs == plan.attrs and target.text == plan.text
    assert result.code_mentions == 0
    assert _plan_leaf(result.document.rules()[0], 0).get("СвойствоПланаОбмена") == "New"


def test_case_normalization_updates_linked_table_and_unload_mode() -> None:
    source = _load()
    metadata = _card(
        ("ДАТАНОВАЯ", "Реквизит", False),
        ("ФИРМЫ", "ТабличнаяЧасть", True),
        ("ФИРМЫ.ФИРМА", "Реквизит", False),
        ("РЕЖИМДРУГОЙ", "Реквизит", False),
    )
    before = dump_rules(source)
    result = retarget_registration(
        source, plan_name=PLAN, node_properties=MAPPING, target_plan=metadata
    )
    date = _plan_leaf(result.document.rules()[0], 0)
    assert date.get("СвойствоПланаОбмена") == "ДАТАНОВАЯ"
    assert _names(date.child("ТаблицаСвойствПланаОбмена")) == ["ДАТАНОВАЯ"]
    assert result.document.rules()[0].get("РеквизитРежимаВыгрузки") == "РЕЖИМДРУГОЙ"
    assert any(n.check == "registration.property_case" for n in result.notices)
    assert dump_rules(source) == before


def test_plan_name_changes_only_the_header_attribute() -> None:
    """Имя плана — атрибут `ПланОбмена`. Синоним и комментарий с тем же текстом остаются."""
    source = _load()
    before = dump_rules(source)
    result = retarget_registration(source, plan_name=PLAN, node_properties=MAPPING)
    plan = result.document.root.child("ПланОбмена")
    assert plan is not None
    assert plan.attrs["Имя"] == PLAN
    assert plan.text == "Старый план"
    assert source.exchange_plan == "СтарыйПлан"
    assert dump_rules(source) == before
    assert result.document.root is not source.root
    comment = result.document.rules()[0].get("Комментарий")
    assert comment == "не менять СтарыйПлан и ДатаНачала"
    assert result.only_expected is True


def test_renames_leaf_and_plan_property_table() -> None:
    result = retarget_registration(_load(), plan_name=PLAN, node_properties=MAPPING)
    income, expense = result.document.rules()
    date = _plan_leaf(income, 0)
    assert date.get("СвойствоПланаОбмена") == "ДатаНовая"
    assert date.get("СвойствоОбъекта") == "ДатаНачала"
    assert date.get("ВидСравнения") == "БольшеИлиРавно"
    plan_rows = date.child("ТаблицаСвойствПланаОбмена")
    assert _names(plan_rows) == ["ДатаНовая"]
    assert plan_rows is not None
    assert plan_rows.items[0].get("Тип") == "Дата"
    assert plan_rows.items[0].get("Вид") == "Реквизит"
    assert _names(date.child("ТаблицаСвойствОбъекта")) == ["ДатаНачала"]
    group = income.child("ОтборПоСвойствамПланаОбмена")
    assert group is not None
    nested = group.items[1]
    assert nested.tag == "Группа"
    tabular = nested.items[1]
    assert tabular.get("СвойствоПланаОбмена") == "[Фирмы].Фирма.ИНН"
    assert tabular.get("СвойствоОбъекта") == "Контрагент.ИНН"
    rows = tabular.child("ТаблицаСвойствПланаОбмена")
    assert rows is not None
    assert _names(rows) == ["[Фирмы]", "Фирма", "ИНН"]
    assert "Тип" not in rows.items[0].values
    assert rows.items[0].get("Вид") == "ТабличнаяЧасть"
    assert rows.items[1].get("Тип") == "СправочникСсылка.Контрагенты"
    assert rows.items[1].get("Вид") == "Реквизит"
    assert rows.items[2].get("Тип") == "Строка"
    assert _names(tabular.child("ТаблицаСвойствОбъекта")) == ["Контрагент", "ИНН"]
    flag = _plan_leaf(expense, 0)
    assert flag.get("СвойствоПланаОбмена") == "ФлагНовый"
    assert _names(flag.child("ТаблицаСвойствПланаОбмена")) == ["ФлагНовый"]
    assert [(item.renamed, item.untouched) for item in result.rules] == [(2, 3), (1, 0)]
    assert result.unused == ("ИНН", "НетТакогоРеквизита")
    assert result.code_mentions == 1
    assert result.mentions[0].name == "ДатаНачала"
    assert result.mentions[0].event.startswith("АлгоритмЗначения")


def test_or_group_order_and_disabled_rule_stay() -> None:
    source = _load()
    result = retarget_registration(source, plan_name=PLAN, node_properties=MAPPING)
    income = result.document.rules()[0]
    container = income.child("ОтборПоСвойствамПланаОбмена")
    assert container is not None
    assert [item.tag for item in container.items] == ["ЭлементОтбора", "Группа"]
    group = container.items[1]
    assert group.get("БулевоЗначениеГруппы") == "ИЛИ"
    assert [item.tag for item in group.items] == ["ЭлементОтбора", "ЭлементОтбора"]
    constant = group.items[0]
    assert constant.get("СвойствоПланаОбмена") == "ИспользоватьОтбор"
    assert constant.get("ЭтоСтрокаКонстанты") is True
    assert constant.get("ВидСравнения") == "Равно"
    obj = income.child("ОтборПоСвойствамОбъекта")
    assert obj is not None
    assert obj.items[1].tag == "Группа"
    assert obj.items[1].get("БулевоЗначениеГруппы") == "И"
    disabled = result.document.rules()[1]
    assert disabled.attrs.get("Отключить") is True
    assert source.rules()[1].attrs.get("Отключить") is True
    assert disabled.get("ОбъектМетаданныхИмя") == "Документ.Расход"


def test_handlers_algorithms_and_unknown_stay_byte_for_byte() -> None:
    source = _load()
    result = retarget_registration(source, plan_name=PLAN, node_properties=MAPPING)
    for old, new in zip(source.rules(), result.document.rules(), strict=True):
        for name in _HANDLERS:
            assert old.values.get(name) == new.values.get(name)
    algorithm = result.document.rules()[0].child("ОтборПоСвойствамОбъекта")
    assert algorithm is not None
    leaf = algorithm.items[1].items[0]
    assert leaf.get("Вид") == "АлгоритмЗначения"
    assert leaf.get("ЗначениеКонстанты") == "Значение = Перечисления.Виды.ДатаНачала;"
    assert leaf.get("СвойствоОбъекта") == "ВидОперации"
    unknown = result.document.rules()[0].unknown
    assert [element.tag for element in unknown] == ["СлужебнаяПометка"]
    assert unknown[0].text == "как было"
    assert result.document.rules()[0].get("РеквизитРежимаВыгрузки") == "РежимДругой"


def test_save_and_reload_keeps_the_model() -> None:
    result = retarget_registration(_load(), plan_name=PLAN, node_properties=MAPPING)
    reloaded = load_registration_rules(dump_rules(result.document))
    assert diff_rules(result.document, reloaded, include_header=True) == []
    assert reloaded.exchange_plan == PLAN


def test_line_endings_and_bom_follow_ordinary_save() -> None:
    """Неизменённые строки сохраняют концы, как `dump_rules` без отдельной правки."""
    canonical = dump_rules(load_registration_rules(FIXTURE))
    marker = "ПередОбработкой".encode()
    body = bytearray()
    for line in canonical.splitlines():
        ending = b"\n" if marker in line else b"\r\n"
        body += line + ending
    data = BOM + bytes(body)
    source = load_registration_rules(data)
    assert dump_rules(source) == data
    result = retarget_registration(source, plan_name=PLAN, node_properties=MAPPING)
    saved = dump_rules(result.document)
    assert saved.startswith(BOM)
    handler = next(line for line in saved.splitlines(keepends=True) if marker in line)
    assert handler.endswith(b"\n") and not handler.endswith(b"\r\n")
    origin = result.document.origin
    result.document.origin = None
    uniform = dump_rules(result.document)
    result.document.origin = origin
    assert saved == preserve_line_endings(data, uniform)


def test_simultaneous_chain_does_not_collapse() -> None:
    rules = _tiny(_rule("Документ.А", "Дата") + _rule("Документ.Б", "Флаг"))
    result = retarget_registration(
        rules, plan_name=PLAN, node_properties={"Дата": "Флаг", "Флаг": "ФлагНовый"}
    )
    first, second = result.document.rules()
    assert first.child("ОтборПоСвойствамПланаОбмена") is not None
    assert second.child("ОтборПоСвойствамПланаОбмена") is not None
    assert _plan_leaf(first, 0).get("СвойствоПланаОбмена") == "Флаг"
    assert _plan_leaf(second, 0).get("СвойствоПланаОбмена") == "ФлагНовый"


@pytest.mark.parametrize(
    ("plan", "mapping"),
    [
        ("", {}),
        ("1План", {}),
        ("План Обмена", {}),
        ("План.Имя", {}),
        (PLAN, {"": "Дата"}),
        (PLAN, {"Дата.Начала": "Дата"}),
        (PLAN, {"[Дата].": "Дата"}),
        (PLAN, {"Дата": "Новая Дата"}),
        (PLAN, {"Дата": ""}),
    ],
)
def test_empty_or_illegal_names_are_rejected(plan: str, mapping: dict[str, str]) -> None:
    rules = _tiny(_rule("Документ.А", "Дата"))
    with pytest.raises(InvalidRegistrationNameError):
        retarget_registration(rules, plan_name=plan, node_properties=mapping)


def test_mapping_must_be_a_dictionary() -> None:
    rules = _tiny(_rule("Документ.А", "Дата"))
    with pytest.raises(InvalidRegistrationNameError):
        retarget_registration(
            rules, plan_name=PLAN, node_properties=cast(Mapping[str, str], ["Дата"])
        )


def test_two_properties_cannot_share_a_target_name() -> None:
    rules = _tiny(_rule("Документ.А", "Дата") + _rule("Документ.Б", "Флаг"))
    with pytest.raises(DuplicateTargetPropertyError):
        retarget_registration(
            rules, plan_name=PLAN, node_properties={"Дата": "Новая", "Флаг": "Новая"}
        )


def test_target_name_already_used_by_another_node_property() -> None:
    rules = _tiny(_rule("Документ.А", "Дата") + _rule("Документ.Б", "Флаг"))
    with pytest.raises(PropertyNameClashError):
        retarget_registration(rules, plan_name=PLAN, node_properties={"Флаг": "Дата"})


@pytest.mark.parametrize("occupied", ["FlagB", "flagb", "FLAGB"])
@pytest.mark.parametrize("target", ["FlagB", "flagb"])
def test_case_insensitive_property_clash(occupied: str, target: str) -> None:
    rules = _tiny(_rule("Документ.A", "OldFlag") + _rule("Документ.B", occupied))
    with pytest.raises(PropertyNameClashError):
        retarget_registration(
            rules,
            plan_name=PLAN,
            node_properties={"OldFlag": target},
            target_plan=_card(("FlagB", "Реквизит", False)),
        )


@pytest.mark.parametrize("targets", [("FlagB", "FlagB"), ("FlagB", "flagb"), ("flagb", "FLAGB")])
def test_case_insensitive_duplicate_target(targets: tuple[str, str]) -> None:
    rules = _tiny(_rule("Документ.A", "OldA") + _rule("Документ.B", "OldB"))
    with pytest.raises(DuplicateTargetPropertyError):
        retarget_registration(
            rules,
            plan_name=PLAN,
            node_properties=dict(zip(("OldA", "OldB"), targets, strict=True)),
            target_plan=_card(("FlagB", "Реквизит", False)),
        )


@pytest.mark.parametrize("target", ["DateA", "FlagB", "flAGb"])
def test_absent_mapping_key_does_not_participate_in_collisions(target: str) -> None:
    rules = _tiny(_rule("Документ.A", "OldFlag") + _rule("Документ.B", "DateA"))
    result = retarget_registration(
        rules, plan_name=PLAN, node_properties={"OldFlag": "FlagB", "OldDate": target}
    )
    assert result.unused == ("OldDate",)
    assert _plan_leaf(result.document.rules()[0], 0).get("СвойствоПланаОбмена") == "FlagB"


@pytest.mark.parametrize("overlap", [True, False])
def test_composite_object_property_type_needs_intersection(overlap: bool) -> None:
    rules = _tiny(_rule("Документ.A", "RefA"))
    _plan_leaf(rules.rules()[0], 0).values.update(
        ЭтоСтрокаКонстанты=False,
        ТипСвойстваОбъекта="СправочникСсылка.Items, СправочникСсылка.Other",
        СвойствоОбъекта="Item",
    )
    card = ObjectCard(
        "ПланОбмена.НовыйПлан",
        "ПланОбменаСсылка.НовыйПлан",
        "ПланОбмена",
        (
            ObjectProperty(
                "RefA",
                "Реквизит",
                False,
                ("СправочникСсылка.Items" if overlap else "СправочникСсылка.Third",),
                (),
            ),
        ),
    )
    result = retarget_registration(rules, plan_name=PLAN, node_properties={}, target_plan=card)
    assert any(n.blocking for n in result.notices) is not overlap


def test_case_insensitive_mapping_renames_header_table_and_unload_mode() -> None:
    rules = _tiny(
        _rule("Документ.A", "OldFlag", unload="OldMode") + _rule("Документ.B", "[OldRows].OldField")
    )
    result = retarget_registration(
        rules,
        plan_name=PLAN,
        node_properties={
            "OLDFLAG": "FlagB",
            "oldmode": "ModeB",
            "[oldrows]": "RowsB",
            "[OLDROWS].oldfield": "FieldB",
        },
        target_plan=_card(
            ("FlagB", "Реквизит", False),
            ("ModeB", "Реквизит", False),
            ("RowsB", "ТабличнаяЧасть", True),
            ("RowsB.FieldB", "Реквизит", False),
        ),
    )
    assert result.unused == ()
    first, second = result.document.rules()
    assert _plan_leaf(first, 0).get("СвойствоПланаОбмена") == "FlagB"
    assert _plan_leaf(second, 0).get("СвойствоПланаОбмена") == "[RowsB].FieldB"
    assert first.get("РеквизитРежимаВыгрузки") == "ModeB"


@pytest.mark.parametrize("occupied", ["[Rows].fieldb", "Other"])
def test_case_insensitive_clashes_include_table_fields_and_unload_mode(occupied: str) -> None:
    rules = _tiny(
        _rule("Документ.A", "[Rows].OldField", unload="modeb") + _rule("Документ.B", occupied)
    )
    mapping = {"[rows].oldfield": "FieldB"} if occupied.startswith("[") else {"other": "ModeB"}
    with pytest.raises(PropertyNameClashError):
        retarget_registration(rules, plan_name=PLAN, node_properties=mapping)


def test_case_insensitive_mapping_still_reports_old_name_in_handler() -> None:
    rules = _tiny(_rule("Документ.A", "OldFlag"))
    rules.rules()[0].values["ПередОбработкой"] = "Узел.OldFlag = Истина;"
    result = retarget_registration(rules, plan_name=PLAN, node_properties={"OLDFLAG": "FlagB"})
    assert result.unused == ()
    assert result.code_mentions == 1
    assert result.mentions[0].name == "OLDFLAG"
    assert result.document.rules()[0].get("ПередОбработкой") == "Узел.OldFlag = Истина;"


def test_unload_mode_name_stays_occupied() -> None:
    rules = _tiny(_rule("Документ.А", "Флаг", unload="РежимВыгрузки"))
    with pytest.raises(PropertyNameClashError):
        retarget_registration(rules, plan_name=PLAN, node_properties={"Флаг": "РежимВыгрузки"})


def test_exchange_rules_are_rejected() -> None:
    rules = load_exchange_rules(EXCHANGE)
    with pytest.raises(NotRegistrationRulesError):
        retarget_registration(rules, plan_name=PLAN, node_properties={})


def test_target_plan_matches_attribute_and_tabular_section() -> None:
    present = _card(
        ("ДатаНовая", "Реквизит", False),
        ("Организации", "ТабличнаяЧасть", True),
        ("Организации.Организация", "Реквизит", False),
    )
    dated = _tiny(_rule("Документ.А", "ДатаНачала"))
    assert (
        retarget_registration(
            dated,
            plan_name=PLAN,
            node_properties={"ДатаНачала": "ДатаНовая"},
            target_plan=present,
        ).remarks
        == ()
    )
    tabular = _tiny(_rule("Документ.А", "[Организации].Организация.ИНН"))
    kept = retarget_registration(tabular, plan_name=PLAN, node_properties={}, target_plan=present)
    assert kept.remarks == ()
    missing = retarget_registration(
        dated, plan_name=PLAN, node_properties={"ДатаНачала": "НетДаты"}, target_plan=present
    )
    assert len(missing.remarks) == 1
    assert missing.remarks[0].property_name == "НетДаты"
    assert missing.remarks[0].address == "ПРО «Документ.А»"
    assert missing.remarks[0].leaf == "ОтборПоСвойствамПланаОбмена/0"
    no_section = retarget_registration(
        _tiny(_rule("Документ.А", "[НетТакой].Поле")),
        plan_name=PLAN,
        node_properties={},
        target_plan=present,
    )
    assert no_section.remarks[0].property_name == "НетТакой"
    no_field = retarget_registration(
        _tiny(_rule("Документ.А", "[Организации].НетПоля")),
        plan_name=PLAN,
        node_properties={},
        target_plan=present,
    )
    assert no_field.remarks[0].property_name == "[Организации].НетПоля"


def test_flat_key_does_not_rename_tabular_attribute() -> None:
    rules = _tiny(
        _rule("Документ.А", "Организация") + _rule("Документ.Б", "[Организации].Организация")
    )
    result = retarget_registration(
        rules, plan_name=PLAN, node_properties={"Организация": "ОсновнаяОрганизация"}
    )
    first, second = result.document.rules()
    assert _plan_leaf(first, 0).get("СвойствоПланаОбмена") == "ОсновнаяОрганизация"
    assert _plan_leaf(second, 0).get("СвойствоПланаОбмена") == "[Организации].Организация"
    section = retarget_registration(
        rules,
        plan_name=PLAN,
        node_properties={
            "[Организации]": "Фирмы",
            "[Организации].Организация": "Фирма",
        },
    )
    renamed = section.document.rules()[1]
    assert _plan_leaf(renamed, 0).get("СвойствоПланаОбмена") == "[Фирмы].Фирма"
    assert _plan_leaf(section.document.rules()[0], 0).get("СвойствоПланаОбмена") == "Организация"


def test_handler_and_algorithm_mentions_are_not_rewritten() -> None:
    handler = (
        "<ПередОбработкой>// ДатаНачала в комментарии\n"
        'Текст = "ВЫБРАТЬ Ссылка ИЗ ПланОбмена.СтарыйПлан КАК П '
        'ГДЕ П.ДатаНачала &lt;= &amp;Д";</ПередОбработкой>'
        "<ПриОбработке>Узел.ДатаНачалаДополнительно = 1;</ПриОбработке>"
        "<ПослеОбработки>М = ПланыОбмена.СтарыйПлан.ЭтотУзел();</ПослеОбработки>"
    )
    algorithm = (
        "<ЭлементОтбора><Вид>АлгоритмЗначения</Вид>"
        "<ЗначениеКонстанты>Если Узел.РежимСтарый Тогда Отказ = Истина;</ЗначениеКонстанты>"
        "</ЭлементОтбора>"
    )
    body = (
        '<Правило Отключить="false" Валидное="true"><Код>1</Код>'
        "<ОбъектМетаданныхИмя>Справочник.Х</ОбъектМетаданныхИмя>"
        "<РеквизитРежимаВыгрузки>РежимСтарый</РеквизитРежимаВыгрузки>"
        f"<ОтборПоСвойствамОбъекта>{algorithm}</ОтборПоСвойствамОбъекта>"
        f"{handler}</Правило>"
    )
    source = _tiny(body)
    before = source.rules()[0].get("ПередОбработкой")
    result = retarget_registration(
        source,
        plan_name=PLAN,
        node_properties={"ДатаНачала": "ДатаНовая", "РежимСтарый": "РежимНовый"},
    )
    rule = result.document.rules()[0]
    assert rule.get("ПередОбработкой") == before
    assert rule.get("РеквизитРежимаВыгрузки") == "РежимНовый"
    assert result.unused == ()
    assert result.code_mentions == 4
    found = {(item.event, item.line, item.name) for item in result.mentions}
    assert ("ПередОбработкой", 1, "ДатаНачала") not in found
    assert ("ПередОбработкой", 2, "СтарыйПлан") in found
    assert ("ПередОбработкой", 2, "ДатаНачала") in found
    assert ("ПослеОбработки", 1, "СтарыйПлан") in found
    assert found & {("АлгоритмЗначения ОтборПоСвойствамОбъекта/0", 1, "РежимСтарый")}
    assert all(item.name != "ДатаНачалаДополнительно" for item in result.mentions)
    assert all("комментарии" not in item.message for item in result.mentions)


def test_unload_mode_is_checked_against_the_target_plan() -> None:
    present = _card(("РежимНовый", "Реквизит", False), ("Дата", "Реквизит", False))
    rules = _tiny(_rule("Документ.А", "Дата", unload="РежимСтарый"))
    renamed = retarget_registration(
        rules,
        plan_name=PLAN,
        node_properties={"РежимСтарый": "РежимНовый"},
        target_plan=present,
    )
    assert renamed.document.rules()[0].get("РеквизитРежимаВыгрузки") == "РежимНовый"
    assert renamed.remarks == ()
    assert renamed.unused == ()
    missing = retarget_registration(
        rules,
        plan_name=PLAN,
        node_properties={},
        target_plan=_card(("Дата", "Реквизит", False)),
    )
    assert len(missing.remarks) == 1
    assert missing.remarks[0].leaf == "РеквизитРежимаВыгрузки"
    assert missing.remarks[0].property_name == "РежимСтарый"
    assert missing.remarks[0].address == "ПРО «Документ.А»"


def test_exchange_plan_type_text_becomes_the_new_plan_name() -> None:
    text = (
        "<ПравилаРегистрации><ВерсияФормата>2.01</ВерсияФормата>"
        '<ПланОбмена Имя="СтарыйПлан">ПланОбменаСсылка.СтарыйПлан</ПланОбмена>'
        "<ПравилаРегистрацииОбъектов></ПравилаРегистрацииОбъектов>"
        "</ПравилаРегистрации>"
    )
    result = retarget_registration(
        load_registration_rules(text.encode()), plan_name=PLAN, node_properties={}
    )
    plan = result.document.root.child("ПланОбмена")
    assert plan is not None
    assert plan.attrs["Имя"] == PLAN
    assert plan.text == PLAN


def test_target_plan_must_be_an_exchange_plan() -> None:
    card = ObjectCard("Справочник.Х", "СправочникСсылка.Х", "Справочник", ())
    rules = _tiny(_rule("Документ.А", "Дата"))
    with pytest.raises(RegistrationRetargetError):
        retarget_registration(rules, plan_name=PLAN, node_properties={}, target_plan=card)


@pytest.mark.parametrize(
    "kind",
    [
        "Справочник",
        "Документ",
        "ПланВидовХарактеристик",
        "ПланСчетов",
        "ПланВидовРасчета",
        "БизнесПроцесс",
        "Задача",
    ],
)
def test_deletion_filter_uses_standard_property_not_object_kind(kind: str) -> None:
    rules = _tiny(_rule(f"{kind}.Sample", "DateA"))
    card = ObjectCard(
        f"{kind}.Sample",
        f"{kind}Ссылка.Sample",
        kind,
        (ObjectProperty("ПометкаУдаления", "Свойство", False, ("Булево",), ()),),
    )
    result = retarget_registration(
        rules,
        plan_name=PLAN,
        node_properties={},
        deletion_mark_filter=True,
        target_objects=(card,),
    )
    filters = result.document.rules()[0].child("ОтборПоСвойствамОбъекта")
    assert filters is not None and len(filters.items) == 1
    assert filters.items[0].values == {
        "ТипСвойстваОбъекта": "Булево",
        "ВидСравнения": "Равно",
        "СвойствоОбъекта": "ПометкаУдаления",
        "Вид": "ЗначениеКонстанты",
        "ЗначениеКонстанты": "false",
    }
    row = filters.items[0].child("ТаблицаСвойствОбъекта")
    assert row is not None and row.items[0].get("Вид") == "Свойство"


def test_deletion_filter_preserves_filters_handlers_disabled_and_repeats() -> None:
    rules = _tiny(
        _rule("Справочник.Sample", "DateA")
        + _rule("Справочник.Sample", "DateA")
        + _rule("РегистрСведений.Other", "DateA")
        + _rule("Справочник.Disabled", "DateA")
    )
    first, existing, _, disabled = rules.rules()
    disabled.attrs["Отключить"] = True
    tree = Node.new("object_filter", "ОтборПоСвойствамОбъекта")
    group = Node.new("object_filter_group", "Группа")
    group.values["БулевоЗначениеГруппы"] = "ИЛИ"
    leaf = Node.new("object_filter_item", "ЭлементОтбора")
    leaf.values.update(СвойствоОбъекта="Code", ВидСравнения="Равно", ЗначениеКонстанты="1")
    group.items.append(leaf)
    tree.items.append(group)
    first.children[tree.tag] = tree
    for event in ("ПередОбработкой", "ПриОбработке", "ПослеОбработки"):
        first.values[event] = "Отказ = Ложь;"
    existing_tree = Node.new("object_filter", "ОтборПоСвойствамОбъекта")
    existing_leaf = Node.new("object_filter_item", "ЭлементОтбора")
    existing_leaf.values.update(
        СвойствоОбъекта="ПометкаУдаления", ВидСравнения="Равно", ЗначениеКонстанты="true"
    )
    existing_tree.items.append(existing_leaf)
    existing.children[existing_tree.tag] = existing_tree
    source = dump_rules(rules)
    card = ObjectCard(
        "Справочник.Sample",
        "СправочникСсылка.Sample",
        "Справочник",
        (ObjectProperty("ПометкаУдаления", "Свойство", False, ("Булево",), ()),),
    )
    other = ObjectCard(
        "РегистрСведений.Other", "РегистрСведенийЗапись.Other", "РегистрСведений", ()
    )
    result = retarget_registration(
        rules,
        plan_name=PLAN,
        node_properties={},
        deletion_mark_filter=True,
        target_objects=(card, other),
    )
    assert [s for _, s in result.deletion_filters] == [
        "added",
        "existing_filter",
        "no_deletion_mark",
        "disabled",
    ]
    assert dump_rules(rules) == source
    restored_first, restored_existing, *_ = result.document.rules()
    filters = restored_first.child("ОтборПоСвойствамОбъекта")
    assert filters is not None and len(filters.items) == 2
    assert filters.items[0].values == group.values
    assert filters.items[0].items[0].values == leaf.values
    restored_tree = restored_existing.child("ОтборПоСвойствамОбъекта")
    assert restored_tree is not None
    assert restored_tree.items[0].values == existing_leaf.values
    for event in ("ПередОбработкой", "ПриОбработке", "ПослеОбработки"):
        assert restored_first.get(event) == first.get(event)
    checks = {n.check for n in result.notices if n.requires_acknowledgement}
    assert {
        "registration.deletion_handler",
        "registration.deletion_partial",
        "registration.deletion_existing",
    } <= checks
    assert all(n.address for n in result.notices)
    repeated = retarget_registration(
        load_registration_rules(dump_rules(result.document)),
        plan_name=PLAN,
        node_properties={},
        deletion_mark_filter=True,
        target_objects=(card, other),
    )
    assert dump_rules(repeated.document) == dump_rules(result.document)
    assert not any(status == "added" for _, status in repeated.deletion_filters)


def test_deletion_filter_reports_unknown_object_and_unchecked_unload_mode() -> None:
    rules = _tiny(
        _rule("Справочник.Sample", "DateA", unload="ModeA") + _rule("Документ.Absent", "DateA")
    )
    card = ObjectCard(
        "Справочник.Sample",
        "СправочникСсылка.Sample",
        "Справочник",
        (ObjectProperty("ПометкаУдаления", "Свойство", False, ("Булево",), ()),),
    )
    result = retarget_registration(
        rules,
        plan_name=PLAN,
        node_properties={},
        deletion_mark_filter=True,
        target_objects=(card,),
    )
    assert [status for _, status in result.deletion_filters] == ["added", "object_unchecked"]
    checks = {n.check for n in result.notices if n.requires_acknowledgement}
    assert {"registration.deletion_mode", "registration.deletion_unchecked"} <= checks
    assert result.document.rules()[0].get("РеквизитРежимаВыгрузки") == "ModeA"


def test_deletion_filter_requires_ack_for_algorithm_that_can_change_rule() -> None:
    rules = _tiny(_rule("Справочник.Sample", "DateA"))
    rule = rules.rules()[0]
    filters = Node.new("object_filter", "ОтборПоСвойствамОбъекта")
    leaf = Node.new("object_filter_item", "ЭлементОтбора")
    code = "ПРО.ПравилоПоСвойствамОбъектаПустое = Истина; Значение = Ложь;"
    leaf.values.update(СвойствоОбъекта="OtherFlag", Вид="АлгоритмЗначения", ЗначениеКонстанты=code)
    filters.items.append(leaf)
    rule.children[filters.tag] = filters
    card = ObjectCard(
        "Справочник.Sample",
        "СправочникСсылка.Sample",
        "Справочник",
        (ObjectProperty("ПометкаУдаления", "Свойство", False, ("Булево",), ()),),
    )
    result = retarget_registration(
        rules,
        plan_name=PLAN,
        node_properties={},
        deletion_mark_filter=True,
        target_objects=(card,),
    )
    warning = next(n for n in result.notices if n.check == "registration.deletion_handler")
    assert warning.requires_acknowledgement and warning.address
    assert "АлгоритмЗначения" in warning.reference
    restored = result.document.rules()[0].child(filters.tag)
    assert restored is not None and restored.items[0].get("ЗначениеКонстанты") == code


def test_review_missing_valid_attribute_skips_rule() -> None:
    rules = _tiny(
        (_rule("Справочник.Multi", "DateA") + _rule("Справочник.Marked", "DateA")).replace(
            ' Валидное="true"', ""
        )
    )
    objects = tuple(
        ObjectCard(
            f"Справочник.{name}",
            f"СправочникСсылка.{name}",
            "Справочник",
            (ObjectProperty("ПометкаУдаления", "Свойство", False, ("Булево",), ()),),
        )
        for name in ("Multi", "Marked")
    )
    plan = ObjectCard(
        f"ПланОбмена.{PLAN}",
        f"ПланОбменаСсылка.{PLAN}",
        "ПланОбмена",
        tuple(
            ObjectProperty(card.name, "ЭлементСоставаПланаОбмена", False, (card.type_name,), ())
            for card in objects
        ),
    )
    result = retarget_registration(
        rules,
        plan_name=PLAN,
        node_properties={},
        target_plan=plan,
        deletion_mark_filter=True,
        target_objects=objects,
    )
    assert [status for _, status in result.deletion_filters] == ["invalid", "invalid"]
    assert all(r.child("ОтборПоСвойствамОбъекта") is None for r in result.document.rules())
    missing = [n for n in result.notices if n.check == "registration.deletion_missing_rule"]
    assert {n.reference for n in missing} == {card.name for card in objects}
    assert all(n.requires_acknowledgement for n in missing)


def test_review_on_demand_mode_explained_and_counted() -> None:
    from kd_rules_mcp.authoring.ed.registration_delivery import deletion_mark_instruction
    from kd_rules_mcp.authoring.registration_retarget import deletion_filter_summary

    rules = _tiny(_rule("Справочник.Sample", "DateA", unload="РежимВыгрузкиПриНеобходимости"))
    card = ObjectCard(
        "Справочник.Sample",
        "СправочникСсылка.Sample",
        "Справочник",
        (ObjectProperty("ПометкаУдаления", "Свойство", False, ("Булево",), ()),),
    )
    result = retarget_registration(
        rules, plan_name=PLAN, node_properties={}, deletion_mark_filter=True, target_objects=(card,)
    )
    notice = next(n for n in result.notices if n.check == "registration.deletion_mode")
    assert notice.requires_acknowledgement and notice.address
    for text in (
        "При необходимости",
        "По условию",
        "Выгружать всегда",
        "Вручную",
        "Не выгружать",
        "перезаписать",
        "снятие пометки",
    ):
        assert text in notice.message
    assert deletion_filter_summary(result)["mode_rules"] == 1
    instruction = deletion_mark_instruction(result)
    assert notice.address in instruction and notice.reference in instruction
    assert "При необходимости" in instruction and "перезапишите" in instruction
    exception = "не отправляются, кроме узлов с режимом «Выгружать всегда»"
    assert exception in notice.message
    assert instruction.count(exception) == 2


@pytest.mark.parametrize(
    "decision,expected,replaced,removed",
    [
        (None, [], 0, 1),
        ({"name": "SendBank", "value": True}, [("SendBank", "true")], 1, 0),
        (
            [
                {"name": "SendStatements", "value": True},
                {"name": "StatementsAsRequests", "value": False},
            ],
            [("SendStatements", "true"), ("StatementsAsRequests", "false")],
            1,
            0,
        ),
    ],
)
def test_review_boolean_filter_decisions(
    decision: object, expected: list, replaced: int, removed: int
) -> None:
    source = _tiny(_rule("Документ.Bank", "OldPersonal"))
    leaf = _plan_leaf(source.rules()[0], 0)
    leaf.values.update(
        ЭтоСтрокаКонстанты=True,
        ТипСвойстваОбъекта="Булево",
        СвойствоОбъекта="false",
        ВидСравнения="Равно",
    )
    card = ObjectCard(
        f"ПланОбмена.{PLAN}",
        f"ПланОбменаСсылка.{PLAN}",
        "ПланОбмена",
        tuple(
            ObjectProperty(name, "Реквизит", False, ("Булево",), ())
            for name in ("SendBank", "SendStatements", "StatementsAsRequests")
        ),
    )
    before = dump_rules(source)
    result = retarget_registration(
        source, plan_name=PLAN, node_properties={"OldPersonal": decision}, target_plan=card
    )
    tree = result.document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert tree is not None
    assert [
        (n.get("СвойствоПланаОбмена"), n.get("СвойствоОбъекта")) for n in tree.items
    ] == expected
    assert result.rules[0].replaced == replaced and result.rules[0].removed == removed
    assert len(result.changes) == 1 and not result.unused
    assert dump_rules(source) == before
    assert dump_rules(load_registration_rules(dump_rules(result.document))) == dump_rules(
        result.document
    )


def test_review_array_is_and_inside_or_group() -> None:
    source = _tiny(_rule("Документ.Bank", "OldPersonal"))
    tree = source.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert tree is not None
    group = Node.new("plan_filter_group", "Группа")
    group.values["БулевоЗначениеГруппы"] = "ИЛИ"
    group.items.extend(tree.items)
    tree.items[:] = [group]
    leaf = group.items[0]
    leaf.values.update(
        ЭтоСтрокаКонстанты=True, ТипСвойстваОбъекта="Булево", СвойствоОбъекта="false"
    )
    card = ObjectCard(
        f"ПланОбмена.{PLAN}",
        f"ПланОбменаСсылка.{PLAN}",
        "ПланОбмена",
        tuple(
            ObjectProperty(name, "Реквизит", False, ("Булево",), ())
            for name in ("SendStatements", "StatementsAsRequests")
        ),
    )
    result = retarget_registration(
        source,
        plan_name=PLAN,
        node_properties={
            "OldPersonal": [
                {"name": "SendStatements", "value": True},
                {"name": "StatementsAsRequests", "value": False},
            ]
        },
        target_plan=card,
    )
    root = result.document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert root is not None
    outer = root.items[0]
    assert outer.get("БулевоЗначениеГруппы") == "ИЛИ"
    inner = outer.items[0]
    assert inner.tag == "Группа" and inner.get("БулевоЗначениеГруппы") == "И"
    assert len(inner.items) == 2


@pytest.mark.parametrize("remove_second", [False, True])
def test_review_actions_and_renames_are_simultaneous(remove_second: bool) -> None:
    source = _tiny(_rule("Документ.Bank", "OldA") + _rule("Документ.Other", "FlagB"))
    for rule in source.rules():
        _plan_leaf(rule, 0).values.update(
            ЭтоСтрокаКонстанты=True, ТипСвойстваОбъекта="Булево", СвойствоОбъекта="false"
        )
    card = ObjectCard(
        f"ПланОбмена.{PLAN}",
        f"ПланОбменаСсылка.{PLAN}",
        "ПланОбмена",
        tuple(
            ObjectProperty(name, "Реквизит", False, ("Булево",), ()) for name in ("FlagB", "FlagC")
        ),
    )
    decisions = (
        {"OldA": "FlagB", "FlagB": None}
        if remove_second
        else {"OldA": {"name": "FlagB", "value": True}, "FlagB": "FlagC"}
    )
    result = retarget_registration(
        source, plan_name=PLAN, node_properties=decisions, target_plan=card
    )
    assert _plan_leaf(result.document.rules()[0], 0).get("СвойствоПланаОбмена") == "FlagB"
    second = result.document.rules()[1].child("ОтборПоСвойствамПланаОбмена")
    assert second is not None
    if remove_second:
        assert second.items == []
    else:
        assert second.items[0].get("СвойствоПланаОбмена") == "FlagC"


@pytest.mark.parametrize("source_name,occupied", [("OldA", "FlagB"), ("A", "Z"), ("z", "a")])
@pytest.mark.parametrize("also_mapped", [False, True])
def test_review_boolean_decision_cannot_merge_attributes(
    source_name: str, occupied: str, also_mapped: bool
) -> None:
    source = _tiny(_rule("Документ.A", source_name) + _rule("Документ.B", occupied))
    target = "Target" if also_mapped else occupied
    decisions: dict[str, object] = {source_name: {"name": target.upper(), "value": True}}
    if also_mapped:
        decisions[occupied] = target.lower()
    error = DuplicateTargetPropertyError if also_mapped else PropertyNameClashError
    with pytest.raises(error):
        retarget_registration(source, plan_name=PLAN, node_properties=decisions)
