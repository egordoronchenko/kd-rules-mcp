"""Опись КД 2 без изменения дерева; адреса совпадают с навигацией rules_list."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from heapq import heappop, heappush
from typing import Any

from lxml import etree

from kd_rules_mcp.ed.errors import EdFormatError
from kd_rules_mcp.ed.lexer import Token, tokenize
from kd_rules_mcp.kd2.model import ExchangeRules, Node, object_name, rule_code
from kd_rules_mcp.validation.address import rule_address, side_name, walk_pks
from kd_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
from kd_rules_mcp.validation.handlers import EVENT_AREAS

# docs/plans/kd2-to-ed-draft-spec-2026-10.md, §4. Это подсказки, не эквивалентность фаз.
EVENT_DESTINATIONS = {
    ("exchange_rules", "ПередВыгрузкойДанных"): "ПередКонвертацией",
    ("exchange_rules", "ПередЗагрузкойДанных"): "ПередКонвертацией",
    ("exchange_rules", "ПослеВыгрузкиДанных"): "ПослеКонвертации",
    ("exchange_rules", "ПослеЗагрузкиДанных"): "ПослеКонвертации",
    ("exchange_rules", "ПередВыгрузкойОбъекта"): "ПриОбработке",
    ("exchange_rules", "ПередКонвертациейОбъекта"): "ПриОтправкеДанных",
    ("exchange_rules", "ПередЗагрузкойОбъекта"): "ПриКонвертацииДанныхXDTO",
    ("exchange_rules", "ПослеЗагрузкиОбъекта"): "ПослеЗагрузкиВсехДанных",
    ("pko", "ПередВыгрузкой"): "ПриОтправкеДанных",
    ("pko", "ПриВыгрузке"): "ПриОтправкеДанных",
    ("pko", "ПередЗагрузкой"): "ПриКонвертацииДанныхXDTO",
    ("pko", "ПриЗагрузке"): "ПередЗаписьюПолученныхДанных",
    ("pko", "ПослеЗагрузки"): "ПослеЗагрузкиВсехДанных",
    ("pko", "ПоследовательностьПолейПоиска"): "АлгоритмПоиска",
    **{
        (kind, event): "ПриОтправкеДанных"
        for kind, event in EVENT_AREAS
        if kind in ("pks", "pks_group")
    },
    ("pvd", "ПередОбработкойПравила"): "ПриОбработке",
    ("pvd", "ПередВыгрузкойОбъекта"): "ПриОбработке",
    ("pod_receive", "ПриОбработке"): "ПриОбработке",
    ("pod_receive", "ИспользуемыеПКО"): "ИспользуемыеПКО",
}

# Р3 design.md: сторона исполнения известна только для перечисленных событий.
# ПередКонвертациейОбъекта — выгрузка: БСП, обработчик конвертации перед сериализацией.
EVENT_SIDES = {
    **{
        (kind, event): "send"
        for kind in ("pko", "pks", "pks_group")
        for event in (
            "ПередВыгрузкой",
            "ПриВыгрузке",
            "ПослеВыгрузки",
            "ПослеВыгрузкиВФайл",
            "ПередОбработкойВыгрузки",
            "ПослеОбработкиВыгрузки",
        )
    },
    **{
        ("pko", event): "receive"
        for event in (
            "ПередЗагрузкой",
            "ПриЗагрузке",
            "ПослеЗагрузки",
            "ПоследовательностьПолейПоиска",
        )
    },
    **{
        ("pvd", event): "send"
        for event in (
            "ПередОбработкойПравила",
            "ПередВыгрузкойОбъекта",
            "ПослеВыгрузкиОбъекта",
            "ПослеОбработкиПравила",
        )
    },
    **{
        ("exchange_rules", event): "send"
        for event in (
            "ПередВыгрузкойДанных",
            "ПослеВыгрузкиДанных",
            "ПередВыгрузкойОбъекта",
            "ПередКонвертациейОбъекта",
            "ПослеВыгрузкиОбъекта",
        )
    },
    **{
        ("exchange_rules", event): "receive"
        for event in (
            "ПередЗагрузкойДанных",
            "ПослеЗагрузкиДанных",
            "ПередЗагрузкойОбъекта",
            "ПослеЗагрузкиОбъекта",
            "ПослеЗагрузкиПараметров",
        )
    },
    ("parameter", "ПослеЗагрузкиПараметра"): "receive",
}

# Р3 design.md; БСП:5618,6079–6082. Неизвестная сторона остаётся ручной политикой.
FLAG_SIDES = {
    "ИмяПараметраДляПередачи": "send",
    "ПолучитьИзВходящихДанных": "send",
    "НеЗапоминатьВыгруженные": "send",
    "НеЗамещать": "receive",
    "ПоискПоДатеНаРавенство": "receive",
    "НеСоздаватьЕслиНеНайден": "receive",
    # БСП:12852–12854,11538,11780–11784 (13035 относится к ПриводитьКДлине).
    "ПриводитьКДлине": "send",
    "ВыгружатьГруппуЧерезФайл": "send",
}


def flag_side(kind: str, name: str) -> str | None:
    # Все политики ПВД исполняются в источнике, как само ПВД (БСП:6764–6886).
    return "send" if kind == "pvd" else FLAG_SIDES.get(name)


# Это понятые поля, а не все поля схемы КД 2. Остальные непустые атрибуты
# и известные значения — отдельные политики, каркас не останавливают.
# VK:498–501,821–840,924; БСП:5984–5991,6601,6641,6794–6796.
COMMON_FIELDS = {"Код", "Наименование", "Описание", "Комментарий", "Порядок"}
UNDERSTOOD_FIELDS = {
    "pko": COMMON_FIELDS
    | {
        "Источник",
        "Приемник",
        "СинхронизироватьПоИдентификатору",
        "ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли",
    },
    "pvd": COMMON_FIELDS
    | {"Отключить", "КодПравилаКонвертации", "СпособОтбораДанных", "ОбъектВыборки"},
    "parameter": {"Имя", "Наименование"},
    "pks": COMMON_FIELDS | {"КодПравилаКонвертации", "Отключить", "Поиск", "Обязательное"},
    "pks_group": COMMON_FIELDS | {"КодПравилаКонвертации", "Отключить", "Поиск", "Обязательное"},
}

DUPLICATE_CODE = "duplicate_code"


@dataclass(slots=True)
class Entity:
    origin: str
    kind: str
    role: str
    state: str = "needs_decision"
    reason: str = "no_candidate"
    message: str = "Нужно решение агента"
    parent: str = ""
    blocked_by: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    candidates_total: int = 0

    def set(self, state: str, reason: str, message: str, *blocked_by: str) -> None:
        self.state, self.reason, self.message = state, reason, message
        self.blocked_by = list(blocked_by)


@dataclass(frozen=True, slots=True)
class ManualTask:
    origin: str
    event: str
    proposed_location: str
    body_sha256: str
    line_count: int
    reason: str
    message: str


@dataclass(slots=True)
class Inventory:
    entities: list[Entity] = field(default_factory=list)
    manual: list[ManualTask] = field(default_factory=list)
    nodes: dict[str, Node] = field(default_factory=dict)
    disabled: set[str] = field(default_factory=set)
    siblings: dict[tuple[str, str], int] = field(default_factory=dict)

    def add(self, role: str, address: str, node: Node, parent: str = "") -> Entity:
        origin = role + ":" + address
        key = (parent, origin)
        count = self.siblings.get(key, 0) + 1
        self.siblings[key] = count
        if count > 1:
            # Повреждённый файл с дублями не превращается в исключение/потерю описи.
            origin += f" / #{count}"
        entity = Entity(origin, node.kind.name, role, parent=parent)
        self.entities.append(entity)
        self.nodes[origin] = node
        return entity

    def task(
        self, entity: Entity, event: str, body: str = "", *, reason: str = "unsupported"
    ) -> None:
        for index, task in enumerate(self.manual):
            if task.origin == entity.origin:
                self.manual[index] = replace(task, message=entity.message)
                return
        self.manual.append(
            ManualTask(
                entity.origin,
                event,
                EVENT_DESTINATIONS.get(
                    (
                        entity.kind
                        if entity.kind == "pod_receive"
                        else self.nodes[entity.origin].kind.name,
                        event,
                    ),
                    "нет прямого соответствия",
                ),
                sha256(body.encode("utf-8")).hexdigest(),
                body.count("\n") + 1 if body else 0,
                reason,
                entity.message,
            )
        )

    def inspect(self, entity: Entity) -> None:
        node = self.nodes[entity.origin]
        for kind, event in EVENT_AREAS:
            if kind != node.kind.name:
                continue
            values = node.attrs if kind == "parameter" else node.values
            if event not in values:
                continue
            handler = self.add(entity.role, entity.origin.split(":", 1)[1] + "/" + event, node)
            handler.kind = "handler"
            handler.parent = entity.origin
            side = EVENT_SIDES.get((kind, event))
            if side is not None and side != entity.role:
                handler.set(
                    "excluded", "opposite_role", "Обработчик исполняется на другой стороне обмена"
                )
                continue
            handler.set("manual", "handler", "Тело обработчика переносится вручную")
            self.task(handler, event, str(values[event]), reason="handler")
        if has_unknown(node):
            entity.set("manual", "unsupported", "Неизвестный тег, атрибут или форма КД 2")
            self.task(entity, "unsupported")
            unknown = [(str(e.tag), e.text or "") for e in node.unknown]
            unknown.extend((k, str(v)) for k, v in node.values.items() if k not in node.kind.leaves)
            for index, (event, body) in enumerate(unknown):
                entry = self.add(
                    entity.role,
                    entity.origin.split(":", 1)[1] + f"/Неподдержано/{event}/{index}",
                    node,
                    entity.origin,
                )
                entry.kind = "unsupported"
                entry.set("manual", "unsupported", "Неподдержанный тег КД 2")
                self.task(entry, event, body)
        if node.kind.name in ("pks", "pks_group") and any(
            not child or not child.attrs.get("Имя")
            for child in (
                [node.child("Источник"), node.child("Приемник")]
                if entity.role == "send"
                else [node.child("Приемник")]
            )
        ):
            entity.set("manual", "unsupported", "Пустая сторона ПКС")
            self.task(entity, "unsupported")
        understood = UNDERSTOOD_FIELDS.get(node.kind.name)
        if understood is not None or node.kind.name == "exchange_rules":
            values = {**node.values, **node.attrs}
            for name, value in values.items():
                if (
                    value in (None, "", False)
                    or (node.kind.name, name) in EVENT_AREAS
                    or (understood is not None and name in understood)
                    or (understood is None and value is not True)
                    or (name in node.values and name not in node.kind.leaves)
                ):
                    continue
                policy = self.add(
                    entity.role,
                    entity.origin.split(":", 1)[1] + "/Политика/" + name,
                    node,
                    entity.origin,
                )
                policy.kind = "policy"
                side = flag_side(node.kind.name, name)
                if side is not None and side != entity.role:
                    policy.set(
                        "excluded", "opposite_role", "Политика исполняется на другой стороне"
                    )
                    continue
                policy.set(
                    "manual", "unsupported", "Политика КД 2 требует отдельного решения агента"
                )
                self.task(policy, name)
        variants = node.child("НастройкаВариантовПоискаОбъектов")
        if variants is not None and not variants.is_empty():
            entry = self.add(
                entity.role,
                entity.origin.split(":", 1)[1] + "/НастройкаВариантовПоискаОбъектов",
                variants,
                entity.origin,
            )
            entry.kind = "policy"
            entry.set("manual", "unsupported", "Варианты поиска требуют ручного переноса")
            self.task(entry, "НастройкаВариантовПоискаОбъектов")
        if (
            node.kind.name in ("pks", "pks_group")
            and entity.role == "receive"
            and node.get("ИмяПараметраДляПередачи")
        ):
            entity.set("excluded", "opposite_role", "ПКС передаёт параметр в базе-источнике")


def has_unknown(node: Node) -> bool:
    """Проверяет также стороны, но не содержимое известных контейнеров правил."""
    return bool(
        node.unknown
        or set(node.values) - node.kind.leaves.keys()
        or (
            node.kind.name not in ("pko", "pvd", "parameter")
            and set(node.attrs) - node.kind.attr_map.keys()
        )
        or set(node.children) - node.kind.children.keys()
        or any(has_unknown(c) for c in node.children.values() if c.kind.name == "pks_side")
    )


def items(node: Node, tag: str) -> list[Node]:
    child = node.child(tag)
    return list(child.walk()) if child else []


def scoped_items(container: Node, disabled: bool = False):
    for item in container.items:
        # БСП:6764–6768,6868–6886: группа ПВД не участвует в исполнении.
        muted = False if item.kind.name == "pvd_group" else disabled or not enabled(item)
        yield item, muted
        if item.is_group:
            yield from scoped_items(item, muted)


def inventory(inputs: list[tuple[str, ExchangeRules]]) -> Inventory:
    result = Inventory()
    for role, rules in sorted(inputs):
        root = result.add(role, "Конвертация", rules.root)
        root.set("excluded", "container", "Заголовок правил; события перечислены отдельно")
        result.inspect(root)
        for tag in (
            "ПравилаКонвертацииОбъектов",
            "ПравилаВыгрузкиДанных",
            "ПравилаОчисткиДанных",
            "Параметры",
            "Алгоритмы",
            "Запросы",
            "Обработки",
        ):
            container = rules.root.child(tag)
            if container is None:
                continue
            if has_unknown(container) or (container.text or "").strip():
                entry = result.add(role, "Конвертация/" + tag, container)
                entry.set("manual", "unsupported", "Неподдержанная форма контейнера")
                result.task(entry, "unsupported", container.text or "")
            for node, muted in scoped_items(container):
                entity = result.add(role, rule_address(node), node)
                if node.is_group:
                    entity.set("excluded", "container", "Группа организации списка правил")
                if muted:
                    result.disabled.add(entity.origin)
                result.inspect(entity)
                if node.kind.name == "pko":
                    address = entity.origin.split(":", 1)[1]
                    props = node.child("Свойства")
                    if props:
                        if has_unknown(props):
                            entry = result.add(role, address + "/Свойства", props, entity.origin)
                            result.inspect(entry)
                        for path, prop in walk_pks(props):
                            entry = result.add(
                                role, address + " / ПКС " + path, prop, entity.origin
                            )
                            result.inspect(entry)
                            if not enabled(prop) or any(
                                entry.origin.startswith(e.origin + "/")
                                for e in result.entities
                                if e.origin in result.disabled
                            ):
                                result.disabled.add(entry.origin)
                    values = node.child("Значения")
                    if values and has_unknown(values):
                        entry = result.add(role, address + "/Значения", values, entity.origin)
                        result.inspect(entry)
                    for value, _ in scoped_items(values) if values else ():
                        if value.is_group:
                            entry = result.add(
                                role,
                                address + "/ГруппаЗначений/" + value.code,
                                value,
                                entity.origin,
                            )
                            entry.set("excluded", "container", "Группа организации значений")
                            result.inspect(entry)
                            continue
                        # В отличие от ПКЗ-навигации имя здесь от выбранной стороны (Р7).
                        side = "Источник" if role == "send" else "Приемник"
                        entry = result.add(
                            role,
                            address + "/Значение/" + str(value.get(side)),
                            value,
                            entity.origin,
                        )
                        result.inspect(entry)
    # БСП:14663,6135,7077: структура по имени оставляет последнюю копию ПКО.
    active = {
        (e.role, result.nodes[e.origin].code.casefold()): e
        for e in result.entities
        if e.kind == "pko"
    }
    by_origin = {e.origin: e for e in result.entities}
    for entity in result.entities:
        if entity.kind == "pko":
            current = active[(entity.role, result.nodes[entity.origin].code.casefold())]
            if current is not entity:
                entity.set(
                    "excluded", DUPLICATE_CODE, "Действует последняя копия ПКО: " + current.origin
                )
        parent = by_origin.get(entity.parent)
        while parent is not None:
            if parent.reason == DUPLICATE_CODE:
                entity.set(
                    "excluded",
                    "parent_excluded",
                    "ПКО заменено последней копией: " + parent.message,
                )
                break
            parent = by_origin.get(parent.parent)
    return result


def enabled(node: Node) -> bool:
    return node.attrs.get("Отключить") is not True


def active_rules(nodes: list[Node]) -> list[Node]:
    """Действующие копии в порядке файла (БСП:6135,7077)."""
    last = {node.code.casefold(): node for node in nodes}
    return [node for node in nodes if last[node.code.casefold()] is node]


def implicit_rule(nodes: list[Node], types: tuple[str, ...], role: str) -> Node | None:
    """БСП:5845,7083,13150–13160: последнее действующее ПКО единственного типа."""
    if len(types) != 1:
        return None
    side = "Источник" if role == "send" else "Приемник"
    found = [n for n in active_rules(nodes) if object_name(n.get(side)) == object_name(types[0])]
    return found[-1] if found else None


def body_tokens(body: object) -> tuple[Token, ...]:
    try:
        return tokenize(str(body or ""))
    except EdFormatError:
        return ()  # Тело остаётся ручной задачей; незакрытые строки не угадываем.


def code_calls(inv: Inventory, role: str) -> dict[str, list[tuple[str, str]]]:
    """Рёбра хозяин → литерал с адресом обработчика; БСП:13451–13473,13633."""
    result: dict[str, list[tuple[str, str]]] = {}
    by_origin = {e.origin: e for e in inv.entities}
    codes = {
        inv.nodes[e.origin].code.casefold()
        for e in inv.entities
        if e.kind == "pko" and e.role == role and e.reason != DUPLICATE_CODE
    }
    algorithms = {
        inv.nodes[e.origin].code.casefold(): body_tokens(inv.nodes[e.origin].get("Текст"))
        for e in inv.entities
        if e.role == role and e.kind == "algorithm" and e.origin not in inv.disabled
    }
    for entity in inv.entities:
        if entity.role != role or entity.kind != "handler":
            continue
        node = inv.nodes[entity.origin]
        event = entity.origin.rsplit("/", 1)[-1]
        # Приёмник файла тоже нуждается в ПКО, вызванных при выгрузке другой базой.
        # Список событий один с opposite_role; обработчики ПОД сюда не входят.
        if EVENT_SIDES.get((node.kind.name, event)) != "send":
            continue
        owner = by_origin.get(entity.parent)
        muted = False
        while owner is not None:
            muted |= owner.origin in inv.disabled or owner.reason == DUPLICATE_CODE
            if owner.kind in ("pko", "pvd", "exchange_rules"):
                break
            owner = by_origin.get(owner.parent)
        if muted or owner is None:
            continue
        tokens = body_tokens(node.get(event))
        referenced = {
            t.value.casefold() for t in tokens if t.kind == "identifier"
        } & algorithms.keys()
        literals = (*tokens, *(t for name in sorted(referenced) for t in algorithms[name]))
        for token in literals:
            code = rule_code(token.value).casefold()
            if token.kind == "string" and code in codes:
                result.setdefault(owner.origin, []).append((code, entity.origin))
    return {owner: sorted(set(edges)) for owner, edges in result.items()}


@dataclass(frozen=True, slots=True)
class Reachability:
    codes: set[str]
    code_only: dict[str, str]


def reachable(
    rules: ExchangeRules,
    extra_roots: tuple[str, ...] = (),
    *,
    structure: StructureSnapshot | None = None,
    role: str = "send",
    inv: Inventory | None = None,
) -> Reachability:
    """Один обход обычных и кодовых рёбер; обычный путь имеет приоритет."""
    inv = inv if inv is not None else inventory([(role, rules)])
    calls = code_calls(inv, role)
    origins = {
        id(inv.nodes[e.origin]): e.origin
        for e in inv.entities
        if e.role == role and e.kind in ("pko", "pvd", "exchange_rules")
    }
    graph: dict[str, set[tuple[str, str]]] = {}
    nodes = active_rules(items(rules.root, "ПравилаКонвертацииОбъектов"))
    codes = {node.code.casefold() for node in nodes}
    for node in nodes:
        edges: set[str] = set()
        key = object_name(node.get("Источник" if role == "send" else "Приемник")).split(".", 1)
        host = structure.objects.get((key[0], key[1])) if structure and len(key) == 2 else None

        def visit(parent: Node, table: str = "", host=host, edges=edges) -> None:
            for prop in parent.items:
                if not enabled(prop):
                    continue
                code = rule_code(prop.get("КодПравилаКонвертации")).casefold()
                if code:
                    edges.add(code)
                elif host and not prop.is_group:
                    name = side_name(prop, "Источник" if role == "send" else "Приемник")
                    attrs = host.property(table + "." + name if table else name, table=bool(table))
                    target = implicit_rule(nodes, attrs[0].types, role) if len(attrs) == 1 else None
                    if target:
                        edges.add(target.code.casefold())
                visit(
                    prop,
                    side_name(prop, "Источник" if role == "send" else "Приемник")
                    if prop.is_group
                    else table,
                )

        props = node.child("Свойства")
        if props:
            visit(props)
        graph[node.code.casefold()] = {(code, "") for code in edges} | set(
            calls.get(origins[id(node)], ())
        )
    roots = {(code.casefold(), "") for code in extra_roots}
    roots.update(calls.get(origins[id(rules.root)], ()))
    for node, muted in scoped_items(
        rules.root.child("ПравилаВыгрузкиДанных") or Node.new("pvd_list", "ПравилаВыгрузкиДанных")
    ):
        if not muted and not node.is_group:
            roots.add((rule_code(node.get("КодПравилаКонвертации")).casefold(), ""))
            roots.update(calls.get(origins[id(node)], ()))
    pending: list[tuple[bool, int, str, str]] = []
    for code, handler in sorted(roots):
        heappush(pending, (bool(handler), 0, handler, code))
    found: dict[str, str] = {}
    while pending:
        _, distance, handler, code = heappop(pending)
        if code not in codes or code in found:
            continue
        found[code] = handler
        for target, via in sorted(graph.get(code, ())):
            nearest = via or handler
            heappush(pending, (bool(nearest), distance + 1, nearest, target))
    return Reachability(set(found), {code: handler for code, handler in found.items() if handler})


def snapshot(node: Node) -> dict[str, Any]:
    """Все данные модели, включая точные тела; наружу возвращается только хеш."""
    return {
        "kind": node.kind.name,
        "tag": node.tag,
        "attrs": node.attrs,
        "values": node.values,
        "text": node.text,
        "leading": node.leading_text,
        "trailing": node.trailing_text,
        "children": {k: snapshot(v) for k, v in node.children.items()},
        "items": [snapshot(v) for v in node.items],
        "unknown": [etree.tostring(v).hex() for v in node.unknown],
    }
