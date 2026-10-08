"""Адреса правил в замечаниях (design.md, Д6).

ПКО, ПВД, ПОД, ПРО — по коду, алгоритмы и запросы — по имени; ПКС — код ПКО и путь
`группа/…/свойство-приёмник`; ПКЗ — код ПКО и имя значения источника. Корень правил
обмена — адрес `Конвертация` (события конвертации, один экземпляр). Адрес — строка,
которую агент передаёт инструментам правки как есть.

Звено ПКС — имя приёмника. Пустой приёмник адресуется именем источника, ПКС-параметр
(`ИмяПараметраДляПередачи` без приёмника) — именем параметра. Квалификатор появляется
только когда в том же контейнере звенья иначе не различить: у ПКС поиска — `[поиск]`,
если имена источника и приёмника различаются — оба имени через `→`, и только затем
позиция `#N` (порядковый номер в `items`, с 1). Уникальное имя не меняется.
Прежние звенья (`(источник)`, голое `#N`) по-прежнему находят то же правило.
"""

from collections import Counter
from collections.abc import Iterator

from kd_rules_mcp.kd2.model import Node

# Метка ПКС поиска в звене адреса. Позиция пишется следом: `Владелец[поиск]#3`.
_SEARCH_MARK = "[поиск]"

# Корень `ПравилаОбмена`: события конвертации, один экземпляр на файл.
CONVERSION_ADDRESS = "Конвертация"

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
    """Адрес правила верхнего уровня (ПКО, ПВД, ПОД, ПРО, алгоритм, запрос, параметр) или группы.

    Корень правил обмена — `Конвертация`: у него нет кода, события лежат полями этого узла.
    """
    if node.kind.name == "exchange_rules":
        return CONVERSION_ADDRESS
    title = TITLES.get(node.kind.name, node.kind.title)
    return f"{title} «{node.code}»"


def side_name(node: Node, side: str) -> str:
    """Имя свойства на стороне `Источник` или `Приемник` ПКС или группы ПКС."""
    child = node.child(side)
    return str(child.attrs.get("Имя", "")) if child is not None else ""


def pks_base_segment(node: Node, index: int) -> str:
    """Звено без квалификатора.

    Имя приёмника. Пустой приёмник у ПКС-параметра — имя параметра, иначе имя источника.
    Нет ни того ни другого — позиция `#N` (с 1).
    """
    target = side_name(node, "Приемник")
    if target:
        return target
    parameter = _parameter_name(node)
    if parameter:
        return parameter
    source = side_name(node, "Источник")
    if source:
        return source
    return f"#{index + 1}"


def _parameter_name(node: Node) -> str:
    return str(node.values.get("ИмяПараметраДляПередачи", "")).strip()


def _legacy_base(node: Node, index: int) -> str:
    """Прежнее звено: приёмник, иначе `(источник)`, иначе `#N`.

    Такие адреса уже выдавались в ответах и должны находить то же правило.
    """
    target = side_name(node, "Приемник")
    if target:
        return target
    source = side_name(node, "Источник")
    return f"({source})" if source else f"#{index + 1}"


def _both_names(node: Node) -> str:
    """Оба имени, когда одно звено их не различает. Пусто, если добавлять нечего."""
    source = side_name(node, "Источник")
    target = side_name(node, "Приемник")
    parameter = _parameter_name(node)
    if source and target and source != target:
        return f"{source}→{target}"
    if parameter and source and not target and parameter != source:
        return f"{source}→{parameter}"
    if parameter and target and parameter != target:
        return f"{parameter}→{target}"
    return ""


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


def _qualify(container: Node, bases: list[str]) -> list[str]:
    """`[поиск]` у повтора, затем позиция `#N`, если звено всё ещё не одно."""
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


def _legacy_segments(container: Node) -> list[str]:
    bases = [_legacy_base(item, index) for index, item in enumerate(container.items)]
    return _qualify(container, bases)


def pks_segments(container: Node) -> list[str]:
    """Звенья прямых элементов контейнера.

    Сначала у ПКС поиска, чьё базовое звено повторяется, добавляется `[поиск]`.
    Если после этого звено всё ещё совпадает и у правил разные имена источника и
    приёмника, в звено входят оба имени. Позиция `#N` — только когда и это не
    различает правила.
    """
    items = container.items
    bases = [pks_base_segment(item, index) for index, item in enumerate(items)]
    counts = Counter(bases)
    marked = [
        f"{base}{_SEARCH_MARK}" if counts[base] > 1 and _is_search(item) else base
        for item, base in zip(items, bases, strict=True)
    ]
    marked_counts = Counter(marked)
    enriched: list[str] = []
    for item, segment in zip(items, marked, strict=True):
        if marked_counts[segment] <= 1:
            enriched.append(segment)
            continue
        both = _both_names(item)
        if both and _SEARCH_MARK in segment:
            both = f"{both}{_SEARCH_MARK}"
        enriched.append(both or segment)
    enriched_counts = Counter(enriched)
    return [
        f"{segment}#{index + 1}" if enriched_counts[segment] > 1 else segment
        for index, segment in enumerate(enriched)
    ]


def pks_candidates(container: Node, segment: str) -> list[tuple[str, Node]]:
    """Элементы контейнера, которым соответствует звено, вместе с построенным звеном.

    Квалифицированное звено сравнивается с построенным и с прежним адресом.
    Голое имя — с построенным звеном, базовым именем и прежним базовым звеном:
    одно совпадение находит правило, несколько — неоднозначность, её разбирает вызывающий.
    """
    built = pks_segments(container)
    legacy = _legacy_segments(container)
    if _segment_is_qualified(segment):
        indexes = [
            index
            for index, (name, old) in enumerate(zip(built, legacy, strict=True))
            if name == segment or old == segment
        ]
    else:
        indexes = [
            index
            for index, item in enumerate(container.items)
            if built[index] == segment
            or legacy[index] == segment
            or pks_base_segment(item, index) == segment
            or _legacy_base(item, index) == segment
        ]
    return [(built[index], container.items[index]) for index in indexes]


def plain_pks_path(path: str) -> str:
    """Путь ПКС без `[поиск]`, позиции и имени источника: для сверки со свойством приёмника."""
    parts: list[str] = []
    for part in path.split("/"):
        plain = part.replace(_SEARCH_MARK, "")
        head, sep, tail = plain.rpartition("#")
        if sep and head and tail.isdigit():
            plain = head
        if "→" in plain:
            plain = plain.split("→")[-1]
        parts.append(plain)
    return "/".join(parts)


def pro_label(node: Node) -> str:
    """Адрес ПРО без порядкового номера: объект метаданных, иначе код правила."""
    meta = str(node.values.get("ОбъектМетаданныхИмя", "")).strip()
    if meta:
        return f"ПРО «{meta}»"
    return rule_address(node)


def pro_addresses(nodes: list[Node]) -> list[str]:
    """Адреса ПРО в порядке документа. Повтор объекта — `#1`, `#2`, … у каждого такого правила."""
    labels = [pro_label(node) for node in nodes]
    counts = Counter(labels)
    seen: Counter[str] = Counter()
    addressed: list[str] = []
    for label in labels:
        seen[label] += 1
        addressed.append(f"{label}#{seen[label]}" if counts[label] > 1 else label)
    return addressed


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
