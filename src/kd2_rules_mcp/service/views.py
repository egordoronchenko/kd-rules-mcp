"""Представления ответов инструментов: страницы, строки правил, итоги проверок и правок."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from kd2_rules_mcp.authoring.candidates import Candidate, Confidence
from kd2_rules_mcp.authoring.edits import EditResult
from kd2_rules_mcp.authoring.registration import (
    ObjectFilter,
    PlanFilter,
    RegistrationObject,
)
from kd2_rules_mcp.errors import Kd2Error, ObjectNotFoundError
from kd2_rules_mcp.kd2.diff import TEXT_LIMIT, clip
from kd2_rules_mcp.kd2.model import Node, RegistrationRules, RulesDocument
from kd2_rules_mcp.structures.queries import MAX_LIMIT, NotFound, Page
from kd2_rules_mcp.validation.address import rule_address, side_name, walk_pks
from kd2_rules_mcp.validation.report import ValidationReport

__all__ = ["TEXT_LIMIT", "clip"]

# Разделы правил обмена: имя раздела в инструментах → тег.
EXCHANGE_SECTIONS = {
    "pko": "ПравилаКонвертацииОбъектов",
    "pvd": "ПравилаВыгрузкиДанных",
    "pod": "ПравилаОчисткиДанных",
    "algorithms": "Алгоритмы",
    "queries": "Запросы",
    "parameters": "Параметры",
}
REGISTRATION_SECTION = "registration"
# Поля правила в строке списка rules_list.
_ROW_FIELDS = (
    "Наименование",
    "Источник",
    "Приемник",
    "ОбъектВыборки",
    "КодПравилаКонвертации",
    "ОбъектМетаданныхИмя",
)
# Коды других правил. В схеме это значение `КодПравилаКонвертации` и атрибут
# `ПравилоКонвертации`. Писатель КД дополняет их пробелами; в ответ отдаём без пробелов.
_RULE_CODE_FIELDS = frozenset({"КодПравилаКонвертации", "ПравилоКонвертации"})
# Вид узла раздела с группами → имя раздела в инструментах (как в `TITLES`).
_KIND_SECTIONS = {
    "pko": "pko",
    "pko_group": "pko",
    "pvd": "pvd",
    "pvd_group": "pvd",
    "pod": "pod",
    "pod_group": "pod",
    "algorithm": "algorithms",
    "algorithm_group": "algorithms",
    "query": "queries",
    "query_group": "queries",
    "pro": REGISTRATION_SECTION,
    "pro_group": REGISTRATION_SECTION,
}


def project_structure_id(project_id: str, configuration_id: str) -> str:
    """Идентификатор структуры конфигурации проекта: `<проект>-<конфигурация>`."""
    return f"{project_id}-{configuration_id}"


def note_private(view: dict[str, Any], private: bool) -> dict[str, Any]:
    """Ключ `private` есть только у приватной копии проекта."""
    if private:
        view["private"] = True
    return view


def page_limit(limit: int) -> int:
    if limit < 1:
        raise Kd2Error(f"Размер страницы должен быть положительным: {limit}")
    return min(limit, MAX_LIMIT)


def slice_rows(rows: Sequence[Any], offset: int, limit: int) -> dict[str, Any]:
    if offset < 0:
        raise Kd2Error(f"Смещение страницы не может быть отрицательным: {offset}")
    limit = page_limit(limit)
    items = list(rows[offset : offset + limit])
    return {
        "items": items,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < len(rows),
    }


def page_view(page: Page) -> dict[str, Any]:
    return {
        "items": page.items,
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
        "has_more": page.has_more,
    }


def require_found[T](result: T | NotFound) -> T:
    if isinstance(result, NotFound):
        raise ObjectNotFoundError(result.message, result.suggestions)
    return result


def candidate_row(candidate: Candidate, path: str) -> dict[str, Any]:
    def side(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            "name": value.name,
            "kind": value.kind,
            "path": value.path,
            "synonym": value.synonym,
            "types": list(value.types[:5]) + (["…"] if len(value.types) > 5 else []),
        }

    row: dict[str, Any] = {
        "confidence": candidate.confidence.value,
        "auto": candidate.auto,
        "source": side(candidate.source),
        "target": side(candidate.target),
    }
    if path:
        row["path"] = path
    if candidate.note:
        row["note"] = candidate.note
    return row


def object_row(candidate: Candidate) -> dict[str, Any]:
    """Кандидат ПКО компактно: `Вид.Имя` сторон и синоним — без наборов типов."""
    row: dict[str, Any] = {"confidence": candidate.confidence.value, "auto": candidate.auto}
    for key, side in (("source", candidate.source), ("target", candidate.target)):
        row[key] = f"{side.kind}.{side.name}" if side is not None else None
    main = candidate.target or candidate.source
    if main is not None and main.synonym:
        row["synonym"] = main.synonym
    if candidate.note:
        row["note"] = candidate.note
    return row


def mentions(candidate: Candidate, needle: str) -> bool:
    """Подстрока в имени или синониме любой стороны кандидата."""
    for side in (candidate.source, candidate.target):
        if side is not None and (
            needle in side.name.casefold() or needle in side.synonym.casefold()
        ):
            return True
    return False


def flatten(candidates: Iterable[Candidate], prefix: str = "") -> Iterator[tuple[str, Candidate]]:
    """Дерево кандидатов свойств → пары (путь ПКС `группа/свойство`, кандидат)."""
    for item in candidates:
        side = item.target or item.source
        name = side.name if side is not None else ""
        path = f"{prefix}{name}"
        yield path, item
        if item.children:
            yield from flatten(item.children, f"{path}/")


def filter_confidence(rows: list[dict[str, Any]], confidence: str | None) -> list[dict[str, Any]]:
    if not confidence:
        return rows
    wanted = Confidence(confidence).value
    return [row for row in rows if row["confidence"] == wanted]


def section_node(document: RulesDocument, tag: str) -> Node | None:
    return document.root.children.get(tag)


def list_section(document: RulesDocument, section: str) -> Node | None:
    """Узел раздела по имени инструмента. Неизвестный раздел — ошибка, пустого нет — `None`."""
    if isinstance(document, RegistrationRules):
        if section != REGISTRATION_SECTION:
            raise Kd2Error(f"У правил регистрации один раздел: «{REGISTRATION_SECTION}»")
        return section_node(document, "ПравилаРегистрацииОбъектов")
    tag = EXCHANGE_SECTIONS.get(section)
    if tag is None:
        known = ", ".join(EXCHANGE_SECTIONS)
        raise Kd2Error(f"Неизвестный раздел «{section}»; разделы правил обмена: {known}")
    return section_node(document, tag)


def section_rules(document: RulesDocument, section: str) -> list[Node]:
    node = list_section(document, section)
    if node is None:
        return []
    if section == "parameters":
        return [item for item in node.items if item.kind.name == "parameter"]
    return list(node.walk())


def group_paths(section: Node) -> dict[int, str]:
    """Путь групп над узлом списка: `id(node)` → коды через `/`.

    У правила и группы верхнего уровня путь пустой. У вложенного — коды групп от раздела
    до родителя (`Перечисления`, `Справочники/Основные`), без собственного кода группы.
    """
    paths: dict[int, str] = {}

    def visit(node: Node, prefix: str) -> None:
        for item in node.items:
            paths[id(item)] = prefix
            if item.is_group:
                nested = f"{prefix}/{item.code}" if prefix else item.code
                visit(item, nested)

    visit(section, "")
    return paths


def rule_group(document: RulesDocument, node: Node) -> str:
    """Путь групп над узлом раздела. У верхнего уровня и у вложенного правила путь пустой."""
    section = _KIND_SECTIONS.get(node.kind.name)
    if section is None:
        return ""
    container = list_section(document, section)
    if container is None:
        return ""
    return group_paths(container).get(id(node), "")


def listed_rule_rows(document: RulesDocument, section: str) -> list[dict[str, Any]]:
    """Строки `rules_list`. Ключ `group` есть только у правила внутри группы."""
    container = list_section(document, section)
    paths = group_paths(container) if container is not None else {}
    return [rule_row(node, paths.get(id(node), "")) for node in section_rules(document, section)]


def overview_groups(document: RulesDocument) -> dict[str, list[dict[str, Any]]]:
    """Группы разделов, где они есть. Ключ совпадает с ключом `counts` того же ответа.

    `count` — число правил непосредственно в группе, без вложенных групп. Раздел без групп
    в словарь не входит. Порядок — порядок групп в документе.
    """
    if isinstance(document, RegistrationRules):
        pairs = (("registration_rules", "ПравилаРегистрацииОбъектов"),)
    else:
        pairs = tuple(
            (name, tag) for name, tag in EXCHANGE_SECTIONS.items() if name != "parameters"
        )
    found: dict[str, list[dict[str, Any]]] = {}
    for name, tag in pairs:
        node = section_node(document, tag)
        if node is None:
            continue
        groups = _group_counts(node)
        if groups:
            found[name] = groups
    return found


def _group_counts(section: Node) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []

    def visit(node: Node, prefix: str) -> None:
        for item in node.items:
            if not item.is_group:
                continue
            path = f"{prefix}/{item.code}" if prefix else item.code
            groups.append(
                {
                    "path": path,
                    "count": sum(1 for child in item.items if not child.is_group),
                }
            )
            visit(item, path)

    visit(section, "")
    return groups


def _reference_text(tag: str, value: Any) -> Any:
    """Код правила в поле-ссылке без хвостовых пробелов. Значение узла не меняется."""
    if tag in _RULE_CODE_FIELDS and isinstance(value, str):
        return value.strip()
    return value


def rule_row(node: Node, group: str = "") -> dict[str, Any]:
    values = node.values
    row: dict[str, Any] = {"address": rule_address(node), "code": node.code}
    if group:
        row["group"] = group
    for tag in _ROW_FIELDS:
        value = _reference_text(tag, values.get(tag))
        if value not in (None, ""):
            row[tag] = value
    for name in ("Отключить", "ИспользуетсяПриЗагрузке"):
        if node.attrs.get(name) is True:
            row[name] = True
    properties = node.child("Свойства")
    if properties is not None:
        row["pks_count"] = sum(1 for _ in walk_pks(properties))
    return row


def _pks_row(path: str, item: Node) -> dict[str, Any]:
    row: dict[str, Any] = {
        "path": path,
        "kind": item.kind.name,
        "source": side_name(item, "Источник"),
        "target": side_name(item, "Приемник"),
    }
    if item.attrs.get("Отключить") is True:
        row["disabled"] = True
    if item.attrs.get("Поиск") is True:
        row["search"] = True
    code = _reference_text("КодПравилаКонвертации", item.values.get("КодПравилаКонвертации"))
    if code:
        row["conversion"] = code
    return row


def node_view(node: Node, limit: int, group: str = "") -> dict[str, Any]:
    view: dict[str, Any] = {"kind": node.kind.name, "title": node.kind.title}
    if group:
        view["group"] = group
    if node.attrs:
        view["attrs"] = {tag: _reference_text(tag, value) for tag, value in node.attrs.items()}
    fields: dict[str, Any] = {}
    for tag, value in node.values.items():
        shown = _reference_text(tag, value)
        fields[tag] = clip(shown) if isinstance(shown, str) else shown
    if fields:
        view["fields"] = fields
    sides = {
        tag: {"attrs": dict(child.attrs), **({"text": child.text} if child.text else {})}
        for tag, child in node.children.items()
        if child.kind.name == "pks_side"
    }
    if sides:
        view["sides"] = sides
    properties = node.child("Свойства")
    # У только что созданного ПКО контейнера `Свойства` ещё нет: он появляется с первой ПКС.
    if properties is not None or node.kind.name == "pko":
        rows: list[dict[str, Any]] = []
        if properties is not None:
            rows = [_pks_row(path, item) for path, item in walk_pks(properties)]
        view["properties"] = {"total": len(rows), "items": rows[:limit]}
        view["address"] = rule_address(node)
    values = node.child("Значения")
    if values is not None:
        rows = [
            {"source": item.values.get("Источник", ""), "target": item.values.get("Приемник", "")}
            for item in values.walk()
        ]
        view["values"] = {"total": len(rows), "items": rows[:limit]}
    other = sorted(
        tag
        for tag, child in node.children.items()
        if child.kind.name != "pks_side" and tag not in ("Свойства", "Значения")
    )
    if other:
        view["nested"] = other
    return view


def counts(document: RulesDocument) -> dict[str, int]:
    def count(tag: str) -> int:
        node = document.root.children.get(tag)
        return sum(1 for _ in node.walk()) if node is not None else 0

    if isinstance(document, RegistrationRules):
        return {"registration_rules": count("ПравилаРегистрацииОбъектов")}
    counts = {section: count(tag) for section, tag in EXCHANGE_SECTIONS.items()}
    parameters = document.root.children.get("Параметры")
    counts["parameters"] = (
        sum(1 for item in parameters.items if item.kind.name == "parameter") if parameters else 0
    )
    return counts


def report_summary(report: ValidationReport) -> dict[str, Any]:
    by_check: dict[str, int] = {}
    for issue in report.issues:
        by_check[issue.check] = by_check.get(issue.check, 0) + 1
    return {
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "skipped": len(report.skipped),
        "by_check": by_check,
        "text": report.summary(),
    }


def edit_view(result: EditResult) -> dict[str, Any]:
    view: dict[str, Any] = {"address": result.address}
    for name in ("warnings", "skipped", "not_applied", "unresolved", "disabled"):
        values = getattr(result, name)
        if values:
            view[name] = values[:MAX_LIMIT]
            if len(values) > MAX_LIMIT:
                view[f"{name}_total"] = len(values)
    return view


def registration_object(item: Mapping[str, Any]) -> RegistrationObject:
    """Объект правил регистрации из словаря инструмента (плоские отборы через «И»)."""
    name = str(item.get("metadata_name", "")).strip()
    if not name:
        raise Kd2Error("У объекта правил регистрации нет «metadata_name»")
    plan_filters = tuple(
        PlanFilter(
            plan_property=str(entry.get("plan_property", "")),
            object_property=str(entry.get("object_property", "")),
            property_type=str(entry.get("property_type", "")),
            comparison=str(entry.get("comparison", "")),
            constant=bool(entry.get("constant", False)),
        )
        for entry in item.get("plan_filters", ()) or ()
    )
    object_filters = tuple(
        ObjectFilter(
            object_property=str(entry.get("object_property", "")),
            property_type=str(entry.get("property_type", "")),
            comparison=str(entry.get("comparison", "")),
            constant_value=str(entry.get("constant_value", "")),
        )
        for entry in item.get("object_filters", ()) or ()
    )
    return RegistrationObject(
        metadata_name=name,
        code=str(item.get("code", "")),
        name=str(item.get("name", "")),
        comment=str(item.get("comment", "")),
        unload_mode=str(item.get("unload_mode", "")),
        plan_filters=plan_filters,
        object_filters=object_filters,
    )
