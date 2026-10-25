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
from collections import Counter
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
BY_COMPOSITION_HINT = "совпадает состав реквизитов: применять по решению агента"
COMPOSITION_MIN_SHARE = 0.7
COMPOSITION_MIN_COMMON = 5
COMPOSITION_NOISE_SHARE = 0.15
COMPOSITION_NOISE_MIN_OBJECTS = 20
_VALUE_COMPOSITION_KINDS = frozenset(
    name.casefold() for name in ("Перечисление", "ТочкаМаршрутаБизнесПроцесса")
)


class Confidence(StrEnum):
    """Класс уверенности кандидата."""

    EXACT = "точно"
    KD_SYNONYM = "синоним КД"
    BY_SYNONYM = "по синониму"
    BY_COMPOSITION = "по составу"
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
    share: float | None = None
    common: int = 0


@dataclass(slots=True)
class ObjectCandidates:
    """Кандидаты ПКО и сводка состава до фильтров и страницы."""

    candidates: list[Candidate]
    composition_pairs: int = 0
    composition_ambiguous: int = 0


@dataclass(slots=True)
class _Item:
    side: Side
    row_id: int
    children: list["_Item"]


def object_candidates(
    source: sqlite3.Connection, target: sqlite3.Connection, kind: str | None = None
) -> list[Candidate]:
    """Кандидаты ПКО с подсказками по синониму и составу, объекты без пары.

    `kind` ограничивает вид объектов (`Справочник`, `Документ`…).
    """
    return object_candidates_report(source, target, kind).candidates


def object_candidates_report(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    kind: str | None = None,
    *,
    composition: bool = True,
) -> ObjectCandidates:
    """Кандидаты ПКО; состав считается только по запросу и только среди объектов без пары."""
    sources = _objects(source, kind)
    targets = _objects(target, kind)
    candidates = _match_level(sources, targets, analogs=False, check_types=False, recurse=False)
    report = ObjectCandidates(candidates)
    if not composition:
        return report
    source_names, source_noise = _composition(source, sources)
    target_names, target_noise = _composition(target, targets)
    free = {
        id(side)
        for candidate in candidates
        if candidate.confidence is Confidence.NO_PAIR
        for side in (candidate.source, candidate.target)
        if side is not None
    }
    pairs, ambiguous = _pair_by_composition(
        [item for item in sources if id(item.side) in free],
        [item for item in targets if id(item.side) in free],
        source_names,
        target_names,
        {
            name: source_noise.get(name, set()) | target_noise.get(name, set())
            for name in source_noise.keys() | target_noise.keys()
        },
    )
    used = {id(candidate.source) for candidate in pairs.values()}
    report.candidates = [
        pairs.get(id(candidate.target), candidate)
        for candidate in candidates
        if id(candidate.source) not in used
    ]
    report.composition_pairs = len(pairs)
    report.composition_ambiguous = ambiguous
    return report


def _composition(
    conn: sqlite3.Connection, objects: Sequence[_Item]
) -> tuple[dict[int, set[str]], dict[str, set[str]]]:
    """Состав одним запросом; шум — по всем допустимым объектам каждого вида стороны."""
    kinds = {item.row_id: item.side.kind.casefold() for item in objects}
    names: dict[int, set[str]] = {row_id: set() for row_id in kinds}
    rows = conn.execute(
        "SELECT object_id, name, 0 FROM properties WHERE parent_id IS NULL "
        "AND kind IN ('Реквизит', 'Измерение', 'Ресурс') "
        "UNION ALL SELECT object_id, name, 1 FROM object_values WHERE parent_id IS NULL"
    )
    for row_id, name, is_value in rows:
        kind = kinds.get(row_id)
        if kind is not None and bool(is_value) == (kind in _VALUE_COMPOSITION_KINDS):
            names[row_id].add(str(name).strip().casefold())
    counts = Counter(kinds.values())
    frequencies: dict[str, Counter[str]] = {}
    for row_id, kind in kinds.items():
        frequencies.setdefault(kind, Counter()).update(names[row_id])
    noise = {
        kind: {
            name
            for name, count in frequencies[kind].items()
            if count >= COMPOSITION_NOISE_SHARE * count_objects
        }
        for kind, count_objects in counts.items()
        if count_objects >= COMPOSITION_NOISE_MIN_OBJECTS
    }
    return names, noise


def _pair_by_composition(
    sources: Sequence[_Item],
    targets: Sequence[_Item],
    source_names: dict[int, set[str]],
    target_names: dict[int, set[str]],
    noise: dict[str, set[str]],
) -> tuple[dict[int, Candidate], int]:
    """Все пары выше порога; выдаются только однозначные с обеих сторон."""
    by_kind: dict[str, list[tuple[_Item, set[str]]]] = {}
    for item in targets:
        kind = item.side.kind.casefold()
        names = target_names[item.row_id] - noise.get(kind, set())
        if len(names) >= COMPOSITION_MIN_COMMON:
            by_kind.setdefault(kind, []).append((item, names))
    above: list[Candidate] = []
    source_counts: Counter[int] = Counter()
    target_counts: Counter[int] = Counter()
    for item in sources:
        kind = item.side.kind.casefold()
        names = source_names[item.row_id] - noise.get(kind, set())
        if len(names) < COMPOSITION_MIN_COMMON:
            continue
        for target, other_names in by_kind.get(kind, []):
            common = len(names & other_names)
            if common < COMPOSITION_MIN_COMMON:
                continue
            share = common / len(names | other_names)
            if share < COMPOSITION_MIN_SHARE:
                continue
            above.append(
                Candidate(
                    item.side,
                    target.side,
                    Confidence.BY_COMPOSITION,
                    auto=False,
                    note=BY_COMPOSITION_HINT,
                    share=share,
                    common=common,
                )
            )
            source_counts[id(item.side)] += 1
            target_counts[id(target.side)] += 1
    pairs = {
        id(candidate.target): candidate
        for candidate in above
        if source_counts[id(candidate.source)] == 1 and target_counts[id(candidate.target)] == 1
    }
    return pairs, len(above) - len(pairs)


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
