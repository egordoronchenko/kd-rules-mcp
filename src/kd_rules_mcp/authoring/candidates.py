"""Кандидаты сопоставления объектов, свойств и значений двух структур (design.md, Д7).

Эталон — обработки автонастройки КД (`reference/kd2-cfg/DataProcessors/…/Ext/ObjectModule.bsl`):

- объекты — `АвтонастройкаПравилКонвертацииОбъектов`: полное соединение по равенству имени
  и вида (`Источники.Наименование = Приемники.Наименование И Источники.Тип = Приемники.Тип`, 139);
- свойства — `АвтонастройкаПравилКонвертацииСвойств`, `СинхронизироватьСвойство` (577–630):
  для каждого свойства приёмника ищется свойство источника того же уровня с тем же именем
  и видом, а если его нет и имя из списка `Номер`/`Дата`/`НомерДок`/`ДатаДок` — с именем-аналогом
  (`CommonModules/ОбщегоНазначения/Ext/Module.bsl`, 1628–1659); вложенные свойства (реквизиты
  табличной части) сопоставляются только внутри сопоставленного родителя (дерево строк, 139–428);
- запрет «примитив → объектная ссылка» — `ОпределитьМожноАвтоматическиСопоставитьЭлементы`
  (517–564): источник `Булево`/`Дата`/`Строка`/`Число`, приёмник — ссылка на бизнес-процесс,
  документ, задачу, план видов расчёта, план видов характеристик, план обмена, план счетов,
  справочник или точку маршрута. КД сравнивает один тип свойства (`Максимум(Тип)`, 92); здесь —
  наборы типов: запрет, если все типы источника примитивные, а среди типов приёмника есть
  такая ссылка;
- значения — `АвтонастройкаПравилКонвертацииЗначений`, `СинхронизироватьЗначение` (335–348):
  по имени.

Класс «по синониму» — собственное расширение (в КД его нет): пара несопоставленных элементов одного
вида с одинаковым непустым синонимом при разных именах; только подсказка, `auto = False`.
Имена и синонимы сравниваются без учёта регистра, как ключи 1С.
"""

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from kd_rules_mcp.structures.queries import NotFound, find_object

# Аналоги имён реквизитов документа (ОбщегоНазначения, 1628–1659).
KD_NAME_ANALOGS: dict[str, str] = {
    "Номер": "НомерДок",
    "Дата": "ДатаДок",
    "НомерДок": "Номер",
    "ДатаДок": "Дата",
}
_PRIMITIVES = frozenset({"Булево", "Дата", "Строка", "Число"})
_OBJECT_REFERENCES = (
    "БизнесПроцессСсылка.",
    "ДокументСсылка.",
    "ЗадачаСсылка.",
    "ПланВидовРасчетаСсылка.",
    "ПланВидовХарактеристикСсылка.",
    "ПланОбменаСсылка.",
    "ПланСчетовСсылка.",
    "СправочникСсылка.",
    "ТочкаМаршрутаБизнесПроцессаСсылка.",
)
PRIMITIVE_TO_REFERENCE = "примитив → объектная ссылка: автоматически не сопоставляется"
BY_SYNONYM_HINT = "совпадает только синоним: применять по решению агента"


class Confidence(StrEnum):
    """Класс уверенности кандидата."""

    EXACT = "точно"
    KD_SYNONYM = "синоним КД"
    BY_SYNONYM = "по синониму"
    NO_PAIR = "нет пары"


@dataclass(frozen=True, slots=True)
class Side:
    """Элемент одной стороны: объект, свойство или значение."""

    name: str
    kind: str
    # Объект — имя типа (`СправочникСсылка.Имя`), свойство — путь от корня объекта, значение — имя.
    path: str
    synonym: str = ""
    types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Candidate:
    """Пара «источник — приёмник» или элемент без пары (`source` или `target` пусты)."""

    source: Side | None
    target: Side | None
    confidence: Confidence
    # Можно ли применять без решения агента (массовое создание правил).
    auto: bool
    note: str = ""
    # Кандидаты вложенных свойств (реквизиты табличной части) сопоставленного родителя.
    children: tuple["Candidate", ...] = ()


@dataclass(slots=True)
class _Item:
    side: Side
    row_id: int
    children: list["_Item"]


def object_candidates(
    source: sqlite3.Connection, target: sqlite3.Connection, kind: str | None = None
) -> list[Candidate]:
    """Кандидаты ПКО: пары объектов по имени и виду, подсказки по синониму, объекты без пары.

    `kind` ограничивает вид объектов (`Справочник`, `Документ`…).
    """
    sources = _objects(source, kind)
    targets = _objects(target, kind)
    return _match_level(sources, targets, analogs=False, check_types=False, recurse=False)


def property_candidates(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    source_object: str,
    target_object: str,
) -> list[Candidate] | NotFound:
    """Кандидаты ПКС пары объектов (`Вид.Имя` или имя типа) деревом по табличным частям."""
    source_row = find_object(source, source_object)
    if isinstance(source_row, NotFound):
        return source_row
    target_row = find_object(target, target_object)
    if isinstance(target_row, NotFound):
        return target_row
    return _match_level(
        _properties(source, int(source_row["id"])),
        _properties(target, int(target_row["id"])),
        analogs=True,
        check_types=True,
        recurse=True,
    )


def value_candidates(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    source_object: str,
    target_object: str,
) -> list[Candidate] | NotFound:
    """Кандидаты ПКЗ: значения (перечисления, предопределённые элементы) по имени."""
    source_row = find_object(source, source_object)
    if isinstance(source_row, NotFound):
        return source_row
    target_row = find_object(target, target_object)
    if isinstance(target_row, NotFound):
        return target_row
    return _match_level(
        _values(source, int(source_row["id"])),
        _values(target, int(target_row["id"])),
        analogs=False,
        check_types=False,
        recurse=False,
    )


def auto_allowed(source_types: Iterable[str], target_types: Iterable[str]) -> bool:
    """Ложь, если все типы источника примитивные, а приёмник допускает объектную ссылку."""
    sources = set(source_types)
    if not sources or not sources <= _PRIMITIVES:
        return True
    return not any(name.startswith(_OBJECT_REFERENCES) for name in target_types)


def _match_level(
    sources: Sequence[_Item],
    targets: Sequence[_Item],
    *,
    analogs: bool,
    check_types: bool,
    recurse: bool,
) -> list[Candidate]:
    free: dict[tuple[str, str], _Item] = {}
    for item in sources:
        free.setdefault(_key(item.side.kind, item.side.name), item)
    paired: dict[int, Candidate] = {}
    for index, target in enumerate(targets):
        found = free.pop(_key(target.side.kind, target.side.name), None)
        confidence = Confidence.EXACT
        if found is None and analogs and target.side.name in KD_NAME_ANALOGS:
            analog = KD_NAME_ANALOGS[target.side.name]
            found = free.pop(_key(target.side.kind, analog), None)
            confidence = Confidence.KD_SYNONYM
        if found is not None:
            paired[index] = _pair(
                found, target, confidence, check_types=check_types, recurse=recurse
            )
    rest = list(free.values())
    _pair_by_synonym(rest, targets, paired)
    used = {id(c.source) for c in paired.values() if c.source is not None}
    result = [
        paired.get(index) or Candidate(None, target.side, Confidence.NO_PAIR, auto=False)
        for index, target in enumerate(targets)
    ]
    result.extend(
        Candidate(item.side, None, Confidence.NO_PAIR, auto=False)
        for item in sources
        if id(item.side) not in used
    )
    return result


def _pair(
    source: _Item,
    target: _Item,
    confidence: Confidence,
    *,
    check_types: bool,
    recurse: bool,
) -> Candidate:
    auto = not check_types or auto_allowed(source.side.types, target.side.types)
    children: tuple[Candidate, ...] = ()
    if recurse and (source.children or target.children):
        children = tuple(
            _match_level(
                source.children, target.children, analogs=True, check_types=True, recurse=True
            )
        )
    note = "" if auto else PRIMITIVE_TO_REFERENCE
    return Candidate(source.side, target.side, confidence, auto, note, children)


def _pair_by_synonym(
    sources: list[_Item], targets: Sequence[_Item], paired: dict[int, Candidate]
) -> None:
    """Однозначные пары одного вида с одинаковым синонимом среди несопоставленных."""
    by_synonym: dict[tuple[str, str], list[_Item]] = {}
    for item in sources:
        if item.side.synonym.strip():
            by_synonym.setdefault(_key(item.side.kind, item.side.synonym), []).append(item)
    free_targets: dict[tuple[str, str], list[int]] = {}
    for index, target in enumerate(targets):
        if index not in paired and target.side.synonym.strip():
            key = _key(target.side.kind, target.side.synonym)
            free_targets.setdefault(key, []).append(index)
    for key, indexes in free_targets.items():
        found = by_synonym.get(key, [])
        if len(indexes) != 1 or len(found) != 1:
            continue
        paired[indexes[0]] = Candidate(
            found[0].side,
            targets[indexes[0]].side,
            Confidence.BY_SYNONYM,
            auto=False,
            note=BY_SYNONYM_HINT,
        )


def _key(kind: str, name: str) -> tuple[str, str]:
    return kind.casefold(), name.strip().casefold()


def _objects(conn: sqlite3.Connection, kind: str | None) -> list[_Item]:
    sql = (
        "SELECT id, kind, name, type_name, synonym FROM objects "
        "WHERE is_group = 0 AND kind <> '' AND type_name LIKE '%.%'"
    )
    params: tuple[str, ...] = ()
    if kind is not None:
        sql += " AND kind = ?"
        params = (kind,)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [
        _Item(Side(str(r[2]), str(r[1]), str(r[3]), str(r[4]), (str(r[3]),)), int(r[0]), [])
        for r in rows
    ]


def _properties(conn: sqlite3.Connection, object_id: int) -> list[_Item]:
    rows = conn.execute(
        "SELECT p.id, p.parent_id, p.kind, p.name, p.path, p.synonym, ts.types "
        "FROM properties AS p LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id "
        "WHERE p.object_id = ? ORDER BY p.id",
        (object_id,),
    ).fetchall()
    items: dict[int, _Item] = {}
    roots: list[_Item] = []
    for row_id, parent_id, kind, name, path, synonym, types in rows:
        side = Side(str(name), str(kind), str(path), str(synonym), _types(types))
        item = _Item(side, int(row_id), [])
        items[item.row_id] = item
        parent = items.get(parent_id) if parent_id is not None else None
        (parent.children if parent is not None else roots).append(item)
    return roots


def _values(conn: sqlite3.Connection, object_id: int) -> list[_Item]:
    rows = conn.execute(
        "SELECT id, name, synonym FROM object_values WHERE object_id = ? ORDER BY id",
        (object_id,),
    ).fetchall()
    return [
        _Item(Side(str(name), "Значение", str(name), str(synonym)), int(row_id), [])
        for row_id, name, synonym in rows
    ]


def _types(value: object) -> tuple[str, ...]:
    return tuple(value.split("\n")) if isinstance(value, str) and value else ()
