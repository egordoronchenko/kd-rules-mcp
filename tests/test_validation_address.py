"""Адреса ПКС: квалификатор только при совпадении звеньев в одном контейнере."""

from pathlib import Path

from kd2_rules_mcp.kd2.model import Node
from kd2_rules_mcp.kd2.rules_io import load_exchange_rules
from kd2_rules_mcp.validation.address import pks_address, walk_pks

DATA = Path(__file__).parent / "data"


def _pks(name: str, *, search: bool = False, group: bool = False) -> Node:
    node = Node.new("pks_group" if group else "pks", "Группа" if group else "Свойство")
    if search:
        node.attrs["Поиск"] = True
    for tag in ("Источник", "Приемник"):
        side = Node.new("pks_side", tag)
        side.attrs["Имя"] = name
        side.attrs["Вид"] = "ТабличнаяЧасть" if group else "Реквизит"
        node.children[tag] = side
    return node


def _container(*items: Node) -> Node:
    container = Node.new("pks_list", "Свойства")
    container.items.extend(items)
    return container


def _paths(container: Node) -> list[str]:
    return [path for path, _node in walk_pks(container)]


def test_search_and_plain_pks_with_the_same_name_get_different_paths() -> None:
    """Поисковая ПКС получает `[поиск]`, обычная с тем же именем остаётся без метки."""
    container = _container(_pks("Владелец", search=True), _pks("Владелец"))
    assert _paths(container) == ["Владелец[поиск]", "Владелец"]
    assert pks_address("БанковскиеСчета", "Владелец[поиск]") == (
        "ПКО «БанковскиеСчета» / ПКС Владелец[поиск]"
    )


def test_two_plain_pks_get_positions() -> None:
    """Две обычные ПКС на один приёмник различаются позицией в контейнере, с 1."""
    container = _container(_pks("Имя"), _pks("Имя"))
    assert _paths(container) == ["Имя#1", "Имя#2"]


def test_unique_search_pks_has_no_qualifier() -> None:
    """Единственное поисковое свойство адресуется как раньше — без `[поиск]`."""
    assert _paths(_container(_pks("Владелец", search=True))) == ["Владелец"]
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    properties = rules.pko()[0].child("Свойства")
    assert properties is not None
    assert properties.items[0].attrs.get("Поиск") is True
    assert _paths(properties) == ["ИНН"]


def test_group_and_pks_with_the_same_name_qualify_the_group() -> None:
    """Группа ПКС подчиняется тому же правилу звена, что и свойство."""
    container = _container(_pks("Контакты", group=True), _pks("Контакты"))
    assert _paths(container) == ["Контакты#1", "Контакты#2"]


def test_two_search_pks_combine_search_mark_and_position() -> None:
    """Две поисковые ПКС: `[поиск]` и позиция того элемента в контейнере."""
    items = [_pks("Код") for _ in range(2)]
    items.append(_pks("Владелец", search=True))
    items.extend(_pks("Код") for _ in range(5))
    items.append(_pks("Владелец", search=True))
    paths = _paths(_container(*items))
    assert paths[2] == "Владелец[поиск]#3"
    assert paths[8] == "Владелец[поиск]#9"
