"""Адреса правил в замечаниях (design.md, Д6).

ПКО, ПВД, ПОД, ПРО — по коду, алгоритмы и запросы — по имени; ПКС — код ПКО и путь
`группа/…/свойство-приёмник`; ПКЗ — код ПКО и имя значения источника. Адрес — строка,
которую агент передаёт инструментам правки как есть.
"""

from collections.abc import Iterator

from kd2_rules_mcp.kd2.model import Node

# Заголовки разделов в адресах: вид узла → короткое имя.
TITLES = {
    "pko": "ПКО",
    "pko_group": "группа ПКО",
    "pvd": "ПВД",
    "pvd_group": "группа ПВД",
    "pod": "ПОД",
    "pod_group": "группа ПОД",
    "algorithm": "алгоритм",
    "algorithm_group": "группа алгоритмов",
    "query": "запрос",
    "query_group": "группа запросов",
    "parameter": "параметр",
    "data_processor": "обработка",
    "pro": "ПРО",
    "pro_group": "группа ПРО",
}


def rule_address(node: Node) -> str:
    """Адрес правила верхнего уровня (ПКО, ПВД, ПОД, ПРО, алгоритм, запрос, параметр) или группы."""
    title = TITLES.get(node.kind.name, node.kind.title)
    return f"{title} «{node.code}»"


def side_name(node: Node, side: str) -> str:
    """Имя свойства на стороне `Источник` или `Приемник` ПКС или группы ПКС."""
    child = node.child(side)
    return str(child.attrs.get("Имя", "")) if child is not None else ""


def pks_segment(node: Node, index: int) -> str:
    """Звено пути ПКС: имя приёмника; без него — имя источника в скобках, иначе номер."""
    target = side_name(node, "Приемник")
    if target:
        return target
    source = side_name(node, "Источник")
    return f"({source})" if source else f"#{index + 1}"


def walk_pks(container: Node, prefix: str = "") -> Iterator[tuple[str, Node]]:
    """ПКС и группы ПКС контейнера в порядке документа с путями `группа/…/свойство`."""
    for index, item in enumerate(container.items):
        path = f"{prefix}{pks_segment(item, index)}"
        yield path, item
        if item.is_group:
            yield from walk_pks(item, f"{path}/")


def pks_address(pko_code: str, path: str) -> str:
    """Адрес ПКС или группы ПКС."""
    return f"ПКО «{pko_code}» / ПКС {path}"


def walk_pkz(container: Node) -> Iterator[Node]:
    """ПКЗ контейнера с раскрытием групп."""
    return container.walk()


def pkz_address(pko_code: str, source_value: str) -> str:
    """Адрес ПКЗ по имени значения источника."""
    return f"ПКО «{pko_code}» / ПКЗ {source_value}"
