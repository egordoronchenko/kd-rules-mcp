"""Предупреждения обмена через план: формы, которые исполнитель БСП теряет или не исполняет.

Все замечания — предупреждения. Исполнитель — модуль объекта
`КонвертацияОбъектовИнформационныхБаз` (БСП 3.1.12). Заголовок комплекта сверяет форма
загрузки правил синхронизации, модуль регистра `ПравилаДляОбменаДанными` (`РПО:N`).

- `structure.enum_pkz` — у ПКО перечисления нет ни одного ПКЗ: сопоставление не строится
  (БСП:738), в сообщение уходит номер без имени значения (БСП:841, БСП:13238–13244).
- `structure.predefined_pkz` — одноимённые предопределённые есть с обеих сторон, ПКЗ на них
  нет, и имя в `<Ссылка>` не пишется: его берёт только соответствие ПКЗ
  (БСП:705–736, БСП:12806–12818) либо присваивание `ИмяПредопределенногоЭлемента` в
  `ПередВыгрузкой` / `ПередКонвертациейОбъекта` (до блока 705). Приёмник ищет по этому
  имени раньше GUID (БСП:8790–8830, БСП:3920–3928). Предупреждение — только если приёмник
  тогда создаёт новый элемент: объект выгружается целиком, `НеСоздаватьЕслиНеНайден` не
  стоит, и до создания не доходит ни шаг 2, ни поля поиска. Ссылка с GUID без полного
  объекта не создаётся (БСП:8736, БСП:8884–8888, БСП:9054–9056).
- `structure.document_posting` — у проводимого документа нет включённого ПКС `Проведен`:
  режим берётся из `Объект.Проведен` (БСП:11065–11083), новый документ непроведён,
  найденный сохраняет состояние приёмника. Не предупреждает, если режим или `Проведен`
  задаёт обработчик до записи: `ПередВыгрузкой` и `ПередКонвертациейОбъекта` (атрибут
  пишется в БСП:924–930, до `ПриВыгрузке`), `ПередЗагрузкой`, `ПриЗагрузке`,
  `ПослеЗагрузки` и глобальные `ПередЗагрузкойОбъекта` / `ПослеЗагрузкиОбъекта`.
  `РежимЗаписи` в `ПриВыгрузке` атрибут уже не меняет.
- `structure.incoming_key` — ПКС `ПолучитьИзВходящихДанных` читает `ВходящиеДанные[Приемник]`
  (БСП:12075–12087; у группы — БСП:11723) после `ПередВыгрузкой` и, для свойств не из
  `<Ссылка>`, после `ПриВыгрузке` ПКО (БСП:941–986, затем БСП:1009; свойства поиска —
  раньше, БСП:826). Нет ключа — исключение внутри `Попытка`: протокол 68, 67 или 66.
  «Продолжать при ошибке» выключено — запись сообщения прерывается (БСП:5028–5030,
  БСП:18515–18519, БСП:18086–18087); включено — только протокол. Явный вызов выгрузки
  этого ПКО, в том числе из строки `Выполнить`, у которого входящие данные не разобрать, —
  пропуск. Свойство не из узла «Ссылка» у ПКО, которое уходит только ссылкой, не читается
  (БСП:881–889) — предупреждения нет.
- `handlers.object_write` — прямой `Объект.Записать(` в `ПослеЗагрузки` ПКО или
  `ПослеЗагрузкиДанных`: режим загрузки ставится позже (БСП:1761), после обработчика
  (БСП:11005, БСП:11112).
- `handlers.pvd_arbitrary` — `СпособОтбораДанных` = `ПроизвольныйАлгоритм` в обмене через
  план не используется: выгрузка берёт изменения узла (БСП:18210–18236). Способ разбирает
  только `ВыгрузитьДанныеПоПравилу` (БСП:1405), её обмен через план не вызывает.
- `handlers.pvd_selection` — `ВыборкаДанных` в `ПередОбработкой` ПВД — заглушка
  (БСП:18173); после обработчика (БСП:18369–18404) выборка изменений её не читает.
- `handlers.export_key` — `КлючВыгружаемыхДанных` в `ПередВыгрузкой` без
  `ЗапоминатьВыгруженные = Истина`: кэш включён только при ссылке на себя и флаге ПКО
  (БСП:388–398, БСП:424, БСП:469–471), ищется после обработчика (БСП:607).
- `format.source_name`, `format.source_version` — `<Источник>` заголовка против структуры
  источника: имя без учёта регистра, из имени базы вырезано «БАЗОВАЯ» (`РПО:43–44`);
  версия — первые три числа (`РПО:64–69`, разбор — `ОбщегоНазначенияКлиентСервер:1030–1043`).
  `<Приемник>` форма не сверяет (`РПО:961–967`).

Не добавлено: одно `НеЗапоминатьВыгруженные`. Флаг читается (БСП:5806–5808) и выключает
запоминание при ссылке объекта на себя (БСП:398) — в обмене через план эта часть исполняется.
"""

import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from kd2_rules_mcp.kd2.model import ExchangeRules, Node, rule_code
from kd2_rules_mcp.validation.address import (
    CONVERSION_ADDRESS,
    pks_address,
    pks_segments,
    rule_address,
    side_name,
)
from kd2_rules_mcp.validation.report import ValidationReport
from kd2_rules_mcp.validation.structure import (
    REF_ONLY_LOAD_NOTE,
    Structure,
    ref_only_pko_codes,
    unreachable_pko_codes,
)

ENUM_PKZ = "structure.enum_pkz"
PREDEFINED_PKZ = "structure.predefined_pkz"
DOCUMENT_POSTING = "structure.document_posting"
INCOMING_KEY = "structure.incoming_key"
OBJECT_WRITE = "handlers.object_write"
PVD_ARBITRARY = "handlers.pvd_arbitrary"
PVD_SELECTION = "handlers.pvd_selection"
EXPORT_KEY = "handlers.export_key"
SOURCE_NAME = "format.source_name"
SOURCE_VERSION = "format.source_version"

_MANUAL = (
    "Правило рассчитано на ручной обмен универсальной обработкой, "
    "в обмене через план обмена эта часть не исполняется"
)
_PREDEFINED_PREFIXES = (
    "СправочникСсылка.",
    "ПланВидовХарактеристикСсылка.",
    "ПланСчетовСсылка.",
    "ПланВидовРасчетаСсылка.",
)
_PREDEFINED_NAME = "имяпредопределенныхданных"
_SHOWN = 12

_STRING = re.compile(r'"(?:[^"]|"")*"')
_ALGORITHM = re.compile(
    r"(?<![\w.])Алгоритмы\s*\.\s*([^\W\d]\w*)\b(?!\s*\()",
    re.IGNORECASE,
)
_EXECUTE_OPAQUE = re.compile(r'(?<![\w.])Выполнить\s*\(\s*(?!")', re.IGNORECASE)
_EXECUTE_STRING = re.compile(
    r'(?<![\w.])Выполнить\s*\(\s*("(?:[^"]|"")*")\s*\)',
    re.IGNORECASE,
)
_POSTED_ASSIGN = re.compile(r"(?<![\w.])Объект\s*\.\s*Проведен\s*=", re.IGNORECASE)
_MODE_ASSIGN = re.compile(r"(?<![\w.])РежимЗаписи\s*=\s*([^\n]*)", re.IGNORECASE)
_OBJECT_WRITE = re.compile(r"(?<![\w.])Объект\s*\.\s*Записать\s*\(", re.IGNORECASE)
_SELECTION = re.compile(r"(?<![\w.])ВыборкаДанных\b", re.IGNORECASE)
_EXPORT_KEY_ASSIGN = re.compile(r"(?<![\w.])КлючВыгружаемыхДанных\s*=", re.IGNORECASE)
_REMEMBER_TRUE = re.compile(
    r"(?<![\w.])ЗапоминатьВыгруженные\s*=\s*Истина\b",
    re.IGNORECASE,
)
_PREDEFINED_MARK = re.compile(
    r"(?<![\w.])(?:ИмяПредопределенныхДанных|ПредопределенноеЗначение)\b",
    re.IGNORECASE,
)
_INCOMING_VAR = re.compile(r"(?<![\w.])(?:ВходящиеДанные|ИсходящиеДанные)\b", re.IGNORECASE)
_DYNAMIC_INSERT = re.compile(
    r'(?<![\w.])(?:ВходящиеДанные|ИсходящиеДанные)\s*\.\s*Вставить\s*\(\s*(?!")',
    re.IGNORECASE,
)
_DYNAMIC_INDEX = re.compile(
    r'(?<![\w.])(?:ВходящиеДанные|ИсходящиеДанные)\s*\[\s*(?!")',
    re.IGNORECASE,
)
_DYNAMIC_OBJECT = re.compile(r"(?<![\w.])Объект\s*\[", re.IGNORECASE)
_BYPASS_TRUE = re.compile(r"(?<![\w.])(?:Отказ|Пусто)\s*=\s*Истина\b", re.IGNORECASE)
_EXPORT_WHOLE = re.compile(r"(?<![\w.])ВыгрузитьОбъект\s*=\s*Истина\b", re.IGNORECASE)
_PREDEFINED_ASSIGN = re.compile(
    r"(?<![\w.])ИмяПредопределенногоЭлемента\s*=\s*([^\n]*)",
    re.IGNORECASE,
)
_PKO_NAME_ASSIGN = re.compile(r'(?<![\w.])ИмяПКО\s*=\s*"([^"]*)"', re.IGNORECASE)
# Вызов выгрузки с аргументом входящих данных и кодом ПКО: (позиция входящих, позиция кода), с 1.
_EXPORT_CALL = re.compile(
    r"(?<![\w.])(ВыгрузитьПоПравилу|ВыгрузитьРегистр|ВыгрузкаОбъектаВыборки)\s*\(",
    re.IGNORECASE,
)
_EXPORT_ARGUMENTS = {
    "выгрузитьпоправилу": (3, 5),
    "выгрузитьрегистр": (3, 5),
    "выгрузкаобъектавыборки": (4, 9),
}
_LOAD_MODE_EVENTS = (
    "ПередКонвертациейОбъекта",
    "ПередЗагрузкойОбъекта",
    "ПослеЗагрузкиОбъекта",
)
_EXPRESSION_ASSIGN = re.compile(r"(?<![\w.])Выражение\s*=\s*([^\n]*)", re.IGNORECASE)
# Обработчики группы, которые видят «ВходящиеДанные» до чтения свойств строки.
_GROUP_FILLERS = ("ПередОбработкойВыгрузки", "ПередВыгрузкой", "ПриВыгрузке")
_INCOMING_ASSIGN = re.compile(
    r"(?<![\w.])(?:ВходящиеДанные|ИсходящиеДанные)\s*=\s*([^\n]*)",
    re.IGNORECASE,
)
_VALUE_ASSIGN = re.compile(r"(?<![\w.])Значение\s*=\s*([^\n]*)", re.IGNORECASE)
_COLLECTION_ASSIGN = re.compile(r"(?<![\w.])КоллекцияОбъектов\s*=\s*([^\n]*)", re.IGNORECASE)
_STRUCTURE_NEW = re.compile(r"Новый\s+(?:Структура|Соответствие)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Text:
    """Развёрнутый текст обработчиков и признак непрозрачности."""

    code: str
    opaque: bool


def check_exchange_plan(
    rules: ExchangeRules,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
) -> ValidationReport:
    """Проверки обмена через план обмена. Без нужной структуры — запись в `skipped`."""
    report = ValidationReport()
    source_structure = Structure.load(source) if source is not None else None
    target_structure = Structure.load(target) if target is not None else None
    algorithms = _algorithms(rules)
    _check_header(report, rules, source)
    _check_values(report, rules, source_structure, target_structure, algorithms)
    _check_posting(report, rules, target_structure, algorithms)
    _check_object_write(report, rules)
    _check_pvd(report, rules, algorithms)
    _check_export_key(report, rules, algorithms)
    _check_incoming(report, rules, algorithms)
    return report


def _check_header(
    report: ValidationReport, rules: ExchangeRules, source: sqlite3.Connection | None
) -> None:
    if source is None:
        report.skip(
            SOURCE_NAME, "нет структуры источника — имя конфигурации в заголовке не с чем сверить"
        )
        report.skip(
            SOURCE_VERSION,
            "нет структуры источника — версию конфигурации в заголовке не с чем сверить",
        )
        return
    meta_name = _meta(source, "config_name")
    meta_version = _meta(source, "config_version")
    rules_name, attrs = rules.config("Источник")
    rules_version = str(attrs.get("ВерсияКонфигурации", ""))
    if not meta_name:
        report.skip(SOURCE_NAME, "в структуре источника нет имени конфигурации")
    elif _config_key(rules_name, strip_basic=False) != _config_key(meta_name, strip_basic=True):
        report.warning(
            SOURCE_NAME,
            CONVERSION_ADDRESS,
            f"Имя конфигурации в «Источник» («{rules_name}») не совпадает с именем "
            f"структуры источника («{meta_name}»): форма загрузки комплекта отклонит правила",
        )
    left, right = _three(rules_version), _three(meta_version)
    if left is None or right is None:
        report.skip(
            SOURCE_VERSION,
            "версия конфигурации в заголовке или в структуре не разбирается на три числа",
        )
    elif left != right:
        report.warning(
            SOURCE_VERSION,
            CONVERSION_ADDRESS,
            f"Версия конфигурации в «Источник» ({_shown_version(rules_version)}) отличается "
            f"от версии структуры источника ({_shown_version(meta_version)}) первыми тремя "
            "числами: загрузка комплекта предупредит о несоответствии",
        )


def _check_values(
    report: ValidationReport,
    rules: ExchangeRules,
    source: Structure | None,
    target: Structure | None,
    algorithms: dict[str, str],
) -> None:
    if source is None:
        report.skip(ENUM_PKZ, "нет структуры источника — не видно, перечисление ли ПКО")
    if source is None or target is None:
        report.skip(
            PREDEFINED_PKZ,
            "нет структуры источника или приёмника — "
            "предопределённые обеих сторон не с чем сверить",
        )
    for pko in rules.pko():
        if _disabled(pko):
            continue
        if source is not None:
            _check_enum(report, pko, source)
        if source is not None and target is not None:
            _check_predefined(report, pko, source, target, rules, algorithms)


def _check_enum(report: ValidationReport, pko: Node, source: Structure) -> None:
    type_name = str(pko.get("Источник"))
    if not type_name.startswith("ПеречислениеСсылка."):
        return
    obj = source.get(type_name)
    if obj is None or obj.kind != "Перечисление":
        return
    if _pkz_sources(pko):
        return
    report.warning(
        ENUM_PKZ,
        rule_address(pko),
        "У ПКО перечисления нет правил конвертации значений: сопоставление пропускается, "
        "имя значения в сообщение не пишется — в приёмнике значение пустое",
    )


def _check_predefined(
    report: ValidationReport,
    pko: Node,
    source: Structure,
    target: Structure,
    rules: ExchangeRules,
    algorithms: dict[str, str],
) -> None:
    source_type = str(pko.get("Источник"))
    target_type = str(pko.get("Приемник"))
    if not source_type.startswith(_PREDEFINED_PREFIXES):
        return
    if not target_type.startswith(_PREDEFINED_PREFIXES):
        return
    source_obj = source.get(source_type)
    target_obj = target.get(target_type)
    if source_obj is None or target_obj is None:
        return
    shared = _predefined(source, source_obj.id) & _predefined(target, target_obj.id)
    uncovered = sorted(shared - _pkz_sources(pko))
    if not uncovered:
        return
    # У одной конфигурации GUID предопределённых совпадают: шаг 3 находит элемент
    # до создания (БСП:8832). Между разными конфигурациями GUID разные.
    if pko.get("СинхронизироватьПоИдентификатору") is True and _same_configuration(source, target):
        return
    named = _predefined_name_assigned(pko, rules, algorithms)
    if named is None:
        report.skip(
            PREDEFINED_PKZ,
            f"{rule_address(pko)}: присваивание имени предопределённого непрозрачно — "
            "не видно, попадёт ли оно в узел «Ссылка»",
        )
        return
    if named:
        return
    found = _predefined_search(pko, algorithms)
    if found is None:
        report.skip(
            PREDEFINED_PKZ,
            f"{rule_address(pko)}: обработчик поиска непрозрачен — не видно, "
            "ищет ли он по имени предопределённого",
        )
        return
    if found:
        return
    creates = _creates_predefined_copy(rules, pko)
    if creates is None:
        report.skip(
            PREDEFINED_PKZ,
            f"{rule_address(pko)}: не видно, выгружается ли объект целиком — "
            "от этого зависит, создаст ли приёмник элемент",
        )
        return
    if not creates:
        return
    report.warning(
        PREDEFINED_PKZ,
        rule_address(pko),
        "Одноимённые предопределённые без ПКЗ ("
        + _sample(uncovered)
        + "): имя предопределённого в узел «Ссылка» не пишется, приёмник не ищет по нему "
        "и создаёт новый элемент",
    )


def _check_posting(
    report: ValidationReport,
    rules: ExchangeRules,
    target: Structure | None,
    algorithms: dict[str, str],
) -> None:
    if target is None:
        report.skip(
            DOCUMENT_POSTING,
            "нет структуры приёмника — не видно, проводится ли документ",
        )
        return
    global_mode, global_opaque = _posting_modes(
        {name: _event(rules, name) for name in _LOAD_MODE_EVENTS},
        algorithms,
    )
    if global_mode == "sets":
        return
    if global_mode == "opaque":
        report.skip(
            DOCUMENT_POSTING,
            "событие конвертации "
            + _quoted(global_opaque)
            + " непрозрачно — не видно, задаёт ли оно режим записи",
        )
        return
    unreachable = unreachable_pko_codes(rules)
    for pko in rules.pko():
        if rule_code(pko.code) in unreachable:
            continue
        if _disabled(pko) or not _postable(pko, target):
            continue
        if _conducted_pks(pko):
            continue
        mode, opaque = _posting_modes(
            {event: str(pko.get(event)) for event in _PKO_POSTING_EVENTS},
            algorithms,
        )
        if mode == "sets":
            continue
        if mode == "opaque":
            report.skip(
                DOCUMENT_POSTING,
                f"{rule_address(pko)}: обработчик {_quoted(opaque)} непрозрачен — не видно, "
                "задаёт ли он режим записи или «Проведен»",
            )
            continue
        report.warning(
            DOCUMENT_POSTING,
            rule_address(pko),
            "У ПКО документа нет включённого ПКС «Проведен»: новый документ записывается "
            "непроведённым, найденный сохраняет состояние приёмника",
        )


_WRITE_PREFIX = "В «{event}» есть «Объект.Записать(»: запись идёт до установки режима загрузки, "
_WRITE_DOCUMENT = _WRITE_PREFIX + "проведение выполнится дважды или прервёт загрузку"
_WRITE_OTHER = _WRITE_PREFIX + "запись выполнится дважды или прервёт загрузку"


def _check_object_write(report: ValidationReport, rules: ExchangeRules) -> None:
    ref_only = ref_only_pko_codes(rules)
    for pko in rules.pko():
        if _disabled(pko):
            continue
        text = str(pko.get("ПослеЗагрузки"))
        if _OBJECT_WRITE.search(_mask(_strip_comments(text))):
            document = str(pko.get("Приемник")).startswith("ДокументСсылка.")
            template = _WRITE_DOCUMENT if document else _WRITE_OTHER
            message = template.format(event="ПослеЗагрузки")
            if rule_code(pko.code) in ref_only:
                message += REF_ONLY_LOAD_NOTE
            report.warning(OBJECT_WRITE, rule_address(pko), message)
    conversion = _event(rules, "ПослеЗагрузкиДанных")
    if _OBJECT_WRITE.search(_mask(_strip_comments(conversion))):
        report.warning(
            OBJECT_WRITE,
            CONVERSION_ADDRESS,
            _WRITE_OTHER.format(event="ПослеЗагрузкиДанных"),
        )


def _check_pvd(report: ValidationReport, rules: ExchangeRules, algorithms: dict[str, str]) -> None:
    for pvd in rules.pvd():
        if _disabled(pvd):
            continue
        if str(pvd.get("СпособОтбораДанных")).casefold() == "произвольныйалгоритм":
            report.warning(
                PVD_ARBITRARY,
                rule_address(pvd),
                f"{_MANUAL}: способ отбора «ПроизвольныйАлгоритм»",
            )
        handler = str(pvd.get("ПередОбработкойПравила"))
        expanded = _expand(handler, algorithms)
        masked = _mask(expanded.code)
        if _SELECTION.search(masked):
            report.warning(
                PVD_SELECTION,
                rule_address(pvd),
                f"{_MANUAL}: «ВыборкаДанных» в «ПередОбработкой»",
            )
        elif expanded.opaque:
            report.skip(
                PVD_SELECTION,
                f"{rule_address(pvd)}: «ПередОбработкой» непрозрачен — не видно, "
                "задаёт ли обработчик «ВыборкаДанных»",
            )


def _check_export_key(
    report: ValidationReport, rules: ExchangeRules, algorithms: dict[str, str]
) -> None:
    for pko in rules.pko():
        if _disabled(pko):
            continue
        expanded = _expand(str(pko.get("ПередВыгрузкой")), algorithms)
        masked = _mask(expanded.code)
        if not _EXPORT_KEY_ASSIGN.search(masked):
            continue
        if _REMEMBER_TRUE.search(masked):
            continue
        if expanded.opaque:
            report.skip(
                EXPORT_KEY,
                f"{rule_address(pko)}: «ПередВыгрузкой» непрозрачен — не видно, "
                "включает ли обработчик запоминание выгруженных",
            )
            continue
        report.warning(
            EXPORT_KEY,
            rule_address(pko),
            f"{_MANUAL}: «КлючВыгружаемыхДанных» без запоминания выгруженных",
        )


def _check_incoming(
    report: ValidationReport, rules: ExchangeRules, algorithms: dict[str, str]
) -> None:
    parents = _parent_index(rules)
    calls = _export_calls(rules, algorithms)
    for pko in rules.pko():
        if _disabled(pko):
            continue
        properties = pko.child("Свойства")
        if properties is None:
            continue
        filling_link = _filling(rules, pko, algorithms, parents, on_export=False)
        filling_object = _filling(rules, pko, algorithms, parents, on_export=True)
        for path, node, _collection, ancestors in _enabled_pks(properties):
            if node.get("ПолучитьИзВходящихДанных") is not True:
                continue
            address = pks_address(pko.code, path)
            key = _incoming_key(node)
            if not key:
                report.skip(
                    INCOMING_KEY,
                    f"{address}: пустое имя приёмника — ключ входящих данных не определён",
                )
                continue
            own = _expand(_own_handler(node), algorithms)
            own_code = _strip_comments(own.code)
            if _assigns_value(own_code, node):
                continue
            # Свойство поиска читается до `ПриВыгрузке` ПКО (БСП:826 против БСП:941).
            filling = filling_link if _is_search_pks(node) else filling_object
            around = _expand(ancestors, algorithms)
            code = "\n".join((filling.code, own_code, _strip_comments(around.code)))
            if _key_present(code, key):
                continue
            call = _incoming_from_call(calls, pko.code, key)
            if call == "present":
                continue
            if call == "opaque":
                report.skip(
                    INCOMING_KEY,
                    f"{address}: ПКО вызывается из обработчика с входящими данными, "
                    f"и не видно, передаётся ли ключ «{key}»",
                )
                continue
            # Свойство не из узла «Ссылка» читается только при выгрузке объекта целиком.
            # По ссылке `ВыгрузитьПоПравилу` возвращается раньше (БСП:881–889).
            if not _is_search_pks(node) and not _pko_is_called(calls, pko.code):
                whole = _exported_as_object(rules, pko)
                if whole is None:
                    report.skip(
                        INCOMING_KEY,
                        f"{address}: не видно, выгружается ли объект целиком — "
                        "свойство вне узла «Ссылка» иначе не читается",
                    )
                    continue
                if not whole:
                    continue
            if own.opaque or around.opaque:
                report.skip(
                    INCOMING_KEY,
                    f"{address}: обработчик ПКС или группы непрозрачен — не видно, задаёт ли он "
                    "значение или ключ входящих данных",
                )
                continue
            if filling.opaque:
                report.skip(
                    INCOMING_KEY,
                    f"{address}: заполнение входящих данных непрозрачно — ключ «{key}» "
                    "может вычисляться",
                )
                continue
            report.warning(
                INCOMING_KEY,
                address,
                f"ПКС «ПолучитьИзВходящихДанных» читает «ВходящиеДанные[{key}]», а этого ключа "
                "нет в обработчиках до чтения свойства: в протокол пишется ошибка, и при "
                "выключенном «продолжать при ошибке» запись сообщения прерывается",
            )


# `ПриВыгрузке` идёт после записи атрибута `РежимЗаписи` (БСП:924–930, затем БСП:941).
# `ПриЗагрузке` — до выбора режима (БСП:10622–10655, решение — БСП:11051).
_PKO_POSTING_EVENTS = ("ПередВыгрузкой", "ПередЗагрузкой", "ПриЗагрузке", "ПослеЗагрузки")


def _quoted(names: list[str]) -> str:
    return ", ".join(f"«{name}»" for name in names)


def _posting_modes(events: dict[str, str], algorithms: dict[str, str]) -> tuple[str, list[str]]:
    """`sets` — хотя бы одно событие задаёт режим; иначе `opaque` с именами непрозрачных."""
    opaque: list[str] = []
    for name, text in events.items():
        if not text.strip():
            continue
        mode = _posting_mode(text, algorithms)
        if mode == "sets":
            return "sets", []
        if mode == "opaque":
            opaque.append(name)
    if opaque:
        return "opaque", opaque
    return "clear", []


def _posting_mode(text: str, algorithms: dict[str, str]) -> str:
    """`sets` — режим или `Проведен` заданы; `opaque` — не разобрать; `clear` — не заданы."""
    expanded = _expand(text, algorithms)
    code = _strip_comments(expanded.code)
    if _sets_posting(code):
        return "sets"
    if expanded.opaque or _DYNAMIC_OBJECT.search(code):
        return "opaque"
    return "clear"


def _sets_posting(code: str) -> bool:
    if _POSTED_ASSIGN.search(code):
        return True
    for match in _MODE_ASSIGN.finditer(code):
        rhs = _rhs(match.group(1))
        if rhs and rhs != '""':
            return True
    return False


def _conducted_pks(pko: Node) -> bool:
    properties = pko.child("Свойства")
    if properties is None:
        return False
    for _path, node, collection, _ancestors in _enabled_pks(properties):
        if collection or node.kind.name != "pks":
            continue
        if side_name(node, "Приемник").casefold() != "проведен":
            continue
        if str(node.get("ИмяПараметраДляПередачи")).strip():
            continue
        return True
    return False


def _postable(pko: Node, target: Structure) -> bool:
    type_name = str(pko.get("Приемник"))
    if not type_name.startswith("ДокументСсылка."):
        return False
    obj = target.get(type_name)
    return obj is not None and target.find(obj, "Проведен", "") is not None


def _predefined_search(pko: Node, algorithms: dict[str, str]) -> bool | None:
    """Дойдёт ли поиск до имени предопределённого помимо ПКЗ.

    `None` — обработчик поиска непрозрачен. При синхронизации по идентификатору без
    «продолжить поиск» поля и обработчик не вызываются (БСП:8856–8858).
    """
    sync = pko.get("СинхронизироватьПоИдентификатору") is True
    continue_search = pko.get("ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли") is True
    if sync and not continue_search:
        return False
    if _predefined_search_pks(pko):
        return True
    expanded = _expand(str(pko.get("ПоследовательностьПолейПоиска")), algorithms)
    if _PREDEFINED_MARK.search(_mask(expanded.code)):
        return True
    if expanded.opaque:
        return None
    return False


def _predefined_search_pks(pko: Node) -> bool:
    properties = pko.child("Свойства")
    if properties is None:
        return False
    for _path, node, collection, _ancestors in _enabled_pks(properties):
        if collection or node.kind.name != "pks":
            continue
        if side_name(node, "Приемник").casefold() != _PREDEFINED_NAME:
            continue
        if str(node.get("ИмяПараметраДляПередачи")).strip():
            continue
        if node.attrs.get("Поиск") is True or node.attrs.get("Обязательное") is True:
            return True
    return False


def _predefined_name_assigned(
    pko: Node, rules: ExchangeRules, algorithms: dict[str, str]
) -> bool | None:
    """Задан ли `ИмяПредопределенногоЭлемента` до записи узла «Ссылка».

    `None` — присваивание есть, но правая часть непрозрачна. `ПриВыгрузке` выполняется
    после этого блока (БСП:705, затем БСП:941) и имя в узел уже не кладёт.
    """
    text = "\n".join((str(pko.get("ПередВыгрузкой")), _event(rules, "ПередКонвертациейОбъекта")))
    expanded = _expand(text, algorithms)
    code = _strip_comments(expanded.code)
    assigned = False
    for match in _PREDEFINED_ASSIGN.finditer(code):
        rhs = _rhs(match.group(1))
        if rhs and rhs.casefold() != "неопределено":
            assigned = True
    if assigned:
        return True
    if expanded.opaque and _PREDEFINED_ASSIGN.search(code):
        return None
    return False


def _creates_predefined_copy(rules: ExchangeRules, pko: Node) -> bool | None:
    """Создаст ли приёмник новый элемент, если имени предопределённого в сообщении нет.

    `None` — не видно, уходит ли объект целиком. Ссылка с GUID не создаёт элемент
    (БСП:8736, БСП:8884–8888, БСП:9054–9056). `НеСоздаватьЕслиНеНайден` гасит создание
    и у основного объекта (БСП:8837, БСП:9058). `ПродолжитьПоиск` с обычным свойством
    поиска доходит до шага 5 раньше создания (БСП:8856 не срабатывает, БСП:9018).
    """
    if pko.get("НеСоздаватьЕслиНеНайден") is True:
        return False
    whole = _exported_as_object(rules, pko)
    if whole is None:
        return None
    sync = pko.get("СинхронизироватьПоИдентификатору") is True
    continue_search = pko.get("ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли") is True
    if sync and not whole:
        return False
    if continue_search and _ordinary_search_pks(pko):
        return False
    if sync and not continue_search:
        return True
    return whole or not sync


def _exported_as_object(rules: ExchangeRules, pko: Node) -> bool | None:
    """Уходит ли ПКО узлом `<Объект>`, а не только `<Ссылка>`.

    `None` — у ссылающейся ПКС непрозрачный обработчик с `ВыгрузитьОбъект`.
    """
    code = pko.code.strip()
    folded = code.casefold()
    for pvd in rules.pvd():
        if _disabled(pvd):
            continue
        ref = str(pvd.get("КодПравилаКонвертации")).strip()
        if ref.casefold() == folded:
            return True
        selection = str(pvd.get("ОбъектВыборки")).strip()
        if not ref and selection and selection == str(pko.get("Источник")).strip():
            return True
        choice = "\n".join(
            (
                str(pvd.get("ПередОбработкойПравила")),
                str(pvd.get("ПередВыгрузкойОбъекта")),
            )
        )
        if any(
            match.group(1).strip().casefold() == folded
            for match in _PKO_NAME_ASSIGN.finditer(choice)
        ):
            return True
    for owner in rules.pko():
        if _disabled(owner):
            continue
        properties = owner.child("Свойства")
        if properties is None:
            continue
        for node in properties.walk_all():
            if node.kind.name not in ("pks", "pks_group"):
                continue
            if str(node.get("КодПравилаКонвертации")).strip().casefold() != folded:
                continue
            handlers = "\n".join(str(node.get(name)) for name in ("ПередВыгрузкой", "ПриВыгрузке"))
            masked = _mask(_strip_comments(handlers))
            if _EXPORT_WHOLE.search(masked):
                return True
            if _EXECUTE_OPAQUE.search(masked) and re.search(
                r"(?<![\w.])ВыгрузитьОбъект\b", masked, re.IGNORECASE
            ):
                return None
    return False


def _ordinary_search_pks(pko: Node) -> bool:
    """Есть свойство поиска, по которому шаг 5 может найти элемент до создания."""
    properties = pko.child("Свойства")
    if properties is None:
        return False
    for _path, node, collection, _ancestors in _enabled_pks(properties):
        if collection or node.kind.name != "pks":
            continue
        if not _is_search_pks(node):
            continue
        name = side_name(node, "Приемник").casefold()
        if name and name != _PREDEFINED_NAME:
            return True
    return False


def _is_search_pks(node: Node) -> bool:
    return node.attrs.get("Поиск") is True or node.attrs.get("Обязательное") is True


def _filling(
    rules: ExchangeRules,
    pko: Node,
    algorithms: dict[str, str],
    parents: dict[str, list[tuple[Node, bool]]],
    *,
    on_export: bool,
) -> _Text:
    """Текст обработчиков, которые кладут ключи во входящие данные до чтения ПКС.

    `on_export` — свойство читается при выгрузке объекта, после `ПриВыгрузке` ПКО
    (БСП:941–986, затем БСП:1009). Свойства поиска читаются раньше (БСП:826).
    `ПриВыгрузке` родителя тоже раньше вызова дочернего ПКО (БСП:13208).
    """
    parts = [
        str(pko.get("ПередВыгрузкой")),
        _event(rules, "ПередКонвертациейОбъекта"),
        _event(rules, "ПередВыгрузкойОбъекта"),
    ]
    if on_export:
        parts.append(str(pko.get("ПриВыгрузке")))
    code_pko = pko.code.strip()
    source_type = str(pko.get("Источник")).strip()
    for pvd in rules.pvd():
        if _disabled(pvd):
            continue
        ref = str(pvd.get("КодПравилаКонвертации")).strip()
        selection = str(pvd.get("ОбъектВыборки")).strip()
        if ref == code_pko or (not ref and selection and selection == source_type):
            parts.append(str(pvd.get("ПередОбработкойПравила")))
            parts.append(str(pvd.get("ПередВыгрузкойОбъекта")))
    for parent, after_export in parents.get(code_pko, ()):
        parts.append(str(parent.get("ПередВыгрузкой")))
        if after_export:
            parts.append(str(parent.get("ПриВыгрузке")))
    return _combine(parts, algorithms)


def _export_calls(rules: ExchangeRules, algorithms: dict[str, str]) -> list[tuple[str, str, str]]:
    """Явные вызовы выгрузки: (код ПКО, выражение входящих данных, текст обработчика).

    Учитываются `ВыгрузитьПоПравилу` и `ВыгрузитьРегистр` (входящие — 3-й аргумент, код —
    5-й; БСП:321, БСП:2289) и `ВыгрузкаОбъектаВыборки` (4-й и 9-й; БСП:13604). Код ПКО —
    только строковый литерал: переменная не доказывает вызов именно этого правила.
    """
    found: list[tuple[str, str, str]] = []
    for text in _handler_texts(rules):
        expanded = _expand(text, algorithms)
        code = _strip_comments(expanded.code)
        # `Выполнить("ВыгрузитьПоПравилу(...)")` — код внутри строки; кавычки удвоены.
        scanned = "\n".join(
            (code, *(_unescape_string(item.group(1)) for item in _EXECUTE_STRING.finditer(code)))
        )
        for match in _EXPORT_CALL.finditer(scanned):
            arguments = _call_arguments(scanned, match.end() - 1)
            if arguments is None:
                continue
            incoming_at, name_at = _EXPORT_ARGUMENTS[match.group(1).casefold()]
            name = arguments[name_at - 1] if len(arguments) >= name_at else ""
            incoming = arguments[incoming_at - 1].strip() if len(arguments) >= incoming_at else ""
            for literal in _quoted_names(name):
                if literal:
                    found.append((literal, incoming, code))
    return found


def _pko_is_called(calls: list[tuple[str, str, str]], pko_code: str) -> bool:
    folded = pko_code.strip().casefold()
    return any(name.casefold() == folded for name, _incoming, _handler in calls)


def _incoming_from_call(calls: list[tuple[str, str, str]], pko_code: str, key: str) -> str:
    """`present` — ключ виден в тексте вызова; `opaque` — входящие переданы, ключ не виден.

    Пустой аргумент и `Неопределено` входящих данных не передают: такой вызов ключ не прячет.
    """
    folded = pko_code.strip().casefold()
    present = False
    for name, incoming, handler in calls:
        if name.casefold() != folded:
            continue
        if not incoming or incoming.casefold() == "неопределено":
            continue
        if _key_present(handler, key):
            present = True
            continue
        return "opaque"
    return "present" if present else "absent"


def _handler_texts(rules: ExchangeRules) -> Iterator[str]:
    for name in (
        "ПослеЗагрузкиПравилОбмена",
        "ПередВыгрузкойДанных",
        "ПослеВыгрузкиДанных",
        "ПередВыгрузкойОбъекта",
        "ПередКонвертациейОбъекта",
        "ПослеВыгрузкиОбъекта",
        "ПередЗагрузкойОбъекта",
        "ПослеЗагрузкиОбъекта",
        "ПослеЗагрузкиДанных",
    ):
        yield _event(rules, name)
    for pko in rules.pko():
        if _disabled(pko):
            continue
        for name in (
            "ПередВыгрузкой",
            "ПриВыгрузке",
            "ПослеВыгрузки",
            "ПослеВыгрузкиВФайл",
            "ПоследовательностьПолейПоиска",
        ):
            yield str(pko.get(name))
        properties = pko.child("Свойства")
        if properties is None:
            continue
        for node in properties.walk_all():
            if node.kind.name == "pks":
                names = ("ПередВыгрузкой", "ПриВыгрузке", "ПослеВыгрузки")
            elif node.kind.name == "pks_group":
                names = (
                    "ПередОбработкойВыгрузки",
                    "ПередВыгрузкой",
                    "ПриВыгрузке",
                    "ПослеВыгрузки",
                    "ПослеОбработкиВыгрузки",
                )
            else:
                continue
            for name in names:
                yield str(node.get(name))
    for pvd in rules.pvd():
        if _disabled(pvd):
            continue
        for name in (
            "ПередОбработкойПравила",
            "ПередВыгрузкойОбъекта",
            "ПослеВыгрузкиОбъекта",
            "ПослеОбработкиПравила",
        ):
            yield str(pvd.get(name))


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


def _quoted_names(expression: str) -> list[str]:
    """Строковые литералы выражения: код ПКО бывает внутри `?(..., "А", "Б")`."""
    return [_unescape_string(item.group(0)) for item in _STRING.finditer(expression)]


def _unescape_string(literal: str) -> str:
    text = literal.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text.replace('""', '"')


def _parent_index(rules: ExchangeRules) -> dict[str, list[tuple[Node, bool]]]:
    """ПКО, чья ПКС ссылается на код: их «ИсходящиеДанные» станут входящими дочернего ПКО.

    Второй элемент — можно ли читать `ПриВыгрузке` родителя. Свойство поиска выгружается
    до него (БСП:826, `ПриВыгрузке` — БСП:941), поэтому ссылка из такого свойства родителя
    `ПриВыгрузке` не видит.
    """
    index: dict[str, list[tuple[Node, bool]]] = {}
    for pko in rules.pko():
        if _disabled(pko):
            continue
        properties = pko.child("Свойства")
        if properties is None:
            continue
        own = pko.code.strip()
        # Свойство поиска верхнего уровня уходит в узел «Ссылка» до `ПриВыгрузке` (БСП:826).
        # Внутри группы «Поиск» в этот список не попадает.
        direct = {id(item) for item in properties.items}
        seen: dict[str, bool] = {}
        for node in properties.walk_all():
            if node.kind.name not in ("pks", "pks_group"):
                continue
            code = str(node.get("КодПравилаКонвертации")).strip()
            if not code or code == own:
                continue
            search_before = id(node) in direct and _is_search_pks(node)
            after_export = seen.get(code, True) and not search_before
            seen[code] = after_export
        for code, after_export in seen.items():
            index.setdefault(code, []).append((pko, after_export))
    return index


def _combine(parts: list[str], algorithms: dict[str, str]) -> _Text:
    chunks: list[str] = []
    opaque = False
    for part in parts:
        expanded = _expand(part, algorithms)
        chunks.append(_strip_comments(expanded.code))
        opaque = opaque or expanded.opaque
    code = "\n".join(chunks)
    return _Text(code, opaque or _incoming_opaque(code))


def _own_handler(node: Node) -> str:
    if node.kind.name == "pks_group":
        return str(node.get("ПередОбработкойВыгрузки"))
    return str(node.get("ПередВыгрузкой"))


def _assigns_value(code: str, node: Node) -> bool:
    """Чтение входящих не выполняется: значение, отказ, пусто или выражение заданы до него.

    ПКС: `Значение` (БСП:12071), `Отказ` (БСП:12958), `Пусто` и `Выражение` (БСП:13019–13029).
    Группа: `КоллекцияОбъектов` (БСП:11715) и `Отказ` (БСП:11591).
    """
    if _BYPASS_TRUE.search(code):
        return True
    pattern = _COLLECTION_ASSIGN if node.kind.name == "pks_group" else _VALUE_ASSIGN
    for match in pattern.finditer(code):
        rhs = _rhs(match.group(1))
        if rhs and rhs != "Неопределено":
            return True
    if node.kind.name == "pks":
        for match in _EXPRESSION_ASSIGN.finditer(code):
            rhs = _rhs(match.group(1))
            if rhs and rhs != "Неопределено":
                return True
    return False


def _incoming_key(node: Node) -> str:
    target = side_name(node, "Приемник").strip()
    if target:
        return target
    if node.kind.name == "pks":
        return str(node.get("ИмяПараметраДляПередачи")).strip()
    return ""


def _key_present(code: str, key: str) -> bool:
    folded = key.casefold()
    if re.search(
        rf"(?<![\w.])(?:ВходящиеДанные|ИсходящиеДанные)\s*\.\s*{re.escape(key)}\b",
        code,
        re.IGNORECASE,
    ):
        return True
    for literal in _literals(code):
        if literal.casefold() == folded:
            return True
        if any(part.strip().casefold() == folded for part in literal.split(",")):
            return True
    return False


def _incoming_opaque(code: str) -> bool:
    if not _INCOMING_VAR.search(code):
        return False
    if _DYNAMIC_INSERT.search(code) or _DYNAMIC_INDEX.search(code):
        return True
    for match in _INCOMING_ASSIGN.finditer(code):
        rhs = _rhs(match.group(1))
        if rhs and rhs != "Неопределено" and not _STRUCTURE_NEW.match(rhs):
            return True
    return False


def _enabled_pks(
    container: Node, prefix: str = "", collection: bool = False, ancestors: str = ""
) -> Iterator[tuple[str, Node, bool, str]]:
    for segment, item in zip(pks_segments(container), container.items, strict=True):
        if _disabled(item):
            continue
        path = f"{prefix}{segment}"
        nested = collection or _is_collection(item)
        yield path, item, nested, ancestors
        if item.is_group:
            extra = "\n".join(str(item.get(name)) for name in _GROUP_FILLERS)
            yield from _enabled_pks(item, f"{path}/", nested, f"{ancestors}\n{extra}")


def _is_collection(node: Node) -> bool:
    child = node.child("Приемник")
    kind = str(child.attrs.get("Вид", "")) if child is not None else ""
    return (
        kind == "ТабличнаяЧасть"
        or kind == "НаборЗаписейПоследовательности"
        or kind.startswith("НаборДвижений")
    )


def _pkz_sources(pko: Node) -> set[str]:
    values = pko.child("Значения")
    if values is None:
        return set()
    return {str(item.get("Источник")) for item in values.walk() if str(item.get("Источник"))}


def _predefined(structure: Structure, object_id: int) -> set[str]:
    rows = structure.connection.execute(
        "SELECT name FROM object_values WHERE object_id = ? AND predefined = 1",
        (object_id,),
    )
    return {name for (name,) in rows}


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


def _same_configuration(source: Structure, target: Structure) -> bool:
    """Одно и то же имя конфигурации с обеих сторон (как сверка заголовка, без «БАЗОВАЯ»)."""
    left = _config_key(_meta(source.connection, "config_name"), strip_basic=True)
    right = _config_key(_meta(target.connection, "config_name"), strip_basic=True)
    return bool(left) and left == right


def _meta(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else ""


def _config_key(name: str, *, strip_basic: bool) -> str:
    """Имя для сравнения как у формы загрузки: без регистра; у базы ещё без «БАЗОВАЯ»."""
    folded = name.strip().casefold()
    if strip_basic:
        folded = folded.replace("базовая", "")
    return folded


def _three(version: str) -> tuple[int, int, int] | None:
    """Первые три числа версии. Пустая строка — `0.0.0`, как подставляет сравнение формы."""
    raw = version.strip()
    if not raw:
        return (0, 0, 0)
    parts = raw.split(".")
    if len(parts) < 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _shown_version(version: str) -> str:
    parsed = _three(version)
    if parsed is None:
        return version.strip() or "пусто"
    return ".".join(str(part) for part in parsed)


def _sample(names: list[str]) -> str:
    shown = ", ".join(names[:_SHOWN])
    rest = len(names) - _SHOWN
    if rest > 0:
        shown += f" и ещё {rest}"
    return shown


def _rhs(raw: str) -> str:
    return raw.strip().rstrip(";").strip()


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
