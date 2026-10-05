"""Представления ответов инструментов: страницы, строки правил, итоги проверок и правок."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from kd2_rules_mcp.authoring.candidates import Candidate, Confidence
from kd2_rules_mcp.authoring.edits import CONVERSION_KIND, EditResult
from kd2_rules_mcp.authoring.registration import (
    RegistrationObject,
    parse_registration_object,
)
from kd2_rules_mcp.errors import (
    AmbiguousAddressError,
    Kd2Error,
    ObjectNotFoundError,
    RuleNotFoundError,
)
from kd2_rules_mcp.kd2.diff import TEXT_LIMIT, clip
from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules, RulesDocument, rule_code
from kd2_rules_mcp.kd2.schema import CONVERSION_EVENTS
from kd2_rules_mcp.projects import Base, LocalSettings, base_login, dev_env_login, resolve
from kd2_rules_mcp.structures.queries import MAX_LIMIT, NotFound, Page
from kd2_rules_mcp.validation.address import (
    CONVERSION_ADDRESS,
    pks_address,
    pro_addresses,
    pro_label,
    rule_address,
    side_name,
    walk_pks,
)
from kd2_rules_mcp.validation.report import Level, ValidationReport

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
# Виртуальный раздел rules_list: ПКС всех ПКО, без кода владельца.
PKS_SECTION = "pks"
# Обработчики ПРО, которые читает загрузчик БСП (ЗПР:327–349).
PRO_HANDLERS = (
    "ПередОбработкой",
    "ПриОбработке",
    "ПриОбработкеДополнительный",
    "ПослеОбработки",
)
_PLAN_FILTER = "ОтборПоСвойствамПланаОбмена"
_OBJECT_FILTER = "ОтборПоСвойствамОбъекта"
_PLAN_FILTER_FIELDS = (
    "ЭтоСтрокаКонстанты",
    "ТипСвойстваОбъекта",
    "СвойствоПланаОбмена",
    "ВидСравнения",
    "СвойствоОбъекта",
)
_OBJECT_FILTER_FIELDS = (
    "ТипСвойстваОбъекта",
    "ВидСравнения",
    "СвойствоОбъекта",
    "Вид",
    "ЗначениеКонстанты",
)
_PROPERTY_TABLES = ("ТаблицаСвойствОбъекта", "ТаблицаСвойствПланаОбмена")
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


def project_base_view(
    project_id: str,
    base: Base,
    local: LocalSettings | None,
    project_dir: Path | None,
) -> dict[str, Any]:
    """Строка базы в `project_list`: роль, признак логина и сервер данных песочницы.

    `login` — задана ли пара пользователя (`logins` личного файла или `IB_USER` в `.dev.env`).
    Личный файл серверу не виден — признак только по `.dev.env`. Имя и пароль не отдаются.
    """
    row: dict[str, Any] = {
        "role": base.role,
        "configuration": base.configuration,
        "login": _base_login_set(project_id, base, local, project_dir),
    }
    if base.data_mcp and base.is_sandbox:
        row["data_mcp"] = base.data_mcp
        row["data_mcp_server"] = f"{project_id}-{base.data_mcp}"
    return row


def _base_login_set(
    project_id: str,
    base: Base,
    local: LocalSettings | None,
    project_dir: Path | None,
) -> bool:
    """Есть ли логин базы. Личные настройки недоступны — смотрим только `.dev.env`."""
    if local is not None:
        return base_login(local, project_id, base) is not None
    if project_dir is None or not base.dev_env:
        return False
    return dev_env_login(resolve(project_dir, base.dev_env)) is not None


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


def exchange_list_sections() -> str:
    """Имена разделов `rules_list` у правил обмена."""
    return ", ".join((*EXCHANGE_SECTIONS, PKS_SECTION, CONVERSION_KIND))


def registration_section_error(section: str) -> Kd2Error:
    """Отказ раздела правил обмена на проекте правил регистрации."""
    return Kd2Error(
        f"Раздел «{section}» относится к правилам обмена. "
        f"У правил регистрации доступен раздел «{REGISTRATION_SECTION}»"
    )


def list_section(document: RulesDocument, section: str) -> Node | None:
    """Узел раздела по имени инструмента. Неизвестный раздел — ошибка, пустого нет — `None`."""
    if isinstance(document, RegistrationRules):
        if section != REGISTRATION_SECTION:
            raise registration_section_error(section)
        return section_node(document, "ПравилаРегистрацииОбъектов")
    if section == PKS_SECTION:
        return None
    tag = EXCHANGE_SECTIONS.get(section)
    if tag is None:
        raise Kd2Error(
            f"Неизвестный раздел «{section}»; разделы правил обмена: {exchange_list_sections()}"
        )
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
    if section == PKS_SECTION:
        if isinstance(document, RegistrationRules):
            raise registration_section_error(section)
        if not isinstance(document, ExchangeRules):
            raise Kd2Error(f"Раздел «{section}» есть у правил обмена")
        return pks_rows(document)
    if section == CONVERSION_KIND:
        if isinstance(document, RegistrationRules):
            raise registration_section_error(section)
        return [_conversion_row(document.root)]
    if section == REGISTRATION_SECTION and isinstance(document, RegistrationRules):
        return registration_rows(document)
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
        return rule_code(value)
    return value


def pks_rows(document: ExchangeRules) -> list[dict[str, Any]]:
    """ПКС всех ПКО: адрес, код ПКО, имена сторон, признаки поиска и отключения."""
    rows: list[dict[str, Any]] = []
    for pko in document.pko():
        properties = pko.child("Свойства")
        if properties is None:
            continue
        for path, item in walk_pks(properties):
            if item.is_group:
                continue
            rows.append(
                {
                    "address": pks_address(pko.code, path),
                    "code": pko.code,
                    "source": side_name(item, "Источник"),
                    "target": side_name(item, "Приемник"),
                    "search": item.attrs.get("Поиск") is True,
                    "disabled": item.attrs.get("Отключить") is True,
                }
            )
    return rows


def registration_rows(document: RegistrationRules) -> list[dict[str, Any]]:
    """Строки раздела правил регистрации: объект, код, группа, отборы и обработчики."""
    section = section_node(document, "ПравилаРегистрацииОбъектов")
    nodes = document.rules()
    paths = group_paths(section) if section is not None else {}
    rows: list[dict[str, Any]] = []
    for address, node in zip(pro_addresses(nodes), nodes, strict=True):
        row: dict[str, Any] = {
            "address": address,
            "ОбъектМетаданныхИмя": str(node.values.get("ОбъектМетаданныхИмя", "")),
            "code": node.code,
            "disabled": node.attrs.get("Отключить") is True,
            "filters": _has_filter(node),
            "handlers": _has_handler(node),
        }
        group = paths.get(id(node), "")
        if group:
            row["group"] = group
        rows.append(row)
    return rows


def _has_filter(node: Node) -> bool:
    for tag in (_PLAN_FILTER, _OBJECT_FILTER):
        child = node.child(tag)
        if child is not None and child.items:
            return True
    return False


def _has_handler(node: Node) -> bool:
    return any(_handler_text(node, name) for name in PRO_HANDLERS)


def _handler_text(node: Node, name: str) -> str:
    value = node.values.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def find_pro(document: RegistrationRules, key: str) -> tuple[Node, str, str]:
    """ПРО по адресу, имени объекта или коду. Нет правила или их несколько — ошибка."""
    section = section_node(document, "ПравилаРегистрацииОбъектов")
    nodes = document.rules()
    addresses = pro_addresses(nodes)
    paths = group_paths(section) if section is not None else {}
    wanted = key.strip()
    exact = [index for index, address in enumerate(addresses) if address == wanted]
    if len(exact) == 1:
        index = exact[0]
        return nodes[index], addresses[index], paths.get(id(nodes[index]), "")
    if len(exact) > 1:
        raise AmbiguousAddressError(
            f"Адрес «{wanted}» подходит нескольким правилам",
            [addresses[index] for index in exact],
        )
    by_label = [index for index, node in enumerate(nodes) if pro_label(node) == wanted]
    by_meta = [
        index
        for index, node in enumerate(nodes)
        if str(node.values.get("ОбъектМетаданныхИмя", "")).strip() == wanted
    ]
    by_code = [index for index, node in enumerate(nodes) if node.code == wanted]
    found = by_label or by_meta or by_code
    if len(found) == 1:
        index = found[0]
        return nodes[index], addresses[index], paths.get(id(nodes[index]), "")
    if len(found) > 1:
        options = ", ".join(addresses[index] for index in found)
        raise AmbiguousAddressError(
            f"Адрес «{wanted}» подходит нескольким правилам: {options}",
            [addresses[index] for index in found],
        )
    raise RuleNotFoundError(f"Правило регистрации «{wanted}» не найдено")


def pro_view(node: Node, address: str, group: str) -> dict[str, Any]:
    """Одно ПРО: отборы, реквизиты свойств, обработчики и то, что читает БСП.

    `Отключить` пропускает правило (ЗПР:282–286). `Валидное` без `true` — тоже
    (ЗПР:289–293). `РеквизитРежимаВыгрузки` — имя реквизита узла, значение которого
    задаёт режим, в том числе «ВыгружатьПриНеобходимости» (ЗПР:309–311); само значение
    живёт на узле, не в правиле. Корень обоих отборов — «И» (ЗПР:1203).
    """
    view: dict[str, Any] = {
        "kind": "pro",
        "title": node.kind.title,
        "address": address,
        "code": node.code,
        "ОбъектМетаданныхИмя": str(node.values.get("ОбъектМетаданныхИмя", "")),
        "disabled": node.attrs.get("Отключить") is True,
        # Пустого атрибута в модели нет: БСП читает его как Ложь и правило не грузит.
        "Валидное": node.attrs.get("Валидное") is True,
    }
    if group:
        view["group"] = group
    for tag in ("ОбъектНастройки", "РеквизитРежимаВыгрузки"):
        value = str(node.values.get(tag, "")).strip()
        if value:
            view[tag] = value
    plan = _filter_tree(node.child(_PLAN_FILTER), plan=True)
    obj = _filter_tree(node.child(_OBJECT_FILTER), plan=False)
    if plan is not None:
        view[_PLAN_FILTER] = plan
    if obj is not None:
        view[_OBJECT_FILTER] = obj
    handlers = [
        {"name": name, "lines": len(text.splitlines()) or 1, "text": clip(text)}
        for name in PRO_HANDLERS
        if (text := _handler_text(node, name))
    ]
    view["handlers"] = handlers
    fields: dict[str, Any] = {}
    for tag in ("Наименование", "Описание", "Комментарий", "ОбъектМетаданныхТип"):
        value = node.values.get(tag)
        if isinstance(value, str) and value.strip():
            fields[tag] = clip(value)
    if fields:
        view["fields"] = fields
    return view


def _filter_tree(node: Node | None, *, plan: bool) -> dict[str, Any] | None:
    if node is None or not node.items:
        return None
    return {"operator": "И", "items": [_filter_item(item, plan=plan) for item in node.items]}


def _filter_item(node: Node, *, plan: bool) -> dict[str, Any]:
    if node.tag == "Группа":
        raw = str(node.values.get("БулевоЗначениеГруппы", "")).strip()
        # Группа плана хранит оператор строкой (ЗПР:601–603).
        # У группы объекта оператор «И» только при явном «И», иначе «ИЛИ» (ЗПР:642–646).
        operator = raw if plan else ("И" if raw == "И" else "ИЛИ")
        return {
            "group": True,
            "operator": operator,
            "items": [_filter_item(item, plan=plan) for item in node.items],
        }
    row: dict[str, Any] = {}
    for tag in _PLAN_FILTER_FIELDS if plan else _OBJECT_FILTER_FIELDS:
        value = node.values.get(tag)
        if value not in (None, ""):
            row[tag] = value
    for tag in _PROPERTY_TABLES:
        table = node.child(tag)
        if table is None or not table.items:
            continue
        props = []
        for item in table.items:
            prop = {
                name: item.values[name]
                for name in ("Наименование", "Тип", "Вид")
                if item.values.get(name) not in (None, "")
            }
            if prop:
                props.append(prop)
        if props:
            row[tag] = props
    return row


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
    counts[CONVERSION_KIND] = len(filled_conversion_events(document.root))
    return counts


def filled_conversion_events(root: Node) -> list[tuple[str, str]]:
    """Непустые события конвертации в порядке писателя КД. Пробельный текст — пустое."""
    found: list[tuple[str, str]] = []
    for name in CONVERSION_EVENTS:
        value = root.values.get(name)
        if isinstance(value, str) and value.strip():
            found.append((name, value))
    return found


def conversion_view(rules: ExchangeRules) -> dict[str, Any]:
    """События конвертации и реквизиты заголовка, которые модель уже хранит рядом.

    Текст события обрезается так же, как текст обработчика в `rules_get` (`clip`).
    Число строк — по полному тексту. Версии конфигураций в ответ не копируются:
    показываются имена источника и приёмника, версия формата и дата.
    """
    events = [
        {"name": name, "lines": len(text.splitlines()) or 1, "text": clip(text)}
        for name, text in filled_conversion_events(rules.root)
    ]
    return {
        "kind": CONVERSION_KIND,
        "address": CONVERSION_ADDRESS,
        "title": "события конвертации",
        "events": events,
        "header": _conversion_header(rules),
    }


def _conversion_row(root: Node) -> dict[str, Any]:
    names = [name for name, _text in filled_conversion_events(root)]
    row: dict[str, Any] = {"address": CONVERSION_ADDRESS, "events": len(names)}
    if names:
        row["names"] = names
    return row


def _conversion_header(rules: ExchangeRules) -> dict[str, Any]:
    """Реквизиты заголовка, которые безопасно показать рядом с событиями."""
    root = rules.root
    header: dict[str, Any] = {}
    version = root.child("ВерсияФормата")
    if version is not None and version.text:
        header["ВерсияФормата"] = version.text
        mode = version.attrs.get("РежимСовместимости")
        if isinstance(mode, str) and mode.strip():
            header["РежимСовместимости"] = mode
    for tag in ("Ид", "Наименование", "ДатаВремяСоздания", "Комментарий"):
        raw = root.values.get(tag)
        if not isinstance(raw, str) or not raw.strip():
            continue
        # Писатель дополняет `Ид` пробелами; в ответ — без хвостовых, как коды правил.
        shown = raw.rstrip() if tag == "Ид" else raw
        header[tag] = clip(shown)
    if rules.source_name:
        header["Источник"] = rules.source_name
    if rules.target_name:
        header["Приемник"] = rules.target_name
    flag = "УдалятьСопоставленныеОбъектыВПриемникеПриИхУдаленииВИсточнике"
    if root.values.get(flag) is True:
        header[flag] = True
    return header


def report_summary(report: ValidationReport) -> dict[str, Any]:
    by_check: dict[str, int] = {}
    for issue in report.issues:
        by_check[issue.check] = by_check.get(issue.check, 0) + 1
    return {
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        **(
            {"info": sum(i.level is Level.INFO for i in report.issues)}
            if any(i.level is Level.INFO for i in report.issues)
            else {}
        ),
        "skipped": len(report.skipped),
        "by_check": by_check,
        "text": report.summary(),
    }


# `rules_validate`: русские подписи ответа и английские синонимы. Регистр не важен.
_LEVEL_ALIASES = {
    "ошибка": Level.ERROR.value,
    "error": Level.ERROR.value,
    "предупреждение": Level.WARNING.value,
    "warning": Level.WARNING.value,
}
LEVEL_ALLOWED = "«ошибка», «предупреждение», «error», «warning»"

# `rules_diff`: полный ответ как раньше или страница адресов без содержимого.
_DETAIL_ALIASES = {
    "full": "full",
    "полный": "full",
    "brief": "brief",
    "кратко": "brief",
    "краткий": "brief",
}
DETAIL_ALLOWED = "«полный», «кратко», «краткий», «full», «brief»"


def parse_detail(detail: str | None) -> str:
    """Уровень подробности `rules_diff`. Пусто — полный ответ.

    Недопустимое значение — `ValueError` со списком допустимых.
    """
    if detail is None or detail == "":
        return "full"
    if not isinstance(detail, str):
        raise ValueError(f"Уровень подробности должен быть строкой. Допустимые: {DETAIL_ALLOWED}")
    found = _DETAIL_ALIASES.get(detail.casefold())
    if found is None:
        raise ValueError(
            f"Уровень подробности «{detail}» не принимается. Допустимые: {DETAIL_ALLOWED}"
        )
    return found


def parse_level(level: str | None) -> str | None:
    """Уровень отбора страницы замечаний. Пусто — без отбора.

    Недопустимое значение — `ValueError` со списком допустимых, до прогона проверок.
    """
    if level is None:
        return None
    if not isinstance(level, str):
        raise ValueError(f"Уровень должен быть строкой. Допустимые: {LEVEL_ALLOWED}")
    found = _LEVEL_ALIASES.get(level.casefold())
    if found is None:
        raise ValueError(f"Уровень «{level}» не принимается. Допустимые: {LEVEL_ALLOWED}")
    return found


def report_view(
    report: ValidationReport,
    level: str | None,
    check_prefix: str | None,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Итог и пропуски — по всему отчёту; уровень и префикс отбирают только страницу замечаний."""
    issues = [issue.to_dict() for issue in report.issues]
    if level:
        issues = [issue for issue in issues if issue["level"] == Level(level).value]
    if check_prefix:
        issues = [issue for issue in issues if issue["check"].startswith(check_prefix)]
    return {
        "summary": report_summary(report),
        "skipped": [item.to_dict() for item in report.skipped],
        "issues": slice_rows(issues, offset, limit),
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
    """Объект правил регистрации из словаря инструмента."""
    return parse_registration_object(item)
