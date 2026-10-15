"""Чтение чужих правил: трасса объекта, поиск обработчиков и запросы."""

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kd_rules_mcp.errors import Kd2Error
from kd_rules_mcp.kd2.model import ExchangeRules, RegistrationRules, object_name
from kd_rules_mcp.kd2.queries import parse_queries
from kd_rules_mcp.kd2.rules_io import load_rules
from kd_rules_mcp.service.base import ServiceBase
from kd_rules_mcp.service.views import pro_view, slice_rows
from kd_rules_mcp.validation.address import pro_addresses, rule_address, side_name
from kd_rules_mcp.validation.call_graph import (
    CallEdge,
    _pks_references,
    build_call_graph,
    call_site,
    scope_call,
    validation_evidence,
)
from kd_rules_mcp.validation.exchange_plan import check_exchange_plan
from kd_rules_mcp.validation.handlers import Handler, HandlerFile, handler_sources
from kd_rules_mcp.validation.structure import (
    check_structures,
    ref_only_pko_codes,
    unreachable_pko_codes,
)

_type_name = object_name


class ReviewMixin(ServiceBase):
    """Инструменты ревью работают с моделью открытого проекта, включая несохранённые правки."""

    _trace_cache: tuple[ExchangeRules, tuple[Any, ...], dict[str, Any]] | None = None
    _adjacent_registration: tuple[Path, int, int, RegistrationRules] | None = None

    def _trace_analysis(self, rules: ExchangeRules) -> dict[str, Any]:
        """Переиспользует факты неизменённой модели; любая правка узла сбрасывает результат.

        Ключ сравнивается целиком, без хеша и без повторного чтения/разбора XML.
        Хранится только последняя модель, поэтому закрытые проекты не накапливаются.
        """

        def signature() -> tuple[Any, ...]:
            return tuple(
                (id(n), n.tag, n.text, tuple(n.attrs.items()), tuple(n.values.items()))
                for n in rules.root.walk_all()
            )

        current = signature()
        if self._trace_cache and self._trace_cache[0] is rules and self._trace_cache[1] == current:
            return self._trace_cache[2]
        graph = build_call_graph(rules)
        checks = check_exchange_plan(rules, None, None, graph=graph)
        checks.extend(check_structures(rules, None, None, graph=graph))
        result: dict[str, Any] = {
            "index": graph.index,
            "graph": graph,
            "edges": graph.edges,
            "unreachable": unreachable_pko_codes(rules, graph),
            "ref_only": ref_only_pko_codes(rules, graph),
            "reference_modes": _pks_references(rules, graph),
            "checks": checks,
            "evidence": {},
        }
        self._trace_cache = rules, signature(), result
        return result

    def _review_handlers(self, project_id: str) -> list[tuple[Handler, HandlerFile]]:
        document = self._document(project_id)
        if not isinstance(document, ExchangeRules | RegistrationRules):
            raise Kd2Error("Нужны правила обмена или регистрации")
        return handler_sources(document)

    def _select_handlers(
        self, project_id: str, address: str = "", file: str = "", handler: str | None = None
    ) -> list[tuple[Handler, HandlerFile]]:
        if bool(address) == bool(file):
            raise Kd2Error("Укажите ровно одно поле: address или file")
        found = [
            (h, loc)
            for h, loc in self._review_handlers(project_id)
            if (h.address == address if address else loc.name == Path(file).name)
            and (handler is None or h.event == handler)
        ]
        if not found:
            raise Kd2Error("Обработчик не найден в текущем снимке правил")
        return found

    def handlers_read(
        self, project_id: str, address: str, handler: str, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        with self._lock:
            h, loc = self._select_handlers(project_id, address, handler=handler)[0]
            rows = [
                {"line": loc.body_start + index, "handler_line": index + 1, "text": text}
                for index, text in enumerate(h.text.splitlines())
            ]
            return {
                "address": h.address,
                "handler": h.event,
                "file": loc.name,
                **slice_rows(rows, offset, limit),
            }

    def handlers_search(
        self,
        project_id: str,
        query: str,
        kind: str = "substring",
        rule: str | None = None,
        handler: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not query:
            raise Kd2Error("Пустая строка поиска")
        if len(query) > 256:
            raise Kd2Error("Длина строки поиска ограничена 256 символами")
        if kind not in ("substring", "regex"):
            raise Kd2Error("kind: substring или regex")
        try:
            pattern = re.compile(query, re.IGNORECASE) if kind == "regex" else None
        except re.error as error:
            raise Kd2Error(f"Неверное регулярное выражение: {error}") from error
        with self._lock:
            rows: list[dict[str, Any]] = []
            for h, loc in self._review_handlers(project_id):
                if rule and h.address != rule and not h.address.startswith(f"{rule} / "):
                    continue
                if handler and h.event != handler:
                    continue
                for number, line in enumerate(h.text.splitlines(), 1):
                    if pattern.search(line) if pattern else query.casefold() in line.casefold():
                        rows.append(
                            {
                                "address": h.address,
                                "handler": h.event,
                                "file": loc.name,
                                "line": number + loc.line_offset,
                                "handler_line": number,
                                "text": line,
                            }
                        )
            return slice_rows(rows, offset, limit)

    def handlers_queries(
        self,
        project_id: str,
        address: str = "",
        file: str = "",
        handler: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        with self._lock:
            rows: list[dict[str, Any]] = []
            document = self._document(project_id)
            named = (
                {query.code: str(query.get("Текст")) for query in document.queries()}
                if isinstance(document, ExchangeRules)
                else {}
            )
            for h, loc in self._select_handlers(project_id, address, file, handler):
                parsed = parse_queries(h.text, loc.line_offset, named)
                rows.extend(
                    {"address": h.address, "handler": h.event, "file": loc.name, **batch}
                    for batch in parsed["batches"]
                )
                rows.extend(
                    {
                        "address": h.address,
                        "handler": h.event,
                        "file": loc.name,
                        "complete": False,
                        "diagnostic": diagnostic,
                    }
                    for diagnostic in parsed["diagnostics"]
                )
            return slice_rows(rows, offset, limit)

    def _trace_registration(self, project_id: str, object_name: str) -> dict[str, Any]:
        project = self.workspace.get(project_id)
        folder = project.source_path.parent if project.source_path else None
        documents: list[tuple[str, RegistrationRules]] = []
        paths: set[Path] = set()
        for other_id in self.workspace.ids():
            other = self.workspace.get(other_id)
            if other.kind != "registration":
                continue
            if folder is not None and other.source_path and other.source_path.parent != folder:
                continue
            document = self._document(other_id)
            if isinstance(document, RegistrationRules):
                documents.append((other_id, document))
                if other.source_path:
                    paths.add(other.source_path)
        # Имена стандартного комплекта БСП; макеты конфигурации лежат в соседнем разделе.
        candidates = (
            [folder / "RegistrationRules.xml", folder / "ПравилаРегистрации.xml"] if folder else []
        )
        if folder and folder.name == "Ext":
            candidates.append(folder.parent.parent / "ПравилаРегистрации" / "Ext" / "Template.txt")
        errors: list[str] = []
        for path in candidates:
            if path in paths or not path.is_file():
                continue
            try:
                stat = path.stat()
                cached = self._adjacent_registration
                if cached and cached[:3] == (path, stat.st_mtime_ns, stat.st_size):
                    document = cached[3]
                else:
                    document = load_rules(path)
                if isinstance(document, RegistrationRules):
                    self._adjacent_registration = path, stat.st_mtime_ns, stat.st_size, document
                    documents.append((path.name, document))
            except (Kd2Error, OSError, ValueError) as error:
                errors.append(f"{path.name}: {type(error).__name__}")
        rows: list[dict[str, Any]] = []
        for origin, document in documents:
            nodes = document.rules()
            for node, address in zip(nodes, pro_addresses(nodes), strict=True):
                if _type_name(str(node.get("ОбъектМетаданныхИмя"))) == _type_name(object_name):
                    rows.append(
                        {
                            "origin": origin,
                            **pro_view(node, address, ""),
                            "node_mode_value": "unknown",
                        }
                    )
        return {"available": bool(documents), "items": rows, "read_errors": errors}

    def rules_trace(
        self,
        project_id: str,
        object: str,
        direction: str = "export",
        depth: int = 2,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if direction not in ("export", "import") or not 0 <= depth <= 10:
            raise Kd2Error("direction: export/import; depth: 0..10")
        with self._lock:
            rules = self._exchange(project_id)
            side = "Источник" if direction == "export" else "Приемник"
            matching = [
                pko
                for pko in rules.pko()
                if pko.code.casefold() == object.casefold()
                or _type_name(str(pko.get(side))) == _type_name(object)
            ]
            selected = [
                p
                for p in rules.pko()
                if p in matching
                or any(
                    str(root.get(side)).strip()
                    and _type_name(p.get(side)) == _type_name(root.get(side))
                    for root in matching
                )
            ]
            selected_branches = [p for p in rules.pvd() if p.code.casefold() == object.casefold()]
            if selected_branches:
                selected.extend(
                    pko
                    for pko in rules.pko()
                    if pko not in selected
                    and any(
                        _type_name(str(pko.get("Источник")))
                        == _type_name(str(pvd.get("ОбъектВыборки")))
                        for pvd in selected_branches
                    )
                )
            object_names = {str(pko.get("Источник")) for pko in selected}
            object_names.update(str(p.get("ОбъектВыборки")) for p in selected_branches)
            if not object_names:
                object_names.add(object)
            analysis = self._trace_analysis(rules)
            graph = analysis["graph"]
            index = analysis["index"]
            edges = analysis["edges"]
            unreachable = analysis["unreachable"]
            ref_only = analysis["ref_only"]
            reference_modes = analysis["reference_modes"]
            checks = analysis["checks"]
            branches: list[dict[str, Any]] = []
            calls: list[tuple[CallEdge, str | None]] = []
            cascades: list[dict[str, Any]] = []
            pko_rows: list[dict[str, Any]] = []
            for branch in graph.branches:
                pvd = branch.node
                name = str(pvd.get("ОбъектВыборки"))
                disabled = branch.disabled
                active = branch.first_for_type
                if not any(_type_name(name) == _type_name(n) for n in object_names):
                    continue
                address = rule_address(pvd)
                branches.append(
                    {
                        "address": address,
                        "order": branch.order,
                        "disabled": disabled,
                        "first_for_type": active,
                        "plan_membership": "unknown",
                        "evidence": ["БСП:6768", "БСП:17931", "БСП:18361-18367"],
                        "checks": [
                            issue.to_dict() for issue in checks.issues if issue.address == address
                        ],
                    }
                )
                if not disabled:
                    calls.extend((edge, address) for edge in graph.from_address(address))

            seen: dict[str, int] = {}
            depth_limited = False
            by_code = {p.code.casefold(): p for p in rules.pko()}

            def visit(code: str, level: int, path: tuple[str, ...]) -> None:
                nonlocal depth_limited
                pko = by_code.get(code.casefold())
                if (
                    pko is None
                    or seen.get(code.casefold(), depth + 1) <= level
                    or index[rule_address(pko)][1]
                ):
                    return
                seen[code.casefold()] = level
                address = rule_address(pko)
                if pko.code not in analysis["evidence"]:
                    analysis["evidence"][pko.code] = validation_evidence(rules, pko.code, graph)
                pko_rows.append(
                    {
                        "address": address,
                        "code": pko.code,
                        "source": pko.get("Источник"),
                        "target": pko.get("Приемник"),
                        "reference_only": pko.code in ref_only,
                        "unreachable": pko.code in unreachable,
                        "validation_evidence": slice_rows(analysis["evidence"][pko.code], 0, limit),
                        "checks": [
                            issue.to_dict() for issue in checks.issues if issue.address == address
                        ],
                    }
                )
                calls.extend(
                    (edge, None) for edge in edges if edge.target.casefold() == code.casefold()
                )
                outgoing = graph.from_address(address, properties=True)
                calls.extend((edge, None) for edge in outgoing if edge.kind == "dynamic_call")
                if level >= depth:
                    depth_limited = depth_limited or any(
                        edge.address == address or edge.address.startswith(f"{address} / ")
                        for edge in edges
                    )
                    return
                for edge in outgoing:
                    if edge.kind not in ("field", "type_lookup"):
                        visit(edge.target, level + 1, (*path, edge.target))
                for edge in outgoing:
                    if edge.kind not in ("field", "type_lookup") or not edge.address.startswith(
                        f"{address} / ПКС "
                    ):
                        continue
                    prop_address, target = edge.address, edge.target
                    linked = by_code.get(target.casefold())
                    linked_properties = linked.child("Свойства") if linked else None
                    mode = next(
                        (r for r in reference_modes.get(target, []) if r.address == prop_address),
                        None,
                    )
                    cascades.append(
                        {
                            "address": prop_address,
                            "target": target,
                            "kind": edge.kind,
                            "depth": level + 1,
                            "path": [*path, target],
                            "reference_only": target in ref_only,
                            "validation_mode": "whole"
                            if mode and mode.exports_whole
                            else "reference",
                            "НеВыгружатьОбъектыСвойствПоСсылкам": linked.get(
                                "НеВыгружатьОбъектыСвойствПоСсылкам"
                            )
                            is True
                            if linked
                            else None,
                            "registration_override": "unknown",
                            "search_fields": [
                                side_name(n, "Приемник")
                                for n in (linked_properties.items if linked_properties else [])
                                if n.attrs.get("Поиск") is True
                                and n.attrs.get("Отключить") is not True
                            ],
                            "evidence": ["БСП:12784", "БСП:12822", "БСП:13170-13196"],
                        }
                    )
                    visit(target, level + 1, (*path, target))

            branch_targets = [edge.target for edge, _ in calls]
            for pko in selected:
                visit(pko.code, 0, (pko.code,))
            for target in branch_targets:
                visit(target, 0, (target,))
            events = [
                call_site(h, loc, 0)
                for h, loc in handler_sources(rules)
                if h.address == "Конвертация"
                and h.event
                in (
                    "ПередКонвертациейОбъекта",
                    "ПередВыгрузкойОбъекта",
                    "ПослеВыгрузкиОбъекта",
                    "ПередЗагрузкойОбъекта",
                    "ПослеЗагрузкиОбъекта",
                )
            ]
            registrations = [
                self._trace_registration(project_id, name) for name in sorted(object_names)
            ]
            unique_calls: dict[tuple[Any, ...], dict[str, Any]] = {}
            roots = {rule_address(by_code[code]) for code in seen}
            roots.update(branch["address"] for branch in branches)
            roots.add("Конвертация")
            for edge, branch in calls:
                call = asdict(scope_call(edge, roots))
                if branch is not None:
                    call["branch"] = branch
                key = (
                    call["target"],
                    call["address"],
                    call["kind"],
                    tuple(
                        (site["address"], site["handler"], site["line"])
                        for site in call.get("chain", ())
                    ),
                )
                unique_calls.setdefault(key, call)
            return {
                "object": (str(selected[0].get(side)) or selected[0].code) if selected else object,
                "found": bool(selected or branches),
                "status": "found" if selected or branches else "not_found",
                "direction": direction,
                "depth": depth,
                "static": True,
                "depth_limited": depth_limited,
                "registration": {
                    "available": any(r["available"] for r in registrations),
                    **slice_rows([row for r in registrations for row in r["items"]], offset, limit),
                    "read_errors": [e for r in registrations for e in r["read_errors"]],
                },
                "branches": slice_rows(branches, offset, limit),
                "pko": slice_rows(
                    list({row["address"]: row for row in pko_rows}.values()), offset, limit
                ),
                "calls": slice_rows(list(unique_calls.values()), offset, limit),
                "cascades": slice_rows(
                    list(
                        {
                            (row["address"], row["target"]): row for row in reversed(cascades)
                        }.values()
                    ),
                    offset,
                    limit,
                ),
                "events": slice_rows(events, offset, limit),
            }
