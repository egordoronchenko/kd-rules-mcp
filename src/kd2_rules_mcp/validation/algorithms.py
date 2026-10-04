"""Проверка ссылок обработчиков на алгоритмы (`Алгоритмы.Имя`).

Читатель — `DataProcessors/КонвертацияОбъектовИнформационныхБаз/Ext/ObjectModule.bsl` (БСП).
Алгоритмы лежат в структуре `Алгоритмы` (БСП:37, БСП:14664), имя — атрибут `Имя` после `СокрП`
(`одАтрибут`, БСП:4408); ключи структуры не различают регистр. `ЗагрузитьАлгоритм` кладёт в
структуру только алгоритмы своего режима (БСП:6455–6468): при загрузке — с
`ИспользуетсяПриЗагрузке`, при выгрузке — без него (помеченные уходят в файл обмена).
Обращение к отсутствующему полю структуры — исключение в обработчике.

- Алгоритма с таким именем нет — ошибка.
- Алгоритм есть, но только другого режима — ошибка: в фазе обработчика его нет в структуре.

Фаза обработчика — по месту вызова в исполнителе. Загрузка: конвертация `ПередЗагрузкойДанных`
(БСП:2877), `ПослеЗагрузкиДанных` (БСП:2999), `ПослеЗагрузкиПараметров` (БСП:7726),
`ПередЗагрузкойОбъекта` (БСП:10043), `ПослеЗагрузкиОбъекта` (БСП:10959),
`ПриПолученииИнформацииОбУдалении` (БСП:16617), `ПослеПолученияИнформацииОбУзлахОбмена`
(БСП:17478); ПКО `ПередЗагрузкой` (БСП:10090), `ПриЗагрузке` (БСП:10220), `ПослеЗагрузки`
(БСП:10282), `ПоследовательностьПолейПоиска` (БСП:8947); параметр `ПослеЗагрузкиПараметра`
(БСП:16172). Выгрузка: остальные события конвертации (БСП:509, БСП:13463, БСП:13538, БСП:13780,
БСП:17955, БСП:18102, БСП:18183) и ПКО (БСП:564, БСП:951, БСП:1041, БСП:1151), ПКС и ПКГС
(БСП:12190, БСП:11549), ПВД (БСП:1436). Алгоритм выполняется в фазе своего режима.
Режим не проверяется у `ПослеЗагрузкиПравилОбмена` (вызывается после чтения правил в обоих
режимах, БСП:16534) и у ПОД (фаза не установлена) — только наличие алгоритма.

Ссылка ищется в тексте без строковых литералов и комментариев `//`; вызов метода структуры
(`Алгоритмы.Свойство(…)`) ссылкой не считается.
"""

import re
from enum import Enum

from kd2_rules_mcp.kd2.model import ExchangeRules, Node
from kd2_rules_mcp.validation.address import rule_address
from kd2_rules_mcp.validation.handlers import ALGORITHM_EVENT, collect_handlers
from kd2_rules_mcp.validation.report import Issue, Level, ValidationReport
from kd2_rules_mcp.validation.structure import (
    PKO_LOAD_EVENTS,
    REF_ONLY_LOAD_NOTE,
    ref_only_pko_addresses,
)

MISSING_ALGORITHM = "algorithm.missing"
WRONG_MODE = "algorithm.wrong_mode"


class Phase(Enum):
    """Режим исполнителя, в котором выполняется обработчик."""

    EXPORT = "выгрузке"
    IMPORT = "загрузке"


_IMPORT_EVENTS: dict[str, frozenset[str]] = {
    "Конвертация": frozenset(
        {
            "ПередЗагрузкойДанных",
            "ПослеЗагрузкиДанных",
            "ПослеЗагрузкиПараметров",
            "ПередЗагрузкойОбъекта",
            "ПослеЗагрузкиОбъекта",
            "ПриПолученииИнформацииОбУдалении",
            "ПослеПолученияИнформацииОбУзлахОбмена",
        }
    ),
    "ПКО": frozenset(
        {"ПередЗагрузкой", "ПриЗагрузке", "ПослеЗагрузки", "ПоследовательностьПолейПоиска"}
    ),
    "Параметры": frozenset({"ПослеЗагрузкиПараметра"}),
}
_EXPORT_ONLY = frozenset({"ПКС", "ПКГС", "ПВД"})
_ANY_PHASE = {("Конвертация", "ПослеЗагрузкиПравилОбмена")}

_LITERAL = re.compile(r'"[^"\n]*"?')
_REFERENCE = re.compile(r"(?<![\w.])Алгоритмы\s*\.\s*([^\W\d]\w*)\b(?!\s*\()", re.IGNORECASE)


def check_algorithm_refs(rules: ExchangeRules) -> ValidationReport:
    """Ссылки `Алгоритмы.Имя` в обработчиках и алгоритмах против списка алгоритмов правил."""
    report = ValidationReport()
    algorithms = _algorithms(rules)
    names = {
        phase: {_key(node) for node in algorithms if _phase_of_algorithm(node) is phase}
        for phase in Phase
    }
    for item in collect_handlers(rules):
        if item.file_prefix == "Алгоритм":
            continue
        _check_text(
            report,
            item.text,
            f"{item.address} / {item.event}",
            _phase(item.file_prefix, item.event),
            names,
        )
    for node in algorithms:
        text = node.values.get(ALGORITHM_EVENT)
        if isinstance(text, str):
            address = f"{rule_address(node)} / {ALGORITHM_EVENT}"
            _check_text(report, text, address, _phase_of_algorithm(node), names)
    _soften_ref_only_load(report, rules)
    return report


def _soften_ref_only_load(report: ValidationReport, rules: ExchangeRules) -> None:
    """Обработчики загрузки ПКО «только ссылка» исполнителем не вызываются (БСП:7984–8010)."""
    addresses = ref_only_pko_addresses(rules)
    if not addresses:
        return
    softened: list[Issue] = []
    for issue in report.issues:
        head, separator, event = issue.address.rpartition(" / ")
        if (
            separator
            and head in addresses
            and event in PKO_LOAD_EVENTS
            and issue.check in (MISSING_ALGORITHM, WRONG_MODE)
            and issue.level is Level.ERROR
        ):
            softened.append(
                Issue(
                    Level.WARNING,
                    issue.check,
                    issue.address,
                    issue.message + REF_ONLY_LOAD_NOTE,
                )
            )
        else:
            softened.append(issue)
    report.issues = softened


def references(text: str) -> list[str]:
    """Имена алгоритмов из `Алгоритмы.Имя` в порядке первого появления."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        code = _LITERAL.sub('""', line).split("//", 1)[0]
        for match in _REFERENCE.finditer(code):
            found.setdefault(match.group(1).casefold(), match.group(1))
    return list(found.values())


def _check_text(
    report: ValidationReport,
    text: str,
    address: str,
    phase: Phase | None,
    names: dict[Phase, set[str]],
) -> None:
    for name in references(text):
        key = name.casefold()
        if phase is not None and key in names[phase]:
            continue
        other = [candidate for candidate in Phase if key in names[candidate]]
        if not other:
            report.error(
                MISSING_ALGORITHM,
                address,
                f"«Алгоритмы.{name}»: алгоритма «{name}» в правилах нет, "
                "обработчик упадёт на обращении",
            )
        elif phase is not None:
            report.error(
                WRONG_MODE,
                address,
                f"«Алгоритмы.{name}»: обработчик выполняется при {phase.value}, "
                f"а алгоритм «{name}» "
                f"загружается только при {other[0].value} (флаг «ИспользуетсяПриЗагрузке»)",
            )


def _phase(prefix: str, event: str) -> Phase | None:
    if (prefix, event) in _ANY_PHASE or prefix == "ПОД":
        return None
    if prefix in _EXPORT_ONLY:
        return Phase.EXPORT
    return Phase.IMPORT if event in _IMPORT_EVENTS.get(prefix, frozenset()) else Phase.EXPORT


def _phase_of_algorithm(node: Node) -> Phase:
    return Phase.IMPORT if node.attrs.get("ИспользуетсяПриЗагрузке") is True else Phase.EXPORT


def _key(node: Node) -> str:
    return str(node.attrs.get("Имя", "")).rstrip().casefold()


def _algorithms(rules: ExchangeRules) -> list[Node]:
    node = rules.root.children.get("Алгоритмы")
    return (
        [item for item in node.walk() if item.kind.name == "algorithm"] if node is not None else []
    )
