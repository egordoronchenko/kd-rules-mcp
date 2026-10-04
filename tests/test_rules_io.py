"""Импорт и экспорт правил КД 2 на ручных примерах (сценарии `exchange-rules-format`)."""

from pathlib import Path

import pytest

from kd2_rules_mcp.errors import RulesFormatError
from kd2_rules_mcp.kd2.canonical import canonical_diff, canonical_form
from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules
from kd2_rules_mcp.kd2.rules_io import (
    dump_rules,
    load_exchange_rules,
    load_registration_rules,
    load_rules,
)
from kd2_rules_mcp.kd2.xmlstyle import BOM, KD_STYLE

DATA = Path(__file__).parent / "data"

EXCHANGE = (DATA / "exchange_rules.xml").read_text(encoding="utf-8")

REGISTRATION = (DATA / "registration_rules.xml").read_text(encoding="utf-8")


def as_file(text: str, *, bom: bool = True, newline: str = "\r\n") -> bytes:
    data = text.replace("\n", newline).encode("utf-8")
    return BOM + data if bom else data


def assert_same_meaning(left: bytes, right: bytes) -> None:
    assert canonical_diff(canonical_form(left), canonical_form(right)) == []


def test_import_exchange_rules_model() -> None:
    rules = load_exchange_rules(as_file(EXCHANGE))
    assert rules.source_name == "БП"
    assert rules.target_name == "ЗУП"
    assert [p.code for p in rules.pko()] == ["Организации", "ВидыОпераций"]
    assert [p.code for p in rules.pvd()] == ["Организации"]
    assert [a.code for a in rules.algorithms()] == ["Общий"]
    pko = rules.pko()[0]
    assert pko.get("СинхронизироватьПоИдентификатору") is True
    assert pko.get("Порядок") == 50
    properties = pko.child("Свойства")
    assert properties is not None
    pks = properties.items[0]
    assert pks.attrs["Поиск"] is True
    source = pks.child("Источник")
    assert source is not None and source.attrs == {"Имя": "ИНН", "Вид": "Реквизит", "Тип": "Строка"}
    values = rules.pko()[1].child("Значения")
    assert values is not None and values.items[0].get("Источник") == "Начисление"


def test_round_trip_is_byte_identical_for_kd_style() -> None:
    raw = as_file(EXCHANGE)
    assert dump_rules(load_rules(raw)) == raw
    raw = as_file(REGISTRATION)
    assert dump_rules(load_rules(raw)) == raw


def test_source_style_is_preserved() -> None:
    declared = '<?xml version="1.0" encoding="UTF-8"?>\n' + REGISTRATION + "\n"
    for raw in (as_file(declared, bom=True, newline="\n"), as_file(REGISTRATION, bom=False)):
        assert dump_rules(load_rules(raw)) == raw


def _mix_endings(raw: bytes) -> bytes:
    parts: list[bytes] = []
    for index, line in enumerate(raw.splitlines(keepends=True)):
        body = line.removesuffix(b"\r\n").removesuffix(b"\n").removesuffix(b"\r")
        ending = b""
        if line.endswith((b"\n", b"\r")):
            ending = b"\n" if index % 2 else b"\r\n"
        parts.append(body + ending)
    return b"".join(parts)


def test_mixed_line_endings_survive_open_and_save() -> None:
    """Выгрузка КД мешает CRLF и LF: без правок файл тот же, правка трогает одну строку."""
    mixed = _mix_endings(as_file(EXCHANGE))
    assert b"\r\n" in mixed and b"\n" in mixed.replace(b"\r\n", b"")
    assert dump_rules(load_rules(mixed)) == mixed
    rules = load_rules(mixed)
    rules.root.values["Наименование"] = "правка"
    changed = dump_rules(rules)
    left = mixed.splitlines(keepends=True)
    right = changed.splitlines(keepends=True)
    assert sum(1 for pair in zip(left, right, strict=True) if pair[0] != pair[1]) == 1


def test_export_in_kd_style_for_new_file() -> None:
    rules = load_rules(as_file(REGISTRATION, bom=False, newline="\n"))
    out = dump_rules(rules, KD_STYLE)
    assert out.startswith(BOM + "<ПравилаРегистрации>\r\n\t<".encode())
    assert not out.endswith(b"\n")


def test_handler_special_characters_are_escaped_without_cdata() -> None:
    rules = load_exchange_rules(as_file(EXCHANGE))
    code = "Если А <> Б И В & Г Тогда\n\tОтказ = Истина;\nКонецЕсли;"
    assert rules.root.get("ПередВыгрузкойДанных") == code
    out = dump_rules(rules)
    assert b"CDATA" not in out
    assert "Если А &lt;&gt; Б И В &amp; Г Тогда".encode() in out
    assert load_exchange_rules(out).root.get("ПередВыгрузкойДанных") == code


def test_text_markers_stay_in_place() -> None:
    rules = load_exchange_rules(as_file(EXCHANGE))
    group = rules.section("ПравилаКонвертацииОбъектов").items[0]
    assert group.items[1].leading_text == "//bt_N"
    assert group.trailing_text == "//bt_K"
    assert b"\t\t\t//bt_N\r\n\t\t\t<\xd0\x9f" in dump_rules(rules)


def test_unknown_tags_are_preserved() -> None:
    text = EXCHANGE.replace(
        "\t\t\t\t<Источник>СправочникСсылка.Организации</Источник>",
        "\t\t\t\t<РегистрироватьОбъектНаУзлеОтправителе>true</РегистрироватьОбъектНаУзлеОтправителе>"
        "\n\t\t\t\t<Источник>СправочникСсылка.Организации</Источник>",
    )
    rules = load_exchange_rules(as_file(text))
    assert [e.tag for e in rules.pko()[0].unknown] == ["РегистрироватьОбъектНаУзлеОтправителе"]
    out = dump_rules(rules)
    assert b"<\xd0\xa0\xd0\xb5\xd0\xb3" in out  # <Рег…
    assert load_exchange_rules(out).pko()[0].unknown[0].text == "true"


def test_default_values_are_not_written_for_new_rule() -> None:
    rules = load_exchange_rules(as_file(EXCHANGE))
    pko = Node.new("pko", "Правило")
    pko.values.update(
        {
            "Код": "Новое",
            "Порядок": 150,
            "НеЗамещать": False,
            "ПриоритетОбъектовОбмена": "",
            "Источник": "СправочникСсылка.Банки",
            "Приемник": "СправочникСсылка.Банки",
        }
    )
    rules.section("ПравилаКонвертацииОбъектов").items.append(pko)
    out = dump_rules(rules).decode("utf-8-sig")
    block = out[out.index("<Код>Новое</Код>") : out.index("</Правило>", out.index("Новое"))]
    assert "НеЗамещать" not in block
    assert "ПриоритетОбъектовОбмена" not in block
    # Порядок тегов как у писателя КД; пустые контейнеры списков выводятся всегда.
    assert block.index("<Порядок>") < block.index("<Источник>") < block.index("<Свойства/>")
    assert block.index("<Свойства/>") < block.index("<Значения/>")


def test_new_nodes_get_always_written_attributes() -> None:
    assert Node.new("pvd", "Правило").attrs == {"Отключить": False}
    assert Node.new("pro", "Правило").attrs == {"Отключить": False, "Валидное": True}
    assert Node.new("pks_side", "Источник").attrs == {"Имя": "", "Вид": ""}


def test_import_registration_rules_model() -> None:
    rules = load_registration_rules(as_file(REGISTRATION))
    assert rules.exchange_plan == "ОбменЗУП"
    assert [e.get("Тип") for e in rules.plan_content()] == ["СправочникСсылка.Организации"]
    assert rules.plan_content()[0].get("Авторегистрация") is False
    rule = rules.rules()[0]
    assert rule.code == "000000002"
    plan_filter = rule.child("ОтборПоСвойствамПланаОбмена")
    assert plan_filter is not None
    item = plan_filter.items[0]
    assert item.get("ЭтоСтрокаКонстанты") is False
    table = item.child("ТаблицаСвойствОбъекта")
    assert table is not None and table.items[0].get("Вид") == "Свойство"
    assert rule.get("ПослеОбработки") == "Если Отказ Тогда Возврат; КонецЕсли;"


def test_always_written_boolean_false_survives_round_trip() -> None:
    raw = as_file(REGISTRATION)
    out = dump_rules(load_rules(raw))
    assert "<Авторегистрация>false</Авторегистрация>".encode() in out
    assert "<ЭтоСтрокаКонстанты>false</ЭтоСтрокаКонстанты>".encode() in out


def test_format_defaults_in_source_are_dropped_on_export() -> None:
    text = EXCHANGE.replace(
        "\t\t\t\t<Порядок>100</Порядок>",
        "\t\t\t\t<Порядок>100</Порядок>\n\t\t\t\t<НеЗамещать>false</НеЗамещать>"
        "\n\t\t\t\t<ПослеЗагрузки></ПослеЗагрузки>",
    ).replace("<Группа>", '<Группа Отключить="false">')
    raw = as_file(text)
    out = dump_rules(load_rules(raw))
    assert out != raw
    assert out == as_file(EXCHANGE)
    assert_same_meaning(raw, out)


@pytest.mark.parametrize(
    ("text", "found"),
    [
        (EXCHANGE.replace(">2.01<", ">2.0<", 1), "версия формата «2.0»"),
        (
            EXCHANGE.replace("<ПравилаОбмена>", "<Правила>").replace(
                "</ПравилаОбмена>", "</Правила>"
            ),
            "корень «Правила»",
        ),
    ],
)
def test_unsupported_format_is_rejected(text: str, found: str) -> None:
    with pytest.raises(RulesFormatError, match=found):
        load_rules(as_file(text))


def test_wrong_document_kind_is_rejected() -> None:
    with pytest.raises(RulesFormatError, match="Ожидались правила обмена"):
        load_exchange_rules(as_file(REGISTRATION))
    with pytest.raises(RulesFormatError, match="Ожидались правила регистрации"):
        load_registration_rules(as_file(EXCHANGE))


def test_bad_boolean_is_reported_with_path() -> None:
    text = EXCHANGE.replace(
        "<СинхронизироватьПоИдентификатору>true<", "<СинхронизироватьПоИдентификатору>да<"
    )
    with pytest.raises(RulesFormatError, match=r"Правило\[Организации\].*ожидается true или false"):
        load_rules(as_file(text))


def test_documents_have_expected_types() -> None:
    assert isinstance(load_rules(as_file(EXCHANGE)), ExchangeRules)
    assert isinstance(load_rules(as_file(REGISTRATION)), RegistrationRules)
