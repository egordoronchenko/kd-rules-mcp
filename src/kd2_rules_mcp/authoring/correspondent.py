"""Черновик правил корреспондента зеркалированием выбранных ПКО (design.md, Д8).

В КД такого механизма нет: правила корреспондента — второй независимый набор (дайджест §2.9, §6),
поэтому результат всегда черновик и помечается в `Комментарий` заголовка.

Что делает зеркалирование:

- заголовок: `Источник` ↔ `Приемник`, новый `Ид`, `Наименование` «<приёмник> --> <источник>»;
  события конвертации, параметры, обработки, ПВД, ПОД, алгоритмы и запросы не переносятся —
  они привязаны к направлению исходных правил;
- ПКО, ПКС (со сторонами и атрибутом `Тип`) и ПКЗ: `Источник` ↔ `Приемник`; признак `Поиск` остаётся
  на ПКС и после обмена сторон относится к свойству нового приёмника;
- обработчики ПКО, ПКС и групп ПКС снимаются и возвращаются списком с кодом («перенести вручную»);
  варианты поиска ПКО (`НастройкаВариантовПоискаОбъектов`) снимаются вместе с обработчиком
  `ПоследовательностьПолейПоиска`, которому они передаются;
- `КодПравилаКонвертации` на ПКО вне выборки очищается — иначе ссылка висячая
  (`format.dangling_ref`);
- со структурой нового приёмника (конфигурации-источника исходных правил) ПКС выключается, если
  свойства нового приёмника нет у объекта или его ссылочного типа `Тип` нет в структуре.
"""

import copy
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from kd2_rules_mcp.kd2.model import ExchangeRules, Node
from kd2_rules_mcp.structures.queries import NotFound, find_object
from kd2_rules_mcp.validation.address import pks_address, walk_pks
from kd2_rules_mcp.validation.handlers import EVENT_AREAS

DRAFT_COMMENT = "Черновик правил корреспондента (зеркалирование ПКО): требует проверки агентом"
MANUAL = "перенести вручную"

# Разделы и события, которые принадлежат направлению исходных правил.
_DROPPED_SECTIONS = (
    "Параметры",
    "Обработки",
    "ПравилаВыгрузкиДанных",
    "ПравилаОчисткиДанных",
    "Алгоритмы",
    "Запросы",
)
_PRIMITIVE_TYPES = frozenset(
    {"Булево", "Дата", "Строка", "Число", "УникальныйИдентификатор", "ХранилищеЗначения"}
)


@dataclass(frozen=True, slots=True)
class Handler:
    """Обработчик, не перенесённый в черновик."""

    address: str
    event: str
    code: str
    note: str = MANUAL


@dataclass(frozen=True, slots=True)
class Disabled:
    """ПКС, выключенное в черновике, и причина."""

    address: str
    reason: str


@dataclass(slots=True)
class MirrorResult:
    """Черновик и всё, что агенту нужно проверить или перенести вручную."""

    rules: ExchangeRules
    handlers: list[Handler] = field(default_factory=list)
    disabled: list[Disabled] = field(default_factory=list)
    # Коды ПКО из выборки, которых нет в исходных правилах.
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    draft: bool = True


def mirror_rules(
    rules: ExchangeRules,
    codes: Iterable[str],
    target_structure: sqlite3.Connection | None = None,
) -> MirrorResult:
    """Черновик правил обратного направления из ПКО с кодами `codes`.

    `target_structure` — структура нового приёмника (конфигурации-источника `rules`); без неё
    свойства и типы не проверяются, и это отмечено в `notes`.
    """
    wanted = list(dict.fromkeys(code.strip() for code in codes))
    by_code = {pko.code.strip(): pko for pko in _pko(rules)}
    selected = [by_code[code] for code in wanted if code in by_code]
    draft = ExchangeRules(copy.deepcopy(rules.root), rules.style)
    result = MirrorResult(draft, missing=[code for code in wanted if code not in by_code])
    _mirror_header(draft.root, result)
    mirrored = [copy.deepcopy(pko) for pko in selected]
    draft.section("ПравилаКонвертацииОбъектов").items = mirrored
    codes_left = {pko.code.strip() for pko in mirrored}
    for pko in mirrored:
        _mirror_pko(pko, codes_left, target_structure, result)
    if target_structure is None:
        result.notes.append("Структура нового приёмника не передана: свойства и типы не проверены")
    return result


def _pko(rules: ExchangeRules) -> list[Node]:
    node = rules.root.children.get("ПравилаКонвертацииОбъектов")
    return list(node.walk()) if node is not None else []


def _mirror_header(root: Node, result: MirrorResult) -> None:
    _swap_children(root)
    source = root.child("Источник")
    target = root.child("Приемник")
    source_name = source.text if source is not None and source.text else ""
    target_name = target.text if target is not None and target.text else ""
    root.values["Ид"] = str(uuid.uuid4())
    root.values["Наименование"] = f"{source_name} --> {target_name}"
    root.values["Комментарий"] = DRAFT_COMMENT
    for event in [tag for (kind_name, tag) in EVENT_AREAS if kind_name == "exchange_rules"]:
        code = root.values.pop(event, None)
        if isinstance(code, str) and code.strip():
            result.handlers.append(Handler("Конвертация", event, code))
    for tag in _DROPPED_SECTIONS:
        if root.children.pop(tag, None) is not None:
            result.notes.append(
                f"Раздел «{tag}» не перенесён: он относится к исходному направлению"
            )


def _mirror_pko(
    pko: Node,
    codes: set[str],
    structure: sqlite3.Connection | None,
    result: MirrorResult,
) -> None:
    code = pko.code.strip()
    _swap_values(pko)
    _take_handlers(pko, "pko", f"ПКО «{code}»", result)
    if pko.children.pop("НастройкаВариантовПоискаОбъектов", None) is not None:
        result.notes.append(
            f"ПКО «{code}»: варианты поиска сняты вместе с ПоследовательностьПолейПоиска"
        )
    target_type = str(pko.values.get("Приемник", ""))
    object_row = _object(structure, target_type) if structure is not None else None
    if structure is not None and object_row is None:
        result.notes.append(
            f"ПКО «{code}»: объекта «{target_type}» нет в структуре нового приёмника"
        )
    properties = pko.child("Свойства")
    if properties is None:
        return
    nodes = [node for _, node in walk_pks(properties)]
    for node in nodes:
        _swap_children(node)
    for path, node in walk_pks(properties):
        address = pks_address(code, path)
        _take_handlers(node, "pks_group" if node.is_group else "pks", address, result)
        _check_reference(node, codes, address, result)
        if node.attrs.get("Отключить") is True:
            continue
        reason = _property_problem(node, path, structure, object_row)
        if reason:
            node.attrs["Отключить"] = True
            result.disabled.append(Disabled(address, reason))


def _swap_children(node: Node) -> None:
    source = node.children.pop("Источник", None)
    target = node.children.pop("Приемник", None)
    if target is not None:
        target.tag = "Источник"
        node.children["Источник"] = target
    if source is not None:
        source.tag = "Приемник"
        node.children["Приемник"] = source


def _swap_values(node: Node) -> None:
    source = node.values.pop("Источник", None)
    target = node.values.pop("Приемник", None)
    if target is not None:
        node.values["Источник"] = target
    if source is not None:
        node.values["Приемник"] = source
    values = node.child("Значения")
    if values is not None:
        for item in values.walk():
            _swap_values(item)


def _take_handlers(node: Node, kind_name: str, address: str, result: MirrorResult) -> None:
    for tag in [tag for (kind, tag) in EVENT_AREAS if kind == kind_name]:
        code = node.values.pop(tag, None)
        if isinstance(code, str) and code.strip():
            result.handlers.append(Handler(address, tag, code))


def _check_reference(node: Node, codes: set[str], address: str, result: MirrorResult) -> None:
    ref = str(node.values.get("КодПравилаКонвертации", "")).strip()
    if ref and ref not in codes:
        del node.values["КодПравилаКонвертации"]
        result.notes.append(f"{address}: ссылка на ПКО «{ref}» вне выборки очищена")


def _object(structure: sqlite3.Connection, type_name: str) -> sqlite3.Row | None:
    row = find_object(structure, type_name) if type_name else None
    return None if row is None or isinstance(row, NotFound) else row


def _property_problem(
    node: Node,
    path: str,
    structure: sqlite3.Connection | None,
    object_row: sqlite3.Row | None,
) -> str:
    """Причина выключить ПКС по структуре нового приёмника или пустая строка."""
    target = node.child("Приемник")
    name = str(target.attrs.get("Имя", "")) if target is not None else ""
    if not name:
        return "у зеркального ПКС нет свойства приёмника"
    if structure is None or object_row is None:
        return ""
    property_path = ".".join(_target_names(path))
    found = structure.execute(
        "SELECT 1 FROM properties WHERE object_id = ? AND path = ?",
        (object_row["id"], property_path),
    ).fetchone()
    if found is None:
        return f"свойства «{property_path}» нет у «{object_row['type_name']}» в структуре"
    type_name = str(target.attrs.get("Тип", "")) if target is not None else ""
    if type_name and type_name not in _PRIMITIVE_TYPES and _object(structure, type_name) is None:
        return f"типа «{type_name}» нет в структуре нового приёмника"
    return ""


def _target_names(path: str) -> list[str]:
    """Имена свойств по пути ПКС; звенья без приёмника (`(имя)`, `#N`) пропускаются."""
    return [part for part in path.split("/") if part and part[0] not in "(#"]
