"""Черновик правил корреспондента (спецификация `rules-authoring`)."""

from pathlib import Path

from kd_rules_mcp.authoring.correspondent import DRAFT_COMMENT, MANUAL, mirror_rules
from kd_rules_mcp.kd2.canonical import canonical_form
from kd_rules_mcp.kd2.model import ExchangeRules, Node
from kd_rules_mcp.kd2.rules_io import dump_rules, load_exchange_rules
from kd_rules_mcp.validation.address import walk_pks
from kd_rules_mcp.validation.format import check_format
from tests.sqlite_structure import StructureBuilder

_HEAD = (
    "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата><Ид>1</Ид><Наименование>БП --&gt; ЗУП"
    "</Наименование>"
    '<Источник ВерсияКонфигурации="3.0" СинонимКонфигурации="БП">БП</Источник>'
    '<Приемник ВерсияКонфигурации="3.1" СинонимКонфигурации="ЗУП">ЗУП</Приемник>'
    "<ПередВыгрузкойДанных>Отказ = Ложь;</ПередВыгрузкойДанных>"
)


def _rules(body: str) -> ExchangeRules:
    return load_exchange_rules(f"{_HEAD}{body}</ПравилаОбмена>".encode())


def _side(tag: str, name: str, kind: str = "Реквизит", type_name: str = "") -> str:
    type_attr = f' Тип="{type_name}"' if type_name else ""
    return f'<{tag} Имя="{name}" Вид="{kind}"{type_attr}/>'


_DOCUMENT = (
    "<ПравилаКонвертацииОбъектов>"
    "<Правило><Код>Ведомость</Код>"
    "<Источник>ДокументСсылка.ВедомостьБП</Источник><Приемник>ДокументСсылка.ВедомостьЗУП</Приемник>"
    "<ПриВыгрузке>Приемник.Сумма = Источник.Итог;</ПриВыгрузке>"
    "<Свойства>"
    '<Свойство Поиск="true"><Код>1</Код>'
    + _side("Источник", "Номер", "Свойство", "Строка")
    + _side("Приемник", "НомерДок", "Свойство", "Строка")
    + "</Свойство>"
    "<Свойство><Код>2</Код>"
    + _side("Источник", "Организация", type_name="СправочникСсылка.Организации")
    + _side("Приемник", "ОрганизацияЗУП", type_name="СправочникСсылка.ОрганизацииЗУП")
    + "<КодПравилаКонвертации>Организации</КодПравилаКонвертации>"
    "<ПередВыгрузкой>Отказ = Ложь;</ПередВыгрузкой>"
    "</Свойство>"
    "<Свойство><Код>3</Код>"
    + _side("Источник", "Склад", type_name="СправочникСсылка.Склады")
    + _side("Приемник", "Склад", type_name="СправочникСсылка.Склады")
    + "<КодПравилаКонвертации>Склады</КодПравилаКонвертации>"
    "</Свойство>"
    "</Свойства>"
    "</Правило>"
    "<Правило><Код>Организации</Код>"
    "<Источник>СправочникСсылка.Организации</Источник>"
    "<Приемник>СправочникСсылка.ОрганизацииЗУП</Приемник>"
    "</Правило>"
    "<Правило><Код>Склады</Код>"
    "<Источник>СправочникСсылка.Склады</Источник><Приемник>СправочникСсылка.Склады</Приемник>"
    "</Правило>"
    "<Правило><Код>Виды</Код>"
    "<Источник>ПеречислениеСсылка.ВидыБП</Источник><Приемник>ПеречислениеСсылка.ВидыЗУП</Приемник>"
    "<Значения><Значение><Код>1</Код><Источник>Первый</Источник><Приемник>ПервыйЗУП</Приемник>"
    "</Значение></Значения>"
    "</Правило>"
    "</ПравилаКонвертацииОбъектов>"
    "<ПравилаВыгрузкиДанных><Правило><Код>В</Код><КодПравилаКонвертации>Ведомость"
    "</КодПравилаКонвертации></Правило></ПравилаВыгрузкиДанных>"
)


def _pko(rules: ExchangeRules, code: str) -> Node:
    return next(pko for pko in rules.pko() if pko.code == code)


def _sides(node: Node) -> tuple[str, str]:
    source, target = node.child("Источник"), node.child("Приемник")
    return (
        str(source.attrs.get("Имя", "")) if source is not None else "",
        str(target.attrs.get("Имя", "")) if target is not None else "",
    )


def _properties(pko: Node) -> dict[str, Node]:
    container = pko.child("Свойства")
    assert container is not None
    return {path: node for path, node in walk_pks(container)}


def test_pko_with_handler_is_mirrored_without_it() -> None:
    result = mirror_rules(_rules(_DOCUMENT), ["Ведомость", "Организации"])
    pko = _pko(result.rules, "Ведомость")
    assert pko.values.get("Источник", "") == "ДокументСсылка.ВедомостьЗУП"
    assert pko.values.get("Приемник", "") == "ДокументСсылка.ВедомостьБП"
    assert "ПриВыгрузке" not in pko.values
    handlers = {(item.address, item.event): item for item in result.handlers}
    manual = handlers[("ПКО «Ведомость»", "ПриВыгрузке")]
    assert manual.code == "Приемник.Сумма = Источник.Итог;"
    assert manual.note == MANUAL
    assert ("ПКО «Ведомость» / ПКС Организация", "ПередВыгрузкой") in handlers
    assert ("Конвертация", "ПередВыгрузкойДанных") in handlers
    assert result.draft


def test_sides_types_values_and_search_flag_are_swapped() -> None:
    result = mirror_rules(_rules(_DOCUMENT), ["Ведомость", "Организации", "Склады", "Виды"])
    properties = _properties(_pko(result.rules, "Ведомость"))
    number = properties["Номер"]
    assert _sides(number) == ("НомерДок", "Номер")
    assert number.attrs.get("Поиск") is True
    organization = properties["Организация"]
    target = organization.child("Приемник")
    assert target is not None and target.attrs["Тип"] == "СправочникСсылка.Организации"
    value = next(iter(_pko(result.rules, "Виды").child("Значения").walk()))  # type: ignore[union-attr]
    assert (value.values["Источник"], value.values["Приемник"]) == ("ПервыйЗУП", "Первый")


def test_header_is_swapped_and_marked_as_draft() -> None:
    result = mirror_rules(_rules(_DOCUMENT), ["Ведомость"])
    rules = result.rules
    assert rules.source_name == "ЗУП" and rules.target_name == "БП"
    assert rules.config("Источник")[1]["СинонимКонфигурации"] == "ЗУП"
    assert rules.root.values["Комментарий"] == DRAFT_COMMENT
    assert rules.root.values["Наименование"] == "ЗУП --> БП"
    assert rules.root.values["Ид"] != "1"
    assert "ПравилаВыгрузкиДанных" not in rules.root.children
    assert "ПередВыгрузкойДанных" not in rules.root.values


def test_reference_outside_selection_is_cleared() -> None:
    result = mirror_rules(_rules(_DOCUMENT), ["Ведомость", "Организации"])
    properties = _properties(_pko(result.rules, "Ведомость"))
    assert properties["Организация"].values["КодПравилаКонвертации"] == "Организации"
    assert "КодПравилаКонвертации" not in properties["Склад"].values
    assert any("Склады" in note for note in result.notes)
    assert check_format(result.rules).errors == []


def test_unknown_codes_are_listed() -> None:
    result = mirror_rules(_rules(_DOCUMENT), ["Ведомость", "Нет"])
    assert result.missing == ["Нет"]
    assert [pko.code for pko in result.rules.pko()] == ["Ведомость"]


def test_type_without_pair_disables_pks(tmp_path: Path) -> None:
    structure = StructureBuilder(tmp_path / "bp.sqlite")
    structure.add(
        "Документ",
        "ВедомостьБП",
        [
            ("Свойство", "Номер", "", "Строка", []),
            ("Реквизит", "Организация", "", "СправочникСсылка.Организации", []),
        ],
    )
    # Справочника Организации в структуре нет, свойства Склад у документа нет.
    result = mirror_rules(_rules(_DOCUMENT), ["Ведомость"], structure.conn)
    disabled = {item.address: item.reason for item in result.disabled}
    assert set(disabled) == {"ПКО «Ведомость» / ПКС Организация", "ПКО «Ведомость» / ПКС Склад"}
    assert "СправочникСсылка.Организации" in disabled["ПКО «Ведомость» / ПКС Организация"]
    properties = _properties(_pko(result.rules, "Ведомость"))
    assert properties["Организация"].attrs.get("Отключить") is True
    assert properties["Номер"].attrs.get("Отключить") is not True
    assert not any("не проверены" in note for note in result.notes)


def test_draft_round_trips_and_source_is_untouched() -> None:
    rules = _rules(_DOCUMENT)
    before = canonical_form(dump_rules(rules))
    result = mirror_rules(rules, ["Ведомость", "Организации", "Склады", "Виды"])
    assert canonical_form(dump_rules(rules)) == before
    data = dump_rules(result.rules)
    assert canonical_form(dump_rules(load_exchange_rules(data))) == canonical_form(data)
    assert check_format(load_exchange_rules(data)).errors == []
