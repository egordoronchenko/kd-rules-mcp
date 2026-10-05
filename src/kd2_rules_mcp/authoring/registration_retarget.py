"""Перенацеливание готовых правил регистрации на другой план обмена.

Имя плана в файле — атрибут ``Имя`` элемента ``ПланОбмена``
(``reference/kd2-cfg/DataProcessors/ВыгрузкаРегистрации/Ext/ObjectModule.bsl``,
строки 89–99; сам атрибут — строка 93). Текст элемента писатель берёт из
``ПланОбмена.Наименование`` (строка 95). Если в файле там стоит тип
``ПланОбменаСсылка.<старое имя>``, текст заменяется на новое имя плана:
наименования целевого плана у функции нет.

Реквизит узла листа — ``СвойствоПланаОбмена`` (строка 332): шапка или
``[ТабличнаяЧасть].Реквизит``. Хвост после первой точки — разыменование,
его не переименовываем (читатель, ``ЗагрузкаПравилРегистрацииОбъектов``,
строки 471–484). Ключ отображения ``Имя`` — только реквизит шапки,
``[ТЧ]`` — табличная часть, ``[ТЧ].Имя`` — её реквизит. Связанная таблица —
``ТаблицаСвойствПланаОбмена`` (строки 342–343 и 402–420): у табличной части
наименование ``[Имя]``, у реквизита — имя; ``Тип`` и ``Вид`` сохраняются.
``РеквизитРежимаВыгрузки`` (строка 220) переименовывается тем же отображением
и сверяется с шапкой целевого плана: исполнитель подставляет его в запрос узлов
(``ОбменДаннымиСобытия``, ``МассивУзловПоЗначениямСвойств``, строки 2198–2215)
и читает ``Узел[ПРО.ИмяРеквизитаФлага]`` (строка 2884).

Тексты обработчиков и алгоритмы значений не переписываются. Старое имя плана
и старые имена реквизитов узла в коде (не в комментариях) — замечание.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import cast

from lxml import etree

from kd2_rules_mcp.authoring.registration import snapshot_registration
from kd2_rules_mcp.errors import (
    DuplicateTargetPropertyError,
    InvalidRegistrationNameError,
    NotRegistrationRulesError,
    PropertyNameClashError,
    RegistrationRetargetError,
    RetargetInvariantError,
)
from kd2_rules_mcp.kd2.diff import diff_rules
from kd2_rules_mcp.kd2.model import Node, RegistrationRules, RulesDocument
from kd2_rules_mcp.structures.queries import ObjectCard
from kd2_rules_mcp.validation.address import pro_addresses
from kd2_rules_mcp.validation.registration import (
    _header_field,
    _Index,
    _Obj,
    _plan_field_exists,
    _Prop,
    _split_plan_property,
)

# Буква или «_», дальше буквы, цифры и «_». Так платформа именует метаданные.
_IDENTIFIER = re.compile(r"[^\W\d]\w*")
_MAPPING_KEY = re.compile(r"[^\W\d]\w*|\[([^\W\d]\w*)](?:\.([^\W\d]\w*))?")
_PLAN_TYPE_PREFIX = "ПланОбменаСсылка."
_PLAN_FILTER = "ОтборПоСвойствамПланаОбмена"
_OBJECT_FILTER = "ОтборПоСвойствамОбъекта"
_PLAN_PROPERTY = "СвойствоПланаОбмена"
_PLAN_TABLE = "ТаблицаСвойствПланаОбмена"
_OBJECT_TABLE = "ТаблицаСвойствОбъекта"
_HANDLERS = (
    "ПередОбработкой",
    "ПриОбработке",
    "ПриОбработкеДополнительный",
    "ПослеОбработки",
)
_UNLOAD_MODE = "РеквизитРежимаВыгрузки"
_ALLOWED_DIFF = frozenset({"ПланОбмена.Имя", "ПланОбмена", _PLAN_FILTER, _UNLOAD_MODE})


@dataclass(frozen=True, slots=True)
class RetargetRule:
    """Итог одного правила: адрес и число листьев отбора."""

    address: str
    renamed: int
    untouched: int


@dataclass(frozen=True, slots=True)
class RetargetRemark:
    """Реквизит узла после замены не найден у целевого плана."""

    address: str
    leaf: str
    property_name: str
    message: str


@dataclass(frozen=True, slots=True)
class CodeMention:
    """Старое имя плана или реквизита узла осталось в коде. Код не переписан."""

    address: str
    event: str
    line: int
    name: str
    message: str


@dataclass(frozen=True, slots=True)
class RetargetResult:
    """Новая модель и отчёт замены. Исходный документ не меняется.

    ``code_mentions`` больше нуля — перенос неполон: в обработчиках или
    алгоритмах остались старые имена. Сам код функция не меняет.
    """

    document: RegistrationRules
    rules: tuple[RetargetRule, ...]
    unused: tuple[str, ...]
    remarks: tuple[RetargetRemark, ...]
    only_expected: bool
    code_mentions: int
    mentions: tuple[CodeMention, ...]


def retarget_registration(
    rules: RulesDocument,
    *,
    plan_name: str,
    node_properties: Mapping[str, str],
    target_plan: ObjectCard | None = None,
) -> RetargetResult:
    """Копия правил регистрации с новым именем плана и реквизитами узла.

    ``node_properties`` — «имя в старом плане → имя в целевом». Ключ ``Имя``
    относится к реквизиту шапки, ``[ТЧ]`` — к табличной части, ``[ТЧ].Имя`` —
    к реквизиту табличной части. Заменяется голова ``СвойствоПланаОбмена``,
    строки ``ТаблицаСвойствПланаОбмена`` и ``РеквизитРежимаВыгрузки``.
    Имена свойств объекта не меняются. Отборы, группы, сравнения, алгоритмы
    значений, обработчики, отключённые правила и неизвестные узлы остаются
    как были. Код со старыми именами не переписывается: он попадает в
    ``mentions``, а ключ, встретившийся только там, — не в ``unused``.

    ``target_plan`` — карточка плана из структуры. Нет реквизита или
    табличной части, на которые после замены ссылается лист или режим
    выгрузки, — замечание, не исключение. Сверяется только реквизит узла,
    без разыменования: полный путь — уже ``registration.plan_property``
    при ``rules_validate``.
    """
    document = _registration(rules)
    name = _check_identifier(plan_name, "плана обмена")
    if target_plan is not None and target_plan.kind != "ПланОбмена":
        raise RegistrationRetargetError(
            f"Сведения целевого плана относятся к «{target_plan.kind}», а не к плану обмена"
        )
    filter_names, unload_names = _node_names(document)
    mapping = _mapping(node_properties, filter_names, unload_names)
    old_plan = document.exchange_plan
    cloned = _clone(document)
    _set_plan_name(cloned, name, old_plan)
    stats, applied = _rename_filters(cloned, mapping)
    _rename_unload_modes(cloned, mapping, applied)
    mentions = _code_mentions(cloned, old_plan, mapping)
    seen_in_code = _mapping_keys_in_code(mapping, mentions)
    result = RetargetResult(
        document=cloned,
        rules=tuple(stats),
        unused=tuple(key for key in mapping if key not in applied and key not in seen_in_code),
        remarks=_remarks(cloned, name, target_plan),
        only_expected=True,
        code_mentions=len(mentions),
        mentions=mentions,
    )
    _assert_only_expected(document, result.document, name, mapping)
    return result


def _registration(rules: RulesDocument) -> RegistrationRules:
    if isinstance(rules, RegistrationRules):
        return rules
    found = rules.root_tag if isinstance(rules, RulesDocument) else type(rules).__name__
    raise NotRegistrationRulesError(
        f"Ожидались правила регистрации «ПравилаРегистрации», найден корень «{found}»"
    )


def _check_identifier(value: object, what: str) -> str:
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
        return value
    shown = value if isinstance(value, str) else type(value).__name__
    raise InvalidRegistrationNameError(
        f"Имя {what} «{shown}» пустое или недопустимое: "
        "нужна буква или «_», дальше буквы, цифры и «_»"
    )


def _mapping(
    raw: Mapping[str, str], filter_names: set[str], unload_names: set[str]
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise InvalidRegistrationNameError("Отображение реквизитов узла должно быть словарём")
    mapping: dict[str, str] = {}
    by_target: dict[str, list[str]] = {}
    for key, value in raw.items():
        old = _check_mapping_key(key)
        new = _check_identifier(value, "реквизита узла")
        mapping[old] = new
        by_target.setdefault(new, []).append(old)
    for new, sources in by_target.items():
        if len(sources) > 1:
            listed = "», «".join(sources)
            raise DuplicateTargetPropertyError(
                f"Реквизиты «{listed}» переименовываются в одно имя «{new}»"
            )
    _check_clash(mapping, filter_names, unload_names)
    return mapping


def _check_mapping_key(value: object) -> str:
    if isinstance(value, str) and _MAPPING_KEY.fullmatch(value):
        return value
    shown = value if isinstance(value, str) else type(value).__name__
    raise InvalidRegistrationNameError(
        f"Ключ отображения «{shown}» пустой или недопустимый: "
        "нужно имя реквизита шапки, «[ТабличнаяЧасть]» или «[ТабличнаяЧасть].Реквизит»"
    )


def _check_clash(mapping: dict[str, str], filter_names: set[str], unload_names: set[str]) -> None:
    """Замена одновременная: А→Б и Б→В не схлопывают А и Б в В.

    Режим выгрузки переписывается тем же отображением и занимает уже новое имя.
    """
    occupied = filter_names | unload_names
    for old, new in mapping.items():
        if old == new:
            continue
        stays = [name for name in occupied if mapping.get(name, name) == new and name != old]
        if stays:
            raise PropertyNameClashError(
                f"Имя «{new}» уже используется другим реквизитом узла в этих правилах"
            )


def _node_names(rules: RegistrationRules) -> tuple[set[str], set[str]]:
    """Имена реквизитов узла в листьях отбора и в реквизите режима выгрузки."""
    filter_names: set[str] = set()
    unload_names: set[str] = set()
    for rule in rules.rules():
        mode = str(rule.values.get(_UNLOAD_MODE, "")).strip()
        if mode:
            unload_names.add(mode)
        for _path, item in _leaves(rule, _PLAN_FILTER):
            tabular, head = _node_parts(str(item.values.get(_PLAN_PROPERTY, "")))
            if tabular:
                filter_names.add(f"[{tabular}]")
                if head:
                    filter_names.add(f"[{tabular}].{head}")
            elif head:
                filter_names.add(head)
    return filter_names, unload_names


def _clone(rules: RegistrationRules) -> RegistrationRules:
    return RegistrationRules(_copy_node(rules.root), rules.style, rules.origin)


def _copy_node(node: Node) -> Node:
    return Node(
        kind=node.kind,
        tag=node.tag,
        attrs=dict(node.attrs),
        values=dict(node.values),
        children={tag: _copy_node(child) for tag, child in node.children.items()},
        items=[_copy_node(item) for item in node.items],
        text=node.text,
        unknown=[deepcopy(element) for element in node.unknown],
        leading_text=node.leading_text,
        trailing_text=node.trailing_text,
    )


def _set_plan_name(rules: RegistrationRules, plan_name: str, old_name: str) -> None:
    node = rules.root.child("ПланОбмена")
    if node is None:
        node = Node.new("exchange_plan", "ПланОбмена")
        rules.root.children["ПланОбмена"] = node
    node.attrs["Имя"] = plan_name
    text = node.text or ""
    if text.strip() == f"{_PLAN_TYPE_PREFIX}{old_name}":
        node.text = plan_name


def _rename_filters(
    rules: RegistrationRules, mapping: Mapping[str, str]
) -> tuple[list[RetargetRule], set[str]]:
    applied: set[str] = set()
    stats: list[RetargetRule] = []
    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        renamed = 0
        untouched = 0
        for _path, item in _leaves(rule, _PLAN_FILTER):
            if _rename_plan_leaf(item, mapping, applied):
                renamed += 1
            else:
                untouched += 1
        untouched += len(_leaves(rule, _OBJECT_FILTER))
        stats.append(RetargetRule(address, renamed, untouched))
    return stats, applied


def _rename_unload_modes(
    rules: RegistrationRules, mapping: Mapping[str, str], applied: set[str]
) -> None:
    for rule in rules.rules():
        old = str(rule.values.get(_UNLOAD_MODE, "")).strip()
        if not old or old not in mapping:
            continue
        applied.add(old)
        new = mapping[old]
        if new != old:
            rule.values[_UNLOAD_MODE] = new


def _rename_plan_leaf(item: Node, mapping: Mapping[str, str], applied: set[str]) -> bool:
    old = str(item.values.get(_PLAN_PROPERTY, ""))
    tabular, head = _node_parts(old)
    if tabular:
        section_key = f"[{tabular}]"
        if section_key in mapping:
            applied.add(section_key)
        if head:
            attr_key = f"[{tabular}].{head}"
            if attr_key in mapping:
                applied.add(attr_key)
    elif head in mapping:
        applied.add(head)
    new = _rename_plan_property(old, mapping)
    if new != old and (_PLAN_PROPERTY in item.values or new):
        item.values[_PLAN_PROPERTY] = new
    table = item.child(_PLAN_TABLE)
    if table is not None:
        names = [str(row.values.get("Наименование", "")) for row in table.items]
        renamed = _rename_row_names(old, names, mapping)
        for row, name in zip(table.items, renamed, strict=True):
            if name != row.values.get("Наименование", ""):
                row.values["Наименование"] = name
    return new != old


def _node_parts(raw: str) -> tuple[str, str]:
    """Табличная часть и первый реквизит узла. Разыменование отбрасывается."""
    tabular, attribute = _split_plan_property(raw)
    head = attribute.split(".", 1)[0] if attribute else ""
    return tabular, head


def _rename_plan_property(raw: str, mapping: Mapping[str, str]) -> str:
    tabular, attribute = _split_plan_property(raw)
    if not attribute and not tabular:
        return raw
    segments = attribute.split(".") if attribute else []
    head = segments[0] if segments else ""
    rest = segments[1:]
    if tabular:
        new_tab = mapping.get(f"[{tabular}]", tabular)
        new_head = mapping.get(f"[{tabular}].{head}", head) if head else ""
    else:
        new_tab = ""
        new_head = mapping.get(head, head) if head else ""
    new_attr = ".".join([new_head, *rest]) if segments else ""
    if new_tab:
        return f"[{new_tab}].{new_attr}" if new_attr else f"[{new_tab}]"
    return new_attr


def _rename_row_names(raw: str, names: list[str], mapping: Mapping[str, str]) -> list[str]:
    """Строки таблицы в порядке писателя: ``[ТабличнаяЧасть]``, затем реквизит.

    Дальше идут разыменования — их наименования не меняются. Нет строки
    табличной части — ожидается сразу реквизит.
    """
    tabular, head = _node_parts(raw)
    expected: list[tuple[str, str]] = []
    if tabular:
        expected.append(("tabular", tabular))
    if head:
        expected.append(("attribute", head))
    result = list(names)
    cursor = 0
    for index, current in enumerate(result):
        if cursor >= len(expected):
            break
        kind, name = expected[cursor]
        if kind == "tabular" and current in (f"[{name}]", name):
            new_name = mapping.get(f"[{name}]", name)
            bracket = current.startswith("[") and current.endswith("]")
            result[index] = f"[{new_name}]" if bracket else new_name
            cursor += 1
        elif kind == "attribute" and current == name:
            key = f"[{tabular}].{name}" if tabular else name
            result[index] = mapping.get(key, name)
            cursor += 1
    return result


def _leaves(rule: Node, tag: str) -> list[tuple[tuple[int, ...], Node]]:
    container = rule.child(tag)
    found: list[tuple[tuple[int, ...], Node]] = []
    if container is None:
        return found

    def visit(node: Node, prefix: tuple[int, ...]) -> None:
        for index, item in enumerate(node.items):
            path = (*prefix, index)
            if item.tag == "Группа":
                visit(item, path)
            else:
                found.append((path, item))

    visit(container, ())
    return found


def _leaf_label(tag: str, path: tuple[int, ...]) -> str:
    return "/".join((tag, *(str(index) for index in path)))


def _remarks(
    rules: RegistrationRules, plan_name: str, target_plan: ObjectCard | None
) -> tuple[RetargetRemark, ...]:
    if target_plan is None:
        return ()
    obj = _obj_from_card(target_plan)
    remarks: list[RetargetRemark] = []
    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        for path, item in _leaves(rule, _PLAN_FILTER):
            raw = str(item.values.get(_PLAN_PROPERTY, ""))
            missing = _missing_node_property(obj, raw)
            if missing is None:
                continue
            leaf = _leaf_label(_PLAN_FILTER, path)
            remarks.append(
                RetargetRemark(
                    address=address,
                    leaf=leaf,
                    property_name=missing,
                    message=(
                        f"{address}, лист {leaf}: реквизит узла «{missing}» "
                        f"не найден у плана обмена «{plan_name}»"
                    ),
                )
            )
        mode = str(rule.values.get(_UNLOAD_MODE, "")).strip()
        if not mode:
            continue
        missing_mode = _missing_node_property(obj, mode)
        if missing_mode is None:
            continue
        remarks.append(
            RetargetRemark(
                address=address,
                leaf=_UNLOAD_MODE,
                property_name=missing_mode,
                message=(
                    f"{address}, {_UNLOAD_MODE}: реквизит узла «{missing_mode}» "
                    f"не найден у плана обмена «{plan_name}»"
                ),
            )
        )
    return tuple(remarks)


def _obj_from_card(card: ObjectCard) -> _Obj:
    properties = {
        item.path: _Prop(item.kind, item.is_group, item.types, item.unresolved)
        for item in card.properties
    }
    return _Obj(card.name, card.type_name, card.kind, properties)


def _missing_node_property(obj: _Obj, raw: str) -> str | None:
    """Имя недостающего реквизита или табличной части. None — реквизит узла есть.

    Совпадает с ``_plan_field_exists`` на пути без разыменования. Расхождение
    с этой проверкой — ошибка функции, а не замечание.
    """
    if not raw:
        return None
    tabular, head = _node_parts(raw)
    owned = _owned_reference(tabular, head)
    exists = bool(owned) and _plan_field_exists(cast(_Index, None), obj, owned)
    missing = _missing_piece(obj, tabular, head, raw)
    if exists != (missing is None):
        raise RetargetInvariantError(
            "Сверка реквизита узла разошлась с проверкой registration.plan_property"
        )
    return missing


def _owned_reference(tabular: str, head: str) -> str:
    if tabular and head:
        return f"[{tabular}].{head}"
    return head


def _missing_piece(obj: _Obj, tabular: str, head: str, raw: str) -> str | None:
    if tabular:
        section = obj.properties.get(tabular)
        if section is None or section.kind != "ТабличнаяЧасть":
            return tabular
        if not head:
            return f"[{tabular}]"
        field = obj.properties.get(f"{tabular}.{head}")
        if field is None or field.is_group:
            return f"[{tabular}].{head}"
        return None
    if not head:
        return raw
    if _header_field(obj, head) is None:
        return head
    return None


def _code_mentions(
    rules: RegistrationRules, plan_name: str, mapping: Mapping[str, str]
) -> tuple[CodeMention, ...]:
    """Старые имена в коде обработчиков и алгоритмов. Комментарии не считаются."""
    names = _code_names(plan_name, mapping)
    if not names:
        return ()
    found: list[CodeMention] = []
    for address, rule in zip(pro_addresses(rules.rules()), rules.rules(), strict=True):
        for event in _HANDLERS:
            text = rule.values.get(event, "")
            if isinstance(text, str) and text:
                found.extend(_mentions_in(address, event, text, names))
        for path, item in _leaves(rule, _OBJECT_FILTER):
            if str(item.values.get("Вид", "")) != "АлгоритмЗначения":
                continue
            text = item.values.get("ЗначениеКонстанты", "")
            if not isinstance(text, str) or not text:
                continue
            leaf = _leaf_label(_OBJECT_FILTER, path)
            found.extend(_mentions_in(address, f"АлгоритмЗначения {leaf}", text, names))
        for path, item in _leaves(rule, _PLAN_FILTER):
            if str(item.values.get("Вид", "")) != "АлгоритмЗначения":
                continue
            text = item.values.get("ЗначениеКонстанты", "")
            if not isinstance(text, str) or not text:
                continue
            leaf = _leaf_label(_PLAN_FILTER, path)
            found.extend(_mentions_in(address, f"АлгоритмЗначения {leaf}", text, names))
    return tuple(found)


def _code_names(plan_name: str, mapping: Mapping[str, str]) -> dict[str, str]:
    """Идентификатор в коде → имя, которое попадёт в замечание."""
    names: dict[str, str] = {}
    if plan_name:
        names[plan_name] = plan_name
    for key in mapping:
        token = _key_token(key)
        names.setdefault(token, token)
    return names


def _key_token(key: str) -> str:
    """Имя, которое пишут в коде: у ``[ТЧ].Имя`` это ``Имя``, у ``[ТЧ]`` — ``ТЧ``."""
    if key.startswith("[") and key.endswith("]") and "]." not in key:
        return key[1:-1]
    if key.startswith("[") and "]." in key:
        return key.split("].", 1)[1]
    return key


def _mapping_keys_in_code(
    mapping: Mapping[str, str], mentions: tuple[CodeMention, ...]
) -> set[str]:
    found = {item.name for item in mentions}
    return {key for key in mapping if _key_token(key) in found}


def _mentions_in(
    address: str, event: str, text: str, names: Mapping[str, str]
) -> list[CodeMention]:
    patterns = {token: re.compile(rf"(?<![\w]){re.escape(token)}(?![\w])") for token in names}
    found: list[CodeMention] = []
    for line_no, line in enumerate(_code_without_comments(text), 1):
        for token, pattern in patterns.items():
            if pattern.search(line) is None:
                continue
            label = names[token]
            found.append(
                CodeMention(
                    address=address,
                    event=event,
                    line=line_no,
                    name=label,
                    message=(
                        f"{address}, {event}, строка {line_no}: в коде осталось имя «{label}»"
                    ),
                )
            )
    return found


def _code_without_comments(text: str) -> list[str]:
    """Строки без комментариев ``//``. В строке так же, если перед ``//`` не идентификатор."""
    lines: list[str] = []
    in_string = False
    for raw in text.splitlines():
        out: list[str] = []
        index = 0
        while index < len(raw):
            if in_string:
                if raw.startswith('""', index):
                    out.append('""')
                    index += 2
                    continue
                if raw[index] == '"':
                    in_string = False
                    out.append('"')
                    index += 1
                    continue
                if raw.startswith("//", index) and _comment_mark(raw, index):
                    break
                out.append(raw[index])
                index += 1
                continue
            if raw.startswith("//", index):
                break
            if raw[index] == '"':
                in_string = True
            out.append(raw[index])
            index += 1
        lines.append("".join(out))
    return lines


def _comment_mark(line: str, index: int) -> bool:
    if index == 0:
        return True
    previous = line[index - 1]
    return not (previous.isalnum() or previous == "_")


def _assert_only_expected(
    source: RegistrationRules,
    result: RegistrationRules,
    plan_name: str,
    mapping: Mapping[str, str],
) -> None:
    problems: list[str] = []
    for change in diff_rules(source, result, include_header=True):
        if change.change != "changed" or change.field not in _ALLOWED_DIFF:
            label = change.field or change.change
            problems.append(f"{change.address or 'заголовок'}: {label}")
        elif change.field == "ПланОбмена.Имя" and change.new != plan_name:
            problems.append(f"имя плана в диффе «{change.new}»")
        elif change.field == "ПланОбмена" and change.new != plan_name:
            problems.append(f"текст плана в диффе «{change.new}»")
        elif change.field == _UNLOAD_MODE:
            previous = change.old or ""
            if change.new != mapping.get(previous, previous):
                problems.append(f"режим выгрузки в диффе «{change.new}»")
    problems.extend(_snapshot_problems(source, result, mapping))
    problems.extend(_leaf_problems(source, result, mapping))
    if problems:
        shown = "; ".join(problems[:8])
        raise RetargetInvariantError(
            "Перенацеливание изменило не только имя плана и реквизиты узла: " + shown
        )


def _snapshot_problems(
    source: RegistrationRules, result: RegistrationRules, mapping: Mapping[str, str]
) -> list[str]:
    left = snapshot_registration(source)
    right = snapshot_registration(result)
    if [item["address"] for item in left] != [item["address"] for item in right]:
        return ["снимки правил разошлись по адресам"]
    problems: list[str] = []
    for old, new in zip(left, right, strict=True):
        address = str(old["address"])
        if old["handlers"] != new["handlers"] or old["object"] != new["object"]:
            problems.append(f"{address}: снимок обработчиков или отбора объекта")
        expected = [_rename_leaf_key(str(key), mapping) for key in old["plan"]]
        if expected != list(new["plan"]):
            problems.append(f"{address}: снимок отбора плана")
    return problems


def _rename_leaf_key(key: str, mapping: Mapping[str, str]) -> str:
    parts: list[str] = []
    for part in key.split(";"):
        tag, separator, value = part.partition("=")
        if separator and tag == _PLAN_PROPERTY:
            value = _rename_plan_property(value, mapping)
        parts.append(f"{tag}={value}" if separator else part)
    return ";".join(parts)


def _leaf_problems(
    source: RegistrationRules, result: RegistrationRules, mapping: Mapping[str, str]
) -> Iterator[str]:
    source_rules = source.rules()
    result_rules = result.rules()
    if len(source_rules) != len(result_rules):
        yield "число правил"
        return
    for left, right in zip(source_rules, result_rules, strict=True):
        for name in _HANDLERS:
            if left.values.get(name) != right.values.get(name):
                yield f"{name}: текст обработчика"
        if _unknown_blobs(left) != _unknown_blobs(right):
            yield "неизвестные узлы"
        for tag in (_PLAN_FILTER, _OBJECT_FILTER):
            if _shape(left.child(tag)) != _shape(right.child(tag)):
                yield f"{tag}: группы"
        yield from _compare_tree(left, right, _PLAN_FILTER, mapping)
        yield from _compare_tree(left, right, _OBJECT_FILTER, mapping)


def _compare_tree(left: Node, right: Node, tag: str, mapping: Mapping[str, str]) -> Iterator[str]:
    source_leaves = _leaves(left, tag)
    result_leaves = _leaves(right, tag)
    if len(source_leaves) != len(result_leaves):
        yield f"{tag}: число листьев"
        return
    plan = tag == _PLAN_FILTER
    for (path, source), (_, result) in zip(source_leaves, result_leaves, strict=True):
        label = _leaf_label(tag, path)
        if plan:
            old = str(source.values.get(_PLAN_PROPERTY, ""))
            new = str(result.values.get(_PLAN_PROPERTY, ""))
            if new != _rename_plan_property(old, mapping):
                yield f"{label}: свойство плана"
            if _without(source, _PLAN_PROPERTY) != _without(result, _PLAN_PROPERTY):
                yield f"{label}: поля листа плана"
            yield from _compare_table(source, result, _PLAN_TABLE, label, old, mapping)
            yield from _compare_table(source, result, _OBJECT_TABLE, label, "", None)
        elif source.values != result.values:
            yield f"{label}: поля листа объекта"
        else:
            yield from _compare_table(source, result, _OBJECT_TABLE, label, "", None)


def _without(node: Node, skip: str) -> dict[str, object]:
    return {key: value for key, value in node.values.items() if key != skip}


def _compare_table(
    source: Node,
    result: Node,
    tag: str,
    label: str,
    raw: str,
    mapping: Mapping[str, str] | None,
) -> Iterator[str]:
    if not tag:
        return
    left = source.child(tag)
    right = result.child(tag)
    left_rows = left.items if left is not None else []
    right_rows = right.items if right is not None else []
    if len(left_rows) != len(right_rows):
        yield f"{label}: число строк {tag}"
        return
    names = [str(row.values.get("Наименование", "")) for row in left_rows]
    expected = _rename_row_names(raw, names, mapping) if mapping is not None else names
    for index, (old, new) in enumerate(zip(left_rows, right_rows, strict=True)):
        if str(new.values.get("Наименование", "")) != expected[index]:
            yield f"{label}: наименование строки {tag}"
        if old.values.get("Тип") != new.values.get("Тип"):
            yield f"{label}: тип строки {tag}"
        if old.values.get("Вид") != new.values.get("Вид"):
            yield f"{label}: вид строки {tag}"


def _shape(node: Node | None) -> tuple[object, ...]:
    """Порядок листьев и операторы групп. Сами имена свойств сюда не входят."""
    if node is None:
        return ()
    items: list[object] = []
    for item in node.items:
        if item.tag == "Группа":
            operator = str(item.values.get("БулевоЗначениеГруппы", ""))
            items.append((item.tag, operator, _shape(item)))
        else:
            items.append((item.tag,))
    return tuple(items)


def _unknown_blobs(node: Node) -> tuple[bytes, ...]:
    found = [etree.tostring(element) for element in node.unknown]
    for child in node.children.values():
        found.extend(_unknown_blobs(child))
    for item in node.items:
        found.extend(_unknown_blobs(item))
    return tuple(found)
