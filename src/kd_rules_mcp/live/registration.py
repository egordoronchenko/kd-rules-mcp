"""Свод ответа live_registration_check: форма сырого JSON и объяснение по узлу.

База отдаёт факты. Здесь они проверяются и сводятся так, как БСП сводит группы:
непосчитанное остаётся неизвестным, расхождение с полным текстом ПРО — mismatch.
"""

from typing import Any, NoReturn

from kd_rules_mcp.errors import LiveParseError

_NODE_LIMIT = 100
_NEGATIVE_MODES = frozenset({"ВыгружатьВручную", "НеВыгружать"})
_SKIP_OUTSIDE = "план вне обмена БСП"
_SKIP_AUTO = "авторегистрация: регистрирует платформа"
_HINT_VERSION = "учтена версия объекта в базе (ПроверятьСсылку)"
_HINT_SUBSCRIPTION = "подписка или модуль объекта"
_HINT_DIRECT = "зарегистрировано напрямую (подписка или модуль объекта)"
_HINT_NOT_MODIFIED = "объект не изменён для плана или запись отклонена"
_MISMATCH_BSP = "свод условий узла не совпал с расчётом БСП"
_MISMATCH_NODE = "узел без причины"
_MISMATCH_OUTSIDE = "получатель вне узлов плана"
_PARTIAL = "итог отбора по объекту неизвестен и решает исход"
_UNKNOWN = "итог отбора по объекту неизвестен"
_CONDITIONS = "условия выполнены"
_OBJECT_FAILED = "отбор по объекту не выполнен"
_NODE_FAILED = "узел не прошёл условия"
_DELETED = "узел помечен на удаление"
_IF_NEEDED_YES = "режим ВыгружатьПриНеобходимости, объект выгружался"
_IF_NEEDED_NO = "режим ВыгружатьПриНеобходимости, объект в узел не выгружался"
_IF_NEEDED_UNKNOWN = "неизвестно, выгружался ли объект в узел"
_CODES = "коды узлов плана не уникальны"
_UNAVAILABLE = "условие узла не посчитано"
_ALWAYS = "режим ВыгружатьВсегда"

_TOP = frozenset(
    {"object", "changes", "before", "plans_without_nodes", "plans", "write", "after", "cache"}
)
_CACHE = frozenset({"mode", "checked"})
# ОДСл:87–103 — штатная проверка перечитывает правила только при смене даты МРО (константа).
CACHE_FALLBACK_WARNING = (
    "в БСП базы нет ОбменДаннымиСлужебный.ОбновитьКэшМеханизмаРегистрацииОбъектов: кэш сброшен "
    "ОбновитьПовторноИспользуемыеЗначения() и штатной проверкой; после смены правил ответ может "
    "опираться на старые правила"
)
CACHE_UNCHECKED_WARNING = (
    "в БСП базы нет ни ОбновитьКэшМеханизмаРегистрацииОбъектов, "
    "ни ПроверитьКэшМеханизмаРегистрацииОбъектов: сброшены только повторно используемые значения; "
    "правила регистрации сеанса могут быть старыми"
)
_OBJECT = frozenset({"name", "ref", "presentation", "new", "modified"})
_REGISTRATION = frozenset({"plan", "code", "name", "message"})
_PLAN = frozenset(
    {
        "plan",
        "bsp",
        "autoregistration",
        "rules",
        "nodes",
        "recipients",
        "recipients_error",
        "detail",
    }
)
_NODE = frozenset({"code", "name", "deleted"})
_RECIPIENT = frozenset({"code", "name"})
_WRITE = frozenset({"error", "recipients", "after_write"})
_WRITE_RECIPIENT = frozenset({"plan", "code", "name"})
_DETAIL = frozenset(
    {
        "flag_attribute",
        "handlers",
        "manager",
        "object_filter",
        "node_filter",
        "nodes_by_bsp",
        "nodes_by_condition",
        "modes",
        "if_needed",
        "error",
    }
)
_OBJECT_ROW = frozenset(
    {
        "path",
        "group",
        "and",
        "property",
        "comparison",
        "kind",
        "expected",
        "actual",
        "result",
        "dropped",
        "algorithm",
        "error",
    }
)
_NODE_ROW = frozenset(
    {
        "path",
        "group",
        "op",
        "attribute",
        "table",
        "comparison",
        "query_operator",
        "operand",
        "nodes",
        "error",
    }
)
_MODE = frozenset({"code", "mode"})


def parse_registration(data: dict[str, Any], *, wrote: bool) -> dict[str, Any]:
    """Строгая форма сырого ответа. Любое отклонение — LiveParseError."""
    if set(data) != _TOP:
        _bad("registration")
    parsed = {
        "object": _object(data["object"]),
        "changes": _string_map(data["changes"], "changes"),
        "before": _registrations(data["before"], "before"),
        "plans_without_nodes": _strings(data["plans_without_nodes"], "plans_without_nodes"),
        "plans": [_plan(item, index) for index, item in enumerate(_list(data["plans"], "plans"))],
        "write": _write(data["write"]),
        "after": _registrations(data["after"], "after") if data["after"] is not None else None,
        "cache": _cache(data["cache"]),
    }
    if wrote:
        if parsed["write"] is None or parsed["after"] is None:
            _bad("write")
    elif parsed["write"] is not None or parsed["after"] is not None:
        _bad("write")
    return parsed


def same_registration(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool:
    """Узел и номер сообщения; наименование и порядок строк не входят в сверку отката."""

    def key(rows: list[dict[str, Any]]) -> list[tuple[str, str, int | None]]:
        return sorted((row["plan"], row["code"], row["message"]) for row in rows)

    return key(before) == key(after)


def summarize_registration(data: dict[str, Any]) -> dict[str, Any]:
    """Ответ инструмента: разница правил и записи и объяснение, прошедшее самопроверку."""
    written_by_plan = _written_index(data["write"], data["before"])
    hint = _HINT_VERSION if data["object"]["new"] is False else _HINT_SUBSCRIPTION
    plans = [
        _public_plan(plan, data["before"], written_by_plan, hint, data["write"])
        for plan in data["plans"]
    ]
    summary: dict[str, Any] = {
        "object": dict(data["object"]),
        "changes": dict(data["changes"]),
        "plans": plans,
        "plans_without_nodes": list(data["plans_without_nodes"]),
        "write": None
        if data["write"] is None
        else {"error": data["write"]["error"], "rollback_verified": True},
        "cache": data["cache"]["mode"],
    }
    if data["cache"]["mode"] == "fallback":
        summary["cache_warning"] = (
            CACHE_FALLBACK_WARNING if data["cache"]["checked"] else CACHE_UNCHECKED_WARNING
        )
    return summary


def _public_plan(
    plan: dict[str, Any],
    before: list[dict[str, Any]],
    written_by_plan: dict[str, list[dict[str, str]]] | None,
    hint_written: str,
    write: dict[str, Any] | None,
) -> dict[str, Any]:
    name = plan["plan"]
    registered_full = _rows_for_plan(before, name)
    recipients = plan["recipients"]
    recipients_full = (
        None
        if recipients is None
        else [{"code": item["code"], "name": item["name"]} for item in recipients]
    )
    written_rows = (
        None
        if write is None or write["error"] is not None or written_by_plan is None
        else written_by_plan.get(name, [])
    )
    written_full = (
        None
        if written_rows is None
        else [{"code": item["code"], "name": item["name"]} for item in written_rows]
    )
    only_written_full, only_rules_full = _difference(recipients_full, written_rows, hint_written)
    registered, registered_cut = _cap(registered_full)
    public_recipients, recipients_cut = _cap(recipients_full)
    written, written_cut = _cap(written_full)
    only_written, only_written_cut = _cap(only_written_full)
    only_rules, only_rules_cut = _cap(only_rules_full)
    skipped = None
    if not plan["bsp"]:
        skipped = _SKIP_OUTSIDE
    elif plan["autoregistration"]:
        skipped = _SKIP_AUTO
    result: dict[str, Any] = {
        "plan": name,
        "bsp": plan["bsp"],
        "autoregistration": plan["autoregistration"],
        "rules": plan["rules"],
        "no_rules": plan["bsp"] and not plan["autoregistration"] and plan["rules"] == 0,
        "nodes": len(plan["nodes"]),
        "registered": registered,
        "recipients": public_recipients,
        "recipients_skipped": skipped,
        "recipients_error": plan["recipients_error"],
        "written": written,
        "only_written": only_written,
        "only_rules": only_rules,
        "explanation": _explanation(plan, recipients),
    }
    if any((registered_cut, recipients_cut, written_cut, only_written_cut, only_rules_cut)):
        result["truncated"] = True
    return result


def _difference(
    recipients: list[dict[str, str]] | None,
    written: list[dict[str, str]] | None,
    hint_written: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    # Оба списка должны быть посчитаны: пропущенные получатели не становятся «только запись».
    if recipients is None or written is None:
        return [], []
    recipient_codes = {item["code"] for item in recipients}
    written_codes = {item["code"] for item in written}
    only_written = [
        {
            "code": item["code"],
            "name": item["name"],
            "hint": _HINT_DIRECT if item["direct"] else hint_written,
        }
        for item in written
        if item["code"] not in recipient_codes
    ]
    only_rules = [
        {"code": item["code"], "name": item["name"], "hint": _HINT_NOT_MODIFIED}
        for item in recipients
        if item["code"] not in written_codes
    ]
    return only_written, only_rules


def _explanation(
    plan: dict[str, Any], recipients: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    detail = plan["detail"]
    if not detail:
        return None
    universe = {node["code"] for node in plan["nodes"] if not node["deleted"]}
    recipient_codes = set() if recipients is None else {item["code"] for item in recipients}
    node_codes = {node["code"] for node in plan["nodes"]}
    outside = recipients is not None and any(item["code"] not in node_codes for item in recipients)
    views = [_rule_view(item, universe) for item in detail]
    rules_out = []
    problems = [view["problem"] for view in views if view["problem"]]
    mismatches = [view for view in views if view["consistent"] is False]
    node_verdicts = [
        _node_verdict(node, views, node["code"] in recipient_codes) for node in plan["nodes"]
    ]
    if not _codes_unique(plan["nodes"]):
        status, reason = "unavailable", _CODES
    elif recipients is None:
        status, reason = "unavailable", plan["recipients_error"] or _UNAVAILABLE
    elif problems:
        status, reason = "unavailable", problems[0]
    elif outside:
        status, reason = "mismatch", _MISMATCH_OUTSIDE
    elif mismatches:
        status, reason = "mismatch", _MISMATCH_BSP
    else:
        status, reason = _plan_status(node_verdicts)
    for view, source in zip(views, detail, strict=True):
        nodes, cut = _cap(
            [_public_node(node, view, node["code"] in recipient_codes) for node in plan["nodes"]]
        )
        rule: dict[str, Any] = {
            "flag_attribute": source["flag_attribute"],
            "handlers": list(source["handlers"]),
            "object_filter": {
                "result": view["object_result"],
                "conditions": _public_rows(source["object_filter"], node_side=False),
            },
            "node_filter": {
                "conditions": _public_rows(source["node_filter"], node_side=True),
                "consistent": view["consistent"],
            },
            "nodes": nodes,
        }
        if cut:
            rule["truncated"] = True
        rules_out.append(rule)
    return {
        "status": status,
        "reference": any(item["handlers"] or item["manager"] for item in detail),
        "reason": reason,
        "rules": rules_out,
    }


def _plan_status(verdicts: list[str]) -> tuple[str, str | None]:
    if any(item == "mismatch" for item in verdicts):
        return "mismatch", _MISMATCH_NODE
    if any(item == "partial_mode" for item in verdicts):
        return "partial", _IF_NEEDED_UNKNOWN
    if any(item == "partial" for item in verdicts):
        return "partial", _PARTIAL
    return "ok", None


def _codes_unique(nodes: list[dict[str, Any]]) -> bool:
    codes = [node["code"] for node in nodes]
    return all(code != "" for code in codes) and len(set(codes)) == len(codes)


def _node_verdict(node: dict[str, Any], views: list[dict[str, Any]], recipient: bool) -> str:
    signals = [_signal(view, node) for view in views]
    explains_in = [item["include"] for item in signals if item["include"]]
    confident = any(item["confident"] for item in signals)
    undecided = any(item["unknown"] for item in signals)
    unknown_mode = any(item["unknown_mode"] for item in signals)
    if recipient:
        if explains_in:
            return "ok"
        if unknown_mode:
            return "partial_mode"
        if undecided:
            return "partial"
        return "mismatch"
    if confident:
        return "mismatch"
    if signals and all(item["exclude"] for item in signals):
        return "ok"
    if unknown_mode:
        return "partial_mode"
    if undecided:
        return "partial"
    return "mismatch"


def _public_node(node: dict[str, Any], view: dict[str, Any], recipient: bool) -> dict[str, Any]:
    signal = _signal(view, node)
    failed: list[str] = []
    if recipient and signal["include"]:
        reason = signal["include"]
    elif not recipient and signal["exclude"]:
        reason = signal["exclude"]
        if reason == _NODE_FAILED:
            failed = signal["failed"]
    elif signal["unknown"]:
        reason = signal["unknown_reason"]
    else:
        reason = signal["include"] or signal["exclude"] or ""
        if reason == _NODE_FAILED:
            failed = signal["failed"]
    return {
        "code": node["code"],
        "name": node["name"],
        "deleted": node["deleted"],
        "recipient": recipient,
        "by_rule": signal["by_rule"],
        "mode": signal["mode"],
        "reason": reason,
        "failed": failed,
    }


def _signal(view: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    # Пометка удаления раньше режима и условий: ОДСоб:2193–2196 и 1713.
    # «При необходимости» включает узел только при соответствии (ОДСоб:1669–1675, 1734–1770).
    code = node["code"]
    mode = view["modes"].get(code) or None
    object_result = view["object_result"]
    condition = view["condition"]
    in_condition = condition is not None and code in condition
    if_needed = view["if_needed"]
    include = None
    exclude = None
    failed: list[str] = []
    unknown = False
    unknown_mode = False
    unknown_reason = _UNKNOWN
    by_rule: bool | None = False
    if node["deleted"]:
        exclude = _DELETED
    elif mode in _NEGATIVE_MODES:
        exclude = f"режим {mode}"
    elif mode == "ВыгружатьВсегда":
        include = _ALWAYS
        by_rule = True
    elif mode == "ВыгружатьПриНеобходимости":
        if if_needed is None:
            unknown = True
            unknown_mode = True
            unknown_reason = _IF_NEEDED_UNKNOWN
            by_rule = None
        elif code in if_needed:
            include = _IF_NEEDED_YES
            by_rule = True
        else:
            exclude = _IF_NEEDED_NO
    elif object_result is True and in_condition:
        include = _CONDITIONS
        by_rule = True
    elif object_result is False:
        exclude = _OBJECT_FAILED
    elif condition is not None and not in_condition:
        exclude = _NODE_FAILED
        failed = _missing_leaves(view["node_roots"], code)
    elif object_result is None or condition is None:
        unknown = True
        by_rule = None
    confident = include in {_CONDITIONS, _ALWAYS, _IF_NEEDED_YES}
    return {
        "include": include,
        "exclude": exclude,
        "unknown": unknown,
        "unknown_mode": unknown_mode,
        "unknown_reason": unknown_reason,
        "failed": failed,
        "confident": confident,
        "by_rule": by_rule,
        "mode": mode,
    }


def _rule_view(rule: dict[str, Any], universe: set[str]) -> dict[str, Any]:
    object_roots = _tree(rule["object_filter"], "object_filter")
    node_roots = _tree(rule["node_filter"], "node_filter")
    object_result = _fold_object(object_roots, conjunction=True)
    folded = _fold_nodes(node_roots, conjunction=True, universe=universe)
    bsp = rule["nodes_by_bsp"]
    consistent = None if rule["error"] or folded is None or bsp is None else folded == set(bsp)
    problem = None
    if rule["error"]:
        problem = rule["error"]
    elif consistent is None:
        problem = _leaf_error(node_roots) or _UNAVAILABLE
    return {
        "object_result": object_result,
        "consistent": consistent,
        "problem": problem,
        "condition": None
        if rule["nodes_by_condition"] is None
        else set(rule["nodes_by_condition"]),
        "modes": {item["code"]: item["mode"] or None for item in rule["modes"]},
        "if_needed": None if rule["if_needed"] is None else set(rule["if_needed"]),
        "node_roots": node_roots,
    }


def _fold_object(nodes: list[dict[str, Any]], *, conjunction: bool) -> bool | None:
    # И: false важнее null; ИЛИ: true важнее null. Пустая И — истина, пустая ИЛИ — ложь.
    # ОДСоб:2601–2641. dropped БСП выбрасывает из дерева до сравнения.
    values: list[bool | None] = []
    for node in nodes:
        if node["group"]:
            values.append(_fold_object(node["children"], conjunction=node["and"]))
        elif not node["dropped"]:
            values.append(node["result"])
    if conjunction:
        if any(item is False for item in values):
            return False
        if any(item is None for item in values):
            return None
        return True
    if any(item is True for item in values):
        return True
    if any(item is None for item in values):
        return None
    return False


def _fold_nodes(
    nodes: list[dict[str, Any]], *, conjunction: bool, universe: set[str]
) -> set[str] | None:
    # Корень — И. Пустая И — все непомеченные узлы (в запросе БСП это «Истина»);
    # пустая ИЛИ — пустое множество. Лист без узлов или с ошибкой не угадывается.
    if not nodes:
        return set(universe) if conjunction else set()
    parts: list[set[str]] = []
    for node in nodes:
        if node["group"]:
            part = _fold_nodes(node["children"], conjunction=node["op"] == "И", universe=universe)
        elif node["error"] or node["nodes"] is None:
            return None
        else:
            part = set(node["nodes"])
        if part is None:
            return None
        parts.append(part)
    if not conjunction:
        united: set[str] = set()
        for part in parts:
            united |= part
        return united
    crossed = set(parts[0])
    for part in parts[1:]:
        crossed &= part
    return crossed


def _subtree_has(node: dict[str, Any], code: str) -> bool | None:
    """Входит ли узел в свод поддерева. None — лист не посчитан."""
    if node["group"]:
        flags = [_subtree_has(child, code) for child in node["children"]]
        if node["op"] == "И":
            if any(flag is False for flag in flags):
                return False
            if any(flag is None for flag in flags):
                return None
            return True
        if any(flag is True for flag in flags):
            return True
        if any(flag is None for flag in flags):
            return None
        return False
    if node["nodes"] is None:
        return None
    return code in node["nodes"]


def _missing_leaves(nodes: list[dict[str, Any]], code: str) -> list[str]:
    # Корень — «И». В группу без узла спускаемся; «И» — только в элементы без узла,
    # «ИЛИ» без узла — во все (у них узла нет); «ИЛИ» с узлом исход не решает.
    found: list[str] = []
    for node in nodes:
        if _subtree_has(node, code) is not False:
            continue
        if node["group"]:
            found.extend(_missing_leaves(node["children"], code))
        else:
            found.append(node["path"])
    return found


def _leaf_error(nodes: list[dict[str, Any]]) -> str | None:
    for node in nodes:
        if node["group"]:
            found = _leaf_error(node["children"])
            if found:
                return found
        elif node["error"]:
            return node["error"]
    return None


def _tree(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    nodes = {row["path"]: {**row, "children": []} for row in rows}
    if len(nodes) != len(rows):
        _bad(name)
    roots: list[dict[str, Any]] = []
    for path, node in nodes.items():
        _path(path, name)
        if "." in path:
            parent_path, _index = path.rsplit(".", 1)
            parent = nodes.get(parent_path)
            if parent is None or not parent["group"]:
                _bad(name)
            parent["children"].append(node)
        else:
            roots.append(node)
    for node in nodes.values():
        node["children"].sort(key=lambda item: int(item["path"].rsplit(".", 1)[-1]))
        if not node["group"] and node["children"]:
            _bad(name)
    roots.sort(key=lambda item: int(item["path"]))
    return roots


def _public_rows(rows: list[dict[str, Any]], *, node_side: bool) -> list[dict[str, Any]]:
    published = []
    for row in rows:
        if node_side:
            item = {key: row[key] for key in _NODE_ROW}
            codes, cut = _cap(row["nodes"]) if row["nodes"] is not None else (None, False)
            item["nodes"] = codes
            if cut:
                item["truncated"] = True
        else:
            item = {key: row[key] for key in _OBJECT_ROW}
        published.append(item)
    return published


def _rows_for_plan(rows: list[dict[str, Any]], plan: str) -> list[dict[str, Any]]:
    return [
        {"code": row["code"], "name": row["name"], "message": row["message"]}
        for row in rows
        if row["plan"] == plan
    ]


def _written_index(
    write: dict[str, Any] | None, before: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]] | None:
    # Получатели записи и новые строки таблицы изменений (пара узла и номера не как в before).
    if write is None or write["error"] is not None:
        return None
    before_pairs: dict[str, set[tuple[str, int | None]]] = {}
    for row in before:
        before_pairs.setdefault(row["plan"], set()).add((row["code"], row["message"]))
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, set[str]] = {}
    for item in write["recipients"]:
        plan_name = item["plan"]
        seen.setdefault(plan_name, set()).add(item["code"])
        grouped.setdefault(plan_name, []).append(
            {"code": item["code"], "name": item["name"], "direct": False}
        )
    for row in write["after_write"]:
        plan_name = row["plan"]
        if (row["code"], row["message"]) in before_pairs.get(plan_name, set()):
            continue
        if row["code"] in seen.get(plan_name, set()):
            continue
        seen.setdefault(plan_name, set()).add(row["code"])
        grouped.setdefault(plan_name, []).append(
            {"code": row["code"], "name": row["name"], "direct": True}
        )
    return grouped


def _cap(items: list[Any] | None) -> tuple[Any, bool]:
    if items is None:
        return None, False
    if len(items) > _NODE_LIMIT:
        return items[:_NODE_LIMIT], True
    return list(items), False


def _object(value: Any) -> dict[str, Any]:
    data = _mapping(value, "object", _OBJECT)
    return {
        "name": _text(data["name"], "object.name"),
        "ref": _text(data["ref"], "object.ref"),
        "presentation": _text(data["presentation"], "object.presentation"),
        "new": _flag(data["new"], "object.new"),
        "modified": _flag(data["modified"], "object.modified"),
    }


def _plan(value: Any, index: int) -> dict[str, Any]:
    name = f"plans[{index}]"
    data = _mapping(value, name, _PLAN)
    nodes = [
        _named(item, f"{name}.nodes[{node_index}]", _NODE, deleted=True)
        for node_index, item in enumerate(_list(data["nodes"], f"{name}.nodes"))
    ]
    if not nodes:
        _bad(f"{name}.nodes")
    bsp = _flag(data["bsp"], f"{name}.bsp")
    auto = _flag(data["autoregistration"], f"{name}.autoregistration")
    rules = _whole(data["rules"], f"{name}.rules")
    recipients = data["recipients"]
    error = data["recipients_error"]
    detail = data["detail"]
    if recipients is not None:
        recipients = [
            _named(item, f"{name}.recipients[{recipient_index}]", _RECIPIENT)
            for recipient_index, item in enumerate(_list(recipients, f"{name}.recipients"))
        ]
    if error is not None:
        error = _text(error, f"{name}.recipients_error")
    if not bsp or auto:
        if recipients is not None or error is not None or detail is not None:
            _bad(name)
        detail_rows = None
    else:
        if not isinstance(detail, list) or len(detail) != rules:
            _bad(f"{name}.detail")
        if (recipients is None) == (error is None):
            _bad(name)
        detail_rows = [
            _detail(item, f"{name}.detail[{rule_index}]") for rule_index, item in enumerate(detail)
        ]
    return {
        "plan": _text(data["plan"], f"{name}.plan"),
        "bsp": bsp,
        "autoregistration": auto,
        "rules": rules,
        "nodes": nodes,
        "recipients": recipients,
        "recipients_error": error,
        "detail": detail_rows,
    }


def _detail(value: Any, name: str) -> dict[str, Any]:
    data = _mapping(value, name, _DETAIL)
    modes = []
    seen: set[str] = set()
    for index, item in enumerate(_list(data["modes"], f"{name}.modes")):
        mode = _mapping(item, f"{name}.modes[{index}]", _MODE)
        code = _text(mode["code"], f"{name}.modes[{index}].code")
        if code in seen:
            _bad(f"{name}.modes")
        seen.add(code)
        modes.append({"code": code, "mode": _text(mode["mode"], f"{name}.modes[{index}].mode")})
    error = data["error"]
    return {
        "flag_attribute": _text(data["flag_attribute"], f"{name}.flag_attribute"),
        "handlers": _strings(data["handlers"], f"{name}.handlers"),
        "manager": _text(data["manager"], f"{name}.manager"),
        "object_filter": [
            _object_row(item, f"{name}.object_filter[{index}]")
            for index, item in enumerate(_list(data["object_filter"], f"{name}.object_filter"))
        ],
        "node_filter": [
            _node_row(item, f"{name}.node_filter[{index}]")
            for index, item in enumerate(_list(data["node_filter"], f"{name}.node_filter"))
        ],
        "nodes_by_bsp": _codes(data["nodes_by_bsp"], f"{name}.nodes_by_bsp"),
        "nodes_by_condition": _codes(data["nodes_by_condition"], f"{name}.nodes_by_condition"),
        "modes": modes,
        "if_needed": _codes(data["if_needed"], f"{name}.if_needed"),
        "error": None if error is None else _text(error, f"{name}.error"),
    }


def _cache(value: Any) -> dict[str, Any]:
    data = _mapping(value, "cache", _CACHE)
    mode = _text(data["mode"], "cache.mode")
    if mode not in ("refreshed", "fallback"):
        _bad("cache.mode")
    checked = _flag(data["checked"], "cache.checked")
    if mode == "refreshed" and checked:
        _bad("cache.checked")
    return {"mode": mode, "checked": checked}


def _object_row(value: Any, name: str) -> dict[str, Any]:
    data = _mapping(value, name, _OBJECT_ROW)
    group = _flag(data["group"], f"{name}.group")
    dropped = _flag(data["dropped"], f"{name}.dropped")
    if group and dropped:
        _bad(name)
    result = data["result"]
    if result is not None and type(result) is not bool:
        _bad(f"{name}.result")
    return {
        "path": _path(data["path"], name),
        "group": group,
        "and": _flag(data["and"], f"{name}.and"),
        "property": _text(data["property"], f"{name}.property"),
        "comparison": _text(data["comparison"], f"{name}.comparison"),
        "kind": _text(data["kind"], f"{name}.kind"),
        "expected": _text(data["expected"], f"{name}.expected"),
        "actual": _text(data["actual"], f"{name}.actual"),
        "result": result,
        "dropped": dropped,
        "algorithm": _text(data["algorithm"], f"{name}.algorithm"),
        "error": None if data["error"] is None else _text(data["error"], f"{name}.error"),
    }


def _node_row(value: Any, name: str) -> dict[str, Any]:
    data = _mapping(value, name, _NODE_ROW)
    group = _flag(data["group"], f"{name}.group")
    op = _text(data["op"], f"{name}.op").upper()
    if op not in ("И", "ИЛИ"):
        _bad(f"{name}.op")
    nodes = _codes(data["nodes"], f"{name}.nodes")
    error = data["error"]
    if group and (nodes is not None or error is not None):
        _bad(name)
    return {
        "path": _path(data["path"], name),
        "group": group,
        "op": op,
        "attribute": _text(data["attribute"], f"{name}.attribute"),
        "table": _text(data["table"], f"{name}.table"),
        "comparison": _text(data["comparison"], f"{name}.comparison"),
        "query_operator": _text(data["query_operator"], f"{name}.query_operator"),
        "operand": _text(data["operand"], f"{name}.operand"),
        "nodes": nodes,
        "error": None if error is None else _text(error, f"{name}.error"),
    }


def _write(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    data = _mapping(value, "write", _WRITE)
    error = data["error"]
    return {
        "error": None if error is None else _text(error, "write.error"),
        "recipients": [
            _named(item, f"write.recipients[{index}]", _WRITE_RECIPIENT)
            for index, item in enumerate(_list(data["recipients"], "write.recipients"))
        ],
        "after_write": _registrations(data["after_write"], "write.after_write"),
    }


def _registrations(value: Any, name: str) -> list[dict[str, Any]]:
    rows = []
    for index, item in enumerate(_list(value, name)):
        data = _mapping(item, f"{name}[{index}]", _REGISTRATION)
        message = data["message"]
        if message is not None and type(message) is not int:
            _bad(f"{name}[{index}].message")
        rows.append(
            {
                "plan": _text(data["plan"], f"{name}[{index}].plan"),
                "code": _text(data["code"], f"{name}[{index}].code"),
                "name": _text(data["name"], f"{name}[{index}].name"),
                "message": message,
            }
        )
    return rows


def _named(value: Any, name: str, keys: frozenset[str], *, deleted: bool = False) -> dict[str, Any]:
    data = _mapping(value, name, keys)
    result: dict[str, Any] = {
        key: _text(data[key], f"{name}.{key}") for key in keys if key != "deleted"
    }
    if deleted:
        result["deleted"] = _flag(data["deleted"], f"{name}.deleted")
    return result


def _codes(value: Any, name: str) -> list[str] | None:
    if value is None:
        return None
    return _strings(value, name)


def _strings(value: Any, name: str) -> list[str]:
    return [_text(item, f"{name}[{index}]") for index, item in enumerate(_list(value, name))]


def _string_map(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        _bad(name)
    return {_text(key, name): _text(item, name) for key, item in value.items()}


def _path(value: Any, name: str) -> str:
    text = _text(value, f"{name}.path")
    parts = text.split(".")
    if not parts or any(not part.isdigit() or str(int(part)) != part for part in parts):
        _bad(f"{name}.path")
    return text


def _mapping(value: Any, name: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _bad(name)
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        _bad(name)
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        _bad(name)
    return value


def _flag(value: Any, name: str) -> bool:
    if type(value) is not bool:
        _bad(name)
    return value


def _whole(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        _bad(name)
    return value


def _bad(name: str) -> NoReturn:
    raise LiveParseError(f"неверное поле ответа: {name}")
