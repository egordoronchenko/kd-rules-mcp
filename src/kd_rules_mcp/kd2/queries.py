"""Консервативное чтение запросов из BSL без исполнения и зависимости от сервиса."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Token:
    """Лексема с исходными позициями и декодированным значением строки."""

    value: str
    start: int
    end: int
    string: bool = False
    closed: bool = True


_STATEMENT_BEFORE = frozenset(
    {
        ";",
        ":",
        "тогда",
        "иначе",
        "цикл",
        "конецесли",
        "конеццикла",
        "конецпопытки",
        "попытка",
        "исключение",
        "then",
        "else",
        "do",
        "endif",
        "enddo",
        "try",
        "except",
        "endtry",
    }
)
_EXECUTIONS = {
    "выполнить": "single",
    "execute": "single",
    "выполнитьпакет": "batch",
    "executebatch": "batch",
    "выполнитьпакетспромежуточнымиданными": "batch",
    "executebatchwithintermediatedata": "batch",
}


def statement_assignment(
    items: list[Token], position: int, equals_position: int | None = None
) -> bool:
    """Имя слева от `=` начинает оператор, а не сравнение в условии или аргументе."""
    after = position + 1 if equals_position is None else equals_position
    if after >= len(items) or items[after].value != "=":
        return False
    if position == 0 or items[position - 1].value.casefold() in _STATEMENT_BEFORE:
        return True
    # Первый оператор после заголовка процедуры/функции с необязательным «Экспорт».
    before = items[:position]
    start = max((i + 1 for i, t in enumerate(before) if t.value == ";"), default=0)
    prefix = before[start:]
    return bool(
        prefix
        and prefix[0].value.casefold() in ("процедура", "функция", "procedure", "function")
        and prefix[-1].value.casefold() in (")", "экспорт", "export")
    )


def tokens(text: str) -> list[Token]:
    """Комментарии пропускаются, кавычки и продолжения BSL читаются целиком."""
    found: list[Token] = []
    index = 0
    while index < len(text):
        start = index
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = end if end >= 0 else len(text)
            continue
        if char == "#" and re.match(
            r"#\s*(?:Область|КонецОбласти|Region|EndRegion)\b", text[index:], re.IGNORECASE
        ):
            end = text.find("\n", index)
            index = end if end >= 0 else len(text)
            # Область не является оператором BSL; её конец служит границей следующего.
            found.append(Token(";", start, index))
            continue
        if char == '"':
            index += 1
            value: list[str] = []
            closed = False
            while index < len(text):
                if text[index] == '"':
                    if text.startswith('""', index):
                        value.append('"')
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                value.append(text[index])
                index += 1
            decoded = re.sub(r"\n[ \t]*\|", "\n", "".join(value)).replace("\r", "")
            found.append(Token(decoded, start, index, True, closed))
            continue
        match = re.match(r"[\w]+", text[index:])
        index += len(match.group()) if match else 1
        found.append(Token(text[start:index], start, index))
    return found


def split_top(text: str, separator: str = ";") -> list[str]:
    """Разделение вне кавычек, комментариев, круглых и квадратных скобок."""
    start = depth = 0
    found: list[str] = []
    for token in tokens(text):
        if token.string:
            continue
        if token.value in ("(", "["):
            depth += 1
        elif token.value in (")", "]"):
            depth = max(0, depth - 1)
        elif token.value == separator and depth == 0:
            part = text[start : token.start].strip()
            if tokens(part):
                found.append(part)
            start = token.end
    part = text[start:].strip()
    if tokens(part):
        found.append(part)
    return found


def _expression(text: str) -> tuple[str, bool, str | None]:
    parts = tokens(text)
    if len(parts) == 1 and parts[0].string and parts[0].closed:
        return parts[0].value, True, None
    literal = "\n".join(part.value for part in parts if part.string)
    names = {part.value.casefold() for part in parts if not part.string}
    if "стрзаменить" in names:
        reason = "СтрЗаменить"
    elif "стршаблон" in names:
        reason = "СтрШаблон"
    elif "+" in names:
        reason = "concatenation"
    elif any(not part.closed for part in parts):
        reason = "unterminated_literal"
    else:
        reason = "dynamic_expression"
    return literal, False, reason


def _batch(text: str, complete: bool) -> list[dict[str, Any]]:
    parts = split_top(text)
    names: dict[str, list[tuple[str, int]]] = {}
    rows: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        lexical = tokens(part)
        into: list[str] = []
        for pos, token in enumerate(lexical[:-1]):
            if not token.string and token.value.casefold() in ("поместить", "into"):
                name = lexical[pos + 1]
                if not name.string and re.fullmatch(r"[^\W\d]\w*", name.value):
                    into.append(name.value)
                    names.setdefault(name.value.casefold(), []).append((name.value, index))
        rows.append(
            {"index": index if complete else "undefined", "text": part, "temporary_tables": into}
        )
    for index, row in enumerate(rows):
        dependencies: list[dict[str, Any]] = []
        lexical = tokens(row["text"])
        from_clause = False
        scopes: list[bool] = []
        for pos, token in enumerate(lexical[:-1]):
            if token.string:
                continue
            word = token.value.casefold()
            if word == "(":
                scopes.append(from_clause)
                from_clause = False
            elif word == ")":
                from_clause = scopes.pop() if scopes else False
            if word in (
                "где",
                "сгруппировать",
                "упорядочить",
                "объединить",
                "по",
                ";",
                "where",
                "group",
                "order",
                "union",
                "on",
            ):
                from_clause = False
            if word in ("из", "from"):
                from_clause = True
            if word not in ("из", "соединение", "from", "join") and not (
                word == "," and from_clause
            ):
                continue
            candidate = lexical[pos + 1]
            if candidate.string:
                continue
            producers = names.get(candidate.value.casefold(), [])
            prior = [table for table in producers if table[1] < index]
            table = prior[-1] if prior else None
            if table is not None:
                dependencies.append(
                    {"name": table[0], "query_index": table[1] if complete else "undefined"}
                )
        row["dependencies"] = list({d["name"].casefold(): d for d in dependencies}.values())
    return rows


def _balanced(items: list[Token], opening: int) -> tuple[list[list[Token]], int] | None:
    depth = 0
    start = opening + 1
    args: list[list[Token]] = []
    for pos in range(opening, len(items)):
        token = items[pos]
        if token.string:
            continue
        if token.value in ("(", "["):
            depth += 1
        elif token.value in (")", "]"):
            depth -= 1
            if depth == 0:
                args.append(items[start:pos])
                return args, pos
        elif token.value == "," and depth == 1:
            args.append(items[start:pos])
            start = pos + 1
    return None


def _raw(text: str, items: list[Token]) -> str:
    return text[items[0].start : items[-1].end] if items else ""


def assignment_expression(text: str, items: list[Token], equals: int) -> str:
    """Правая часть присваивания до точки с запятой верхнего уровня."""
    end = equals + 1
    depth = 0
    while end < len(items):
        value = items[end].value if not items[end].string else ""
        if value == ";" and depth == 0:
            break
        if value in ("(", "["):
            depth += 1
        elif value in (")", "]"):
            depth -= 1
        end += 1
    return _raw(text, items[equals + 1 : end])


def _member(items: list[Token], position: int) -> tuple[str, str, int] | None:
    """Приёмник метода/поля, включая `Запросы.Имя`; вложенные лексемы не дублируются."""
    if items[position].string or (position and items[position - 1].value == "."):
        return None
    names = [items[position].value]
    end = position
    while end + 2 < len(items) and items[end + 1].value == "." and not items[end + 2].string:
        names.append(items[end + 2].value)
        end += 2
    if len(names) < 2:
        return None
    return ".".join(names[:-1]), names[-1].casefold(), end + 1


def _merge_branches(
    states: list[dict[str, list[dict[str, Any]]]],
) -> dict[str, list[dict[str, Any]]]:
    """Объединение вариантов без предположения о значении условия."""
    merged: dict[str, list[dict[str, Any]]] = {}
    for state in states:
        for name, variants in state.items():
            unique = merged.setdefault(name, [])
            for batch in variants:
                if not any(batch is old for old in unique):
                    unique.append(batch)
    for variants in merged.values():
        texts = {
            tuple(query["text"] for query in batch["queries"])
            if batch["queries"]
            else batch["expression"]
            for batch in variants
        }
        if len(texts) > 1:
            for batch in variants:
                if batch.get("reason") and batch["reason"] != "branching_text":
                    batch["text_reason"] = batch["reason"]
                batch.update(complete=False, reason="branching_text")
                for query in batch["queries"]:
                    query["index"] = "undefined"
                    for dependency in query["dependencies"]:
                        dependency["query_index"] = "undefined"
    return merged


def _merge_results(
    states: list[dict[str, list[tuple[dict[str, Any], str]]]],
) -> dict[str, list[tuple[dict[str, Any], str]]]:
    """После ветвления результат может принадлежать любому выполнению из его веток."""
    merged: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for state in states:
        for name, bindings in state.items():
            unique = merged.setdefault(name, [])
            for batch, mode in bindings:
                if not any(batch is old and mode == old_mode for old, old_mode in unique):
                    unique.append((batch, mode))
    return merged


def parse_queries(
    text: str, line_offset: int = 0, named_queries: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Запросы, варианты ветвления и связи исполнения без вычисления BSL.

    Именованные запросы передаёт вызывающий код из модели. Неизвестные тексты и индексы
    помечаются явно; строковые аргументы построителей запроса не выдаются за его части.
    """
    items = tokens(text)
    definitions = {name.casefold(): (name, value) for name, value in (named_queries or {}).items()}
    batches: list[dict[str, Any]] = []
    current: dict[str, list[dict[str, Any]]] = {}
    results: dict[str, list[tuple[dict[str, Any], str]]] = {}
    parameters: dict[str, list[dict[str, Any]]] = {}
    diagnostics: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    known: set[str] = set()
    for pos in range(len(items)):
        member = _member(items, pos)
        if member and member[1] in _EXECUTIONS:
            known.add(member[0].casefold())
        if (
            statement_assignment(items, pos)
            and pos + 3 < len(items)
            and [t.value.casefold() for t in items[pos + 2 : pos + 4]]
            in (
                ["новый", "запрос"],
                ["new", "query"],
            )
        ):
            known.add(items[pos].value.casefold())

    def line(position: int) -> int:
        return line_offset + text.count("\n", 0, position) + 1

    def add(
        variable: str, expression: str, position: int, named: tuple[str, str] | None = None
    ) -> None:
        value, complete, reason = (named[1], True, None) if named else _expression(expression)
        batch: dict[str, Any] = {
            "variable": variable,
            "line": line(position),
            "expression": expression,
            "complete": complete,
            "queries": _batch(value, True) if complete else [],
            "parameters": list(parameters.get(variable.casefold(), [])),
            "executions": [],
            "results": [],
        }
        if reason:
            batch["reason"] = reason
        if named:
            batch["query_name"] = named[0]
        elif variable.casefold().startswith(("запросы.", "queries.")):
            name = variable.split(".", 1)[1]
            batch["query_name"] = definitions.get(name.casefold(), (name, ""))[0]
        batches.append(batch)
        current[variable.casefold()] = [batch]

    for pos, token in enumerate(items):
        if token.string:
            if not token.closed:
                diagnostics.append({"line": line(token.start), "reason": "unterminated_literal"})
            continue
        word = token.value.casefold()
        if word in ("если", "if", "попытка", "try") and (pos == 0 or items[pos - 1].value != "."):
            frames.append(
                {
                    "base": dict(current),
                    "base_results": dict(results),
                    "states": [],
                    "result_states": [],
                    "else": False,
                }
            )
        elif (
            word in ("иначе", "иначеесли", "else", "elsif", "elseif", "исключение", "except")
            and frames
        ):
            frames[-1]["states"].append(dict(current))
            frames[-1]["result_states"].append(dict(results))
            current = dict(frames[-1]["base"])
            results = dict(frames[-1]["base_results"])
            frames[-1]["else"] = word in ("иначе", "else", "исключение", "except")
        elif word in ("конецесли", "endif", "конецпопытки", "endtry") and frames:
            frame = frames.pop()
            frame["states"].append(dict(current))
            frame["result_states"].append(dict(results))
            if not frame["else"]:
                frame["states"].append(frame["base"])
                frame["result_states"].append(frame["base_results"])
            current = _merge_branches(frame["states"])
            results = _merge_results(frame["result_states"])

        tail = items[pos : pos + 5]
        folded = [t.value.casefold() if not t.string else "" for t in tail]
        assignment = statement_assignment(items, pos)
        if assignment:
            results.pop(word, None)
            current.pop(word, None)
            parameters.pop(word, None)
        # Конструктор с текстом либо без него; простое сравнение состояние не меняет.
        if (
            assignment
            and len(tail) == 5
            and folded[2:4] in (["новый", "запрос"], ["new", "query"])
            and folded[4] == "("
        ):
            parsed = _balanced(items, pos + 4)
            if parsed and parsed[0][0]:
                add(token.value, _raw(text, parsed[0][0]), token.start)
            elif parsed is None:
                diagnostics.append({"line": line(token.start), "reason": "unclosed_call"})

        member = _member(items, pos)
        if member:
            variable, method, after = member
            key = variable.casefold()
            if method in ("текст", "text") and statement_assignment(items, pos, after):
                expression = assignment_expression(text, items, after)
                literal, _, _ = _expression(expression)
                if key in known or re.match(r"\s*(ВЫБРАТЬ|SELECT)\b", literal, re.IGNORECASE):
                    add(variable, expression, token.start)
            if method not in (*_EXECUTIONS, "установитьпараметр", "setparameter"):
                continue
            if after >= len(items) or items[after].value != "(":
                continue
            if key.startswith(("запросы.", "queries.")) and key not in current:
                name = variable.split(".", 1)[1]
                definition = definitions.get(name.casefold())
                if definition is not None:
                    add(variable, "", token.start, definition)
                else:
                    add(variable, variable, token.start)
                    current[key][0]["reason"] = "unknown_named_query"
            variants = current.get(key, [])
            parsed = _balanced(items, after)
            if parsed is None:
                diagnostics.append({"line": line(token.start), "reason": "unclosed_call"})
                continue
            args, _ = parsed
            if method in ("установитьпараметр", "setparameter"):
                name = args[0][0].value if len(args[0]) == 1 and args[0][0].string else "undefined"
                parameter = {
                    "name": name,
                    "expression": _raw(text, args[1]) if len(args) > 1 else "",
                    "line": line(token.start),
                }
                parameters.setdefault(key, []).append(parameter)
                for batch in variants:
                    batch["parameters"].append(parameter)
            else:
                for batch in variants:
                    indices: Any = (
                        list(range(len(batch["queries"])))
                        if _EXECUTIONS[method] == "batch"
                        else [len(batch["queries"]) - 1]
                    )
                    if not batch["complete"] or not batch["queries"]:
                        indices = "undefined"
                    batch["executions"].append(
                        {
                            "method": items[after - 1].value,
                            "query_indices": indices,
                            "line": line(token.start),
                        }
                    )
                if (
                    pos >= 2
                    and items[pos - 1].value == "="
                    and statement_assignment(items, pos - 2)
                ):
                    results[items[pos - 2].value.casefold()] = [
                        (batch, _EXECUTIONS[method]) for batch in variants
                    ]
        if len(tail) >= 2 and folded[1] == "[" and word in results:
            parsed = _balanced(items, pos + 1)
            for batch, mode in results[word]:
                index: Any = "undefined"
                if parsed and mode == "batch" and batch["complete"]:
                    args, _ = parsed
                    if len(args) == 1 and len(args[0]) == 1 and not args[0][0].string:
                        literal = args[0][0].value
                        if re.fullmatch(r"[0-9]{1,9}", literal) and int(literal) < len(
                            batch["queries"]
                        ):
                            index = int(literal)
                batch["results"].append(
                    {"variable": token.value, "query_index": index, "line": line(token.start)}
                )
    return {"batches": batches, "diagnostics": diagnostics}
