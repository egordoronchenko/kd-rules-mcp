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
- `handlers.export_cache_without_key` — обратное: `ПередВыгрузкой` ставит
  `ЗапоминатьВыгруженные = Истина` и не задаёт ключ. До обработчика ключ — имя ПКО
  (БСП:424); внутреннее представление подставляется только если локальный флаг уже
  истина (БСП:469–471), а он истина лишь при ссылке на себя и флаге ПКО (БСП:398).
  Флаг ПКО по умолчанию включён (БСП:5745), но обычную выгрузку это не запоминает:
  присваивание в обработчике включает кэш с ключом имени ПКО (БСП:607–633).
  На одном обработчике с `handlers.export_key` не совпадает.
- `handlers.ignored_modified_flag` — `ОбъектМодифицирован = Ложь` в `ПриЗагрузке` /
  `ПослеЗагрузки`: флаг ставится в Истина и нигде не читается (БСП:10624, БСП:10953),
  объект записывается всегда (БСП:11119, БСП:11228).
- `handlers.table_no_clear` — `НеОчищать` у группы табличной части: атрибут читается
  (БСП:10897), но передаётся только загрузке движений (БСП:10922–10924).
- `handlers.pvd_refusal` — `Отказ = Истина` в `ПередОбработкой` ПВД, и объект выборки
  входит в состав плана обмена правил: после обработчика отказ не проверяется
  (БСП:18369–18404). Проверка — в `ПередВыгрузкой` (БСП:13618–13628). Объекта в составе
  нет — замечания нет. Состава не видно — пропуск.
- `structure.repeated_table_target` — несколько групп одной табличной части: каждая
  загрузка замещает строки (БСП:9200–9264).
- `structure.ambiguous_default_pko` — несколько ПКО одного источника и ссылка без имени
  правила: берётся последнее загруженное (БСП:5837–5845, БСП:6977–6997, БСП:13131–13164).
- `structure.multiple_pvd_same_type` — несколько включённых ПВД одного объекта выборки,
  и этот объект входит в состав плана: обмен через план берёт первое
  (БСП:6768, БСП:17931, БСП:18361–18367). Вне состава замечания нет.
- `handlers.attached_processing` — обращение к `ДопОбработки.<Имя>` или
  `ДопОбработки["<Имя>"]`. Переменная — пустая структура (БСП:39, БСП:14665);
  `ЗагрузитьОбработки` её очищает (БСП:6735), `ЗагрузитьОбработку` читает хранилище,
  параметры и описание (БСП:6688–6722) и в `ДопОбработки` экземпляр не помещает.
  Имени нет в разделе «Обработки» — то же: загрузчик не добавляет ни одного ключа.
  Собственное присваивание этого имени в любом включённом тексте правил гасит замечание везде.
- `format.source_name`, `format.source_version` — `<Источник>` заголовка против структуры
  источника: имя без учёта регистра, из имени базы вырезано «БАЗОВАЯ» (`РПО:43–44`);
  версия — первые три числа (`РПО:64–69`, разбор — `ОбщегоНазначенияКлиентСервер:1030–1043`).
  `<Приемник>` форма не сверяет (`РПО:961–967`).

Не добавлено: одно `НеЗапоминатьВыгруженные`. Флаг читается (БСП:5806–5808) и выключает
запоминание при ссылке объекта на себя (БСП:398) — в обмене через план эта часть исполняется.
"""

import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from kd2_rules_mcp.kd2.model import ExchangeRules, Node, rule_code
from kd2_rules_mcp.structures.queries import exchange_plan_autoregistration
from kd2_rules_mcp.validation.address import (
    CONVERSION_ADDRESS,
    pks_address,
    pks_segments,
    rule_address,
    side_name,
    walk_pks,
)
from kd2_rules_mcp.validation.handlers import EVENT_AREAS
from kd2_rules_mcp.validation.report import ValidationReport
from kd2_rules_mcp.validation.structure import (
    REF_ONLY_LOAD_NOTE,
    Structure,
    is_ref,
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
EXPORT_CACHE = "handlers.export_cache_without_key"
ATTACHED = "handlers.attached_processing"
MODIFIED_FLAG = "handlers.ignored_modified_flag"
TABLE_NO_CLEAR = "handlers.table_no_clear"
PVD_REFUSAL = "handlers.pvd_refusal"
REPEATED_TABLE = "structure.repeated_table_target"
AMBIGUOUS_PKO = "structure.ambiguous_default_pko"
MULTIPLE_PVD = "structure.multiple_pvd_same_type"
SOURCE_NAME = "format.source_name"
SOURCE_VERSION = "format.source_version"

_MANUAL = (
    "Правило рассчитано на ручной обмен универсальной обработкой, "
    "в обмене через план обмена эта часть не исполняется"
)
# Нет структуры источника или плана в ней: не видно, дойдёт ли объект до ПВД.
_PLAN_UNKNOWN = "состав плана обмена неизвестен: не видно, регистрируется ли объект"
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
# Штатный вызов алгоритма: тело подставляется, само `Выполнить` код не прячет.
_ALGORITHM_EXECUTE = re.compile(
    r"(?<![\w.])Выполнить\s*\(\s*Алгоритмы\s*\.\s*([^\W\d]\w*)\b\s*\)",
    re.IGNORECASE,
)
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
# Граница оператора: присваивание, а не сравнение в `Если Отказ = Истина`.
_ASSIGN_BEFORE = frozenset(
    {"", ";", "тогда", "иначе", "цикл", "конецесли", "конеццикла", "конецпопытки"}
)
_LOAD_FLAG_EVENTS = ("ПриЗагрузке", "ПослеЗагрузки")
_PKS_NAME_EVENTS = ("ПередВыгрузкой", "ПриВыгрузке")
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
    plan_name: str | None = None,
    plan_hints: Sequence[str] = (),
) -> ValidationReport:
    """Проверки обмена через план обмена. Без нужной структуры — запись в `skipped`.

    `plan_name` — имя плана этих правил (каталог `ExchangePlans/<Имя>`). Состав
    ищется в структуре источника так же, как у `registration.plan_membership`.
    `plan_hints` — каталоги пути файла правил: когда имя не задано, план называет
    каталог, равный имени ровно одного плана обмена структуры (папка живых правил).
    """
    _REACH_CACHE.clear()
    _NAME_CACHE.clear()
    report = ValidationReport()
    source_structure = Structure.load(source) if source is not None else None
    target_structure = Structure.load(target) if target is not None else None
    algorithms = _algorithms(rules)
    plan_content = _rules_plan_content(source, plan_name, plan_hints)
    _check_header(report, rules, source)
    _check_values(report, rules, source_structure, target_structure, algorithms)
    _check_posting(report, rules, target_structure, algorithms)
    _check_object_write(report, rules)
    _check_modified_flag(report, rules, algorithms)
    _check_table_clear(report, rules, target_structure, algorithms)
    _check_repeated_table(report, rules, target_structure)
    _check_ambiguous_pko(report, rules, source_structure, algorithms)
    _check_multiple_pvd(report, rules, plan_content)
    _check_pvd(report, rules, algorithms)
    _check_pvd_refusal(report, rules, algorithms, plan_content)
    _check_export_key(report, rules, algorithms)
    _check_attached(report, rules)
    _check_incoming(report, rules, algorithms)
    return report


def plan_name_from_path(path: Path | str | None) -> str | None:
    """Имя плана обмена, если файл правил лежит в каталоге `ExchangePlans/<Имя>`."""
    if not path:
        return None
    parts = Path(path).parts
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "exchangeplans":
            return parts[index + 1] or None
    return None


def _rules_plan_content(
    source: sqlite3.Connection | None,
    plan_name: str | None,
    plan_hints: Sequence[str] = (),
) -> dict[str, bool] | None:
    """Состав плана правил: тип элемента → авторегистрация. Нет плана — `None`.

    Имя задано — ищется `ПланОбмена.<Имя>` (`registration.plan_membership`,
    `exchange_plan_autoregistration`). Имя не задано и в структуре ровно один
    план обмена — берётся он. Несколько планов без имени или план не найден —
    состав неизвестен.
    """
    if source is None:
        return None
    name = (plan_name or "").strip()
    if not name:
        names = _exchange_plan_names(source)
        if len(names) != 1:
            # Живые правила лежат в папке с именем плана (`…/ПравилаОбмена/<план>/`): каталог
            # пути, равный имени ровно одного плана структуры, называет план.
            named = [item for item in names if item in plan_hints]
            if len(named) != 1:
                return None
            names = named
        name = names[0]
    if not _exchange_plan_exists(source, name):
        return None
    factory = source.row_factory
    try:
        return exchange_plan_autoregistration(source, name)
    finally:
        source.row_factory = factory


def _exchange_plan_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM objects WHERE kind = 'ПланОбмена' AND is_group = 0 ORDER BY name"
    )
    return [str(row[0]) for row in rows]


def _exchange_plan_exists(connection: sqlite3.Connection, name: str) -> bool:
    """План `ПланОбмена.<Имя>` есть в структуре. Имя с точкой — уже полное."""
    lookup = name if "." in name else f"ПланОбмена.{name}"
    row = connection.execute(
        "SELECT kind FROM objects WHERE is_group = 0 AND kind || '.' || name = ? LIMIT 1",
        (lookup,),
    ).fetchone()
    return row is not None and str(row[0]) == "ПланОбмена"


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


_MODIFIED_TEXT = (
    "Присваивание «ОбъектМодифицирован = Ложь» в «{event}» не отменяет запись: "
    "исполнитель ставит флаг перед обработчиком и нигде его не читает, объект "
    "записывается всегда. Рецепт относится к универсальной обработке обмена, "
    "а не к обмену через план"
)


def _check_modified_flag(
    report: ValidationReport, rules: ExchangeRules, algorithms: dict[str, str]
) -> None:
    """`ОбъектМодифицирован = Ложь` в загрузке ПКО не отменяет штатную запись.

    `ПрочитатьОбъект` ставит переменную в Истина перед `ПриЗагрузке` (БСП:10624)
    и перед `ПослеЗагрузки` (БСП:10953) и дальше её не читает: документ пишется
    в БСП:11119, прочие объекты — в БСП:11228. Поле структуры и текст в строке
    или комментарии не считаются.
    """
    for pko in rules.pko():
        if _disabled(pko):
            continue
        events = [name for name in _LOAD_FLAG_EVENTS if str(pko.get(name)).strip()]
        if not events:
            continue
        found: list[str] = []
        opaque = False
        for name in events:
            state = _scan_assignment(
                str(pko.get(name)), algorithms, "ОбъектМодифицирован", frozenset({"ложь"})
            )
            if state == "hit":
                found.append(name)
            elif state == "opaque":
                opaque = True
        if found:
            report.warning(
                MODIFIED_FLAG,
                rule_address(pko),
                _MODIFIED_TEXT.format(event="», «".join(found)),
            )
            continue
        if opaque:
            report.skip(
                MODIFIED_FLAG,
                f"{rule_address(pko)}: обработчик загрузки непрозрачен — не видно, "
                "присваивается ли «ОбъектМодифицирован = Ложь»",
            )


def _check_table_clear(
    report: ValidationReport,
    rules: ExchangeRules,
    target: Structure | None,
    algorithms: dict[str, str],
) -> None:
    """`НеОчищать` у группы табличной части загрузчик ТЧ не получает.

    Атрибут читается (БСП:10897) и передаётся только `ЗагрузитьДвижения`
    (БСП:10922–10924). `ЗагрузитьТабличнуюЧасть` его не видит (БСП:10913–10918).
    """
    for pko in rules.pko():
        if _disabled(pko):
            continue
        properties = pko.child("Свойства")
        if properties is None:
            continue
        for path, group in _object_groups(properties, pko, target):
            handler = str(group.get("ПередОбработкойВыгрузки"))
            if not handler.strip():
                continue
            kind = _collection_class(group, pko, target)
            address = pks_address(pko.code, path)
            if kind is None:
                if _may_assign_clear(handler, algorithms):
                    report.skip(
                        TABLE_NO_CLEAR,
                        f"{address}: вид группы не определён — не видно, "
                        "табличная часть это или набор записей",
                    )
                continue
            if kind != "table":
                continue
            state = _scan_assignment(handler, algorithms, "НеОчищать", frozenset({"истина", "1"}))
            if state == "hit":
                name = side_name(group, "Приемник") or side_name(group, "Источник")
                report.warning(
                    TABLE_NO_CLEAR,
                    address,
                    f"Группа выставляет «НеОчищать» для табличной части «{name}». "
                    "Атрибут читается, но передаётся только загрузке движений; "
                    "загрузка табличной части его не получает, прежние строки этим "
                    "не сохраняются",
                )
                continue
            if state == "opaque":
                report.skip(
                    TABLE_NO_CLEAR,
                    f"{address}: «ПередОбработкойВыгрузки» непрозрачен — не видно, "
                    "выставляет ли обработчик «НеОчищать»",
                )


def _may_assign_clear(handler: str, algorithms: dict[str, str]) -> bool:
    return _scan_assignment(handler, algorithms, "НеОчищать", frozenset({"истина", "1"})) != "none"


def _check_repeated_table(
    report: ValidationReport, rules: ExchangeRules, target: Structure | None
) -> None:
    """Несколько групп одной табличной части замещают строки, а не складывают их.

    `ЗагрузитьТабличнуюЧасть` собирает результат из текущего фрагмента и загружает
    его в табличную часть (БСП:9200–9264). Для движений действует `НеОчищать`,
    поэтому наборы записей здесь не проверяются.
    """
    for pko in rules.pko():
        if _disabled(pko):
            continue
        properties = pko.child("Свойства")
        if properties is None:
            continue
        grouped: dict[str, list[str]] = {}
        unknown: dict[str, int] = {}
        for path, group in _object_groups(properties, pko, target):
            name = side_name(group, "Приемник")
            kind = _collection_class(group, pko, target)
            if kind is None:
                if name:
                    unknown[name] = unknown.get(name, 0) + 1
                continue
            if kind != "table" or not name:
                continue
            grouped.setdefault(name, []).append(pks_address(pko.code, path))
        for name, addresses in grouped.items():
            if len(addresses) < 2:
                continue
            shown = " и ".join(addresses) if len(addresses) == 2 else ", ".join(addresses)
            report.warning(
                REPEATED_TABLE,
                rule_address(pko),
                f"Группы {shown} пишут табличную часть «{name}». Каждая загружается "
                "отдельно и замещает строки. Объедините строки до выгрузки или "
                "подтвердите, что группы взаимоисключающие",
            )
        for name, count in unknown.items():
            if count < 2 or name in grouped:
                continue
            report.skip(
                REPEATED_TABLE,
                f"{rule_address(pko)}: несколько групп «{name}», а вид не определён — "
                "не видно, повторяется ли табличная часть приёмника",
            )


def _check_ambiguous_pko(
    report: ValidationReport,
    rules: ExchangeRules,
    source: Structure | None,
    algorithms: dict[str, str],
) -> None:
    """Неявный выбор ПКО — последнее загруженное правило этого типа источника.

    `ЗагрузитьПравилоКонвертации` перезаписывает ПКО менеджера типа (БСП:5837–5845),
    восстановление кэша повторяет присваивание (БСП:7081–7085). `НайтиПравило` без
    имени берёт `Менеджеры[ТипЗнч].ПКО` (БСП:6977–6997). Так же свойство без
    `КодПравилаКонвертации` (БСП:12908, БСП:13131–13164) и ПВД с пустым кодом
    (БСП:13451, БСП:13633, БСП:13311–13316). Вызов без имени для вида субконто —
    `НайтиПравило(ВидСубконто)` при пустом `ИмяПКОВидСубконто` (БСП:12303–12305):
    имя задаёт обработчик, а не код правила, поэтому здесь не проверяется.
    Код группы ПКС читатель загружает (БСП:5353–5354) и при выгрузке не читает.
    """
    by_type: dict[str, list[Node]] = {}
    for pko in rules.pko():
        if _disabled(pko):
            continue
        source_type = str(pko.get("Источник")).strip()
        if source_type:
            by_type.setdefault(source_type, []).append(pko)
    ambiguous = {name: nodes for name, nodes in by_type.items() if len(nodes) > 1}
    if not ambiguous:
        return
    refs: dict[str, list[str]] = {name: [] for name in ambiguous}
    opaque_types: set[str] = set()
    untyped = False
    for pko in rules.pko():
        if _disabled(pko):
            continue
        properties = pko.child("Свойства")
        if properties is None:
            continue
        untyped = _implicit_properties(
            properties,
            pko,
            [],
            "",
            source,
            algorithms,
            ambiguous,
            refs,
            opaque_types,
            untyped=untyped,
        )
    global_choice = _name_choice(_event(rules, "ПередВыгрузкойОбъекта"), algorithms)
    for pvd in rules.pvd():
        if _disabled(pvd):
            continue
        selection = str(pvd.get("ОбъектВыборки")).strip()
        if selection not in ambiguous or _conversion_code(pvd):
            continue
        choice = _merge_name(
            global_choice, _name_choice(str(pvd.get("ПередВыгрузкойОбъекта")), algorithms)
        )
        if choice == "explicit":
            continue
        if choice == "opaque":
            opaque_types.add(selection)
            continue
        refs[selection].append(rule_address(pvd))
    pending_untyped = False
    for source_type, nodes in ambiguous.items():
        found = refs[source_type]
        codes = ", ".join(f"«{rule_code(node.code)}»" for node in nodes)
        if found:
            shown = ", ".join(found[:_SHOWN])
            if len(found) > _SHOWN:
                shown += f" и ещё {len(found) - _SHOWN}"
            report.warning(
                AMBIGUOUS_PKO,
                rule_address(nodes[-1]),
                f"Для источника «{source_type}» включены ПКО {codes}. Неявный выбор — "
                "последнее загруженное ПКО этого типа. Ссылка без кода правила: "
                f"{shown}",
            )
            continue
        if source_type in opaque_types:
            report.skip(
                AMBIGUOUS_PKO,
                f"Для источника «{source_type}» несколько ПКО, а ссылка без кода правила "
                "непрозрачна — не видно, задаёт ли обработчик имя ПКО",
            )
            continue
        pending_untyped = untyped
    if pending_untyped:
        report.skip(
            AMBIGUOUS_PKO,
            "у части свойств без кода правила тип не виден — неявный выбор ПКО не проверен",
        )


def _implicit_properties(
    container: Node,
    pko: Node,
    ancestors: list[Node],
    prefix: str,
    source: Structure | None,
    algorithms: dict[str, str],
    ambiguous: dict[str, list[Node]],
    refs: dict[str, list[str]],
    opaque_types: set[str],
    *,
    untyped: bool,
) -> bool:
    segments = pks_segments(container)
    for segment, item in zip(segments, container.items, strict=True):
        if _disabled(item):
            continue
        path = f"{prefix}{segment}"
        # Код группы при выгрузке не читается. Свойством без имени правила считается
        # ПКС; у группы — только если в правилах явно указан ссылочный тип источника.
        if item.is_group:
            _implicit_node(
                item,
                pko,
                path,
                ancestors,
                source,
                algorithms,
                ambiguous,
                refs,
                opaque_types,
                allow_untyped=False,
            )
            untyped = _implicit_properties(
                item,
                pko,
                [*ancestors, item],
                f"{path}/",
                source,
                algorithms,
                ambiguous,
                refs,
                opaque_types,
                untyped=untyped,
            )
            continue
        if _implicit_node(
            item,
            pko,
            path,
            ancestors,
            source,
            algorithms,
            ambiguous,
            refs,
            opaque_types,
            allow_untyped=True,
        ):
            untyped = True
    return untyped


def _implicit_node(
    node: Node,
    pko: Node,
    path: str,
    ancestors: list[Node],
    source: Structure | None,
    algorithms: dict[str, str],
    ambiguous: dict[str, list[Node]],
    refs: dict[str, list[str]],
    opaque_types: set[str],
    *,
    allow_untyped: bool,
) -> bool:
    """Возвращает истину, если тип свойства не виден и неявный выбор не доказан."""
    if _conversion_code(node):
        return False
    handlers = "\n".join(str(node.get(name)) for name in _PKS_NAME_EVENTS)
    choice = _name_choice(handlers, algorithms)
    if choice == "explicit":
        return False
    types = _property_source_types(node, pko, ancestors, source)
    if types is None:
        return allow_untyped and choice == "none"
    matched = [name for name in types if name in ambiguous and is_ref(name)]
    if choice == "opaque":
        opaque_types.update(matched)
        return False
    address = pks_address(pko.code, path)
    for name in matched:
        refs[name].append(address)
    return False


def _check_multiple_pvd(
    report: ValidationReport, rules: ExchangeRules, plan_content: dict[str, bool] | None
) -> None:
    """Обмен через план берёт одно ПВД на объект метаданных — первое включённое.

    В таблицу попадают только правила с `Включить` (БСП:6768, БСП:17931).
    `Найти` возвращает первую строку (БСП:18361–18367). Объект вне состава плана
    не регистрируется и до этой выборки не доходит — замечания нет.
    """
    grouped: dict[str, list[Node]] = {}
    for pvd in rules.pvd():
        if _disabled(pvd):
            continue
        selection = str(pvd.get("ОбъектВыборки")).strip()
        if selection:
            grouped.setdefault(selection, []).append(pvd)
    for selection, nodes in grouped.items():
        if len(nodes) < 2:
            continue
        if plan_content is None:
            report.skip(MULTIPLE_PVD, f"{rule_address(nodes[0])}: {_PLAN_UNKNOWN}")
            continue
        if selection not in plan_content:
            continue
        codes = ", ".join(f"«{rule_code(node.code)}»" for node in nodes)
        report.warning(
            MULTIPLE_PVD,
            rule_address(nodes[0]),
            f"Для «{selection}» включены ПВД {codes}. При обмене через план берётся "
            "первое; остальные ПВД этого типа не работают, их обработчики не выполняются",
        )


def _check_pvd_refusal(
    report: ValidationReport,
    rules: ExchangeRules,
    algorithms: dict[str, str],
    plan_content: dict[str, bool] | None,
) -> None:
    """`Отказ` в `ПередОбработкой` ПВД выборка изменений не проверяет.

    Обработчик выполняется (БСП:18369–18404), и дальше `Отказ` не читается.
    Отказ от объекта проверяет `ВыгрузкаОбъектаВыборки` после `ПередВыгрузкой`
    (БСП:13618–13628). Замечание — только если объект выборки входит в состав
    плана и может быть зарегистрирован. `ВыборкаДанных` в том же обработчике —
    отдельное замечание.
    """
    for pvd in rules.pvd():
        if _disabled(pvd):
            continue
        handler = str(pvd.get("ПередОбработкойПравила"))
        if not handler.strip():
            continue
        state = _scan_assignment(handler, algorithms, "Отказ", frozenset({"истина"}))
        if state == "hit":
            selection = str(pvd.get("ОбъектВыборки")).strip()
            if plan_content is None:
                report.skip(PVD_REFUSAL, f"{rule_address(pvd)}: {_PLAN_UNKNOWN}")
                continue
            if selection not in plan_content:
                continue
            report.warning(
                PVD_REFUSAL,
                rule_address(pvd),
                f"«Отказ» в «ПередОбработкой» ПВД «{pvd.code}» при обмене через план "
                f"не проверяется: зарегистрированные объекты «{selection}» будут выгружены. "
                "Чтобы объект не уходил — правило регистрации или отказ в «ПередВыгрузкой» ПВД.",
            )
            continue
        if state == "opaque":
            report.skip(
                PVD_REFUSAL,
                f"{rule_address(pvd)}: «ПередОбработкой» непрозрачен — не видно, "
                "присваивается ли «Отказ = Истина»",
            )


def _check_export_key(
    report: ValidationReport, rules: ExchangeRules, algorithms: dict[str, str]
) -> None:
    for pko in rules.pko():
        if _disabled(pko):
            continue
        expanded = _expand(str(pko.get("ПередВыгрузкой")), algorithms)
        masked = _mask(expanded.code)
        has_key = _EXPORT_KEY_ASSIGN.search(masked) is not None
        remembers = _REMEMBER_TRUE.search(masked) is not None
        if has_key and not remembers:
            if expanded.opaque:
                report.skip(
                    EXPORT_KEY,
                    f"{rule_address(pko)}: «ПередВыгрузкой» непрозрачен — не видно, "
                    "включает ли обработчик запоминание выгруженных",
                )
            else:
                report.warning(
                    EXPORT_KEY,
                    rule_address(pko),
                    f"{_MANUAL}: «КлючВыгружаемыхДанных» без запоминания выгруженных",
                )
                continue
        _check_export_cache(report, pko, expanded, algorithms)


def _check_export_cache(
    report: ValidationReport, pko: Node, expanded: _Text, algorithms: dict[str, str]
) -> None:
    """Кэш включён присваиванием, а ключ остаётся именем ПКО.

    Флаг ПКО сам по себе обычную выгрузку не запоминает: локальная переменная
    до обработчика истинна только при ссылке объекта на себя (БСП:398). Ключ
    к этому моменту — имя ПКО (БСП:424), внутреннее представление пишется раньше
    обработчика и только при уже истинном флаге (БСП:469–471).
    """
    cleaned = _strip_comments(expanded.code)
    state = _export_cache_state(cleaned)
    address = rule_address(pko)
    if state == "off" or state == "keyed":
        return
    if state == "literal" or _text_opaque(expanded, algorithms):
        report.skip(
            EXPORT_CACHE,
            f"{address}: «ПередВыгрузкой» непрозрачен — не видно, задаёт ли обработчик "
            "«КлючВыгружаемыхДанных» при включённом запоминании",
        )
        return
    report.warning(
        EXPORT_CACHE,
        address,
        "«ПередВыгрузкой» включает запоминание выгруженных и не задаёт "
        "«КлючВыгружаемыхДанных». В обычном входе ключ остаётся именем ПКО, "
        "поэтому разные объекты принимаются за один",
    )


def _export_cache_state(code: str) -> str:
    """Состояние кэша в обработчике.

    `on` — `= Истина` без ключа; `keyed` — ключ задан;
    `literal` — правая часть не булев литерал.
    """
    on = False
    unknown = False
    for raw in _assign_rhs(code, "ЗапоминатьВыгруженные"):
        token = raw.strip().rstrip(";").strip().casefold()
        if token == "истина":
            on = True
        elif token != "ложь":
            unknown = True
    if _assign_rhs(code, "КлючВыгружаемыхДанных"):
        return "keyed"
    if on:
        return "on"
    if unknown:
        return "literal"
    return "off"


_STRUCTURE_METHODS = frozenset({"вставить", "свойство", "очистить", "количество", "удалить"})
# Признак «структура присвоена целиком» среди имён с собственным присваиванием.
_ATTACHED_WHOLE = "*"


def _check_attached(report: ValidationReport, rules: ExchangeRules) -> None:
    """Ссылка на вложенную обработку: загрузчик БСП экземпляр в `ДопОбработки` не кладёт.

    Экземпляр обычно создают один раз — в событии конвертации или алгоритме, — а читают в
    других обработчиках. Поэтому собственное присваивание имени в любом включённом тексте
    правил гасит замечание везде, а присваивание `ДопОбработки` целиком — все замечания.
    """
    known = _processor_names(rules)
    scanned: list[tuple[str, str, list[str], str]] = []
    defined: set[str] = set()
    for address, event, text in _iter_handler_code(rules):
        if "допобработки" not in text.casefold():
            continue
        referenced, own, opaque = _attached_references(_strip_comments(text))
        defined |= own
        scanned.append((address, event, referenced, opaque))
    if _ATTACHED_WHOLE in defined:
        for address, event, _referenced, opaque in scanned:
            if opaque:
                report.skip(ATTACHED, f"{address}: «{event}» {opaque}")
        return
    for address, event, referenced, opaque in scanned:
        names = [name for name in referenced if name.casefold() not in defined]
        described = [known[name.casefold()] for name in names if name.casefold() in known]
        missing = [name for name in names if name.casefold() not in known]
        if described:
            quoted = ", ".join(f"«{name}»" for name in described)
            report.warning(
                ATTACHED,
                address,
                f"В «{event}» обращение к вложенной обработке {quoted}: загрузчик сохраняет "
                "описание, но не создаёт экземпляр в «ДопОбработки»",
            )
        if missing:
            quoted = ", ".join(f"«{name}»" for name in missing)
            report.warning(
                ATTACHED,
                address,
                f"В «{event}» обращение к «ДопОбработки» {quoted}: обработка не описана "
                "в правилах, загрузчик не помещает в «ДопОбработки» ни одного экземпляра",
            )
        if opaque and not names:
            report.skip(ATTACHED, f"{address}: «{event}» {opaque}")
        elif opaque:
            report.skip(
                ATTACHED,
                f"{address}: «{event}» задаёт имя вложенной обработки кодом — это имя не сверяется",
            )


def _processor_names(rules: ExchangeRules) -> dict[str, str]:
    """Имена раздела «Обработки»: свёртка → написание в правилах."""
    root = rules.root.child("Обработки")
    found: dict[str, str] = {}
    if root is None:
        return found
    for item in root.items:
        if item.kind.name != "data_processor":
            continue
        name = str(item.attrs.get("Имя", "")).strip()
        if name:
            found.setdefault(name.casefold(), name)
    return found


def _iter_handler_code(rules: ExchangeRules) -> Iterator[tuple[str, str, str]]:
    """Включённые обработчики и тексты алгоритмов: адрес, событие, текст."""
    for tag in _event_tags("exchange_rules"):
        text = _event(rules, tag)
        if text.strip():
            yield CONVERSION_ADDRESS, tag, text
    for pko in rules.pko():
        if _disabled(pko):
            continue
        address = rule_address(pko)
        for tag in _event_tags("pko"):
            text = str(pko.get(tag))
            if text.strip():
                yield address, tag, text
        properties = pko.child("Свойства")
        if properties is not None:
            yield from _pks_handler_code(pko.code, properties)
    for pvd in rules.pvd():
        if _disabled(pvd):
            continue
        yield from _node_handlers(pvd, "pvd")
    for pod in rules.pod():
        if _disabled(pod):
            continue
        yield from _node_handlers(pod, "pod")
    parameters = rules.root.child("Параметры")
    if parameters is not None:
        for item in parameters.items:
            if item.kind.name != "parameter":
                continue
            raw = item.attrs.get("ПослеЗагрузкиПараметра", "")
            if isinstance(raw, str) and raw.strip():
                yield rule_address(item), "ПослеЗагрузкиПараметра", raw
    for algorithm in rules.algorithms():
        text = str(algorithm.get("Текст"))
        if text.strip():
            yield rule_address(algorithm), "Текст", text


def _pks_handler_code(pko_code: str, properties: Node) -> Iterator[tuple[str, str, str]]:
    disabled: list[str] = []
    for path, node in walk_pks(properties):
        blocked = _disabled(node) or any(
            path == prefix or path.startswith(prefix + "/") for prefix in disabled
        )
        if node.is_group and _disabled(node):
            disabled.append(path)
        if blocked:
            continue
        kind_name = "pks_group" if node.is_group else "pks"
        address = pks_address(pko_code, path)
        for tag in _event_tags(kind_name):
            text = str(node.get(tag))
            if text.strip():
                yield address, tag, text


def _node_handlers(node: Node, kind_name: str) -> Iterator[tuple[str, str, str]]:
    address = rule_address(node)
    for tag in _event_tags(kind_name):
        text = str(node.get(tag))
        if text.strip():
            yield address, tag, text


def _event_tags(kind_name: str) -> tuple[str, ...]:
    return tuple(tag for kind, tag in EVENT_AREAS if kind == kind_name)


def _attached_references(code: str) -> tuple[list[str], set[str], str]:
    """Имена обращений, имена с собственным присваиванием и причина, если имя задано кодом.

    Целиком присвоенная `ДопОбработки` прячет все имена: экземпляр мог быть создан
    этой строкой, и порядок присваивания без разбора ветвлений не виден — во втором
    элементе тогда `_ATTACHED_WHOLE`.
    """
    if "допобработки" not in code.casefold():
        return [], set(), ""
    referenced: list[str] = []
    seen: set[str] = set()
    defined: set[str] = set()
    opaque = ""
    folded = "допобработки"
    size = len(folded)
    lower = code.casefold()
    index = 0
    length = len(code)
    while index < length:
        if code[index] == '"':
            index = _skip_bsl_string(code, index)
            continue
        if lower.startswith(folded, index) and _is_bare_name(code, index, size):
            index += size
            index = _skip_ws(code, index)
            if index < length and code[index] == ".":
                name, index, kind, literal = _attached_member(code, index + 1)
                if kind == "defined" and name:
                    defined.add(name.casefold())
                elif kind == "defined" and literal:
                    defined.add(literal.casefold())
                elif kind == "read" and name:
                    _remember_name(referenced, seen, name)
                elif kind == "opaque":
                    opaque = opaque or (
                        "задаёт имя вложенной обработки кодом — это имя не сверяется"
                    )
                continue
            if index < length and code[index] == "[":
                literal, index, assigned, dynamic = _attached_index(code, index + 1)
                if dynamic:
                    opaque = opaque or (
                        "задаёт имя вложенной обработки кодом — это имя не сверяется"
                    )
                elif literal and assigned:
                    defined.add(literal.casefold())
                elif literal:
                    _remember_name(referenced, seen, literal)
                continue
            if index < length and code[index] == "=":
                return (
                    [],
                    {_ATTACHED_WHOLE},
                    "присваивает «ДопОбработки» целиком — не видно, создан ли экземпляр",
                )
        index += 1
    return referenced, defined, opaque


def _attached_member(code: str, start: int) -> tuple[str, int, str, str]:
    """После точки: имя, новый индекс, `read`/`defined`/`opaque`/`skip`, литерал `Вставить`."""
    index = _skip_ws(code, start)
    name, index = _read_ident(code, index)
    if not name:
        return "", index, "skip", ""
    index = _skip_ws(code, index)
    if index < len(code) and code[index] == "=":
        return name, index, "defined", ""
    if index < len(code) and code[index] == "(":
        folded = name.casefold()
        if folded == "вставить":
            literal, dynamic = _insert_key(code, index)
            if dynamic:
                return "", index + 1, "opaque", ""
            if literal:
                return "", index + 1, "defined", literal
            return "", index + 1, "skip", ""
        if folded in _STRUCTURE_METHODS:
            return "", index + 1, "skip", ""
        return name, index + 1, "read", ""
    return name, index, "read", ""


def _attached_index(code: str, start: int) -> tuple[str, int, bool, bool]:
    """Скобка `ДопОбработки[…]`: литерал, индекс после неё, присваивание ли, динамический ключ."""
    index = _skip_ws(code, start)
    if index >= len(code) or code[index] != '"':
        return "", index, False, True
    end = _skip_bsl_string(code, index)
    literal = code[index + 1 : end - 1].replace('""', '"')
    index = _skip_ws(code, end)
    if index < len(code) and code[index] == "]":
        index += 1
    index = _skip_ws(code, index)
    assigned = index < len(code) and code[index] == "="
    return literal, index, assigned, False


def _insert_key(code: str, paren: int) -> tuple[str, bool]:
    """Первый аргумент `Вставить`. Второй элемент истинен, если это не строковый литерал."""
    index = _skip_ws(code, paren + 1)
    if index < len(code) and code[index] == '"':
        end = _skip_bsl_string(code, index)
        return code[index + 1 : end - 1].replace('""', '"'), False
    if index < len(code) and code[index] != ")":
        return "", True
    return "", False


def _remember_name(found: list[str], seen: set[str], name: str) -> None:
    key = name.casefold()
    if key and key not in seen:
        seen.add(key)
        found.append(name)


def _read_ident(text: str, index: int) -> tuple[str, int]:
    start = index
    while index < len(text) and (text[index].isalnum() or text[index] == "_"):
        index += 1
    return text[start:index], index


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


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


def _conversion_code(node: Node) -> str:
    """Код ПКО из `КодПравилаКонвертации`. Пустая строка — правило не названо."""
    return rule_code(node.get("КодПравилаКонвертации"))


def _object_groups(
    container: Node, pko: Node, target: Structure | None, prefix: str = ""
) -> Iterator[tuple[str, Node]]:
    """Группы, которые пишут коллекцию самого объекта, а не строки вложенной."""
    segments = pks_segments(container)
    for segment, item in zip(segments, container.items, strict=True):
        if _disabled(item) or not item.is_group:
            continue
        path = f"{prefix}{segment}"
        yield path, item
        kind = _collection_class(item, pko, target)
        rules_kind = _side_kind(item, "Приемник")
        # Пустой вид — простая группировка: дети пишут тот же объект (БСП:11654).
        # Непустой неразобранный вид коллекцией может быть, внутрь не спускаемся.
        if kind in ("table", "movement") or (kind is None and rules_kind):
            continue
        yield from _object_groups(item, pko, target, f"{path}/")


def _collection_class(node: Node, pko: Node, target: Structure | None) -> str | None:
    """`table`, `movement`, `other` либо `None`, если вид коллекции не разобрать.

    Пустой `Вид` без структуры не доказывает табличную часть: исполнитель относит
    его к простой группировке (БСП:11654), но это не вид из правил. Структура
    приёмника, если свойство найдено, вид подтверждает.
    """
    rules_class = _classify_kind(_side_kind(node, "Приемник"))
    if target is None:
        return rules_class
    name = side_name(node, "Приемник")
    receiver = str(pko.get("Приемник")).strip()
    obj = target.get(receiver) if receiver else None
    if obj is None or not name:
        return rules_class
    matches = [
        prop
        for (path, parent), props in target.properties(obj).items()
        if parent == "" and path == name
        for prop in props
    ]
    if not matches:
        return rules_class
    classes = {item for item in (_classify_kind(prop.kind) for prop in matches) if item}
    if rules_class not in (None, "other") and rules_class in classes:
        return rules_class
    if len(classes) == 1:
        return next(iter(classes))
    if not classes:
        return rules_class if rules_class is not None else "other"
    return None


def _classify_kind(kind: str) -> str | None:
    """`None` — вид в правилах не задан или не из списка коллекций."""
    if kind == "ТабличнаяЧасть":
        return "table"
    if kind.startswith("НаборДвижений") or kind == "НаборЗаписейПоследовательности":
        return "movement"
    if kind == "ПодчиненныйСправочник":
        return "other"
    # Пустой и неизвестный вид без структуры не считаем табличной частью.
    return None


def _side_kind(node: Node, side: str) -> str:
    child = node.child(side)
    return str(child.attrs.get("Вид", "")) if child is not None else ""


def _property_source_types(
    node: Node, pko: Node, ancestors: list[Node], source: Structure | None
) -> tuple[str, ...] | None:
    """Типы источника свойства. `None` — ни правила, ни структура их не показывают."""
    declared = _declared_types(node, "Источник")
    if declared:
        return declared
    if source is None:
        return None
    obj = source.get(str(pko.get("Источник")).strip())
    if obj is None:
        return None
    prefix = ""
    parent_kind = ""
    chain = [*ancestors, node]
    for index, item in enumerate(chain):
        name = side_name(item, "Источник")
        if not name:
            return None
        kind = _side_kind(item, "Источник") if item.is_group else ""
        prop = source.find(obj, f"{prefix}{name}", parent_kind, kind)
        if prop is None:
            return None
        if index == len(chain) - 1:
            return prop.types or None
        prefix = f"{prefix}{name}."
        parent_kind = prop.kind
    return None


def _declared_types(node: Node, side: str) -> tuple[str, ...]:
    child = node.child(side)
    if child is None:
        return ()
    raw = str(child.attrs.get("Тип", "")).strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in re.split(r"[,;\n]", raw) if part.strip())


def _name_choice(text: str, algorithms: dict[str, str]) -> str:
    """`none` — имя ПКО не задаётся; `explicit` — строковый литерал; `opaque` — иначе."""
    if not text.strip() or not _may_set_pko_name(text, algorithms, set()):
        return "none"
    expanded = _expand(text, algorithms)
    opaque = _text_opaque(expanded, algorithms)
    values = _assign_rhs(expanded.code, "ИмяПКО")
    literals = all(_bsl_string(rhs) is not None for rhs in values)
    if values and literals and not opaque:
        return "explicit"
    if values or opaque:
        return "opaque"
    return "none"


def _merge_name(earlier: str, later: str) -> str:
    """Поздний обработчик перекрывает ранний, если сам задаёт имя."""
    if later == "explicit":
        return "explicit"
    if later == "opaque" or earlier == "opaque":
        return "opaque"
    if earlier == "explicit":
        return "explicit"
    return "none"


_REACH_CACHE: dict[tuple[str, str], str] = {}
_NAME_CACHE: dict[str, bool] = {}


def _may_set_pko_name(text: str, algorithms: dict[str, str], seen: set[str]) -> bool:
    """Имя ПКО может быть задано в этом тексте или в вызванном алгоритме.

    Ложь — разворачивать тело незачем. Неразрешённый `Выполнить` оставляет истину:
    такой код непрозрачен и разбирается обычным путём.
    """
    lowered = text.casefold()
    if "имяпко" in lowered:
        return True
    if "алгоритм" not in lowered and "выполнить" not in lowered:
        return False
    cleaned = _mask(_strip_comments(text))
    if _EXECUTE_OPAQUE.search(
        _ALGORITHM_EXECUTE.sub(
            lambda match: "" if match.group(1).casefold() in algorithms else match.group(0),
            cleaned,
        )
    ):
        return True
    for match in _ALGORITHM.finditer(cleaned):
        key = match.group(1).casefold()
        if key in seen:
            continue
        if key not in algorithms:
            return True
        cached = _NAME_CACHE.get(key)
        if cached is None:
            _NAME_CACHE[key] = False
            cached = _may_set_pko_name(algorithms[key], algorithms, seen | {key})
            _NAME_CACHE[key] = cached
        if cached:
            return True
    return False


def _assigns_literal(code: str, name: str, accepted: frozenset[str]) -> bool:
    return any(rhs.casefold() in accepted for rhs in _assign_rhs(code, name))


def _scan_assignment(
    text: str, algorithms: dict[str, str], name: str, accepted: frozenset[str]
) -> str:
    """`hit` — присваивание литерала; `opaque` — код не виден; `none` — присваивания нет.

    Тело алгоритма читается, только если в нём есть имя или вызов, и один раз на
    проверку: один и тот же алгоритм из многих обработчиков повторно не обходится.
    """
    return _scan_chunks(text, algorithms, name.casefold(), accepted, set())


def _scan_chunks(
    text: str,
    algorithms: dict[str, str],
    folded: str,
    accepted: frozenset[str],
    seen: set[str],
) -> str:
    opaque = False
    stack = [text]
    while stack:
        chunk = stack.pop()
        lowered = chunk.casefold()
        mentions = folded in lowered
        calls = "алгоритм" in lowered or "выполнить" in lowered
        if not mentions and not calls:
            continue
        cleaned = _strip_comments(chunk)
        if mentions and any(rhs.casefold() in accepted for rhs in _assign_rhs(cleaned, folded)):
            return "hit"
        if not calls:
            continue
        masked = _mask(cleaned)
        pending = _ALGORITHM_EXECUTE.sub(
            lambda match: "" if match.group(1).casefold() in algorithms else match.group(0),
            masked,
        )
        if _EXECUTE_OPAQUE.search(pending):
            opaque = True
        for match in _ALGORITHM.finditer(masked):
            key = match.group(1).casefold()
            if key in seen:
                continue
            state = _REACH_CACHE.get((folded, key))
            if state is None or state == "open":
                if state == "open":
                    continue
                body = algorithms.get(key)
                if body is None:
                    state = "opaque"
                else:
                    _REACH_CACHE[(folded, key)] = "open"
                    state = _scan_chunks(body, algorithms, folded, accepted, seen | {key})
                    _REACH_CACHE[(folded, key)] = state
            if state == "hit":
                return "hit"
            if state == "opaque":
                opaque = True
    return "opaque" if opaque else "none"


def _text_opaque(expanded: _Text, algorithms: dict[str, str]) -> bool:
    """Код не виден целиком: `Выполнить` не от известного алгоритма или алгоритма нет.

    `Выполнить(Алгоритмы.Имя)` при известном имени телом уже развёрнут и код не прячет.
    """
    if not expanded.opaque:
        return False
    cleaned = _mask(_strip_comments(expanded.code))
    pending = _ALGORITHM_EXECUTE.sub(
        lambda match: "" if match.group(1).casefold() in algorithms else match.group(0),
        cleaned,
    )
    if _EXECUTE_OPAQUE.search(pending):
        return True
    return any(
        match.group(1).casefold() not in algorithms for match in _ALGORITHM.finditer(cleaned)
    )


def _assign_rhs(code: str, name: str) -> list[str]:
    """Правые части присваиваний `name` на границе оператора, без строк и комментариев."""
    if name.casefold() not in code.casefold():
        return []
    cleaned = _strip_comments(code)
    found: list[str] = []
    folded = name.casefold()
    size = len(folded)
    lower = cleaned.casefold()
    index = 0
    length = len(cleaned)
    while index < length:
        if cleaned[index] == '"':
            index = _skip_bsl_string(cleaned, index)
            continue
        if lower.startswith(folded, index) and _is_bare_name(cleaned, index, size):
            after = index + size
            if _is_statement_assign(cleaned, index, after):
                rhs, index = _read_rhs_until_break(cleaned, _after_equals(cleaned, after))
                found.append(rhs.strip())
                continue
        index += 1
    return found


def _is_bare_name(text: str, index: int, size: int) -> bool:
    before = text[index - 1] if index else ""
    after = text[index + size] if index + size < len(text) else ""
    # Пустая строка входит в любую: без `before` имя в начале текста тоже голое.
    if before and (before.isalnum() or before in "._"):
        return False
    return not (after.isalnum() or after == "_")


def _is_statement_assign(text: str, start: int, after_name: int) -> bool:
    if _token_before(text, start).casefold() not in _ASSIGN_BEFORE:
        return False
    cursor = after_name
    while cursor < len(text) and text[cursor] in " \t\r\n":
        cursor += 1
    return cursor < len(text) and text[cursor] == "="


def _after_equals(text: str, after_name: int) -> int:
    cursor = after_name
    while cursor < len(text) and text[cursor] != "=":
        cursor += 1
    return cursor + 1


def _token_before(text: str, pos: int) -> str:
    cursor = pos - 1
    while cursor >= 0 and text[cursor] in " \t\r\n":
        cursor -= 1
    if cursor < 0:
        return ""
    if text[cursor] == ";":
        return ";"
    end = cursor + 1
    while cursor >= 0 and (text[cursor].isalnum() or text[cursor] == "_"):
        cursor -= 1
    return text[cursor + 1 : end]


def _read_rhs_until_break(text: str, start: int) -> tuple[str, int]:
    chars: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            end = _skip_bsl_string(text, index)
            chars.append(text[index:end])
            index = end
            continue
        if char in ";\n":
            break
        chars.append(char)
        index += 1
    return "".join(chars), index


def _bsl_string(rhs: str) -> str | None:
    """Содержимое одного строкового литерала либо `None`, если справа не только он."""
    text = rhs.strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return None
    end = _skip_bsl_string(text, 0)
    if end != len(text):
        return None
    return text[1:-1].replace('""', '"')


def _skip_bsl_string(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == '"':
            if index + 1 < len(text) and text[index + 1] == '"':
                index += 2
                continue
            return index + 1
        index += 1
    return len(text)


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
