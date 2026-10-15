"""Граф полей, явных вызовов и алгоритмов; один источник фактов для трассы и проверок."""

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from typing import Any

from kd_rules_mcp.kd2.model import ExchangeRules, Node, object_name, rule_code
from kd_rules_mcp.kd2.queries import assignment_expression, statement_assignment, tokens
from kd_rules_mcp.validation.address import (
    pks_address,
    pkz_address,
    rule_address,
    side_name,
    walk_pks,
)
from kd_rules_mcp.validation.handlers import Handler, HandlerFile, handler_sources


@dataclass(frozen=True, slots=True)
class CallEdge:
    """Ребро принадлежит исходному обработчику; `chain` — узлы DAG с объединёнными путями."""

    target: str
    address: str
    kind: str
    chain: tuple[dict[str, Any], ...] = ()
    callers: tuple[str, ...] = ()
    whole: bool = False


@dataclass(frozen=True, slots=True)
class Branch:
    """Ветка ПВД в порядке загрузки; флаг группы не наследуется (БСП:6768, БСП:6868–6882)."""

    node: Node
    order: int
    disabled: bool
    first_for_type: bool
    target: str


@dataclass(frozen=True, slots=True)
class _Occurrence:
    """Один исходный вызов: аргументы нужны проверке входящих данных, позиции — трассе."""

    handler: Handler
    location: HandlerFile
    site: dict[str, Any]
    target: str
    incoming: str
    kind: str
    whole: bool | None = None


@dataclass(slots=True)
class CallGraph:
    """Связи модели и исходные вызовы без произведения путей через алгоритмы."""

    edges: list[CallEdge]
    index: dict[str, tuple[Node, bool]]
    branches: list[Branch]
    occurrences: list[_Occurrence] = field(repr=False)
    sources: dict[str, tuple[Handler, HandlerFile]] = field(repr=False)

    @property
    def targets(self) -> set[str]:
        """Разрешённые цели, включая поля; неизвестный динамический код не придумывается."""
        return {edge.target.casefold() for edge in self.edges if edge.target}

    def from_address(self, address: str, *, properties: bool = False) -> list[CallEdge]:
        """Исходящие связи адреса и вызванных алгоритмов; каждый исходный вызов один раз."""

        def matches(value: str) -> bool:
            return value == address or (properties and value.startswith(f"{address} / "))

        return [
            edge
            for edge in self.edges
            if matches(edge.address) or any(matches(caller) for caller in edge.callers)
        ]


def scope_call(edge: CallEdge, roots: set[str]) -> CallEdge:
    """Оставляет в DAG маршруты из обработчиков текущей трассы, без чужих вызывающих."""
    if not edge.chain:
        return edge
    nodes = {site["id"]: site for site in edge.chain}
    pending = [
        site["id"]
        for site in edge.chain
        if any(
            site["address"] == root or site["address"].startswith(f"{root} / ") for root in roots
        )
    ]
    reached: set[str] = set()
    while pending:
        point = pending.pop()
        if point in reached:
            continue
        reached.add(point)
        pending.extend(nodes[point].get("next", ()))
    # Для входящего вызова выбранного ПКО сохраняем исходное место вызова в алгоритме,
    # но не добавляем маршруты из обработчиков других объектов.
    if not reached:
        reached = {site["id"] for site in edge.chain if site["role"] == "call"}
    chain = tuple(
        {**site, "next": [point for point in site["next"] if point in reached]}
        if "next" in site
        else site
        for site in edge.chain
        if site["id"] in reached
    )
    callers = tuple(
        address for address in edge.callers if any(s["address"] == address for s in chain)
    )
    return replace(edge, chain=chain, callers=callers)


def node_index(rules: ExchangeRules) -> dict[str, tuple[Node, bool]]:
    """Полные адреса. Группы ПКО/ПВД не выключают потомков; группы ПКС выключают."""
    found: dict[str, tuple[Node, bool]] = {}

    def visit(node: Node) -> None:
        # ЗагрузитьПКО не читает Отключить: БСП:5667–5860, БСП:5845.
        disabled = _disabled(node) if node.kind.name == "pvd" else False
        found[rule_address(node)] = node, disabled
        if node.kind.name == "pko":
            properties = node.child("Свойства")
            if properties is not None:
                parents: dict[str, bool] = {}
                for path, prop in walk_pks(properties):
                    parents[path] = parents.get(path.rpartition("/")[0], False) or _disabled(prop)
                    found[pks_address(node.code, path)] = prop, parents[path]
            values = node.child("Значения")
            if values is not None:
                for value in values.walk():
                    found[pkz_address(node.code, side_name(value, "Источник"))] = (
                        value,
                        _disabled(value),
                    )
        for child in (*node.children.values(), *node.items):
            if child.kind.name not in ("pks", "pks_group", "pks_list", "pkz", "pkz_list"):
                visit(child)

    visit(rules.root)
    return found


def _targets(expression: str, nesting: int = 0) -> tuple[list[str], bool]:
    """Известные ветви тернарного кода и признак оставшейся динамической ветви."""
    if nesting >= 30:
        return [], True
    parts = tokens(expression)
    if len(parts) == 1 and parts[0].string and parts[0].closed:
        return ([parts[0].value] if parts[0].value else []), False
    names = _target_names(expression)
    if parts and parts[0].value == "?" and len(parts) > 1:
        arguments = _call_arguments(expression, parts[1].start)
        if arguments and len(arguments) == 3:
            left, left_unknown = _targets(arguments[1], nesting + 1)
            right, right_unknown = _targets(arguments[2], nesting + 1)
            return [*left, *right], left_unknown or right_unknown
    return names, True


def _explicit_calls(code: str, masked: str) -> list[tuple[int, list[str], bool, str, str, bool]]:
    """Общий разбор трёх вызовов: входящие и код — БСП:321, БСП:2289, БСП:13604."""
    found: list[tuple[int, list[str], bool, str, str, bool]] = []
    for match in _EXPORT_CALL.finditer(masked):
        arguments = _call_arguments(code, match.end() - 1)
        if arguments is None:
            continue
        incoming_at, name_at = _EXPORT_ARGUMENTS[match.group(1).casefold()]
        expression = arguments[name_at - 1] if len(arguments) >= name_at else ""
        incoming = arguments[incoming_at - 1].strip() if len(arguments) >= incoming_at else ""
        names, unknown = _targets(expression)
        # Явная выгрузка целого объекта по умолчанию, в том числе из ПКС (БСП:321–330).
        reference = (
            match.group(1).casefold() == "выгрузитьпоправилу"
            and len(arguments) >= 7
            and arguments[6].strip().casefold() == "истина"
        )
        found.append((match.start(), names, unknown, incoming, expression, not reference))
    return found


def _whole_property(node: Node, algorithms: dict[str, str]) -> bool:
    """Явное присваивание целого объекта до выгрузки ПКС (БСП:12924, БСП:12949)."""
    code = _expand(str(node.get("ПередВыгрузкой")), algorithms).code
    lexical = tokens(code)
    return any(
        t.value.casefold() == "выгрузитьобъект"
        and statement_assignment(lexical, i)
        and i + 2 < len(lexical)
        and lexical[i + 2].value.casefold() == "истина"
        for i, t in enumerate(lexical)
        if not t.string
    )


def build_call_graph(rules: ExchangeRules) -> CallGraph:
    """Читает каждое тело один раз, алгоритмы связывает как DAG без раскрытия путей."""
    index = node_index(rules)
    sources = {loc.name: (h, loc) for h, loc in handler_sources(rules)}
    algorithms = _algorithms(rules)
    algorithm_files = {
        h.address: name for name, (h, _) in sources.items() if h.file_prefix == "Алгоритм"
    }
    by_name = {
        node.code.casefold(): algorithm_files.get(rule_address(node)) for node in rules.algorithms()
    }
    defaults: dict[str, Node] = {}
    for pko in rules.pko():
        if str(pko.get("Источник")).strip():
            # Каждый ПКО перезаписывает менеджер типа, в том числе с атрибутом Отключить:
            # БСП:5845, БСП:6947; подбор без имени — БСП:6977–6997.
            defaults[object_name(pko.get("Источник"))] = pko
    edges: list[CallEdge] = []
    branches: list[Branch] = []
    seen_types: set[str] = set()
    for order, pvd in enumerate(rules.pvd(), 1):
        key = object_name(pvd.get("ОбъектВыборки"))
        disabled = _disabled(pvd)
        code = _conversion_code(pvd)
        target = code or (defaults[key].code if key in defaults else "")
        first = not disabled and key not in seen_types
        if not disabled and key:
            seen_types.add(key)
        branches.append(Branch(pvd, order, disabled, first, target))
        if not disabled and target:
            edges.append(
                CallEdge(
                    target, rule_address(pvd), "field" if code else "branch_properties", whole=True
                )
            )
    for address, (node, disabled) in index.items():
        if (
            not disabled
            and node.kind.name in ("pks", "pks_group", "pkz")
            and (target := _conversion_code(node))
        ):
            edges.append(
                CallEdge(target, address, "field", whole=_whole_property(node, algorithms))
            )
        elif not disabled and node.kind.name == "pks" and not _conversion_code(node):
            source = node.child("Источник")
            types = str(source.attrs.get("Тип", "")).split(",") if source is not None else []
            # Без кода ПКС выбирает менеджер фактического типа значения:
            # Ссылка не ставится напрямую: БСП:12620–12625; выбор — БСП:12908,
            # БСП:12985–12991, БСП:13131–13158; последнее ПКО — БСП:5845, БСП:6947.
            targets = dict.fromkeys(
                defaults[key].code
                for value_type in types
                if "." in value_type and (key := object_name(value_type)) in defaults
            )
            edges.extend(
                CallEdge(target, address, "type_lookup", whole=_whole_property(node, algorithms))
                for target in targets
            )

    occurrences: list[_Occurrence] = []
    # Для ссылки на алгоритм храним только исходный узел и вызываемый файл.
    references: list[tuple[str, str, dict[str, Any]]] = []
    for name, (h, loc) in sources.items():
        if index.get(h.address, (rules.root, False))[1]:
            continue
        code = _positions(h.text, strings=False)
        masked = _positions(h.text, strings=True)
        lexical = tokens(h.text)
        for pos, token in enumerate(lexical):
            if (
                not token.string
                and token.value.casefold() == "имяпко"
                and statement_assignment(lexical, pos)
            ):
                expression = assignment_expression(h.text, lexical, pos + 1)
                names, unknown = _targets(expression)
                site = call_site(h, loc, token.start)
                for target in names:
                    occurrences.append(_Occurrence(h, loc, site, target, "", "pko_assignment"))
                if unknown:
                    occurrences.append(
                        _Occurrence(
                            h,
                            loc,
                            {**site, "target_expression": expression[:200]},
                            "",
                            "",
                            "dynamic_call",
                        )
                    )
        for start, names, unknown, incoming, expression, whole in _explicit_calls(code, masked):
            site = call_site(h, loc, start)
            for target in names:
                occurrences.append(_Occurrence(h, loc, site, target, incoming, "call", whole))
            if unknown:
                occurrences.append(
                    _Occurrence(
                        h,
                        loc,
                        {**site, "target_expression": expression[:200]},
                        "",
                        incoming,
                        "dynamic_call",
                        whole,
                    )
                )
        for execute in _EXECUTE_STRING.finditer(code):
            if not masked[execute.start() : execute.start() + len("Выполнить")].strip():
                continue
            # Тот же декодер, что у запросов: убирает | и удвоенные кавычки, сохраняя строки.
            literal = tokens(execute.group(1))[0]
            nested = literal.value
            for start, names, unknown, incoming, expression, whole in _explicit_calls(
                nested, _positions(nested, strings=True)
            ):
                site = call_site(h, loc, execute.start(1))
                extra = nested.count("\n", 0, start)
                site["line"] += extra
                site["handler_line"] += extra
                for target in names:
                    occurrences.append(
                        _Occurrence(h, loc, site, target, incoming, "execute_string", whole)
                    )
                if unknown:
                    occurrences.append(
                        _Occurrence(
                            h,
                            loc,
                            {**site, "target_expression": expression[:200]},
                            "",
                            incoming,
                            "dynamic_call",
                            whole,
                        )
                    )
        for match in _ALGORITHM.finditer(masked):
            target_file = by_name.get(match.group(1).casefold())
            if target_file and target_file != name:
                references.append((name, target_file, call_site(h, loc, match.start())))

    incoming_refs: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
    for ref in references:
        incoming_refs[ref[1]].append(ref)
    groups: dict[tuple[str, str, str], list[_Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        key = (
            occurrence.location.name,
            occurrence.target.casefold(),
            "algorithm_call" if occurrence.handler.file_prefix == "Алгоритм" else occurrence.kind,
        )
        groups[key].append(occurrence)
    for (name, _target, _kind), group in groups.items():
        origin = group[0]
        ancestors: set[str] = {name}
        queue = deque([name])
        links: list[tuple[str, str, dict[str, Any]]] = []
        adjacency: dict[str, set[str]] = defaultdict(set)
        while queue:
            for ref in incoming_refs.get(queue.popleft(), []):
                # Осекаем только цикл текущего терминального вызова; общие ветви ромба сохраняем.
                pending = [ref[1]]
                reached: set[str] = set()
                while pending:
                    point = pending.pop()
                    if point not in reached:
                        reached.add(point)
                        pending.extend(adjacency[point])
                if ref[0] in reached:
                    continue
                adjacency[ref[0]].add(ref[1])
                links.append(ref)
                if ref[0] not in ancestors:
                    ancestors.add(ref[0])
                    queue.append(ref[0])
        chain = [
            dict(
                site,
                role="algorithm",
                id=f"{source}:{site['handler_line']}:algorithm:{target}",
                callee=target,
            )
            for source, target, site in links
        ]
        chain.extend(
            dict(o.site, role="call", id=f"{name}:{o.site['handler_line']}:call") for o in group
        )
        # Узлы и рёбра DAG, не список всех маршрутов. На ромбе число узлов линейно.
        unique = {site["id"]: site for site in chain}
        chain = sorted(
            unique.values(), key=lambda site: (site["role"] == "call", site["file"], site["line"])
        )
        for site in chain:
            if "callee" in site:
                site["next"] = [node["id"] for node in chain if node["file"] == site["callee"]]
        callers = tuple(
            dict.fromkeys(sources[file][0].address for file in sources if file in ancestors)
        )
        node = index.get(origin.handler.address, (rules.root, False))[0]
        property_call = node.kind.name in ("pks", "pks_group")
        whole = any(
            occurrence.whole
            if occurrence.whole is not None
            else not property_call or _whole_property(node, algorithms)
            for occurrence in group
        )
        edges.append(
            CallEdge(
                origin.target, origin.handler.address, origin.kind, tuple(chain), callers, whole
            )
        )
    return CallGraph(edges, index, branches, occurrences, sources)


def call_graph(rules: ExchangeRules) -> list[CallEdge]:
    """Совместимый список рёбер; исходная пара алгоритм → цель представлена один раз."""
    return build_call_graph(rules).edges


def validation_evidence(
    rules: ExchangeRules, code: str, graph: CallGraph | None = None
) -> list[dict[str, Any]]:
    """Места вызовов графа; адрес ПКС всегда содержит ПКО и путь свойства."""
    graph = graph or build_call_graph(rules)
    evidence: list[dict[str, Any]] = []
    for edge in graph.edges:
        if edge.target.casefold() != code.casefold():
            continue
        if edge.chain:
            evidence.extend(
                {"kind": edge.kind, **site} for site in edge.chain if site["role"] == "call"
            )
        else:
            evidence.append({"kind": edge.kind, "address": edge.address})
    return evidence


@dataclass(frozen=True, slots=True)
class _PksReference:
    """Ссылающееся свойство и режим из общего графа."""

    address: str
    exports_whole: bool


def _pks_references(
    rules: ExchangeRules, graph: CallGraph | None = None
) -> dict[str, list[_PksReference]]:
    graph = graph or build_call_graph(rules)
    found: dict[str, list[_PksReference]] = {}
    for edge in graph.edges:
        if edge.target and "/ ПКС " in edge.address:
            reference = _PksReference(edge.address, edge.whole)
            if reference not in found.setdefault(edge.target, []):
                found[edge.target].append(reference)
    return found


def _referenced_pko_codes(rules: ExchangeRules, graph: CallGraph | None = None) -> set[str]:
    return {edge.target for edge in (graph or build_call_graph(rules)).edges if edge.target}


def _export_calls(
    rules: ExchangeRules, algorithms: dict[str, str], graph: CallGraph | None = None
) -> list[tuple[str, str, str]]:
    """Проверка входящих данных читает ровно те же вызовы, что трасса."""
    graph = graph or build_call_graph(rules)
    result: list[tuple[str, str, str]] = []
    for occurrence in graph.occurrences:
        if not occurrence.target or occurrence.kind == "pko_assignment":
            continue
        # Алгоритм вне всех вызывающих обработчиков не является входящим вызовом объекта.
        callers = [
            edge
            for edge in graph.edges
            if edge.target.casefold() == occurrence.target.casefold()
            and edge.address == occurrence.handler.address
        ]
        files = {site["file"] for edge in callers for site in edge.chain}
        for file, (handler, _) in graph.sources.items():
            if handler.file_prefix == "Алгоритм" or file not in files:
                continue
            if handler.event not in (
                "ПередВыгрузкой",
                "ПриВыгрузке",
                "ПослеВыгрузки",
                "ПослеВыгрузкиВФайл",
                "ПоследовательностьПолейПоиска",
                "ПередОбработкойПравила",
                "ПередВыгрузкойОбъекта",
                "ПослеВыгрузкиОбъекта",
                "ПослеОбработкиПравила",
                "ПередКонвертациейОбъекта",
                "ПослеЗагрузкиПравилОбмена",
                "ПередВыгрузкойДанных",
                "ПослеВыгрузкиДанных",
                "ПередЗагрузкойОбъекта",
                "ПослеЗагрузкиОбъекта",
                "ПослеЗагрузкиДанных",
                "ПередОбработкойВыгрузки",
                "ПослеОбработкиВыгрузки",
            ):
                continue
            expanded = _expand(handler.text, algorithms)
            result.append((occurrence.target, occurrence.incoming, _strip_comments(expanded.code)))
    return result


def _conversion_code(node: Node) -> str:
    return rule_code(node.get("КодПравилаКонвертации")).strip()


def call_site(handler: Handler, location: HandlerFile, position: int) -> dict[str, Any]:
    """Позиция в исходном обработчике → строка обёртки и строка тела, обе с единицы."""
    line = handler.text.count("\n", 0, position) + 1
    return {
        "address": handler.address,
        "handler": handler.event,
        "file": location.name,
        "line": line + location.line_offset,
        "handler_line": line,
    }


def _positions(text: str, *, strings: bool) -> str:
    """Маска с сохранением позиций, включая многострочные литералы."""
    pattern = re.compile(r'"(?:[^"]|"")*"|//[^\n]*')

    def replace(match: re.Match[str]) -> str:
        if match.group().startswith('"') and not strings:
            return match.group()
        return "".join("\n" if c == "\n" else " " for c in match.group())

    return pattern.sub(replace, text)


def _target_names(expression: str, nesting: int = 0) -> list[str]:
    """Только литерал либо ветви тернарного выражения; строки условия не являются целями."""
    if nesting >= 30:
        return []
    parts = tokens(expression)
    if len(parts) == 1 and parts[0].string and parts[0].closed:
        return [parts[0].value] if parts[0].value else []
    if len(parts) < 4 or parts[0].value != "?" or parts[1].value != "(":
        return []
    depth = 0
    for pos, token in enumerate(parts[1:], 1):
        if token.string:
            continue
        if token.value == "(":
            depth += 1
        elif token.value == ")":
            depth -= 1
            if depth == 0 and pos != len(parts) - 1:
                return []
    arguments = _call_arguments(expression, parts[1].start)
    if depth or arguments is None or len(arguments) != 3:
        return []
    return [*_target_names(arguments[1], nesting + 1), *_target_names(arguments[2], nesting + 1)]


_STRING = re.compile(r'"(?:[^"]|"")*"')


_ALGORITHM = re.compile(
    r"(?<![\w.])Алгоритмы\s*\.\s*([^\W\d]\w*)\b(?!\s*\()",
    re.IGNORECASE,
)


_EXECUTE_OPAQUE = re.compile(r'(?<![\w.])Выполнить\s*\(\s*(?!")', re.IGNORECASE)


_ALGORITHM_EXECUTE = re.compile(
    r"(?<![\w.])Выполнить\s*\(\s*Алгоритмы\s*\.\s*([^\W\d]\w*)\b\s*\)",
    re.IGNORECASE,
)


_EXECUTE_STRING = re.compile(
    r'(?<![\w.])Выполнить\s*\(\s*("(?:[^"]|"")*")\s*\)',
    re.IGNORECASE,
)


_EXPORT_CALL = re.compile(
    r"(?<![\w.])(ВыгрузитьПоПравилу|ВыгрузитьРегистр|ВыгрузкаОбъектаВыборки)\s*\(",
    re.IGNORECASE,
)


_EXPORT_ARGUMENTS = {
    "выгрузитьпоправилу": (3, 5),
    "выгрузитьрегистр": (3, 5),
    "выгрузкаобъектавыборки": (4, 9),
}


@dataclass(frozen=True, slots=True)
class _Text:
    """Развёрнутый текст обработчиков и признак непрозрачности."""

    code: str
    opaque: bool


def _call_arguments(code: str, open_paren: int) -> list[str] | None:
    """Аргументы вызова, `open_paren` — индекс «(». `None` — скобка не закрыта."""
    depth = 0
    start = open_paren + 1
    arguments: list[str] = []
    index = open_paren
    in_string = False
    while index < len(code):
        char = code[index]
        if in_string:
            if char == '"':
                if index + 1 < len(code) and code[index + 1] == '"':
                    index += 2
                    continue
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                arguments.append(code[start:index])
                return arguments
        elif char == "," and depth == 1:
            arguments.append(code[start:index])
            start = index + 1
        index += 1
    return None


def _unescape_string(literal: str) -> str:
    text = literal.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text.replace('""', '"')


def _expand(text: str, algorithms: dict[str, str], seen: set[str] | None = None) -> _Text:
    """Текст с телами вызванных алгоритмов.

    Непрозрачен `Выполнить` не со строкой и алгоритм, которого в правилах нет.
    """
    if seen is None:
        seen = set()
    if not text.strip():
        return _Text("", False)
    code = _strip_comments(text)
    opaque = _EXECUTE_OPAQUE.search(code) is not None
    parts = [text]
    for match in _ALGORITHM.finditer(_mask(code)):
        key = match.group(1).casefold()
        if key in seen:
            continue
        seen.add(key)
        body = algorithms.get(key)
        if body is None:
            opaque = True
            continue
        nested = _expand(body, algorithms, seen)
        parts.append(nested.code)
        opaque = opaque or nested.opaque
    return _Text("\n".join(parts), opaque)


def _algorithms(rules: ExchangeRules) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in rules.algorithms():
        name = str(node.attrs.get("Имя", "")).strip()
        if name:
            found[name.casefold()] = str(node.get("Текст"))
    return found


def _event(rules: ExchangeRules, name: str) -> str:
    value = rules.root.get(name)
    return value if isinstance(value, str) else ""


def _disabled(node: Node) -> bool:
    flag = node.attrs.get("Отключить")
    return flag is True or flag == "true"


def _strip_comments(text: str) -> str:
    return "\n".join(_without_comment(line) for line in text.splitlines())


def _without_comment(line: str) -> str:
    """Строка до комментария `//` вне строкового литерала."""
    quoted = False
    index = 0
    chars: list[str] = []
    while index < len(line):
        char = line[index]
        if char == '"':
            chars.append(char)
            if quoted and index + 1 < len(line) and line[index + 1] == '"':
                chars.append('"')
                index += 2
                continue
            quoted = not quoted
        elif not quoted and line.startswith("//", index):
            break
        else:
            chars.append(char)
        index += 1
    return "".join(chars)


def _mask(code: str) -> str:
    """Строковые литералы заменены пустыми кавычками: вызов в тексте сообщения не считается."""
    return _STRING.sub('""', code)


def _literals(code: str) -> list[str]:
    return [match.group(0)[1:-1].replace('""', '"') for match in _STRING.finditer(code)]


PKS_HANDLERS = ("ПередВыгрузкой", "ПриВыгрузке", "ПослеВыгрузки")


PKO_HANDLERS = (
    "ПередВыгрузкой",
    "ПриВыгрузке",
    "ПослеВыгрузки",
    "ПослеВыгрузкиВФайл",
    "ПередЗагрузкой",
    "ПриЗагрузке",
    "ПослеЗагрузки",
)


_EXPORT_OBJECT_TRUE = re.compile(r"ВыгрузитьОбъект\s*=\s*Истина", re.IGNORECASE)
