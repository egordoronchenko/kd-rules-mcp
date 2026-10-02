"""Точечные правки правил обмена (спецификация `rules-authoring`, «Точечные правки правил»).

Документ `ExchangeRules` меняется на месте. Адрес — как в `validation.address` (design.md, Д6):
ПКО, ПВД и ПОД по `Код`; алгоритм, запрос и параметр по `Имя`; ПКС — код ПКО и путь
`группа/…/свойство-приёмник`; ПКЗ — код ПКО и имя значения источника. Изменение записывает
только переданные поля.

Поля новых ПКС — как у автонастройки
`АвтонастройкаПравилКонвертацииСвойств/Ext/ObjectModule.bsl`, `СохранитьПравилаКС` (794–898):
`Порядок` с нуля шагом 50 (796, 815, 864), группа при вложенных строках (802–806), имя, вид
и тип сторон (831–850). `Отключить` у свойства приёмника без пары — по спецификации и Д7;
КД ставит этот флаг в строке 816 и пишет атрибутом (`ВыгрузкаКонвертации`, 619–621).
В XML тип стороны пишется только при единственном типе, у группы тип не пишется
(`ВыгрузкаКонвертации/Ext/ObjectModule.bsl`, 643–672 и 714–726).

ПКО при массовом создании получает источник, приёмник и наименование
(`АвтонастройкаПравилКонвертацииОбъектов/Ext/ObjectModule.bsl`, `СохранитьПравила`, 227–234;
`ОбщегоНазначения/Ext/Module.bsl`, `глНаименованиеПКО`, 113). В XML это имена типов
(`ВыгрузкаКонвертации/Ext/ObjectModule.bsl`, 842–846).
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from kd2_rules_mcp.authoring.candidates import Candidate, Side, property_candidates
from kd2_rules_mcp.errors import (
    DanglingReferenceError,
    DuplicateRuleError,
    ObjectNotFoundError,
    RuleEditError,
    RuleNotFoundError,
    UnknownFieldError,
)
from kd2_rules_mcp.kd2.model import ExchangeRules, Node
from kd2_rules_mcp.kd2.schema import Scalar, ValueType
from kd2_rules_mcp.structures.queries import NotFound, find_object
from kd2_rules_mcp.validation.address import (
    pks_address,
    pks_segment,
    pkz_address,
    rule_address,
    side_name,
    walk_pks,
)
from kd2_rules_mcp.validation.structure import SIDE_TITLES, SOURCE, TARGET, Structure, is_ref

# Шаг `Порядок` автонастройки ПКС (СохранитьПравилаКС, 796 и 864).
_ORDER_STEP = 50

# Вид верхнего уровня: раздел, тег элемента, поле-адрес.
_TOP: dict[str, tuple[str, str, str]] = {
    "pko": ("ПравилаКонвертацииОбъектов", "Правило", "Код"),
    "pvd": ("ПравилаВыгрузкиДанных", "Правило", "Код"),
    "pod": ("ПравилаОчисткиДанных", "Правило", "Код"),
    "algorithm": ("Алгоритмы", "Алгоритм", "Имя"),
    "query": ("Запросы", "Запрос", "Имя"),
    "parameter": ("Параметры", "Параметр", "Имя"),
}
_NESTED_TAGS = {"pks": "Свойство", "pks_group": "Группа", "pkz": "Значение"}
_KINDS = set(_TOP) | set(_NESTED_TAGS)
# Группа списка при создании — только у ПКО, ПВД и ПОД. У алгоритма, запроса и параметра
# группы в схеме есть, но параметр `group` для них не задаётся.
_GROUPED = frozenset({"pko", "pvd", "pod"})
_STRUCTURE_KINDS = frozenset({"pko", "pvd", "pod", "pks", "pks_group", "pkz"})
_ATTR_IDENTITY = frozenset({"algorithm", "query", "parameter"})
_REF_FIELDS = {
    "pks": "КодПравилаКонвертации",
    "pks_group": "КодПравилаКонвертации",
    "pvd": "КодПравилаКонвертации",
    "parameter": "ПравилоКонвертации",
}
_SOURCE_SKIPPED = (
    "правка применена; проверка по структуре источника не выполнена: структура не передана"
)
_TARGET_SKIPPED = (
    "правка применена; проверка по структуре приёмника не выполнена: структура не передана"
)

FieldValue = Scalar | Mapping[str, Scalar]


@dataclass(slots=True)
class EditResult:
    """Компактный результат правки: адрес, предупреждения и перечни, без документа.

    `skipped` заполняется у ПКО, ПКС, ПКЗ, ПВД и ПОД, когда структура стороны не передана:
    проверка объектов, свойств и значений тогда не выполняется.
    """

    address: str
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Пары кандидатов, которые не стали ПКС (`auto = False`, в том числе «по синониму»).
    not_applied: list[str] = field(default_factory=list)
    # Ссылочный тип приёмника без ровно одного ПКО источника и приёмника.
    unresolved: list[str] = field(default_factory=list)
    # Созданные выключенные ПКС (свойство приёмника без пары).
    disabled: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Snap:
    values: dict[str, Scalar]
    attrs: dict[str, Scalar]
    children: dict[str, dict[str, Scalar]]
    child_names: set[str]


@dataclass(slots=True)
class _Sides:
    source: Structure | None
    target: Structure | None

    def of(self, side: str) -> Structure | None:
        return self.source if side == SOURCE else self.target


def create_rule(
    rules: ExchangeRules,
    kind_name: str,
    key: str,
    fields: Mapping[str, FieldValue] | None = None,
    *,
    owner: str = "",
    source: sqlite3.Connection | None = None,
    target: sqlite3.Connection | None = None,
    group: str = "",
) -> EditResult:
    """Создаёт правило. `key` — код, имя, путь ПКС или имя значения источника ПКЗ.

    `owner` — код ПКО для ПКС, группы ПКС и ПКЗ. `group` — путь кодов групп списка
    через `/` (`Справочники` или `Справочники/Подгруппа`); пусто — корень списка.
    Только для ПКО, ПВД и ПОД, группа должна уже существовать. `source` и `target` —
    структуры сторон; без них проверка объектов, свойств и значений не выполняется.
    """
    _require_key(kind_name, key)
    list_group = _list_group(rules, kind_name, group)
    node = Node.new(kind_name, _tag(kind_name))
    _set_identity(node, kind_name, key)
    _reject_identity_mismatch(kind_name, key, fields)
    if fields:
        _apply_fields(node, fields)
    pko, parent, groups = _place_context(rules, kind_name, key, owner)
    _reject_duplicate(rules, kind_name, key, owner, parent, node)
    if kind_name in ("pks", "pks_group"):
        _reject_segment_mismatch(node, key.rpartition("/")[2])
    sides = _load_sides(source, target)
    _check_dangling(rules, kind_name, node)
    _check_structure(kind_name, node, pko, groups, sides)
    _attach(rules, kind_name, node, pko, parent, list_group)
    return _result(kind_name, node, owner, pko, source, target)


def update_rule(
    rules: ExchangeRules,
    kind_name: str,
    key: str,
    fields: Mapping[str, FieldValue] | None = None,
    *,
    owner: str = "",
    source: sqlite3.Connection | None = None,
    target: sqlite3.Connection | None = None,
) -> EditResult:
    """Меняет только переданные поля. Вложенные правила и остальные поля не трогает."""
    _require_key(kind_name, key)
    container, node, pko, groups = _require(rules, kind_name, key, owner)
    old_code = node.code
    snap = _save(node)
    try:
        if fields:
            _apply_fields(node, fields)
        _after_change(rules, kind_name, node, container, pko, groups, old_code, source, target)
    except Exception:
        _restore(node, snap)
        raise
    return _result(kind_name, node, owner, pko, source, target)


def delete_rule(
    rules: ExchangeRules,
    kind_name: str,
    key: str,
    *,
    owner: str = "",
    source: sqlite3.Connection | None = None,
    target: sqlite3.Connection | None = None,
) -> EditResult:
    """Удаляет правило. ПКО, на которое ссылаются, не удаляется."""
    _require_key(kind_name, key)
    container, node, pko, _groups = _require(rules, kind_name, key, owner)
    if kind_name == "pko":
        _reject_referenced(rules, node)
    result = _result(kind_name, node, owner, pko, source, target)
    container.items.remove(node)
    return result


def find_rule(rules: ExchangeRules, kind_name: str, key: str, owner: str = "") -> Node:
    """Правило по адресу (Д6); нет такого — `RuleNotFoundError`. Документ не меняется."""
    _require_key(kind_name, key)
    return _require(rules, kind_name, key, owner)[1]


def create_pko_with_properties(
    rules: ExchangeRules,
    code: str,
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    source_object: str,
    target_object: str,
    fields: Mapping[str, FieldValue] | None = None,
    *,
    group: str = "",
) -> EditResult:
    """Создаёт ПКО пары объектов и ПКС по `property_candidates`.

    ПКС получают пары «точно» и «синоним КД» с `auto = True`. Для табличной части это группа
    с вложенными ПКС. Свойство приёмника без пары становится выключенным ПКС. Пары с
    `auto = False` и «по синониму» не создаются и попадают в `not_applied`. Для ссылочного
    типа приёмника `КодПравилаКонвертации` заполняется, если в правилах ровно одно ПКО
    с такими типами источника и приёмника; иначе поле пустое, а свойство — в `unresolved`.
    `group` — путь кодов групп списка ПКО через `/`; пусто — корень списка. Группа должна
    уже существовать.
    """
    if not code:
        raise RuleEditError("Пустой адрес правила")
    list_group = _list_group(rules, "pko", group)
    source_row = find_object(source, source_object)
    if isinstance(source_row, NotFound):
        raise ObjectNotFoundError(source_row.message, source_row.suggestions)
    target_row = find_object(target, target_object)
    if isinstance(target_row, NotFound):
        raise ObjectNotFoundError(target_row.message, target_row.suggestions)
    if _find_coded(rules, "pko", code) is not None:
        raise DuplicateRuleError(f"ПКО с кодом «{code}» уже есть")
    candidates = property_candidates(source, target, source_object, target_object)
    if isinstance(candidates, NotFound):
        # Тот же смысл, что у find_object выше: объекта источника или приёмника нет.
        raise ObjectNotFoundError(candidates.message, candidates.suggestions)
    _reject_identity_mismatch("pko", code, fields)
    # глНаименованиеПКО (ОбщегоНазначения, 113): тип источника, двоеточие, синоним.
    node = Node.new("pko", "Правило")
    node.values["Код"] = code
    node.values["Наименование"] = f"{source_row['kind']}: {source_row['synonym']}"
    node.values[SOURCE] = str(source_row["type_name"])
    node.values[TARGET] = str(target_row["type_name"])
    if fields:
        _apply_fields(node, fields)
    _check_structure("pko", node, None, [], _load_sides(source, target))
    container = list_group
    if container is None:
        container = rules.section("ПравилаКонвертацииОбъектов")
    container.items.append(node)
    result = EditResult(rule_address(node))
    try:
        properties = Node.new("pks_list", "Свойства")
        node.children["Свойства"] = properties
        _fill_properties(properties, candidates, rules, "", result)
    except Exception:
        container.items.remove(node)
        raise
    return result


# --- Поля --------------------------------------------------------------------------------------


def _tag(kind_name: str) -> str:
    if kind_name in _TOP:
        return _TOP[kind_name][1]
    if kind_name in _NESTED_TAGS:
        return _NESTED_TAGS[kind_name]
    raise RuleEditError(f"Неизвестный вид правила «{kind_name}»")


def _require_key(kind_name: str, key: str) -> None:
    _tag(kind_name)
    if not key:
        raise RuleEditError("Пустой адрес правила")


def _set_identity(node: Node, kind_name: str, key: str) -> None:
    if kind_name in _ATTR_IDENTITY:
        node.attrs["Имя"] = key
    elif kind_name in _TOP:
        node.values["Код"] = key
    elif kind_name == "pkz":
        node.values[SOURCE] = key


def _reject_identity_mismatch(
    kind_name: str, key: str, fields: Mapping[str, FieldValue] | None
) -> None:
    if not fields:
        return
    if kind_name == "pkz":
        name: str | None = SOURCE
    elif kind_name in _ATTR_IDENTITY:
        name = "Имя"
    elif kind_name in _TOP:
        name = "Код"
    else:
        name = None
    if name is None or name not in fields or fields[name] == key:
        return
    raise RuleEditError(f"Поле «{name}» «{fields[name]}» не совпадает с адресом «{key}»")


def _apply_fields(node: Node, fields: Mapping[str, FieldValue]) -> None:
    for name, value in fields.items():
        if name in node.kind.children and node.kind.children[name].kind == "pks_side":
            _apply_side(node, name, value)
        elif name in node.kind.attr_map:
            node.attrs[name] = _coerce(node.kind.attr_map[name].type, value, node, name)
        elif name in node.kind.leaves:
            node.values[name] = _coerce(node.kind.leaves[name].type, value, node, name)
        else:
            title = "сторону ПКС" if node.kind.name == "pks_side" else node.kind.title
            raise UnknownFieldError(f"Поле «{name}» не входит в {title}")


def _apply_side(node: Node, name: str, value: FieldValue) -> None:
    if not isinstance(value, Mapping):
        raise RuleEditError(f"Поле «{name}» у {node.kind.title} задаётся атрибутами Имя, Вид и Тип")
    side = node.child(name)
    if side is None:
        side = Node.new("pks_side", name)
        node.children[name] = side
    _apply_fields(side, value)


def _coerce(value_type: ValueType, value: FieldValue, node: Node, name: str) -> Scalar:
    if isinstance(value, Mapping):
        raise RuleEditError(f"Поле «{name}» у {node.kind.title} — простое значение")
    if value_type is ValueType.STR:
        if isinstance(value, str):
            return value
        raise RuleEditError(f"Поле «{name}» у {node.kind.title}: ожидается строка")
    if value_type is ValueType.BOOL:
        if isinstance(value, bool):
            return value
        if value in ("true", "false"):
            return value == "true"
        raise RuleEditError(f"Поле «{name}» у {node.kind.title}: ожидается да или нет")
    if isinstance(value, str) and _is_int(value):
        return int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleEditError(f"Поле «{name}» у {node.kind.title}: ожидается целое число")
    return value


def _is_int(value: str) -> bool:
    digits = value[1:] if value.startswith("-") else value
    return bool(digits) and digits.isdigit()


def _save(node: Node) -> _Snap:
    return _Snap(
        dict(node.values),
        dict(node.attrs),
        {name: dict(child.attrs) for name, child in node.children.items()},
        set(node.children),
    )


def _restore(node: Node, snap: _Snap) -> None:
    node.values.clear()
    node.values.update(snap.values)
    node.attrs.clear()
    node.attrs.update(snap.attrs)
    for name in list(node.children):
        if name not in snap.child_names:
            del node.children[name]
    for name, attrs in snap.children.items():
        child = node.children.get(name)
        if child is not None:
            child.attrs.clear()
            child.attrs.update(attrs)


# --- Поиск и вставка ---------------------------------------------------------------------------


def _section_node(rules: ExchangeRules, section: str) -> Node | None:
    return rules.root.children.get(section)


def _walk_coded(rules: ExchangeRules, kind_name: str) -> list[Node]:
    root = _section_node(rules, _TOP[kind_name][0])
    return list(root.walk()) if root is not None else []


def _find_coded(rules: ExchangeRules, kind_name: str, key: str) -> tuple[Node, Node] | None:
    root = _section_node(rules, _TOP[kind_name][0])
    return _find_in(root, key) if root is not None else None


def _find_in(container: Node, key: str) -> tuple[Node, Node] | None:
    for item in container.items:
        if not item.is_group and item.code == key:
            return container, item
        if item.is_group:
            found = _find_in(item, key)
            if found is not None:
                return found
    return None


def _find_pkz(container: Node, source_name: str) -> tuple[Node, Node] | None:
    for item in container.items:
        if item.is_group:
            found = _find_pkz(item, source_name)
            if found is not None:
                return found
        elif str(item.get(SOURCE)) == source_name:
            return container, item
    return None


def _find_pks(container: Node, path: str, prefix: str = "") -> tuple[Node, Node] | None:
    for index, item in enumerate(container.items):
        item_path = f"{prefix}{pks_segment(item, index)}"
        if item_path == path:
            return container, item
        if item.is_group and path.startswith(f"{item_path}/"):
            found = _find_pks(item, path, f"{item_path}/")
            if found is not None:
                return found
    return None


def _require_pko(rules: ExchangeRules, owner: str) -> Node:
    if not owner:
        raise RuleEditError("Для ПКС и ПКЗ нужен код ПКО")
    found = _find_coded(rules, "pko", owner)
    if found is None:
        raise RuleNotFoundError(f"ПКО «{owner}» не найдено")
    return found[1]


def _child_list(owner_node: Node, tag: str, *, create: bool) -> Node | None:
    child = owner_node.child(tag)
    if child is None and create:
        child = Node.new(owner_node.kind.children[tag].kind, tag)
        owner_node.children[tag] = child
    return child


def _group_chain(container: Node, target: Node) -> list[Node] | None:
    """Группы ПКС от корня до `target` включительно."""
    for item in container.items:
        if item is target:
            return [item] if item.is_group else []
        if item.is_group:
            nested = _group_chain(item, target)
            if nested is not None:
                return [item, *nested]
    return None


def _groups_above(pko: Node, parent: Node) -> list[Node]:
    """Группы, внутри которых лежат свойства `parent` (сам `parent`, если это группа)."""
    properties = pko.child("Свойства")
    if properties is None or parent is properties:
        return []
    chain = _group_chain(properties, parent)
    return chain or []


def _place_context(
    rules: ExchangeRules, kind_name: str, key: str, owner: str
) -> tuple[Node | None, Node | None, list[Node]]:
    """ПКО-владелец, контейнер для нового правила и группы ПКС над ним.

    Контейнер `None` у вложенного правила значит, что список (`Свойства` или `Значения`)
    ещё не создан и появится при вставке.
    """
    if kind_name in _TOP:
        return None, None, []
    pko = _require_pko(rules, owner)
    if kind_name == "pkz":
        return pko, _child_list(pko, "Значения", create=False), []
    properties = pko.child("Свойства")
    parent_path, _, _last = key.rpartition("/")
    if not parent_path:
        return pko, properties, []
    if properties is None:
        raise RuleNotFoundError(f"Группа ПКС «{parent_path}» в ПКО «{pko.code}» не найдена")
    found = _find_pks(properties, parent_path)
    if found is None or not found[1].is_group:
        raise RuleNotFoundError(f"Группа ПКС «{parent_path}» в ПКО «{pko.code}» не найдена")
    return pko, found[1], _groups_above(pko, found[1])


def _require(
    rules: ExchangeRules, kind_name: str, key: str, owner: str
) -> tuple[Node, Node, Node | None, list[Node]]:
    """Контейнер, узел, ПКО-владелец и группы ПКС над контейнером."""
    if kind_name in _TOP:
        found = _find_coded(rules, kind_name, key)
        if found is None:
            raise RuleNotFoundError(_missing(kind_name, key, owner))
        return found[0], found[1], None, []
    pko = _require_pko(rules, owner)
    if kind_name == "pkz":
        values = pko.child("Значения")
        found_pkz = _find_pkz(values, key) if values is not None else None
        if found_pkz is None:
            raise RuleNotFoundError(_missing(kind_name, key, owner))
        return found_pkz[0], found_pkz[1], pko, []
    properties = pko.child("Свойства")
    found_pks = _find_pks(properties, key) if properties is not None else None
    if found_pks is None:
        raise RuleNotFoundError(_missing(kind_name, key, owner))
    if found_pks[1].kind.name != kind_name:
        raise RuleNotFoundError(
            f"По пути «{key}» в ПКО «{pko.code}» находится {found_pks[1].kind.title}"
        )
    return found_pks[0], found_pks[1], pko, _groups_above(pko, found_pks[0])


def _missing(kind_name: str, key: str, owner: str) -> str:
    if kind_name == "pko":
        return f"ПКО «{key}» не найдено"
    if kind_name == "pvd":
        return f"ПВД «{key}» не найдено"
    if kind_name == "pod":
        return f"ПОД «{key}» не найдено"
    if kind_name == "algorithm":
        return f"Алгоритм «{key}» не найден"
    if kind_name == "query":
        return f"Запрос «{key}» не найден"
    if kind_name == "parameter":
        return f"Параметр «{key}» не найден"
    if kind_name == "pkz":
        return f"ПКО «{owner}» / ПКЗ {key} не найдено"
    return f"ПКО «{owner}» / ПКС {key} не найдено"


def _reject_duplicate(
    rules: ExchangeRules,
    kind_name: str,
    key: str,
    owner: str,
    parent: Node | None,
    node: Node,
) -> None:
    if kind_name in _TOP:
        if _find_coded(rules, kind_name, key) is not None:
            noun = "именем" if kind_name in _ATTR_IDENTITY else "кодом"
            title = node.kind.title
            raise DuplicateRuleError(f"{title} с {noun} «{key}» уже есть")
        return
    if kind_name == "pkz":
        if parent is not None and _find_pkz(parent, key) is not None:
            raise DuplicateRuleError(f"ПКЗ «{key}» в ПКО «{owner}» уже есть")
        return
    last = key.rpartition("/")[2]
    if parent is not None and _segment_taken(parent, last):
        raise DuplicateRuleError(f"ПКС «{key}» в ПКО «{owner}» уже есть")


def _segment_taken(container: Node, segment: str) -> bool:
    return any(pks_segment(item, index) == segment for index, item in enumerate(container.items))


def _reject_segment_mismatch(node: Node, last: str) -> None:
    segment = pks_segment(node, 0)
    if segment == last:
        return
    raise RuleEditError(
        f"Путь «{last}» не совпадает с именем свойства приёмника «{segment}»."
        " Адрес ПКС — путь группа/…/свойство-приёмник"
    )


def _list_group(rules: ExchangeRules, kind_name: str, group: str) -> Node | None:
    """Группа верхнего списка по пути кодов; пустой путь — корень списка (`None`).

    Ищется по цепочке `items`: узел `is_group` с `code`, равным сегменту пути.
    Группа не создаётся.
    """
    if not group:
        return None
    if kind_name not in _GROUPED:
        raise RuleEditError(f"Группа «{group}» задаётся только для ПКО, ПВД и ПОД")
    section = _TOP[kind_name][0]
    container = _section_node(rules, section)
    for segment in group.split("/"):
        found: Node | None = None
        if container is not None:
            for item in container.items:
                if item.is_group and item.code == segment:
                    found = item
                    break
        if found is None:
            raise RuleNotFoundError(f"Группа «{group}» в списке {section} не найдена")
        container = found
    return container


def _attach(
    rules: ExchangeRules,
    kind_name: str,
    node: Node,
    pko: Node | None,
    parent: Node | None,
    list_group: Node | None,
) -> None:
    if kind_name in _TOP:
        container = list_group if list_group is not None else rules.section(_TOP[kind_name][0])
        container.items.append(node)
        return
    if parent is None:
        if pko is None:
            raise RuleEditError("Некуда добавить правило")
        tag = "Значения" if kind_name == "pkz" else "Свойства"
        parent = _child_list(pko, tag, create=True)
    if parent is None:
        raise RuleEditError("Некуда добавить правило")
    parent.items.append(node)


def _after_change(
    rules: ExchangeRules,
    kind_name: str,
    node: Node,
    container: Node,
    pko: Node | None,
    groups: list[Node],
    old_code: str,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
) -> None:
    if kind_name == "pko" and node.code != old_code:
        refs = _referrers(rules, old_code, node)
        if refs:
            joined = ", ".join(refs)
            raise DanglingReferenceError(
                f"Код ПКО «{old_code}» изменить нельзя: на него ссылаются {joined}"
            )
    if kind_name in _TOP and node.code != old_code:
        for other in _walk_coded(rules, kind_name):
            if other is not node and other.code == node.code:
                noun = "именем" if kind_name in _ATTR_IDENTITY else "кодом"
                raise DuplicateRuleError(f"{node.kind.title} с {noun} «{node.code}» уже есть")
    if kind_name in ("pks", "pks_group"):
        _reject_receiver_clash(container, node)
    if kind_name == "pkz" and pko is not None:
        _reject_pkz_clash(container, node)
    sides = _load_sides(source, target)
    _check_dangling(rules, kind_name, node)
    _check_structure(kind_name, node, pko, groups, sides)


def _reject_receiver_clash(container: Node, node: Node) -> None:
    name = side_name(node, TARGET)
    if not name:
        return
    for item in container.items:
        if item is not node and side_name(item, TARGET) == name:
            raise DuplicateRuleError(f"ПКС «{name}» в этом списке уже есть")


def _reject_pkz_clash(container: Node, node: Node) -> None:
    source_name = str(node.get(SOURCE))
    for item in container.items:
        if item is not node and not item.is_group and str(item.get(SOURCE)) == source_name:
            raise DuplicateRuleError(f"ПКЗ «{source_name}» в этом ПКО уже есть")


def _reject_referenced(rules: ExchangeRules, node: Node) -> None:
    refs = _referrers(rules, node.code, node)
    if not refs:
        return
    raise DanglingReferenceError(
        f"ПКО «{node.code}» удалить нельзя: на него ссылаются {', '.join(refs)}"
    )


def _referrers(rules: ExchangeRules, code: str, skip: Node) -> list[str]:
    if not code:
        return []
    found: list[str] = []
    for pko in rules.pko():
        if pko is skip:
            continue
        properties = pko.child("Свойства")
        if properties is None:
            continue
        for path, item in walk_pks(properties):
            if str(item.get("КодПравилаКонвертации")) == code:
                found.append(pks_address(pko.code, path))
    for pvd in rules.pvd():
        if str(pvd.get("КодПравилаКонвертации")) == code:
            found.append(rule_address(pvd))
    for item in _parameters(rules):
        if str(item.attrs.get("ПравилоКонвертации", "")) == code:
            found.append(rule_address(item))
    return found


def _parameters(rules: ExchangeRules) -> list[Node]:
    root = _section_node(rules, "Параметры")
    return list(root.walk()) if root is not None else []


# --- Висячие ссылки и структуры ---------------------------------------------------------------


def _load_sides(source: sqlite3.Connection | None, target: sqlite3.Connection | None) -> _Sides:
    return _Sides(
        Structure.load(source) if source is not None else None,
        Structure.load(target) if target is not None else None,
    )


def _check_dangling(rules: ExchangeRules, kind_name: str, node: Node) -> None:
    field_name = _REF_FIELDS.get(kind_name)
    if field_name is None:
        return
    ref = _stored(node, field_name)
    if not ref or any(item.code == ref for item in rules.pko()):
        return
    type_side, type_name = _ref_type(kind_name, node)
    listed = rules.pko()
    if type_name:
        listed = [item for item in listed if str(item.get(type_side)) == type_name]
    raise DanglingReferenceError(_dangling_message(field_name, ref, listed, type_name))


def _stored(node: Node, name: str) -> str:
    if name in node.kind.attr_map:
        return str(node.attrs.get(name, "")).strip()
    return str(node.values.get(name, "")).strip()


def _ref_type(kind_name: str, node: Node) -> tuple[str, str]:
    """Сторона ПКО и тип, по которому сужается перечень. Пустой тип — все ПКО."""
    if kind_name in ("pks", "pks_group"):
        side = node.child(TARGET)
        type_name = str(side.attrs.get("Тип", "")).strip() if side is not None else ""
        return TARGET, type_name
    if kind_name == "pvd":
        return SOURCE, str(node.get("ОбъектВыборки")).strip()
    return "", ""


def _dangling_message(field_name: str, ref: str, listed: list[Node], type_name: str) -> str:
    codes = ", ".join(f"«{item.code}»" for item in listed) or "нет"
    if type_name:
        return f"«{field_name}» «{ref}» не найден. ПКО для типа «{type_name}»: {codes}"
    return f"«{field_name}» «{ref}» не найден. Существующие ПКО: {codes}"


def _check_structure(
    kind_name: str,
    node: Node,
    pko: Node | None,
    groups: list[Node],
    sides: _Sides,
) -> None:
    if kind_name not in _STRUCTURE_KINDS:
        return
    if kind_name == "pko":
        _check_type(node, SOURCE, sides.source)
        _check_type(node, TARGET, sides.target)
    elif kind_name in ("pks", "pks_group"):
        if pko is None:
            raise RuleEditError("ПКС без ПКО")
        _check_pks(pko, node, groups, sides)
    elif kind_name == "pkz":
        if pko is None:
            raise RuleEditError("ПКЗ без ПКО")
        _check_pkz(pko, node, sides)
    elif kind_name == "pvd":
        _check_selection(node, sides.source, "источника")
    elif kind_name == "pod":
        _check_selection(node, sides.target, "приёмника")


def _check_type(node: Node, side: str, structure: Structure | None) -> None:
    type_name = str(node.get(side)).strip()
    if structure is None or not type_name:
        return
    if structure.get(type_name) is None:
        raise DanglingReferenceError(f"Тип {SIDE_TITLES[side]} «{type_name}» не найден в структуре")


def _prefixes(groups: list[Node]) -> tuple[dict[str, str], dict[str, str]]:
    prefixes = {SOURCE: "", TARGET: ""}
    parent_kinds = {SOURCE: "", TARGET: ""}
    for group in groups:
        for side in (SOURCE, TARGET):
            name = side_name(group, side)
            if not name:
                prefixes[side] = ""
                parent_kinds[side] = ""
                continue
            prefixes[side] = f"{prefixes[side]}{name}."
            child = group.child(side)
            parent_kinds[side] = str(child.attrs.get("Вид", "")) if child is not None else ""
    return prefixes, parent_kinds


def _check_pks(pko: Node, node: Node, groups: list[Node], sides: _Sides) -> None:
    prefixes, parent_kinds = _prefixes(groups)
    for side in (SOURCE, TARGET):
        structure = sides.of(side)
        name = side_name(node, side)
        type_name = str(pko.get(side)).strip()
        if structure is None or not name or not type_name:
            continue
        obj = structure.get(type_name)
        if obj is None:
            raise DanglingReferenceError(
                f"Тип {SIDE_TITLES[side]} «{type_name}» не найден в структуре"
            )
        group_kind = ""
        if node.is_group:
            child = node.child(side)
            group_kind = str(child.attrs.get("Вид", "")) if child is not None else ""
        full = f"{prefixes[side]}{name}"
        if structure.find(obj, full, parent_kinds[side], group_kind) is None:
            raise DanglingReferenceError(f"Свойства {SIDE_TITLES[side]} «{full}» нет у {type_name}")


def _check_pkz(pko: Node, node: Node, sides: _Sides) -> None:
    for side in (SOURCE, TARGET):
        structure = sides.of(side)
        type_name = str(pko.get(side)).strip()
        value = str(node.get(side)).strip()
        if structure is None or not type_name or not value:
            continue
        obj = structure.get(type_name)
        if obj is None:
            raise DanglingReferenceError(
                f"Тип {SIDE_TITLES[side]} «{type_name}» не найден в структуре"
            )
        if value not in structure.values(obj):
            raise DanglingReferenceError(
                f"Значения {SIDE_TITLES[side]} «{value}» нет у {type_name}"
            )


def _check_selection(node: Node, structure: Structure | None, title: str) -> None:
    if structure is None:
        return
    selection = str(node.get("ОбъектВыборки")).strip()
    if selection and structure.get(selection) is None:
        raise DanglingReferenceError(f"Объект выборки «{selection}» не найден в структуре {title}")


def _result(
    kind_name: str,
    node: Node,
    owner: str,
    pko: Node | None,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
) -> EditResult:
    return EditResult(
        _address(kind_name, node, owner, pko),
        skipped=_skipped(kind_name, source, target),
    )


def _skipped(
    kind_name: str, source: sqlite3.Connection | None, target: sqlite3.Connection | None
) -> list[str]:
    if kind_name not in _STRUCTURE_KINDS:
        return []
    skipped: list[str] = []
    if source is None:
        skipped.append(_SOURCE_SKIPPED)
    if target is None:
        skipped.append(_TARGET_SKIPPED)
    return skipped


def _address(kind_name: str, node: Node, owner: str, pko: Node | None) -> str:
    if kind_name in ("pks", "pks_group") and pko is not None:
        properties = pko.child("Свойства")
        if properties is not None:
            for path, item in walk_pks(properties):
                if item is node:
                    return pks_address(pko.code, path)
        return pks_address(owner, "")
    if kind_name == "pkz":
        return pkz_address(owner or (pko.code if pko is not None else ""), str(node.get(SOURCE)))
    return rule_address(node)


# --- Массовые ПКС ------------------------------------------------------------------------------


def _fill_properties(
    container: Node,
    candidates: list[Candidate],
    rules: ExchangeRules,
    prefix: str,
    result: EditResult,
) -> None:
    order = 0
    for candidate in candidates:
        target = candidate.target
        if target is None:
            continue
        path = f"{prefix}{target.name}"
        if candidate.source is not None and not candidate.auto:
            _collect_skipped(candidate, prefix, result)
            continue
        group = _is_group(candidate)
        node = _property_rule(candidate, group)
        node.values["Порядок"] = order
        order += _ORDER_STEP
        if candidate.source is None:
            node.attrs["Отключить"] = True
            result.disabled.append(path)
        code = _conversion_code(rules, candidate.source, target, path, result)
        if code:
            node.values["КодПравилаКонвертации"] = code
        container.items.append(node)
        if group:
            _fill_properties(node, list(candidate.children), rules, f"{path}/", result)


def _is_group(candidate: Candidate) -> bool:
    kinds = {side.kind for side in (candidate.source, candidate.target) if side is not None}
    # Группа ПКС — табличная часть или строка с подчинёнными (СохранитьПравилаКС, 802–806).
    return "ТабличнаяЧасть" in kinds or bool(candidate.children)


def _property_rule(candidate: Candidate, group: bool) -> Node:
    node = Node.new("pks_group" if group else "pks", "Группа" if group else "Свойство")
    node.children[SOURCE] = _side_node(SOURCE, candidate.source, group=group)
    node.children[TARGET] = _side_node(TARGET, candidate.target, group=group)
    return node


def _side_node(tag: str, side: Side | None, *, group: bool) -> Node:
    """Сторона ПКС: имя, вид и тип единственного типа (ВК:643–672; у группы без типа, 714–726)."""
    node = Node.new("pks_side", tag)
    if side is None:
        return node
    node.attrs["Имя"] = side.name
    node.attrs["Вид"] = side.kind
    if not group and len(side.types) == 1:
        node.attrs["Тип"] = side.types[0]
    return node


def _conversion_code(
    rules: ExchangeRules,
    source: Side | None,
    target: Side,
    path: str,
    result: EditResult,
) -> str:
    """Код ПКО, если тип приёмника ссылочный и такая пара типов есть ровно у одного ПКО.

    КД ищет ПКО по обоим типам (`ОпределитьПоТипамНаличиеПКО`, 702–710) и берёт первое.
    Здесь при нуле или нескольких совпадениях код остаётся пустым.
    """
    if len(target.types) != 1 or not is_ref(target.types[0]):
        return ""
    target_type = target.types[0]
    source_type = source.types[0] if source is not None and len(source.types) == 1 else ""
    if not source_type:
        result.unresolved.append(
            f"{path}: ссылочный тип приёмника «{target_type}», тип источника не задан"
        )
        return ""
    matches = [
        item
        for item in rules.pko()
        if str(item.get(SOURCE)) == source_type and str(item.get(TARGET)) == target_type
    ]
    if len(matches) == 1:
        return matches[0].code
    codes = ", ".join(f"«{item.code}»" for item in matches) or "нет"
    result.unresolved.append(
        f"{path}: нет однозначного ПКО для «{source_type}» → «{target_type}»"
        f" (найдено {len(matches)}: {codes})"
    )
    return ""


def _collect_skipped(candidate: Candidate, prefix: str, result: EditResult) -> None:
    target = candidate.target
    if target is None:
        return
    path = f"{prefix}{target.name}"
    if candidate.source is not None and not candidate.auto:
        result.not_applied.append(_pair_label(candidate, path))
    for child in candidate.children:
        _collect_skipped(child, f"{path}/", result)


def _pair_label(candidate: Candidate, path: str) -> str:
    source_name = candidate.source.name if candidate.source is not None else ""
    target_name = candidate.target.name if candidate.target is not None else ""
    note = f": {candidate.note}" if candidate.note else ""
    return f"{path}: «{source_name}» → «{target_name}» ({candidate.confidence.value}){note}"
