"""Проверка поиска объекта в приёмнике и параметров его обработчика.

Читатель — `DataProcessors/КонвертацияОбъектовИнформационныхБаз/Ext/ObjectModule.bsl` (БСП).
Объект ищется сразу по узлу `<Ссылка>`: загрузка встречает его первым и вызывает
`НайтиОбъектПоСсылке` с текущими `ПараметрыОбъекта` (БСП:10735–10757), а обработчик поиска
получает их же (БСП:8947). В узел `<Ссылка>` выгрузка пишет только свойства поиска ПКО
(БСП:790–826); ПКС с `ИмяПараметраДляПередачи` пишется узлом `<ЗначениеПараметра>`
(БСП:12691), и из узла ссылки такой параметр попадает в `ПараметрыОбъекта` до поиска
(БСП:8228–8291). Параметр ПКС без флага «Поиск» идёт в свойствах объекта после ссылки и читается
уже после поиска (БСП:10705): в обработчике поиска его нет, `ПараметрыОбъекта` — `Неопределено`,
если других параметров в ссылке нет.

- Обработчик поиска читает параметр, который передаёт ПКС без «Поиск», — предупреждение
  (ветка обработчика по этому параметру не выполнится — типичная причина дублей).
- Параметр не передаёт ни одно включённое ПКС верхнего уровня ПКО — предупреждение.

Ссылка на параметр — `ПараметрыОбъекта["Имя"]` или `ПараметрыОбъекта.Получить("Имя")` в тексте
без комментариев `//`.

Формы поиска объекта в приёмнике — тоже предупреждения (`check_search_objects`). Уровень
предупреждения: объект может найтись по данным или обработчик может подменить ссылку, но форма
правил по коду исполнителя даёт дубль, слияние или невызываемый обработчик.

- Нет ключей у ссылочного приёмника — новый объект при каждой загрузке
  (БСП:788–791, БСП:10617–10671).
- Единственное поле поиска `ЭтоГруппа` — первая строка запроса (БСП:8282–8286, БСП:8424–8468).
- Нет `ЭтоГруппа` у иерархии групп и элементов — группы создаются элементами (БСП:7873–7881).
- Нет `Владелец` у подчинённого справочника — запрос владельца не различает (БСП:8424–8468).
- Имя в литерале `СтрокаИменСвойствПоиска` не из свойств поиска молча выпадает
  (БСП:8565–8574, БСП:8452–8454). Имя, собранное кодом, — пропуск, а не «замечаний нет».
- Обработчик при синхронизации без продолжения поиска не вызывается (БСП:8835–8860).
- Продолжение поиска без полей и без обработчика ничего не находит
  (БСП:8452–8454, БСП:9058–9061).

`Поиск` и `Обязательное` равнозначны (БСП:5482–5502). Вложенное ПКС в таблицу поиска ПКО
не попадает: группа свойств грузится без неё (БСП:5331). ПКС с `ИмяПараметраДляПередачи`
уходит параметром, а не ключом `СвойстваПоиска` (БСП:12691–12700). Без структуры приёмника
проверки иерархии и владельца пропускаются.
"""

import json
import re
import sqlite3
from dataclasses import dataclass

from kd2_rules_mcp.kd2.model import ExchangeRules, Node
from kd2_rules_mcp.validation.address import rule_address, side_name
from kd2_rules_mcp.validation.report import ValidationReport

PARAM_NOT_IN_SEARCH = "search.param_not_in_search"
PARAM_NOT_PASSED = "search.param_not_passed"

_PARAMETER = re.compile(
    r'ПараметрыОбъекта\s*(?:\[\s*"([^"\n]+)"\s*\]|\.\s*Получить\s*\(\s*"([^"\n]+)"\s*\))',
    re.IGNORECASE,
)


def check_search_params(rules: ExchangeRules) -> ValidationReport:
    """Параметры, которые обработчик поиска ПКО читает, против ПКС, которые их передают."""
    report = ValidationReport()
    for pko in rules.pko():
        text = pko.get("ПоследовательностьПолейПоиска")
        if not isinstance(text, str) or not text.strip():
            continue
        passed = _passed_parameters(pko)
        for name in sorted(_read_parameters(text), key=str.casefold):
            search = passed.get(name.casefold())
            address = rule_address(pko)
            if search is None:
                report.warning(
                    PARAM_NOT_PASSED,
                    address,
                    f"ПоследовательностьПолейПоиска читает параметр «{name}», но ни одно "
                    "включённое ПКС верхнего уровня его не передаёт (ИмяПараметраДляПередачи): "
                    "при поиске его нет",
                )
            elif not search:
                report.warning(
                    PARAM_NOT_IN_SEARCH,
                    address,
                    f"ПоследовательностьПолейПоиска читает параметр «{name}», а ПКС передаёт его "
                    "без флага «Поиск»: параметр приходит в свойствах объекта уже после поиска, "
                    "в обработчике поиска его нет и ветка по нему не выполнится — включите «Поиск» "
                    "у ПКС параметра",
                )
    return report


def _read_parameters(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        for match in _PARAMETER.finditer(_without_comment(line)):
            names.add(match.group(1) or match.group(2))
    return names


def _without_comment(line: str) -> str:
    """Строка до комментария `//` вне строкового литерала."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif not quoted and line.startswith("//", index):
            return line[:index]
    return line


def _passed_parameters(pko: Node) -> dict[str, bool]:
    """Параметры, которые передают включённые ПКС верхнего уровня: имя → есть ли флаг «Поиск»."""
    passed: dict[str, bool] = {}
    properties = pko.child("Свойства")
    if properties is None:
        return passed
    for item in properties.items:
        if item.is_group or item.attrs.get("Отключить") is True:
            continue
        name = str(item.get("ИмяПараметраДляПередачи")).strip()
        if name:
            key = name.casefold()
            passed[key] = passed.get(key, False) or item.attrs.get("Поиск") is True
    return passed


# --- Формы поиска объекта -----------------------------------------------------------------

NO_KEYS = "search.no_keys"
ONLY_GROUP_KEY = "search.only_group_key"
GROUP_FLAG = "search.group_flag"
OWNER_KEY = "search.owner_key"
NAME_NOT_SEARCH_PROP = "search.name_not_search_prop"
UNREACHABLE_HANDLER = "search.unreachable_handler"
CONTINUE_WITHOUT_FIELDS = "search.continue_without_fields"

# Ссылочные приёмники, для которых `СоздатьНовыйОбъект` создаёт объект (БСП:7870–7913).
# Перечисление возвращает пустую ссылку (БСП:7915–7918), точка маршрута — `Неопределено`
# (БСП:7920–7922); регистры и константы ссылкой не являются.
_REF_CREATING = (
    "СправочникСсылка.",
    "ДокументСсылка.",
    "ПланВидовХарактеристикСсылка.",
    "ПланСчетовСсылка.",
    "ПланВидовРасчетаСсылка.",
    "ПланОбменаСсылка.",
    "БизнесПроцессСсылка.",
    "ЗадачаСсылка.",
)
_CATALOG = "СправочникСсылка."
_CHARACTERISTIC = "ПланВидовХарактеристикСсылка."
_GROUP_KINDS = frozenset({"Справочник", "ПланВидовХарактеристик"})
# В условие запроса не входят (БСП:8429–8431) и ПКС с таким именем не бывает.
_SYSTEM_NAMES = frozenset(
    name.casefold() for name in ("{УникальныйИдентификатор}", "{ИмяПредопределенногоЭлемента}")
)
_SEARCH_STRING = "строкаименсвойствпоиска"
_ASSIGN_BEFORE = frozenset({"", ";", "тогда", "иначе", "цикл"})
_SYNC = "СинхронизироватьПоИдентификатору"
_CONTINUE = "ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли"
_HANDLER = "ПоследовательностьПолейПоиска"
_NO_STRUCTURE = "структура приёмника не загружена"


@dataclass(frozen=True, slots=True)
class _Receiver:
    """Приёмник по структуре: иерархия групп и элементов и подчинённость справочника."""

    kind: str
    folders: bool
    subordinate: bool


def check_search_objects(
    rules: ExchangeRules, target: sqlite3.Connection | None = None
) -> ValidationReport:
    """Формы ПКО, из-за которых приёмник создаёт дубль, сливает объекты или не вызывает поиск.

    Без структуры приёмника `search.group_flag` и `search.owner_key` не выполняются.
    Объект приёмника, которого в загруженной структуре нет, пропускается отдельно:
    иерархию и подчинённость по нему установить нельзя. Имя свойств поиска, собранное
    кодом, пропускает `search.name_not_search_prop` на этом ПКО.
    """
    report = ValidationReport()
    receivers = _receivers(target)
    if target is None:
        report.skip(GROUP_FLAG, _NO_STRUCTURE)
        report.skip(OWNER_KEY, _NO_STRUCTURE)
    for pko in rules.pko():
        _check_pko(report, pko, receivers, target is not None)
    return report


def _check_pko(
    report: ValidationReport, pko: Node, receivers: dict[str, _Receiver] | None, has_target: bool
) -> None:
    address = rule_address(pko)
    receiver = str(pko.get("Приемник")).strip()
    sync = _flag(pko, _SYNC)
    continue_search = _flag(pko, _CONTINUE)
    handler = _handler_text(pko)
    flagged = _flagged_search(pko)
    fields = _field_names(pko)
    by_fields = (not sync) or continue_search

    if _creates_reference(receiver) and not sync and not flagged and not handler:
        report.warning(
            NO_KEYS,
            address,
            "Нет синхронизации по идентификатору, нет ПКС с признаком поиска "
            "(«Поиск» или «Обязательное») и нет обработчика «ПоследовательностьПолейПоиска»: "
            "узел «Ссылка» не формируется, при каждой загрузке создаётся новый объект",
        )
    if by_fields and _only_group(fields):
        report.warning(
            ONLY_GROUP_KEY,
            address,
            "Единственное свойство поиска — «ЭтоГруппа»: условие запроса только по признаку "
            "группы, берётся первая строка — разные элементы находят один и тот же объект",
        )
    _check_structure(report, address, receiver, fields, by_fields, receivers, has_target)
    # При синхронизации без продолжения обработчик не выполняется (БСП:8856–8860,
    # БСП:8884–8888): строка имён не разбирается, отдельное замечание — unreachable.
    if handler and not (sync and not continue_search):
        _check_names(report, address, handler, fields)
    if handler and sync and not continue_search:
        report.warning(
            UNREACHABLE_HANDLER,
            address,
            "Обработчик «ПоследовательностьПолейПоиска» не вызывается: включена синхронизация "
            "по идентификатору и нет продолжения поиска по полям",
        )
    # Создание на шаге «не нашли» выключено флагом (БСП:9058); объект пропускается
    # (БСП:10771–10776), нового экземпляра нет.
    if (
        continue_search
        and not fields
        and not handler
        and _creates_reference(receiver)
        and not _flag(pko, "НеСоздаватьЕслиНеНайден")
    ):
        report.warning(
            CONTINUE_WITHOUT_FIELDS,
            address,
            "Включено продолжение поиска по полям, но свойств поиска и обработчика "
            "«ПоследовательностьПолейПоиска» нет: по полям объект не находится и создаётся новый",
        )


def _check_structure(
    report: ValidationReport,
    address: str,
    receiver: str,
    fields: set[str],
    by_fields: bool,
    receivers: dict[str, _Receiver] | None,
    has_target: bool,
) -> None:
    group_applies = receiver.startswith((_CATALOG, _CHARACTERISTIC))
    owner_applies = receiver.startswith(_CATALOG)
    if not has_target or receivers is None or not (group_applies or owner_applies):
        return
    info = receivers.get(receiver)
    if info is None:
        if group_applies:
            report.skip(
                GROUP_FLAG,
                f"{address}: объекта «{receiver}» нет в структуре приёмника — "
                "нельзя установить иерархию групп и элементов",
            )
        if owner_applies:
            report.skip(
                OWNER_KEY,
                f"{address}: объекта «{receiver}» нет в структуре приёмника — "
                "нельзя установить, подчинённый ли справочник",
            )
        return
    if info.folders and "этогруппа" not in fields:
        title = "справочник" if info.kind == "Справочник" else "план видов характеристик"
        report.warning(
            GROUP_FLAG,
            address,
            f"Приёмник «{receiver}» — {title} с иерархией групп и элементов, а включённого "
            "ПКС «ЭтоГруппа» с признаком поиска нет: новые группы создаются элементами, "
            "в том числе при синхронизации по идентификатору; при поиске по полям группа "
            "находит одноимённый элемент",
        )
    if info.subordinate and by_fields and "владелец" not in fields:
        report.warning(
            OWNER_KEY,
            address,
            f"Приёмник «{receiver}» — подчинённый справочник, поиск идёт по полям, "
            "а «Владелец» не среди свойств поиска: элементы разных владельцев сливаются в один",
        )


def _check_names(report: ValidationReport, address: str, handler: str, fields: set[str]) -> None:
    if not handler:
        return
    unknown: list[str] = []
    seen: set[str] = set()
    none_left = False
    opaque = False
    # Поиск идёт после обработчика (БСП:8959, БСП:8981): имя, записанное в `СвойстваПоиска`,
    # к этому моменту уже ключ соответствия и из строки не выпадает (БСП:8565–8574).
    known = fields | _written_search_names(handler)
    for literal in _search_string_assignments(handler):
        if literal is None:
            opaque = True
            continue
        names = _split_names(literal)
        business = [name for name in names if name.casefold() not in _SYSTEM_NAMES]
        missed = [name for name in business if name.casefold() not in known]
        for name in missed:
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                unknown.append(name)
        if business and not any(name.casefold() in known for name in business):
            none_left = True
    if unknown:
        listed = "», «".join(unknown)
        if len(unknown) == 1:
            intro = f"имя «{listed}», которого нет"
            drop = "имя молча выпадает из условия"
        else:
            intro = f"имена «{listed}», которых нет"
            drop = "эти имена молча выпадают из условия"
        tail = (
            "; не осталось ни одного имени — вариант поиска ничего не находит" if none_left else ""
        )
        report.warning(
            NAME_NOT_SEARCH_PROP,
            address,
            "ПоследовательностьПолейПоиска задаёт "
            f"{intro} среди ПКС с признаком поиска: {drop}{tail}",
        )
    if opaque:
        report.skip(
            NAME_NOT_SEARCH_PROP,
            f"{address}: строка имён свойств поиска собрана кодом, а не литералом — "
            "нельзя проверить, какие имена попадут в условие",
        )


def _creates_reference(type_name: str) -> bool:
    return type_name.startswith(_REF_CREATING)


def _flag(pko: Node, name: str) -> bool:
    return pko.get(name) is True


def _handler_text(pko: Node) -> str:
    text = pko.get(_HANDLER)
    return text.strip() if isinstance(text, str) else ""


def _only_group(fields: set[str]) -> bool:
    return fields == {"этогруппа"}


def _flagged_search(pko: Node) -> bool:
    """Есть включённое ПКС верхнего уровня с «Поиск» или «Обязательное»."""
    return any(_is_search(item) for item in _top_properties(pko))


def _field_names(pko: Node) -> set[str]:
    """Имена приёмника свойств, которые попадают в `СвойстваПоиска`, в нижнем регистре.

    Параметр (`ИмяПараметраДляПередачи`) пишется узлом `<ЗначениеПараметра>` и ключом
    соответствия не становится (БСП:12691–12700).
    """
    names: set[str] = set()
    for item in _top_properties(pko):
        if not _is_search(item) or str(item.get("ИмяПараметраДляПередачи")).strip():
            continue
        receiver = side_name(item, "Приемник")
        if receiver:
            names.add(receiver.casefold())
    return names


def _top_properties(pko: Node) -> list[Node]:
    properties = pko.child("Свойства")
    if properties is None:
        return []
    return [
        item
        for item in properties.items
        if not item.is_group and item.attrs.get("Отключить") is not True
    ]


def _is_search(item: Node) -> bool:
    return item.attrs.get("Поиск") is True or item.attrs.get("Обязательное") is True


def _receivers(connection: sqlite3.Connection | None) -> dict[str, _Receiver] | None:
    if connection is None:
        return None
    # Один проход по свойствам вместо двух вложенных выборок на каждый объект: на большой
    # конфигурации вложенные выборки без индекса по имени перебирали всю таблицу свойств
    # для каждого объекта (минуты вместо долей секунды).
    with_group: set[int] = set()
    with_owner: set[int] = set()
    for object_id, name in connection.execute(
        "SELECT object_id, name FROM properties "
        "WHERE kind = 'Свойство' AND name IN ('ЭтоГруппа', 'Владелец')"
    ):
        (with_group if name == "ЭтоГруппа" else with_owner).add(int(object_id))
    rows = connection.execute(
        "SELECT o.id, o.type_name, o.kind, o.attrs FROM objects AS o WHERE o.is_group = 0"
    )
    found: dict[str, _Receiver] = {}
    for object_id, type_name, kind, attrs_raw in rows:
        has_group = int(object_id) in with_group
        has_owner = int(object_id) in with_owner
        attrs = _attrs(str(attrs_raw))
        kind_name = str(kind)
        # Иерархия групп и элементов есть, только когда объект иерархический и вид —
        # групп и элементов. У не иерархического справочника вид по умолчанию всё равно
        # «ИерархияГруппИЭлементов», поэтому одного вида мало. Свойство «ЭтоГруппа» —
        # тот же признак, если флаг в attrs не попал.
        hierarchical = str(attrs.get("Иерархический", "")).casefold() == "true"
        folders = kind_name in _GROUP_KINDS and (
            has_group or (hierarchical and attrs.get("ВидИерархии") == "ИерархияГруппИЭлементов")
        )
        subordinate = kind_name == "Справочник" and (
            attrs.get("Подчиненный") == "true" or has_owner
        )
        found[str(type_name)] = _Receiver(kind_name, folders, subordinate)
    return found


def _attrs(raw: str) -> dict[str, str]:
    parsed = json.loads(raw) if raw else {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


def _search_string_assignments(text: str) -> list[str | None]:
    """Литералы `СтрокаИменСвойствПоиска`: текст без кавычек либо `None`, если собрано кодом.

    Пустая строка — отдельный случай: поиск по всем свойствам (БСП:8559–8561), это не пропуск.
    Сравнение в `Если` присваиванием не считается. Комментарии `//` отброшены.
    """
    cleaned = "\n".join(_without_comment(line) for line in text.splitlines())
    found: list[str | None] = []
    length = len(cleaned)
    lower = cleaned.casefold()
    index = 0
    while index < length:
        if cleaned[index] == '"':
            index = _skip_string(cleaned, index)
            continue
        if lower.startswith(_SEARCH_STRING, index) and _is_identifier(cleaned, index):
            after = index + len(_SEARCH_STRING)
            if _is_assignment(cleaned, index, after):
                rhs, index = _read_rhs(cleaned, _skip_equals(cleaned, after))
                found.append(_literal_inner(rhs))
                continue
        index += 1
    return found


def _is_identifier(text: str, start: int) -> bool:
    before = text[start - 1] if start else ""
    after_at = start + len(_SEARCH_STRING)
    after = text[after_at] if after_at < len(text) else ""
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _is_assignment(text: str, start: int, after_name: int) -> bool:
    if _previous_token(text, start).casefold() not in _ASSIGN_BEFORE:
        return False
    cursor = after_name
    while cursor < len(text) and text[cursor] in " \t\r\n":
        cursor += 1
    return cursor < len(text) and text[cursor] == "="


def _skip_equals(text: str, after_name: int) -> int:
    cursor = after_name
    while cursor < len(text) and text[cursor] != "=":
        cursor += 1
    return cursor + 1


def _previous_token(text: str, pos: int) -> str:
    cursor = pos - 1
    while cursor >= 0 and text[cursor] in " \t\r\n":
        cursor -= 1
    if cursor < 0:
        return ""
    if not (text[cursor].isalnum() or text[cursor] == "_"):
        return text[cursor]
    end = cursor + 1
    while cursor >= 0 and (text[cursor].isalnum() or text[cursor] == "_"):
        cursor -= 1
    return text[cursor + 1 : end]


def _read_rhs(text: str, start: int) -> tuple[str, int]:
    """Текст до `;` вне строкового литерала."""
    index = start
    in_string = False
    while index < len(text):
        char = text[index]
        if char == '"':
            if in_string and index + 1 < len(text) and text[index + 1] == '"':
                index += 2
                continue
            in_string = not in_string
        elif char == ";" and not in_string:
            return text[start:index], index + 1
        index += 1
    return text[start:], len(text)


def _literal_inner(rhs: str) -> str | None:
    text = rhs.strip()
    if len(text) < 2 or text[0] != '"' or not _closes_one_string(text):
        return None
    return text[1:-1].replace('""', '"')


def _closes_one_string(text: str) -> bool:
    index = 1
    while index < len(text):
        if text[index] == '"':
            if index + 1 < len(text) and text[index + 1] == '"':
                index += 2
                continue
            return index == len(text) - 1
        index += 1
    return False


def _skip_string(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == '"':
            if index + 1 < len(text) and text[index + 1] == '"':
                index += 2
                continue
            return index + 1
        index += 1
    return len(text)


def _split_names(inner: str) -> list[str]:
    """Имена из литерала: разделители — запятая и пробел, пустые части отбрасываются (БСП:8565)."""
    return [part for part in re.split(r"[, ]+", inner) if part]


def _written_search_names(text: str) -> set[str]:
    """Имена, которые обработчик записывает в `СвойстваПоиска` строковым литералом.

    Чтение `СвойстваПоиска["Имя"]` в условии присваиванием не считается.
    """
    cleaned = "\n".join(_without_comment(line) for line in text.splitlines())
    names: set[str] = set()
    lower = cleaned.casefold()
    ident = "свойствапоиска"
    index = 0
    while index < len(cleaned):
        pos = lower.find(ident, index)
        if pos < 0:
            break
        index = pos + len(ident)
        if not _is_word(cleaned, pos, len(ident)):
            continue
        if _previous_token(cleaned, pos).casefold() not in _ASSIGN_BEFORE:
            continue
        names.update(_property_write(cleaned, index))
    return names


def _is_word(text: str, start: int, size: int) -> bool:
    before = text[start - 1] if start else ""
    after_at = start + size
    after = text[after_at] if after_at < len(text) else ""
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _property_write(text: str, after_name: int) -> set[str]:
    cursor = after_name
    while cursor < len(text) and text[cursor] in " \t\r\n":
        cursor += 1
    if cursor >= len(text):
        return set()
    if text[cursor] == ".":
        match = re.match(r"\.\s*Вставить\s*\(\s*\"([^\"\n]+)\"", text[cursor:], re.IGNORECASE)
        return {match.group(1).casefold()} if match else set()
    if text[cursor] == "[":
        match = re.match(r"\[\s*\"([^\"\n]+)\"\s*\]\s*=", text[cursor:])
        return {match.group(1).casefold()} if match else set()
    return set()
