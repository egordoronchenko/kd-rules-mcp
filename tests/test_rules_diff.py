"""Смысловой дифф правил: синтетика, заголовок и корпус."""

from pathlib import Path
from typing import cast

import pytest

from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.kd2.canonical import canonical_form
from kd2_rules_mcp.kd2.diff import diff_rules
from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules
from kd2_rules_mcp.kd2.rules_io import (
    dump_rules,
    load_exchange_rules,
    load_registration_rules,
    load_rules,
)
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.service.views import TEXT_LIMIT
from tests.corpus import CorpusFile, corpus_params

DATA = Path(__file__).parent / "data"
EXCHANGE = DATA / "exchange_rules.xml"
REGISTRATION = DATA / "registration_rules.xml"


def _clone[T: ExchangeRules | RegistrationRules](document: T) -> T:
    return cast(T, load_rules(dump_rules(document)))


def _pko(document: ExchangeRules, code: str) -> Node:
    found = next((node for node in document.pko() if node.code == code), None)
    assert found is not None
    return found


def _properties(document: ExchangeRules, code: str) -> Node:
    container = _pko(document, code).child("Свойства")
    assert container is not None
    return container


def _property(name: str, code: str) -> Node:
    node = Node.new("pks", "Свойство")
    node.values["Код"] = code
    node.values["Наименование"] = name
    for tag in ("Источник", "Приемник"):
        side = Node.new("pks_side", tag)
        side.attrs["Имя"] = name
        side.attrs["Вид"] = "Реквизит"
        side.attrs["Тип"] = "Строка"
        node.children[tag] = side
    return node


def _copy_node(node: Node) -> Node:
    return Node(node.kind, node.tag, dict(node.attrs), dict(node.values), dict(node.children))


@pytest.mark.parametrize("path", [EXCHANGE, REGISTRATION])
def test_sample_round_trip_diff_is_empty(path: Path) -> None:
    document = load_rules(path)
    assert diff_rules(document, load_rules(dump_rules(document))) == []


def test_added_pko_is_one_change() -> None:
    """Добавленное ПКО вместе с ПКС — одно изменение, без строки на каждое свойство."""
    left = load_exchange_rules(EXCHANGE)
    right = _clone(left)
    node = Node.new("pko", "Правило")
    node.values["Код"] = "Сотрудники"
    node.values["Наименование"] = "Сотрудники"
    properties = Node.new("pks_list", "Свойства")
    properties.items.append(_property("ФИО", "1"))
    node.children["Свойства"] = properties
    container = right.root.child("ПравилаКонвертацииОбъектов")
    assert container is not None
    container.items.append(node)
    changes = diff_rules(left, right)
    assert len(changes) == 1
    assert changes[0].section == "pko"
    assert changes[0].address == "ПКО «Сотрудники»"
    assert changes[0].change == "added"
    assert changes[0].field is None


def test_removed_pvd_is_one_change() -> None:
    left = load_exchange_rules(EXCHANGE)
    right = _clone(left)
    container = right.root.child("ПравилаВыгрузкиДанных")
    assert container is not None
    container.items.clear()
    changes = diff_rules(left, right)
    assert len(changes) == 1
    assert changes[0].change == "removed"
    assert changes[0].section == "pvd"
    assert changes[0].address == "ПВД «Организации»"


def test_renamed_pko_is_removed_and_added() -> None:
    left = load_exchange_rules(EXCHANGE)
    right = _clone(left)
    _pko(right, "Организации").values["Код"] = "Организации2"
    changes = diff_rules(left, right)
    assert {(item.change, item.address) for item in changes} == {
        ("removed", "ПКО «Организации»"),
        ("added", "ПКО «Организации2»"),
    }


def test_duplicate_pks_address_gets_ordinal() -> None:
    """Две обычные ПКС на один приёмник в диффе различаются позицией `#N`."""
    left = load_exchange_rules(EXCHANGE)
    properties = _properties(left, "Организации")
    properties.items[0].attrs.pop("Поиск", None)
    properties.items.append(_property("ИНН", "2"))
    right = _clone(left)
    _properties(right, "Организации").items[1].values["Наименование"] = "Второй ИНН"
    changes = diff_rules(left, right)
    assert len(changes) == 1
    assert changes[0].section == "pks"
    assert changes[0].address == "ПКО «Организации» / ПКС ИНН#2"
    assert changes[0].field == "Наименование"


def test_search_pks_address_in_diff_matches_walk() -> None:
    """Поисковая ПКС рядом с обычной того же имени в диффе получает `[поиск]`."""
    left = load_exchange_rules(EXCHANGE)
    _properties(left, "Организации").items.append(_property("ИНН", "2"))
    right = _clone(left)
    _properties(right, "Организации").items[0].values["Наименование"] = "Поисковый ИНН"
    changes = diff_rules(left, right)
    assert len(changes) == 1
    assert changes[0].address == "ПКО «Организации» / ПКС ИНН[поиск]"


def test_duplicate_pro_code_includes_metadata_and_ordinal() -> None:
    left = load_registration_rules(REGISTRATION)
    group = left.section("ПравилаРегистрацииОбъектов").items[0]
    group.items.append(_copy_node(group.items[0]))
    right = _clone(left)
    right_group = right.section("ПравилаРегистрацииОбъектов").items[0]
    right_group.items[1].values["Комментарий"] = "второй"
    changes = diff_rules(left, right)
    assert len(changes) == 1
    assert changes[0].section == "registration"
    assert changes[0].address == "ПРО «000000002» / Справочник.Организации #2"
    assert changes[0].field == "Комментарий"


def test_crlf_without_bom_matches_lf_with_bom() -> None:
    text = EXCHANGE.read_bytes().decode("utf-8-sig")
    lf = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    crlf = lf.replace(b"\n", b"\r\n")
    bom = b"\xef\xbb\xbf" + lf
    assert diff_rules(load_rules(crlf), load_rules(bom)) == []


def test_explicit_false_flag_matches_absence() -> None:
    left = load_exchange_rules(EXCHANGE)
    right = _clone(left)
    _pko(right, "Организации").values["НеЗамещать"] = False
    assert diff_rules(left, right) == []


def test_pks_disable_flag_false_to_true() -> None:
    left = load_exchange_rules(EXCHANGE)
    right = _clone(left)
    _properties(left, "Организации").items[0].attrs["Отключить"] = False
    _properties(right, "Организации").items[0].attrs["Отключить"] = True
    changes = diff_rules(left, right)
    assert len(changes) == 1
    change = changes[0]
    assert change.section == "pks"
    assert change.address == "ПКО «Организации» / ПКС ИНН"
    assert change.change == "changed"
    assert change.field == "Отключить"
    assert change.old == ""
    assert change.new == "true"


def test_handler_diff_keeps_changed_lines_and_their_numbers() -> None:
    left = load_exchange_rules(EXCHANGE)
    base = [f"строка{i}" for i in range(1, 12)]
    _pko(left, "Организации").values["ПослеЗагрузки"] = "\n".join(base)
    right = _clone(left)
    changed = base.copy()
    changed[2] = "ДРУГАЯ3"
    changed[8] = "ДРУГАЯ9"
    _pko(right, "Организации").values["ПослеЗагрузки"] = "\n".join(changed)
    changes = diff_rules(left, right)
    assert len(changes) == 1
    change = changes[0]
    assert change.field == "ПослеЗагрузки"
    assert change.old is None and change.new is None
    assert change.handler_diff is not None
    text = "\n".join(change.handler_diff)
    assert "@@" in text
    assert "-строка3" in text and "+ДРУГАЯ3" in text
    assert "-строка9" in text and "+ДРУГАЯ9" in text
    assert "строка6" not in text


def test_long_text_is_truncated() -> None:
    left = load_exchange_rules(EXCHANGE)
    _pko(left, "Организации").values["Комментарий"] = "а" * (TEXT_LIMIT + 20)
    right = _clone(left)
    _pko(right, "Организации").values["Комментарий"] = "б" * (TEXT_LIMIT + 20)
    comment = diff_rules(left, right)
    assert len(comment) == 1
    assert comment[0].old is not None and comment[0].new is not None
    assert "обрезано" in comment[0].old
    assert comment[0].old.startswith("а" * 10)

    handler_left = load_exchange_rules(EXCHANGE)
    source = [f"строка {i} {'x' * 40}" for i in range(80)]
    _pko(handler_left, "Организации").values["ПослеЗагрузки"] = "\n".join(source)
    handler_right = _clone(handler_left)
    _pko(handler_right, "Организации").values["ПослеЗагрузки"] = "\n".join(
        f"другая {i} {'y' * 40}" for i in range(80)
    )
    handler = diff_rules(handler_left, handler_right)
    assert len(handler) == 1
    lines = handler[0].handler_diff
    assert lines is not None
    assert lines[-1] == "… усечено"
    assert len("\n".join(lines[:-1])) <= TEXT_LIMIT


def test_volatile_header_is_ignored_unless_requested(tmp_path: Path) -> None:
    left = load_exchange_rules(EXCHANGE)
    right = _clone(left)
    right.root.values["ДатаВремяСоздания"] = "2000-01-01T00:00:00"
    right.root.values["Ид"] = "other-id"
    assert diff_rules(left, right) == []
    included = diff_rules(left, right, include_header=True)
    assert len(included) == 2
    assert {item.section for item in included} == {"header"}
    assert {item.field for item in included} == {"ДатаВремяСоздания", "Ид"}

    saved = tmp_path / "right.xml"
    saved.write_bytes(dump_rules(right))
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "ws"))
    quiet = service.rules_diff(str(EXCHANGE), str(saved), False, False, None, 0, 50)
    assert quiet["changes"]["total"] == 0
    assert quiet["ignored_fields"] == ["ДатаВремяСоздания", "Ид"]
    loud = service.rules_diff(str(EXCHANGE), str(saved), True, False, None, 0, 50)
    assert loud["ignored_fields"] == []
    assert loud["changes"]["total"] == 2


def test_pks_order_is_hidden_unless_requested() -> None:
    left = load_exchange_rules(EXCHANGE)
    _properties(left, "Организации").items.append(_property("КПП", "2"))
    right = _clone(left)
    _properties(right, "Организации").items.reverse()
    assert diff_rules(left, right) == []
    changes = diff_rules(left, right, order=True)
    assert len(changes) == 1
    change = changes[0]
    assert change.section == "pks"
    assert change.change == "changed"
    assert change.field == "#порядок"
    assert change.address == "ПКО «Организации»"
    assert change.old is not None and change.new is not None
    assert change.old != change.new


def test_exchange_against_registration_is_rejected() -> None:
    with pytest.raises(Kd2Error, match="правила обмена"):
        diff_rules(load_exchange_rules(EXCHANGE), load_registration_rules(REGISTRATION))


@pytest.mark.corpus
@pytest.mark.parametrize("item", corpus_params())
def test_corpus_diff_matches_canonical(item: CorpusFile) -> None:
    """Дифф макета с собой и с round-trip пуст ровно тогда, когда равны канонические формы."""
    raw = item.path.read_bytes()
    document = load_rules(item.path)
    dumped = dump_rules(document)
    assert diff_rules(document, document) == []
    diff_empty = diff_rules(document, load_rules(dumped)) == []
    canon_equal = canonical_form(raw) == canonical_form(dumped)
    assert diff_empty
    assert diff_empty == canon_equal
