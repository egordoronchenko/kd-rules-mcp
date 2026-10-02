"""Адреса правил в замечаниях (design.md, Д6).

ПКО, ПВД, ПОД, ПРО — по коду, алгоритмы и запросы — по имени; ПКС — код ПКО и путь
`группа/…/свойство-приёмник`; ПКЗ — код ПКО и имя значения источника. Адрес — строка,
которую агент передаёт инструментам правки как есть.

Звено ПКС — имя приёмника. Квалификатор появляется только когда в том же контейнере
уже есть правило с тем же звеном: у ПКС поиска — `[поиск]`, а если и после этого
звенья совпали — позиция `#N` (порядковый номер в `items`, с 1). Уникальное имя
не меняется.
"""

from collections import Counter
from collections.abc import Iterator

from kd2_rules_mcp.kd2.model import Node

# Метка ПКС поиска в звене адреса. Позиция пишется следом: `Владелец[поиск]#3`.
_SEARCH_MARK = "[поиск]"

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


def pks_base_segment(node: Node, index: int) -> str:
    """Звено без квалификатора: имя приёмника; без него — имя источника в скобках, иначе номер."""
    target = side_name(node, "Приемник")
    if target:
        return target
    source = side_name(node, "Источник")
    return f"({source})" if source else f"#{index + 1}"


def _is_search(node: Node) -> bool:
    return node.attrs.get("Поиск") is True


def _segment_is_qualified(segment: str) -> bool:
    """Звено уже несёт `[поиск]` или позицию `#N` после имени.

    Голое `#N` — это базовое звено правила без имени, а не квалификатор позиции.
    """
    if _SEARCH_MARK in segment:
        return True
    head, sep, tail = segment.rpartition("#")
    return bool(sep and head and tail.isdigit())


def pks_segments(container: Node) -> list[str]:
    """Звенья прямых элементов контейнера: квалификатор только у совпавших имён.

    Сначала у ПКС поиска, чьё базовое звено повторяется, добавляется `[поиск]`.
    Если после этого звено всё ещё совпадает с другим (две обычные ПКС или две
    поисковые), к каждому такому звену дописывается позиция в `items`.
    """
    bases = [pks_base_segment(item, index) for index, item in enumerate(container.items)]
    counts = Counter(bases)
    marked = [
        f"{base}{_SEARCH_MARK}" if counts[base] > 1 and _is_search(item) else base
        for item, base in zip(container.items, bases, strict=True)
    ]
    marked_counts = Counter(marked)
    return [
        f"{segment}#{index + 1}" if marked_counts[segment] > 1 else segment
        for index, segment in enumerate(marked)
    ]


def pks_candidates(container: Node, segment: str) -> list[tuple[str, Node]]:
    """Элементы контейнера, которым соответствует звено, вместе с построенным звеном.

    Квалифицированное звено сравнивается с построенным как есть. Голое имя — со
    всеми элементами с таким базовым звеном: одно совпадение находит правило,
    несколько — неоднозначность, её разбирает вызывающий.
    """
    built = pks_segments(container)
    if _segment_is_qualified(segment):
        indexes = [index for index, name in enumerate(built) if name == segment]
    else:
        indexes = [
            index
            for index, item in enumerate(container.items)
            if pks_base_segment(item, index) == segment
        ]
    return [(built[index], container.items[index]) for index in indexes]


def walk_pks(container: Node, prefix: str = "") -> Iterator[tuple[str, Node]]:
    """ПКС и группы ПКС контейнера в порядке документа с путями `группа/…/свойство`."""
    for segment, item in zip(pks_segments(container), container.items, strict=True):
        path = f"{prefix}{segment}"
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
