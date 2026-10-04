"""Проверка формата правил обмена и регистрации по читателю БСП.

Читатели (только факты, на которые опираются уровни):
`БСП` — `DataProcessors/КонвертацияОбъектовИнформационныхБаз/Ext/ObjectModule.bsl`,
`РЕГ` — `DataProcessors/ЗагрузкаПравилРегистрацииОбъектов/Ext/ObjectModule.bsl`.

Неизвестный тег (`Node.unknown`) и атрибут вне схемы писателя КД.
Колонки решения: где — уровень — почему — строка.

- Корень ПравилаОбмена, тег не из следующего списка — ошибка — ветка
  «ошибка формата», протокол 7 — БСП:16489.
- Корень: `РежимСовместимости`, `ОсновнойПланОбмена`,
  `ПравилаРегистрацииОбъектов` — предупреждение — `одПропустить` —
  БСП:16359, БСП:16366, БСП:16467.
- ПКО: `РегистрироватьОбъектНаУзлеОтправителе` — предупреждение —
  `одПропустить`, «не поддерживается» — БСП:5774.
- ПКО: `ПоискПоТабличнымЧастям`, `ПоляПоиска`, `ПоляТаблицы`,
  `ОбъектСДвижениями` — нет замечания — читатель разбирает тег —
  БСП:5999, БСП:6019, БСП:6023, БСП:6027.
- ПВД: `ИмяТипаПриемника` — нет замечания — читатель пишет поле — БСП:6798.
- ПОД: `Наименование`, `УдалятьЗаПериод` — нет замечания — БСП:6313, БСП:6333.
- Группа ПОД: `Наименование` — нет замечания — БСП:6269.
- ПКО, ПКС, ПКГС, ПКЗ, ПВД, ПОД, списки правил, группы, параметры,
  обработки, варианты поиска: тег без своей ветки — ошибка — нет
  `одПропустить`, следующий `Прочитать` читает вложенные узлы как поля
  родителя — БСП:5751, БСП:5510, БСП:5306, БСП:5686, БСП:6770, БСП:6305,
  БСП:6227, БСП:5643, БСП:5716, БСП:6872, БСП:6589, БСП:6743, БСП:6487,
  БСП:6560, БСП:6169.
- Алгоритм, запрос — предупреждение — `Иначе одПропустить` —
  БСП:6448, БСП:6521.
- Сторона ПКС (`Источник`/`Приемник`) — предупреждение — после атрибутов
  `одПропустить` — БСП:5518, БСП:5314.
- `ВерсияФормата`, конфигурация, план обмена, тело обработки — ошибка —
  `одЗначениеЭлемента` на вложенном элементе возвращает `Неопределено`
  и не дочитывает поддерево — БСП:4504.
- Корень ПравилаРегистрации и циклы с `Иначе одПропустить` (список ПРО,
  ПРО, группа, отборы; состав плана целиком пропускает корень) —
  предупреждение — РЕГ:222, РЕГ:255, РЕГ:355, РЕГ:390, РЕГ:425, РЕГ:502,
  РЕГ:568, РЕГ:609, РЕГ:652, РЕГ:680.
- Любой атрибут вне схемы — предупреждение — `одАтрибут` читает только
  запрошенное имя — БСП:4406.

Обязательные элементы — предупреждение: читатель не отказывает в загрузке, но правило теряет смысл
или адрес. Пустой `Код` ПКО/ПВД/ПОД — БСП:5756, БСП:6773, БСП:6309; пустой `Код` ПРО читатель
не читает вовсе (РЕГ:355); пустое `Имя` алгоритма/запроса/параметра/обработки — БСП:6438,
БСП:6511, БСП:6596, БСП:6688; пустой `Источник` ПКО не привязывает тип (БСП:5837), пустой
`Приемник` записывается пустым (БСП:5816); пустой `ОбъектМетаданныхИмя` ПРО не попадёт в отбор
(РЕГ:305, поиск БСП:3411).

Уникальность. Ошибка, если читатель кладёт правило в соответствие и повтор перезаписывает первое:
ПКО `Правила.Вставить` (БСП:6135), алгоритм (БСП:6467), запрос (БСП:6541), параметр (БСП:6629),
обработка (БСП:6699). Предупреждение, если оба остаются: ПВД `Добавить` (БСП:6766), поиск по типу,
не по коду (БСП:18363); ПОД `Строки.Добавить` (БСП:6400); ПРО `Добавить` (РЕГ:270), код не читается
(РЕГ:355), отбор по `ОбъектМетаданныхИмя` возвращает все строки (БСП:3411).

Висячая ссылка — ошибка: нет ПКО, исполнитель пишет протокол 45 и взводит флаг ошибки
(БСП:341, БСП:348, БСП:4789). ПВД ищет ПКО в `НайтиПравило` (БСП:6892), ПКС и группа ПКС
кладут код в `ПравилоКонвертации` (БСП:5546, БСП:5353) и резолвят его в `ОпределитьПКОПоПараметрам`
(БСП:13316), параметр — в `ПередатьОдинПараметрВПриемник` (БСП:17809). Пустой код ссылки
висячей ссылкой не считается.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from lxml import etree

from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules, RulesDocument, rule_code
from kd2_rules_mcp.validation.address import TITLES, pks_address, rule_address, walk_pks
from kd2_rules_mcp.validation.report import ValidationReport

UNKNOWN_TAG = "format.unknown_tag"
UNKNOWN_ATTR = "format.unknown_attr"
REQUIRED = "format.required"
DUPLICATE_CODE = "format.duplicate_code"
DUPLICATE_NAME = "format.duplicate_name"
DANGLING_REF = "format.dangling_ref"

_EXCHANGE = "КонвертацияОбъектовИнформационныхБаз/Ext/ObjectModule.bsl"
_REGISTER = "ЗагрузкаПравилРегистрацииОбъектов/Ext/ObjectModule.bsl"


class TagFate(Enum):
    """Что читатель делает с тегом, которого нет в схеме писателя."""

    ERROR = "error"
    WARNING = "warning"
    ACCEPT = "accept"


@dataclass(frozen=True, slots=True)
class TagRule:
    """Решение по одному тегу или по умолчанию для вида узла."""

    fate: TagFate
    citation: str
    reason: str


def _misread(line: int) -> TagRule:
    """Нет ветки `одПропустить`: вложенные узлы читаются как поля родителя."""
    return TagRule(
        TagFate.ERROR,
        f"{_EXCHANGE}:{line}",
        "читатель не пропускает тег, вложенные узлы читаются как поля родителя",
    )


def _skip(line: int, *, registration: bool = False) -> TagRule:
    """Читатель вызывает `одПропустить`."""
    module = _REGISTER if registration else _EXCHANGE
    return TagRule(TagFate.WARNING, f"{module}:{line}", "читатель пропустит")


def _accept(line: int) -> TagRule:
    """Читатель разбирает тег, хотя писатель КД его не выводит."""
    return TagRule(TagFate.ACCEPT, f"{_EXCHANGE}:{line}", "")


def _broken_value(line: int) -> TagRule:
    """`одЗначениеЭлемента` не дочитывает элемент с вложенными узлами."""
    return TagRule(
        TagFate.ERROR,
        f"{_EXCHANGE}:{line}",
        "одЗначениеЭлемента встречает вложенный элемент и возвращает Неопределено,"
        " не пропуская поддерево",
    )


@dataclass(frozen=True, slots=True)
class KindTags:
    """Политика неизвестных тегов одного вида узла."""

    default: TagRule
    by_tag: Mapping[str, TagRule] = field(default_factory=dict)


# Политика по виду узла модели. Ссылки на строки — в `TagRule.citation`.
_KIND_TAGS: dict[str, KindTags] = {
    "exchange_rules": KindTags(
        TagRule(
            TagFate.ERROR,
            f"{_EXCHANGE}:16489",
            "читатель останавливает загрузку (ошибка формата)",
        ),
        {
            "РежимСовместимости": _skip(16359),
            "ОсновнойПланОбмена": _skip(16366),
            "ПравилаРегистрацииОбъектов": _skip(16467),
        },
    ),
    "format_version": KindTags(_broken_value(4504)),
    "config": KindTags(_broken_value(4504)),
    "parameters": KindTags(_misread(6589)),
    "parameter": KindTags(_misread(6589)),
    "data_processors": KindTags(_misread(6743)),
    "data_processor": KindTags(_broken_value(4504)),
    "pko_list": KindTags(_misread(6227)),
    "pko": KindTags(
        _misread(5751),
        {
            "РегистрироватьОбъектНаУзлеОтправителе": _skip(5774),
            "ПоискПоТабличнымЧастям": _accept(5999),
            "ПоляПоиска": _accept(6019),
            "ПоляТаблицы": _accept(6023),
            "ОбъектСДвижениями": _accept(6027),
        },
    ),
    "pko_group": KindTags(_misread(6227)),
    "search_variants": KindTags(_misread(6187)),
    "search_variant": KindTags(_misread(6141)),
    "pks_list": KindTags(_misread(5643)),
    "pks_side": KindTags(_skip(5518)),
    "pks": KindTags(_misread(5510)),
    "pks_group": KindTags(_misread(5306)),
    "pkz_list": KindTags(_misread(5716)),
    "pkz": KindTags(_misread(5686)),
    "pkz_group": KindTags(_misread(5716)),
    "pvd_list": KindTags(_misread(6872)),
    "pvd": KindTags(_misread(6770), {"ИмяТипаПриемника": _accept(6798)}),
    "pvd_group": KindTags(_misread(6872)),
    "pod_list": KindTags(_misread(6398)),
    "pod": KindTags(
        _misread(6305),
        {"Наименование": _accept(6313), "УдалятьЗаПериод": _accept(6333)},
    ),
    "pod_group": KindTags(_misread(6261), {"Наименование": _accept(6269)}),
    "algorithm_list": KindTags(_misread(6487)),
    "algorithm": KindTags(_skip(6448)),
    "algorithm_group": KindTags(_misread(6487)),
    "query_list": KindTags(_misread(6560)),
    "query": KindTags(_skip(6521)),
    "query_group": KindTags(_misread(6560)),
    "exchange_plan": KindTags(_broken_value(4504)),
    # Состав плана целиком пропускает корень правил регистрации (РЕГ:222).
    "plan_content": KindTags(_skip(222, registration=True)),
    "plan_content_item": KindTags(_skip(222, registration=True)),
    "registration_rules": KindTags(_skip(222, registration=True)),
    "pro_list": KindTags(_skip(255, registration=True)),
    "pro": KindTags(_skip(355, registration=True)),
    "pro_group": KindTags(_skip(680, registration=True)),
    "plan_filter": KindTags(_skip(390, registration=True)),
    "object_filter": KindTags(_skip(425, registration=True)),
    "plan_filter_item": KindTags(_skip(502, registration=True)),
    "object_filter_item": KindTags(_skip(568, registration=True)),
    "plan_filter_group": KindTags(_skip(609, registration=True)),
    "object_filter_group": KindTags(_skip(652, registration=True)),
    # Таблицы свойств отбора читатель не открывает: родитель вызывает одПропустить (РЕГ:502).
    "property_table": KindTags(_skip(502, registration=True)),
    "property_row": KindTags(_skip(502, registration=True)),
}

_FALLBACK = TagRule(
    TagFate.ERROR,
    f"{_EXCHANGE}:16489",
    "читатель останавливает загрузку (ошибка формата)",
)


def check_format(document: RulesDocument) -> ValidationReport:
    """Проверяет правила обмена или регистрации (вид — по классу документа)."""
    report = ValidationReport()
    _check_unknown(document.root, report)
    if isinstance(document, ExchangeRules):
        _check_exchange(document, report)
    elif isinstance(document, RegistrationRules):
        _check_registration(document, report)
    return report


def _check_unknown(root: Node, report: ValidationReport) -> None:
    _walk_unknown(root, [], report)


def _walk_unknown(node: Node, stack: list[Node], report: ValidationReport) -> None:
    stack.append(node)
    for element in node.unknown:
        _report_unknown_tag(stack, element, report)
    for name in node.attrs:
        if name not in node.kind.attr_map:
            # БСП:4406 — одАтрибут читает только запрошенное имя, лишние атрибуты не проверяет.
            report.warning(
                UNKNOWN_ATTR,
                f"{_base_address(stack)} / атрибут {name}",
                f"Атрибут «{name}»: читатель пропустит",
            )
    for child in node.children.values():
        _walk_unknown(child, stack, report)
    for item in node.items:
        _walk_unknown(item, stack, report)
    stack.pop()


def _report_unknown_tag(
    stack: list[Node], element: etree._Element, report: ValidationReport
) -> None:
    tag = str(element.tag)
    rule = _tag_rule(stack[-1].kind.name, tag)
    if rule.fate is TagFate.ACCEPT:
        return
    message = f"Тег «{tag}»: {rule.reason}"
    address = f"{_base_address(stack)} / тег {tag}"
    if rule.fate is TagFate.ERROR:
        report.error(UNKNOWN_TAG, address, message)
    else:
        report.warning(UNKNOWN_TAG, address, message)


def _tag_rule(kind_name: str, tag: str) -> TagRule:
    policy = _KIND_TAGS.get(kind_name)
    if policy is None:
        return _FALLBACK
    return policy.by_tag.get(tag, policy.default)


def _base_address(stack: list[Node]) -> str:
    """Адрес ближайшего правила; вне правила — корневой тег."""
    for node in reversed(stack):
        if node.kind.name in TITLES:
            return rule_address(node)
    return stack[0].tag


def _text(node: Node, tag: str) -> str:
    """Текст тега так, как его обрезает читатель (`СокрП`, БСП:4495)."""
    return rule_code(node.values.get(tag, ""))


def _attr(node: Node, name: str) -> str:
    """Атрибут так, как его обрезает `одАтрибут` (`СокрП`, БСП:4408)."""
    return rule_code(node.attrs.get(name, ""))


def _rules(root: Node, tag: str) -> list[Node]:
    """Правила раздела без создания отсутствующего раздела (в отличие от `section`)."""
    node = root.children.get(tag)
    return list(node.walk()) if node is not None else []


def _check_exchange(document: ExchangeRules, report: ValidationReport) -> None:
    root = document.root
    pko = _rules(root, "ПравилаКонвертацииОбъектов")
    pvd = _rules(root, "ПравилаВыгрузкиДанных")
    pod = _rules(root, "ПравилаОчисткиДанных")
    algorithms = _rules(root, "Алгоритмы")
    queries = _rules(root, "Запросы")
    parameters = _rules(root, "Параметры")
    processors = _rules(root, "Обработки")

    for node in pko:
        _require_tag(report, node, "Код", "правило нельзя найти по коду")
        _require_tag(
            report,
            node,
            "Источник",
            "читатель не привязывает тип и не отказывает в загрузке",
        )
        _require_tag(report, node, "Приемник", "тип приёмника записывается пустым")
    for node in pvd:
        _require_tag(report, node, "Код", "правило нельзя отличить по коду")
    for node in pod:
        _require_tag(report, node, "Код", "правило нельзя отличить по коду")
    for node in algorithms:
        _require_attr(report, node, "Имя", "алгоритм нельзя вызвать")
    for node in queries:
        _require_attr(report, node, "Имя", "запрос нельзя вызвать")
    for node in parameters:
        _require_attr(report, node, "Имя", "параметр нельзя найти")
    for node in processors:
        _require_attr(report, node, "Имя", "обработку нельзя найти")

    _check_unique(
        report,
        pko,
        key=lambda node: _text(node, "Код"),
        check=DUPLICATE_CODE,
        noun="Код",
        overwrite=True,
        reason="Читатель хранит ПКО в соответствии по коду и перезапишет первое",
    )
    _check_unique(
        report,
        pvd,
        key=lambda node: _text(node, "Код"),
        check=DUPLICATE_CODE,
        noun="Код",
        overwrite=False,
        reason="Читатель добавляет каждое ПВД в таблицу и не перезаписывает по коду",
    )
    _check_unique(
        report,
        pod,
        key=lambda node: _text(node, "Код"),
        check=DUPLICATE_CODE,
        noun="Код",
        overwrite=False,
        reason="Читатель добавляет каждое ПОД в дерево и не перезаписывает по коду",
    )
    _check_unique(
        report,
        algorithms,
        key=lambda node: _attr(node, "Имя"),
        check=DUPLICATE_NAME,
        noun="Имя",
        overwrite=True,
        reason="Читатель хранит алгоритмы в соответствии по имени и перезапишет первый",
    )
    _check_unique(
        report,
        queries,
        key=lambda node: _attr(node, "Имя"),
        check=DUPLICATE_NAME,
        noun="Имя",
        overwrite=True,
        reason="Читатель хранит запросы в соответствии по имени и перезапишет первый",
    )
    _check_unique(
        report,
        parameters,
        key=lambda node: _attr(node, "Имя"),
        check=DUPLICATE_NAME,
        noun="Имя",
        overwrite=True,
        reason="Читатель хранит параметры в соответствии по имени и перезапишет первый",
    )
    _check_unique(
        report,
        processors,
        key=lambda node: _attr(node, "Имя"),
        check=DUPLICATE_NAME,
        noun="Имя",
        overwrite=True,
        reason="Читатель хранит обработки в соответствии по имени и перезапишет первую",
    )

    codes = {code for node in pko if (code := _text(node, "Код"))}
    for node in pvd:
        _dangling_tag(report, node, "КодПравилаКонвертации", codes, rule_address(node))
    for node in parameters:
        _dangling_attr(report, node, "ПравилоКонвертации", codes, rule_address(node))
    for owner in pko:
        properties = owner.child("Свойства")
        if properties is None:
            continue
        for path, node in walk_pks(properties):
            _dangling_tag(
                report,
                node,
                "КодПравилаКонвертации",
                codes,
                pks_address(_text(owner, "Код"), path),
            )


def _check_registration(document: RegistrationRules, report: ValidationReport) -> None:
    rules = _rules(document.root, "ПравилаРегистрацииОбъектов")
    for node in rules:
        _require_tag(report, node, "Код", "правило нельзя адресовать")
        _require_tag(
            report,
            node,
            "ОбъектМетаданныхИмя",
            "отбор по объекту правило не найдёт",
        )
    _check_unique(
        report,
        rules,
        key=lambda node: _text(node, "Код"),
        check=DUPLICATE_CODE,
        noun="Код",
        overwrite=False,
        reason="Читатель не читает код ПРО и не перезаписывает правило",
    )


def _require_tag(report: ValidationReport, node: Node, tag: str, reason: str) -> None:
    if _text(node, tag):
        return
    report.warning(REQUIRED, f"{rule_address(node)} / тег {tag}", f"Пустой «{tag}»: {reason}")


def _require_attr(report: ValidationReport, node: Node, name: str, reason: str) -> None:
    if _attr(node, name):
        return
    report.warning(REQUIRED, f"{rule_address(node)} / атрибут {name}", f"Пустой «{name}»: {reason}")


def _check_unique(
    report: ValidationReport,
    nodes: list[Node],
    *,
    key: Callable[[Node], str],
    check: str,
    noun: str,
    overwrite: bool,
    reason: str,
) -> None:
    grouped: dict[str, list[tuple[int, Node]]] = {}
    for index, node in enumerate(nodes, start=1):
        grouped.setdefault(key(node), []).append((index, node))
    for value, entries in grouped.items():
        if len(entries) < 2:
            continue
        shown = f"«{value}»" if value else "пустой"
        labels = ", ".join(_rule_label(index, node) for index, node in entries)
        message = f"{noun} {shown} повторяется: {labels}. {reason}"
        _add(report, overwrite, check, rule_address(entries[1][1]), message)


def _rule_label(index: int, node: Node) -> str:
    """Порядковый номер в списке, наименование и поле `Порядок`, если они есть."""
    label = f"№{index}"
    name = _text(node, "Наименование")
    if name:
        label += f" «{name}»"
    order = node.values.get("Порядок", "")
    if order != "":
        label += f", порядок {order}"
    return label


def _dangling_tag(
    report: ValidationReport, node: Node, tag: str, codes: set[str], address: str
) -> None:
    ref = _text(node, tag)
    if ref and ref not in codes:
        _dangling(report, address, tag, ref)


def _dangling_attr(
    report: ValidationReport, node: Node, name: str, codes: set[str], address: str
) -> None:
    ref = _attr(node, name)
    if ref and ref not in codes:
        _dangling(report, address, name, ref)


def _dangling(report: ValidationReport, address: str, field_name: str, ref: str) -> None:
    report.error(
        DANGLING_REF,
        address,
        f"«{field_name}» «{ref}» не найден среди ПКО: исполнитель сообщит, что ПКО нет",
    )


def _add(report: ValidationReport, error: bool, check: str, address: str, message: str) -> None:
    if error:
        report.error(check, address, message)
    else:
        report.warning(check, address, message)
