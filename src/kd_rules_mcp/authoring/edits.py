"""Точечные правки правил обмена (спецификация `rules-authoring`, «Точечные правки правил»).

Документ `ExchangeRules` меняется на месте. Адрес — как в `validation.address` (design.md, Д6):
ПКО, ПВД и ПОД по `Код`; алгоритм, запрос и параметр по `Имя`; ПКС — код ПКО и путь
`группа/…/свойство-приёмник`; ПКЗ — код ПКО и имя значения источника. Изменение записывает
только переданные поля.

Поля новых ПКС — как у автонастройки
`АвтонастройкаПравилКонвертацииСвойств/Ext/ObjectModule.bsl`, `СохранитьПравилаКС` (794–898):
`Порядок` с нуля шагом 50 (796, 815, 864), группа при вложенных строках (802–806), имя, вид
и тип сторон (831–850). `Отключить` у свойства приёмника без пары — по спецификации и Д7;
КД ставит этот флаг в строке 816 и пишет атрибутом (`ВыгрузкаКонвертации`, 619–621).
В XML тип стороны пишется только при единственном типе, у группы тип не пишется
(`ВыгрузкаКонвертации/Ext/ObjectModule.bsl`, 643–672 и 714–726).

Одиночные ПКС и группа ПКС в `create_rule` получают `Код` и `Порядок`, если их не
передали. `Порядок` считается по контейнеру (`Свойства` или группа): максимум плюс
шаг 50; пустой контейнер и контейнер, где `Порядок` ни у кого не задан, получают 0 —
как первая строка `_fill_properties`, чтобы оба пути нумеровали одинаково (0, 50, 100, …).
`Код` — следующее целое по всем ПКС ПКО.

ПКО при массовом создании получает источник, приёмник и наименование
(`АвтонастройкаПравилКонвертацииОбъектов/Ext/ObjectModule.bsl`, `СохранитьПравила`, 227–234;
`ОбщегоНазначения/Ext/Module.bsl`, `глНаименованиеПКО`, 113). В XML это имена типов
(`ВыгрузкаКонвертации/Ext/ObjectModule.bsl`, 842–846).

То же создание ставит `СинхронизироватьПоИдентификатору`, если приёмник ссылочный и не
перечисление (`ОбщегоНазначения/Ext/Module.bsl`, `ОпределитьНужнаСинхронизацияПоИдентификатору`,
1907–1924). Обе стороны — приложения 8: структуры сервера собраны из выгрузок 8.3, отдельной
проверки приложения нет. ПКС, у которого имя приёмника подобно `%ЭтоГруппа%`, получает
`Обязательное` (`ВыгрузкаКонвертации/Ext/ObjectModule.bsl`, 1214–1216 и 627) — и по кандидатам,
и в `create_rule`, если поле не передано. Явное значение в `fields`, в том числе ложь, сильнее.
Оба умолчания попадают в `warnings` ответа, чтобы их было видно вместе с прочими сведениями
о созданных ПКС.
"""

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kd_rules_mcp.authoring.candidates import (
    Candidate,
    Side,
    object_candidates_report,
    property_candidates,
)
from kd_rules_mcp.errors import (
    AmbiguousAddressError,
    CountMismatchError,
    DanglingReferenceError,
    DuplicateRuleError,
    ObjectNotFoundError,
    PreviewRequiredError,
    RuleEditError,
    RuleNotFoundError,
    UnknownFieldError,
)
from kd_rules_mcp.kd2.model import ExchangeRules, Node, rule_code
from kd_rules_mcp.kd2.schema import CONVERSION_EVENTS, Policy, Scalar, ValueType
from kd_rules_mcp.structures.queries import NotFound, find_object
from kd_rules_mcp.structures.xmldump import KINDS as _METADATA_KINDS
from kd_rules_mcp.validation.address import (
    CONVERSION_ADDRESS,
    pks_address,
    pks_base_segment,
    pks_candidates,
    pkz_address,
    rule_address,
    side_name,
    walk_pks,
)
from kd_rules_mcp.validation.report import Level, ValidationReport
from kd_rules_mcp.validation.structure import (
    SOURCE,
    TARGET,
    Structure,
    check_rule,
    is_ref,
    load_sides,
)

# Шаг `Порядок` автонастройки ПКС (СохранитьПравилаКС, 796 и 864).
_ORDER_STEP = 50
# Ссылочные виды, у которых `глЕстьСсылка` возвращает Истина, кроме перечисления
# (ОбщегоНазначения, 350–370 и 1907–1924).
_SYNC_REFERENCE_KINDS = frozenset(
    {
        "БизнесПроцесс",
        "Документ",
        "Задача",
        "ПланВидовРасчета",
        "ПланВидовХарактеристик",
        "ПланОбмена",
        "ПланСчетов",
        "Справочник",
        "ТочкаМаршрутаБизнесПроцесса",
    }
)
_SYNC_BY_ID = "СинхронизироватьПоИдентификатору"
_MANDATORY = "Обязательное"
# Фрагмент шаблона `ПОДОБНО "%ЭтоГруппа%"` (ВыгрузкаКонвертации, 1214–1216).
_GROUP_PROPERTY_MARK = "этогруппа"
# Первая синхронизация баз с заведёнными данными: без продолжения поиска объект, не найденный
# по GUID, создаётся сразу (БСП:8837, 8856-8860), с ним — ищется по полям поиска (БСП:8862-8882).
_SYNC_DEFAULT_NOTE = (
    "СинхронизироватьПоИдентификатору включено по умолчанию: ссылочный приёмник, не перечисление. "
    "Если данные уже заведены в обеих базах, при первой синхронизации поиск только по GUID даст "
    "дубли: нужны поля поиска и ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли"
)

# События конвертации — одно правило на файл, ключ пустой или `Конвертация`.
CONVERSION_KIND = "conversion"
# Реквизиты заголовка: rule_update вида conversion их не меняет.
_CONVERSION_HEADER = frozenset(
    {
        "ВерсияФормата",
        "РежимСовместимости",
        "Ид",
        "Наименование",
        "ДатаВремяСоздания",
        "Источник",
        "Приемник",
        "ВерсияПлатформы",
        "ВерсияКонфигурации",
        "СинонимКонфигурации",
        "УдалятьСопоставленныеОбъектыВПриемникеПриИхУдаленииВИсточнике",
        "Комментарий",
    }
)
_CONVERSION_EVENTS = frozenset(CONVERSION_EVENTS)

# Вид верхнего уровня: раздел, тег элемента, поле-адрес.
_TOP: dict[str, tuple[str, str, str]] = {
    "pko": ("ПравилаКонвертацииОбъектов", "Правило", "Код"),
    "pvd": ("ПравилаВыгрузкиДанных", "Правило", "Код"),
    "pod": ("ПравилаОчисткиДанных", "Правило", "Код"),
    "algorithm": ("Алгоритмы", "Алгоритм", "Имя"),
    "query": ("Запросы", "Запрос", "Имя"),
    "parameter": ("Параметры", "Параметр", "Имя"),
}
_NESTED_TAGS = {"pks": "Свойство", "pks_group": "Группа", "pkz": "Значение"}
# Групповая правка вложенных правил одного ПКО и правил верхнего уровня ПКО и ПВД.
_NESTED_BATCH = frozenset(_NESTED_TAGS)
_TOP_BATCH = frozenset({"pko", "pvd"})
_BATCH_KINDS = _NESTED_BATCH | _TOP_BATCH
_FILTER_KEYS = frozenset({"source_kind", "target_kind", "group"})
# Виды объектов из `KINDS` (`info[1]`) — единственные допустимые значения фильтра.
_OBJECT_KINDS = tuple(info[1] for info in _METADATA_KINDS.values())
_OBJECT_KIND_LIST = ", ".join(_OBJECT_KINDS)
# Первые 45 символов имени — длина кода в `СформироватьУникальныйКодДляСправочника`
# (`CommonModules/ОбщегоНазначения/Ext/Module.bsl:200`).
_CODE_LIMIT = 45
_CODE_SUFFIX = 5
_ASCII_DIGITS = frozenset("0123456789")
# Общие поля пачки одинаковы у всех ПКО; код и стороны задаёт пара.
_BATCH_PKO_FIELDS = frozenset({"Код", "Источник", "Приемник"})
_KINDS = set(_TOP) | set(_NESTED_TAGS)
# Группа списка при создании — только у ПКО, ПВД и ПОД. У алгоритма, запроса и параметра
# группы в схеме есть, но параметр `group` для них не задаётся.
_GROUPED = frozenset({"pko", "pvd", "pod"})
_STRUCTURE_KINDS = frozenset({"pko", "pvd", "pod", "pks", "pks_group", "pkz"})
_ATTR_IDENTITY = frozenset({"algorithm", "query", "parameter"})
_REF_FIELDS = {
    "pks": "КодПравилаКонвертации",
    "pks_group": "КодПравилаКонвертации",
    "pvd": "КодПравилаКонвертации",
    "parameter": "ПравилоКонвертации",
}
_SOURCE_SKIPPED = (
    "правка применена; проверка по структуре источника не выполнена: структура не передана"
)
_TARGET_SKIPPED = (
    "правка применена; проверка по структуре приёмника не выполнена: структура не передана"
)

FieldValue = Scalar | Mapping[str, Scalar]


@dataclass(slots=True)
class EditResult:
    """Компактный результат правки: адрес, предупреждения и перечни, без документа.

    `skipped` заполняется у ПКО, ПКС, ПКЗ, ПВД и ПОД, когда структура стороны не передана:
    проверка объектов, свойств и значений тогда не выполняется.
    `warnings` — замечания проверки и заметки об умолчаниях КД, которые создание подставило само
    (`_SYNC_DEFAULT_NOTE`, `_mandatory_default_note`).
    """

    address: str
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Пары кандидатов, которые не стали ПКС (`auto = False`, в том числе «по синониму»).
    not_applied: list[str] = field(default_factory=list)
    # Ссылочный тип приёмника без ровно одного ПКО источника и приёмника.
    unresolved: list[str] = field(default_factory=list)
    # Созданные выключенные ПКС (свойство приёмника без пары).
    disabled: list[str] = field(default_factory=list)
    # Только групповая правка: изменённые поля снимка, `{"old", "new"}`.
    changes: dict[str, dict[str, Scalar | None]] = field(default_factory=dict)


@dataclass(slots=True)
class BatchUpdate:
    """Итог `update_rules`: сколько правил выбрано и результат по каждому."""

    matched: int
    results: list[EditResult]

    @property
    def changed(self) -> int:
        """Правила, у которых значения на деле изменились."""
        return sum(bool(item.changes) for item in self.results)


@dataclass(frozen=True, slots=True)
class _ObjectRef:
    """Объект структуры для пачки ПКО: подпись `Вид.Имя` и имя типа."""

    kind: str
    name: str
    type_name: str
    synonym: str

    @property
    def label(self) -> str:
        return f"{self.kind}.{self.name}"


@dataclass(slots=True)
class PkoBatchItem:
    """Одна пара пачки. `status` — `created` или `skipped`; preview показывает `planned`."""

    source: str
    target: str
    status: str
    reason: str = ""
    code: str = ""
    existing: str = ""
    node: Node | None = None
    result: EditResult | None = None


@dataclass(slots=True)
class PkoBatch:
    """Построенная пачка ПКО и хеш итога. Страницу нарезает вид ответа."""

    mode: str
    preview_hash: str
    items: list[PkoBatchItem]


@dataclass(slots=True)
class _Snap:
    values: dict[str, Scalar]
    attrs: dict[str, Scalar]
    children: dict[str, dict[str, Scalar]]
    child_names: set[str]


@dataclass(slots=True)
class _Inserted:
    """Куда вставлен узел и какие пустые контейнеры появились вместе с ним."""

    container: Node
    created: list[tuple[Node, str]] = field(default_factory=list)


def create_rule(
    rules: ExchangeRules,
    kind_name: str,
    key: str,
    fields: Mapping[str, FieldValue] | None = None,
    *,
    owner: str = "",
    source: sqlite3.Connection | None = None,
    target: sqlite3.Connection | None = None,
    group: str = "",
) -> EditResult:
    """Создаёт правило. `key` — код, имя, путь ПКС или имя значения источника ПКЗ.

    `owner` — код ПКО для ПКС, группы ПКС и ПКЗ. `group` — путь кодов групп списка
    через `/` (`Справочники` или `Справочники/Подгруппа`); пусто — корень списка.
    Только для ПКО, ПВД и ПОД, группа должна уже существовать. `source` и `target` —
    структуры сторон; без них проверка объектов, свойств и значений не выполняется.
    Вид `conversion` не создаётся: экземпляр один и появляется вместе с правилами.
    У ПКС и группы ПКС `Код` и `Порядок`, которых нет в `fields`, подставляются как у
    соседей (`_assign_pks_defaults`). В пустом контейнере `Порядок` равен 0 — как у
    первой строки `_fill_properties`, а не шаг 50: иначе нумерация разошлась бы с
    автонастройкой. ПКС без явного `Обязательное` получает этот атрибут, если имя приёмника
    подобно `%ЭтоГруппа%`; ПКО со ссылочным приёмником, кроме перечисления, без явного
    `СинхронизироватьПоИдентификатору` получает этот флаг (КД ставит его любому новому ПКО:
    `ОбщегоНазначения/Ext/Module.bsl`, `ПриемникПриИзмененииПКО`, 1876–1884). Заметки об
    обоих умолчаниях попадают в `warnings`.
    """
    if kind_name == CONVERSION_KIND:
        raise RuleEditError(
            "События конвертации не создаются отдельно: экземпляр один на файл правил "
            "и появляется вместе с ними. Текст события задаёт rule_update; "
            "пустая строка удаляет событие."
        )
    _require_key(kind_name, key)
    list_group = _list_group(rules, kind_name, group)
    node = Node.new(kind_name, _tag(kind_name))
    _set_identity(node, kind_name, key)
    _reject_identity_mismatch(kind_name, key, fields)
    if fields:
        _apply_fields(node, fields)
    sync_by_id = kind_name == "pko" and _apply_identifier_sync(
        node, _reference_kind(node.values.get(TARGET)), fields
    )
    pko, parent = _place_context(rules, kind_name, key, owner)
    if kind_name in ("pks", "pks_group"):
        _assign_pks_defaults(node, pko, parent, fields)
    _reject_duplicate(rules, kind_name, key, owner, parent, node)
    if kind_name in ("pks", "pks_group"):
        _reject_segment_mismatch(node, key.rpartition("/")[2])
    _check_dangling(rules, kind_name, node)
    inserted = _attach(rules, kind_name, node, pko, parent, list_group)
    try:
        result = _result(kind_name, node, owner, pko, source, target)
        result.warnings.extend(_apply_structure(rules, kind_name, node, source, target))
        if sync_by_id:
            result.warnings.append(_SYNC_DEFAULT_NOTE)
        if _mandatory_default_applied(kind_name, node, fields):
            result.warnings.append(_mandatory_default_note(key))
    except Exception:
        _detach(inserted, node)
        raise
    return result


def update_rule(
    rules: ExchangeRules,
    kind_name: str,
    key: str,
    fields: Mapping[str, FieldValue] | None = None,
    *,
    owner: str = "",
    source: sqlite3.Connection | None = None,
    target: sqlite3.Connection | None = None,
) -> EditResult:
    """Меняет только переданные поля. Вложенные правила и остальные поля не трогает.

    Вид `conversion` меняет только тексты событий. Пустая строка удаляет событие.
    Поля заголовка (версия формата, имена конфигураций, дата и остальные реквизиты)
    этим вызовом не правятся.
    """
    if not fields:
        raise RuleEditError("Нет полей для правки: передайте в fields хотя бы одно поле")
    if kind_name == CONVERSION_KIND:
        return _update_conversion(rules, key, fields)
    _require_key(kind_name, key)
    container, node, pko = _require(rules, kind_name, key, owner)
    old_code = node.code
    previous_target = side_name(node, TARGET)
    snap = _save(node)
    try:
        if fields:
            _apply_fields(node, fields)
        warnings = _after_change(
            rules, kind_name, node, container, pko, old_code, previous_target, source, target
        )
    except Exception:
        _restore(node, snap)
        raise
    result = _result(kind_name, node, owner, pko, source, target)
    result.warnings.extend(warnings)
    return result


def update_rules(
    rules: ExchangeRules,
    kind_name: str,
    fields: Mapping[str, FieldValue],
    *,
    owner: str = "",
    keys: Sequence[str] | None = None,
    except_keys: Sequence[str] = (),
    source: sqlite3.Connection | None = None,
    target: sqlite3.Connection | None = None,
    filter: Mapping[str, Any] | None = None,
    preview: bool = False,
    expected_count: int | None = None,
) -> BatchUpdate:
    """Меняет одни и те же поля у нескольких правил одного вида.

    Вложенные виды — `pks`, `pks_group`, `pkz` одного ПКО (`owner`). `keys` — адреса;
    `keys is None` — все правила этого вида у ПКО, кроме `except_keys`. ПКО и ПВД
    выбираются ровно одним способом: `keys` или `filter` (`source_kind`, `target_kind`,
    `group`). Пустой фильтр — все правила вида. `except_keys` вместе с `keys` у ПКО и
    ПВД — ошибка. Неизвестный код, ключ фильтра или вид объекта отклоняют пачку до правок.

    `preview` выполняет ту же правку и проверки и возвращает узлы по снимкам. `expected_count`
    сверяется с числом выбранных правил до изменений. Правка атомарна: ошибка на любой цели
    возвращает уже затронутые узлы.
    """
    if kind_name == CONVERSION_KIND:
        raise RuleEditError("События конвертации меняются вызовом rule_update, не пакетом")
    if kind_name not in _BATCH_KINDS:
        raise RuleEditError("Для правил верхнего уровня — rule_update по одному")
    _reject_count(expected_count)
    if isinstance(keys, str) or isinstance(except_keys, str):
        raise ValueError("keys и except_keys должны быть списками кодов")
    if kind_name in _TOP_BATCH:
        if owner:
            raise ValueError("Для ПКО и ПВД параметр owner не передаётся")
        if fields and "Код" in fields:
            raise RuleEditError("Поле «Код» в групповой правке ПКО и ПВД не меняется")
        # Узлы, а не коды: при повторе кода (`format.duplicate_code`) правится ровно выбранное.
        loaded: list[tuple[Node, Node, Node | None]] = [
            (container, node, None)
            for container, node in _select_top(rules, kind_name, keys, except_keys, filter)
        ]
    else:
        if filter is not None:
            raise ValueError("Фильтр задаётся только для ПКО и ПВД")
        selected = _select_batch(rules, kind_name, owner, keys, except_keys)
        loaded = _load_nested(rules, kind_name, owner, selected)
    if not loaded:
        raise RuleEditError(_empty_selection(kind_name, keys, filter, except_keys))
    if expected_count is not None and expected_count != len(loaded):
        raise CountMismatchError(
            f"Под выбор попало {len(loaded)} правил, ожидалось {expected_count}", len(loaded)
        )
    snaps = [_save(node) for _, node, _ in loaded]
    sides = load_sides(source, target)
    results: list[EditResult] = []
    for index, (container, node, pko) in enumerate(loaded):
        old_code = node.code
        previous_target = side_name(node, TARGET)
        try:
            if fields:
                _apply_fields(node, fields)
            warnings = _after_change(
                rules,
                kind_name,
                node,
                container,
                pko,
                old_code,
                previous_target,
                source,
                target,
                sides,
            )
        except Exception:
            # Текущая цель могла измениться частично — её снимок тоже возвращается.
            for done in range(index + 1):
                _restore(loaded[done][1], snaps[done])
            raise
        result = _result(kind_name, node, owner, pko, source, target)
        result.warnings.extend(warnings)
        result.changes = _diff_snap(node, snaps[index])
        results.append(result)
    if preview:
        for done, snap in enumerate(snaps):
            _restore(loaded[done][1], snap)
    return BatchUpdate(len(loaded), results)


def _load_nested(
    rules: ExchangeRules, kind_name: str, owner: str, selected: Sequence[str]
) -> list[tuple[Node, Node, Node | None]]:
    """Контейнер, узел и ПКО выбранных вложенных правил в порядке обхода.

    ПКЗ адресуется именем значения источника, а оно может повторяться в разных группах
    значений: берутся все ПКЗ с выбранными именами, а не первая (`_find_pkz`).
    """
    if kind_name != "pkz":
        return [_require(rules, kind_name, key, owner) for key in selected]
    pko = _require_pko(rules, owner)
    values = pko.child("Значения")
    wanted = set(selected)
    if values is None:
        return []
    return [
        (container, node, pko)
        for container, node in _coded_places(values)
        if str(node.get(SOURCE)) in wanted
    ]


def _coded_places(container: Node) -> list[tuple[Node, Node]]:
    """Правила списка с их непосредственным контейнером; группы раскрываются."""
    places: list[tuple[Node, Node]] = []
    for item in container.items:
        if item.is_group:
            places.extend(_coded_places(item))
        else:
            places.append((container, item))
    return places


def object_kind(type_name: object) -> str:
    """Вид объекта по префиксу имени типа из `KINDS`; нет префикса — пустая строка.

    `СправочникСсылка.Валюты` → `Справочник`. `КонстантыНабор`, пустое поле и примитив
    не совпадают ни с одним префиксом и дают пустую строку: такое правило не попадает
    под фильтр вида.
    """
    if not isinstance(type_name, str) or not type_name:
        return ""
    for info in _METADATA_KINDS.values():
        if type_name.startswith(info[4]):
            return info[1]
    return ""


def _reject_count(expected_count: int | None) -> None:
    if isinstance(expected_count, bool):
        raise ValueError("expected_count должен быть целым числом")


def _empty_selection(
    kind_name: str,
    keys: Sequence[str] | None,
    rule_filter: Mapping[str, Any] | None,
    except_keys: Sequence[str],
) -> str:
    parts = [f"вид {kind_name}"]
    if keys is not None:
        parts.append("keys: " + (", ".join(keys) or "пусто"))
    if rule_filter is not None:
        if rule_filter:
            listed = ", ".join(f"{key}={value}" for key, value in rule_filter.items())
        else:
            listed = "{}"
        parts.append(f"filter: {listed}")
    if except_keys:
        parts.append("кроме " + ", ".join(except_keys))
    return "Нечего менять: " + "; ".join(parts)


def _select_top(
    rules: ExchangeRules,
    kind_name: str,
    keys: Sequence[str] | None,
    except_keys: Sequence[str],
    rule_filter: Mapping[str, Any] | None,
) -> list[tuple[Node, Node]]:
    """Контейнеры и узлы ПКО или ПВД в порядке документа. Неясный вход — ошибка до правок.

    Код в `keys` выбирает все правила с этим кодом (при повторе кода их несколько).
    """
    if (keys is None) == (rule_filter is None):
        raise ValueError("Для ПКО и ПВД укажите ровно одно: keys или filter")
    if keys is not None and except_keys:
        raise ValueError("except_keys вместе с keys для ПКО и ПВД не используется")
    section = _section_node(rules, _TOP[kind_name][0])
    places = _coded_places(section) if section is not None else []
    known = {node.code for _container, node in places}
    if rule_filter is not None:
        cleaned = _validate_filter(kind_name, rule_filter)
        allowed: set[int] | None = None
        if "group" in cleaned:
            group_node = _list_group(rules, kind_name, cleaned["group"])
            if group_node is None:
                raise RuleNotFoundError(f"Группа «{cleaned['group']}» не найдена")
            allowed = {id(node) for node in group_node.walk()}
        source_field = "Источник" if kind_name == "pko" else "ОбъектВыборки"
        chosen: list[tuple[Node, Node]] = []
        for container, node in places:
            if allowed is not None and id(node) not in allowed:
                continue
            if (
                "source_kind" in cleaned
                and object_kind(node.get(source_field)) != cleaned["source_kind"]
            ):
                continue
            if "target_kind" in cleaned and object_kind(node.get(TARGET)) != cleaned["target_kind"]:
                continue
            chosen.append((container, node))
        excluded = _require_codes(kind_name, except_keys, known)
        return [place for place in chosen if place[1].code not in excluded]
    wanted = _require_codes(kind_name, keys or (), known)
    return [place for place in places if place[1].code in wanted]


def _require_codes(kind_name: str, keys: Sequence[str], known: set[str]) -> set[str]:
    """Коды из списка. Повторы схлопываются. Неизвестного кода нет — ошибка."""
    found: set[str] = set()
    for key in keys:
        code = rule_code(key)
        if code not in known:
            raise RuleNotFoundError(_missing(kind_name, code, ""))
        found.add(code)
    return found


def _validate_filter(kind_name: str, raw: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ValueError("filter должен быть объектом с ключами source_kind, target_kind, group")
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in _FILTER_KEYS:
            raise ValueError(
                f"Неизвестный ключ фильтра «{key}». Допустимые: source_kind, target_kind, group"
            )
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"Значение фильтра «{key}» должно быть непустой строкой")
        cleaned[key] = value
    if kind_name == "pvd" and "target_kind" in cleaned:
        raise ValueError(
            "У ПВД параметр target_kind не задаётся: приёмник — свойство его ПКО. "
            "Отберите ПВД по кодам ПКО из rules_list"
        )
    for name in ("source_kind", "target_kind"):
        if name in cleaned:
            _require_object_kind(cleaned[name])
    return cleaned


def _require_object_kind(value: str) -> None:
    if value not in _OBJECT_KINDS:
        raise ValueError(f"Неизвестный вид объекта «{value}». Допустимые: {_OBJECT_KIND_LIST}")


def _diff_snap(node: Node, snap: _Snap) -> dict[str, dict[str, Scalar | None]]:
    """Поля снимка, которые правка изменила так, что меняется файл. Равные значения не попадают.

    Сравнивается то, что пишет писатель (`Policy`): флаг «только истина» без значения и
    ложь — одно и то же (ВК:740-748), пустая строка и 0 тега `ADD` не выводятся (ВК:83-95).
    """
    policies = {name: leaf.policy for name, leaf in node.kind.leaves.items()}
    changes = _diff_map(snap.values, node.values, policies)
    attr_policies = {name: attr.policy for name, attr in node.kind.attr_map.items()}
    changes.update(_diff_map(snap.attrs, node.attrs, attr_policies))
    names = list(snap.children)
    names.extend(name for name in node.children if name not in snap.children)
    for name in names:
        child = node.children.get(name)
        new_attrs = child.attrs if child is not None else {}
        child_policies = (
            {key: attr.policy for key, attr in child.kind.attr_map.items()}
            if child is not None
            else {}
        )
        old_attrs = snap.children.get(name, {})
        for attr, pair in _diff_map(old_attrs, new_attrs, child_policies).items():
            changes[f"{name}.{attr}"] = pair
    return changes


def _diff_map(
    before: Mapping[str, Scalar],
    after: Mapping[str, Scalar],
    policies: Mapping[str, Policy],
) -> dict[str, dict[str, Scalar | None]]:
    names = list(before)
    names.extend(name for name in after if name not in before)
    changes: dict[str, dict[str, Scalar | None]] = {}
    for name in names:
        old = before.get(name)
        new = after.get(name)
        policy = policies.get(name, Policy.KEEP)
        if _written(policy, old) != _written(policy, new):
            changes[name] = {"old": old, "new": new}
    return changes


def _written(policy: Policy, value: Scalar | None) -> Scalar | None:
    """Значение так, как его увидит файл правил; `None` — поле не выводится."""
    if policy is Policy.IF_TRUE:
        return True if value is True else None
    if policy is Policy.ADD and not isinstance(value, bool) and value in ("", 0):
        return None
    return value


def delete_rule(
    rules: ExchangeRules,
    kind_name: str,
    key: str,
    *,
    owner: str = "",
    source: sqlite3.Connection | None = None,
    target: sqlite3.Connection | None = None,
) -> EditResult:
    """Удаляет правило. ПКО, на которое ссылаются, не удаляется.

    Вид `conversion` не удаляется: экземпляр один. Событие убирает пустая строка в `rule_update`.
    """
    if kind_name == CONVERSION_KIND:
        raise RuleEditError(
            "События конвертации не удаляются как правило: экземпляр один на файл. "
            "Чтобы убрать событие, передайте в rule_update пустую строку."
        )
    _require_key(kind_name, key)
    container, node, pko = _require(rules, kind_name, key, owner)
    if kind_name == "pko":
        _reject_referenced(rules, node)
    result = _result(kind_name, node, owner, pko, source, target)
    container.items.remove(node)
    return result


def find_rule(rules: ExchangeRules, kind_name: str, key: str, owner: str = "") -> Node:
    """Правило по адресу (Д6); нет такого — `RuleNotFoundError`. Документ не меняется.

    Вид `conversion` — корень правил. Ключ пустой или `Конвертация`.
    """
    if kind_name == CONVERSION_KIND:
        _require_conversion_key(key)
        return rules.root
    _require_key(kind_name, key)
    return _require(rules, kind_name, key, owner)[1]


def create_pko_with_properties(
    rules: ExchangeRules,
    code: str,
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    source_object: str,
    target_object: str,
    fields: Mapping[str, FieldValue] | None = None,
    *,
    group: str = "",
) -> EditResult:
    """Создаёт ПКО пары объектов и ПКС по `property_candidates`.

    ПКС получают пары «точно» и «синоним КД» с `auto = True`. Для табличной части это группа
    с вложенными ПКС. Свойство приёмника без пары становится выключенным ПКС. Пары с
    `auto = False` и «по синониму» не создаются и попадают в `not_applied`. Для ссылочного
    типа приёмника `КодПравилаКонвертации` заполняется, если в правилах ровно одно ПКО
    с такими типами источника и приёмника; иначе поле пустое, а свойство — в `unresolved`.
    Ссылочный приёмник, кроме перечисления, получает `СинхронизироватьПоИдентификатору`,
    если этого поля нет в `fields`. ПКС с именем приёмника `%ЭтоГруппа%` получает `Обязательное`.
    Обе подстановки видны в `warnings`. `group` — путь кодов групп списка ПКО через `/`;
    пусто — корень списка. Группа должна уже существовать.
    """
    if not code:
        raise RuleEditError("Пустой адрес правила")
    list_group = _list_group(rules, "pko", group)
    source_ref = _require_object(source, source_object)
    target_ref = _require_object(target, target_object)
    if _find_coded(rules, "pko", code) is not None:
        raise DuplicateRuleError(f"ПКО с кодом «{code}» уже есть")
    candidates = property_candidates(source, target, source_object, target_object)
    if isinstance(candidates, NotFound):
        # Тот же смысл, что у find_object выше: объекта источника или приёмника нет.
        raise ObjectNotFoundError(candidates.message, candidates.suggestions)
    node, result, sync_by_id = _new_pko_node(code, source_ref, target_ref, fields)
    inserted = _attach(rules, "pko", node, None, None, list_group)
    try:
        # Сгенерированные ПКС не проверяются: ссылка без ПКО остаётся в `unresolved`.
        result.warnings.extend(_apply_structure(rules, "pko", node, source, target))
        if sync_by_id:
            result.warnings.append(_SYNC_DEFAULT_NOTE)
        _fill_pko_properties(node, candidates, rules, result)
    except Exception:
        _detach(inserted, node)
        raise
    return result


@dataclass(frozen=True, slots=True)
class _PairSpec:
    """Пара пачки: объекты сторон и явный код, если его передали."""

    source: _ObjectRef
    target: _ObjectRef
    code: str | None = None


def create_pko_many(
    rules: ExchangeRules,
    source_structure: str,
    target_structure: str,
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    kind: str | None = None,
    pairs: Sequence[Mapping[str, Any]] | None = None,
    fields: Mapping[str, FieldValue] | None = None,
    group: str = "",
    mode: str = "preview",
    expected_preview_hash: str | None = None,
) -> PkoBatch:
    """ПКО и ПКС по многим парам. Сначала все узлы ПКО, затем их ПКС.

    `kind` берёт только пары `auto` с обеими сторонами. `pairs` — решение агента.
    Хеш считается по входам и итогу; статус в хеше — `created`, ответ preview
    показывает `planned`. `offset` и `limit` в хеш не входят: страницу режет вид ответа.
    Предупреждения тоже не входят: они выводятся из тех же узлов.
    """
    if mode not in ("preview", "apply"):
        raise ValueError(f"Режим «{mode}» не принимается. Допустимые: preview, apply")
    if (kind is None) == (pairs is None):
        raise ValueError("Укажите ровно одно: kind или pairs")
    if kind is not None and not isinstance(kind, str):
        raise ValueError("kind должен быть строкой")
    _reject_batch_pko_fields(fields)
    list_group = _list_group(rules, "pko", group)
    specs = _resolve_pair_specs(source, target, kind, pairs)
    inserted: list[tuple[_Inserted, Node]] = []
    items: list[PkoBatchItem] = []
    sides = load_sides(source, target)

    def rollback() -> None:
        for place, node in reversed(inserted):
            _detach(place, node)

    try:
        for spec in specs:
            existing = _pko_by_types(rules, spec.source.type_name, spec.target.type_name)
            if existing is not None:
                items.append(
                    PkoBatchItem(
                        spec.source.label,
                        spec.target.label,
                        "skipped",
                        reason="exists",
                        existing=existing.code,
                    )
                )
                continue
            explicit = spec.code
            if explicit is not None and _code_taken(rules, explicit):
                items.append(
                    PkoBatchItem(
                        spec.source.label,
                        spec.target.label,
                        "skipped",
                        reason="code_taken",
                        code=explicit,
                    )
                )
                continue
            base = _pko_code_base(spec.target.type_name)
            code = explicit if explicit is not None else _allocate_pko_code(rules, base)
            node, result, sync_by_id = _new_pko_node(code, spec.source, spec.target, fields)
            inserted.append((_attach(rules, "pko", node, None, None, list_group), node))
            result.warnings.extend(_apply_structure(rules, "pko", node, source, target, sides))
            if sync_by_id:
                result.warnings.append(_SYNC_DEFAULT_NOTE)
            if explicit is None and code != base:
                result.warnings.append(f"код «{base}» занят — взят «{code}»")
            items.append(
                PkoBatchItem(
                    spec.source.label,
                    spec.target.label,
                    "created",
                    code=node.code,
                    node=node,
                    result=result,
                )
            )
        for item in items:
            if item.status != "created" or item.node is None or item.result is None:
                continue
            candidates = property_candidates(source, target, item.source, item.target)
            if isinstance(candidates, NotFound):
                raise ObjectNotFoundError(candidates.message, candidates.suggestions)
            _fill_pko_properties(item.node, candidates, rules, item.result)
    except Exception:
        rollback()
        raise
    digest = _pko_batch_hash(source_structure, target_structure, kind, pairs, fields, group, items)
    if mode == "preview":
        rollback()
    elif expected_preview_hash != digest:
        rollback()
        raise PreviewRequiredError(digest)
    return PkoBatch(mode, digest, items)


def _require_object(conn: sqlite3.Connection, name: str) -> _ObjectRef:
    row = find_object(conn, name)
    if isinstance(row, NotFound):
        raise ObjectNotFoundError(row.message, row.suggestions)
    return _ObjectRef(
        str(row["kind"]), str(row["name"]), str(row["type_name"]), str(row["synonym"])
    )


def _new_pko_node(
    code: str,
    source_ref: _ObjectRef,
    target_ref: _ObjectRef,
    fields: Mapping[str, FieldValue] | None,
) -> tuple[Node, EditResult, bool]:
    """Узел ПКО без ПКС: наименование, стороны, поля и синхронизация по идентификатору."""
    _reject_identity_mismatch("pko", code, fields)
    # глНаименованиеПКО (ОбщегоНазначения, 113): тип источника, двоеточие, синоним.
    node = Node.new("pko", "Правило")
    node.values["Код"] = code
    node.values["Наименование"] = f"{source_ref.kind}: {source_ref.synonym}"
    node.values[SOURCE] = source_ref.type_name
    node.values[TARGET] = target_ref.type_name
    if fields:
        _apply_fields(node, fields)
    sync_by_id = _apply_identifier_sync(node, target_ref.kind, fields)
    return node, EditResult(rule_address(node)), sync_by_id


def _fill_pko_properties(
    node: Node, candidates: list[Candidate], rules: ExchangeRules, result: EditResult
) -> None:
    """ПКС узла по уже полученным кандидатам."""
    properties = Node.new("pks_list", "Свойства")
    node.children["Свойства"] = properties
    _fill_properties(properties, candidates, rules, "", result)


def _reject_batch_pko_fields(fields: Mapping[str, FieldValue] | None) -> None:
    if not fields:
        return
    banned = [name for name in fields if name in _BATCH_PKO_FIELDS]
    if not banned:
        return
    names = ", ".join(f"«{name}»" for name in banned)
    raise RuleEditError(f"Поля {names} в массовом создании задаются парами, а не общими fields")


def _resolve_pair_specs(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    kind: str | None,
    pairs: Sequence[Mapping[str, Any]] | None,
) -> list[_PairSpec]:
    if kind is not None:
        return _specs_from_kind(source, target, kind)
    return _specs_from_pairs(source, target, pairs)


def _specs_from_kind(
    source: sqlite3.Connection, target: sqlite3.Connection, kind: str
) -> list[_PairSpec]:
    present_source = _present_kinds(source)
    present_target = _present_kinds(target)
    if kind not in present_source and kind not in present_target:
        source_list = ", ".join(present_source) or "нет"
        target_list = ", ".join(present_target) or "нет"
        raise ValueError(
            f"Вида «{kind}» нет в структурах. "
            f"Виды источника: {source_list}. Виды приёмника: {target_list}"
        )
    report = object_candidates_report(source, target, kind, composition=False)
    specs: list[_PairSpec] = []
    for candidate in report.candidates:
        if not candidate.auto or candidate.source is None or candidate.target is None:
            continue
        specs.append(_PairSpec(_ref_from_side(candidate.source), _ref_from_side(candidate.target)))
    return specs


def _ref_from_side(side: Side) -> _ObjectRef:
    return _ObjectRef(side.kind, side.name, side.path, side.synonym)


def _present_kinds(conn: sqlite3.Connection) -> list[str]:
    """Виды объектов с теми же условиями, что `_objects` в кандидатах."""
    rows = conn.execute(
        "SELECT DISTINCT kind FROM objects "
        "WHERE is_group = 0 AND kind <> '' AND type_name LIKE '%.%' ORDER BY kind"
    )
    return [str(row[0]) for row in rows]


def _specs_from_pairs(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    pairs: Sequence[Mapping[str, Any]] | None,
) -> list[_PairSpec]:
    parsed = _parse_pairs(pairs)
    missing: list[str] = []
    suggestions: list[str] = []
    found: list[tuple[_ObjectRef | None, _ObjectRef | None, str | None]] = []
    for spec in parsed:
        source_ref = _lookup_ref(source, spec["source"], "источника", missing, suggestions)
        target_ref = _lookup_ref(target, spec["target"], "приёмника", missing, suggestions)
        found.append((source_ref, target_ref, spec.get("code")))
    if missing:
        raise ObjectNotFoundError("Объекты не найдены: " + "; ".join(missing), suggestions)
    specs: list[_PairSpec] = []
    for source_ref, target_ref, code in found:
        if source_ref is None or target_ref is None:
            continue
        specs.append(_PairSpec(source_ref, target_ref, code))
    return specs


def _lookup_ref(
    conn: sqlite3.Connection,
    name: str,
    side: str,
    missing: list[str],
    suggestions: list[str],
) -> _ObjectRef | None:
    row = find_object(conn, name)
    if isinstance(row, NotFound):
        missing.append(f"{side} «{name}»")
        for item in row.suggestions:
            if item not in suggestions:
                suggestions.append(item)
        return None
    return _ObjectRef(
        str(row["kind"]), str(row["name"]), str(row["type_name"]), str(row["synonym"])
    )


def _parse_pairs(pairs: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    if pairs is None or isinstance(pairs, (str, bytes)) or not isinstance(pairs, Sequence):
        raise ValueError("pairs должен быть списком пар {source, target, code?}")
    parsed: list[dict[str, str]] = []
    for index, item in enumerate(pairs, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"Пара {index} должна быть объектом {{source, target, code?}}")
        unknown = [str(key) for key in item if key not in {"source", "target", "code"}]
        if unknown:
            names = ", ".join(f"«{key}»" for key in unknown)
            raise ValueError(
                f"Пара {index}: неизвестные ключи {names}. Допустимые: source, target, code"
            )
        if "source" not in item or "target" not in item:
            raise ValueError(f"Пара {index}: нужны source и target")
        source_name = item["source"]
        target_name = item["target"]
        if not isinstance(source_name, str) or source_name.strip() == "":
            raise ValueError(f"Пара {index}: source должен быть непустой строкой")
        if not isinstance(target_name, str) or target_name.strip() == "":
            raise ValueError(f"Пара {index}: target должен быть непустой строкой")
        spec = {"source": source_name, "target": target_name}
        if "code" in item:
            code = item["code"]
            if not isinstance(code, str) or code.strip() == "":
                raise ValueError(f"Пара {index}: code должен быть непустой строкой")
            spec["code"] = code
        parsed.append(spec)
    return parsed


def _pko_by_types(rules: ExchangeRules, source_type: str, target_type: str) -> Node | None:
    source_key = rule_code(source_type)
    target_key = rule_code(target_type)
    for item in _walk_coded(rules, "pko"):
        if rule_code(item.get(SOURCE)) == source_key and rule_code(item.get(TARGET)) == target_key:
            return item
    return None


def _code_taken(rules: ExchangeRules, code: str) -> bool:
    folded = rule_code(code).casefold()
    return any(item.casefold() == folded for item in _pko_list_codes(rules))


def _pko_list_codes(rules: ExchangeRules) -> list[str]:
    """Коды ПКО и групп списка ПКО: КД ищет занятый код по всему справочнику, группы тоже
    (`СформироватьУникальныйКодДляСправочника`, ОбщегоНазначения:206-216, 234-245)."""
    section = _section_node(rules, _TOP["pko"][0])
    return _list_codes(section) if section is not None else []


def _list_codes(container: Node) -> list[str]:
    codes: list[str] = []
    for item in container.items:
        codes.append(item.code)
        if item.is_group:
            codes.extend(_list_codes(item))
    return codes


def _pko_code_base(type_name: str) -> str:
    """Имя приёмника без вида, не длиннее 45 символов (`глИмяОбъекта`, ОбщегоНазначения:185-189)."""
    _head, dot, tail = type_name.partition(".")
    if not dot:
        return ""
    return tail.strip()[:_CODE_LIMIT]


def _allocate_pko_code(rules: ExchangeRules, base: str) -> str:
    """Свободный код ПКО: основа или суффикс из пяти цифр, пока код не свободен.

    Занятая основа ищет наибольший (сравнением строк) код вида `<основа>[0-9]…`
    (`СформироватьУникальныйКодДляСправочника`, ОбщегоНазначения:198-302). Если этот
    код тоже занят (`Валюты1` больше `Валюты00002` как строка), число увеличивается.
    """
    if not base:
        raise RuleEditError("Не удалось построить код ПКО: у приёмника пустое имя")
    if not _code_taken(rules, base):
        return base
    folded_base = base.casefold()
    matched: list[str] = []
    for code in _pko_list_codes(rules):
        folded = code.casefold()
        if not folded.startswith(folded_base):
            continue
        rest = folded[len(folded_base) :]
        if rest and rest[0] in _ASCII_DIGITS:
            matched.append(code)
    if not matched:
        candidate = base + "00001"
    else:
        greatest = max(matched)
        prefix, digits = _split_trailing_digits(greatest)
        candidate = greatest + "00001" if not digits else prefix + _code_suffix(int(digits) + 1)
    seen: set[str] = set()
    while _code_taken(rules, candidate):
        if candidate in seen:
            raise RuleEditError(f"Не удалось подобрать свободный код ПКО для основы «{base}»")
        seen.add(candidate)
        candidate = _bump_code(candidate)
    return candidate


def _split_trailing_digits(code: str) -> tuple[str, str]:
    index = len(code)
    while index > 0 and code[index - 1] in _ASCII_DIGITS:
        index -= 1
    return code[:index], code[index:]


def _code_suffix(number: int) -> str:
    return str(number).zfill(_CODE_SUFFIX)[-_CODE_SUFFIX:]


def _bump_code(code: str) -> str:
    prefix, digits = _split_trailing_digits(code)
    if not digits:
        return code + "00001"
    return prefix + _code_suffix(int(digits) + 1)


def _pko_batch_hash(
    source_structure: str,
    target_structure: str,
    kind: str | None,
    pairs: Sequence[Mapping[str, Any]] | None,
    fields: Mapping[str, FieldValue] | None,
    group: str,
    items: list[PkoBatchItem],
) -> str:
    payload = {
        "source_structure": source_structure,
        "target_structure": target_structure,
        "kind": kind,
        "pairs": None if pairs is None else [dict(item) for item in pairs],
        "fields": None if fields is None else dict(fields),
        "group": group,
        "items": [_item_canon(item) for item in items],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _item_canon(item: PkoBatchItem) -> dict[str, Any]:
    return {
        "status": item.status,
        "reason": item.reason or None,
        "code": item.code or None,
        "existing": item.existing or None,
        "source": item.source,
        "target": item.target,
        "node": None if item.node is None else _node_canon(item.node),
    }


def _node_canon(node: Node) -> dict[str, Any]:
    return {
        "kind": node.kind.name,
        "tag": node.tag,
        "attrs": dict(node.attrs),
        "values": dict(node.values),
        "text": node.text,
        "children": {name: _node_canon(child) for name, child in node.children.items()},
        "items": [_node_canon(child) for child in node.items],
    }


# --- События конвертации -----------------------------------------------------------------------


def _require_conversion_key(key: str) -> None:
    """Ключ единственного правила событий: пустая строка или `Конвертация`."""
    if key in ("", CONVERSION_ADDRESS):
        return
    raise RuleNotFoundError(
        f"События конвертации адресуются как «{CONVERSION_ADDRESS}» или пустым ключом"
    )


def _update_conversion(
    rules: ExchangeRules,
    key: str,
    fields: Mapping[str, FieldValue] | None,
) -> EditResult:
    """Меняет тексты событий корня. Пустая строка снимает событие. Заголовок не трогает."""
    _require_conversion_key(key)
    if fields:
        _apply_conversion_fields(rules.root, fields)
    return EditResult(CONVERSION_ADDRESS)


def _apply_conversion_fields(root: Node, fields: Mapping[str, FieldValue]) -> None:
    """Проверяет имена до записи: неизвестное событие и поле заголовка отклоняют весь вызов."""
    unknown = [
        name for name in fields if name not in _CONVERSION_EVENTS and name not in _CONVERSION_HEADER
    ]
    if unknown:
        allowed = ", ".join(CONVERSION_EVENTS)
        names = ", ".join(f"«{name}»" for name in unknown)
        noun = "событие" if len(unknown) == 1 else "события"
        raise ValueError(f"Неизвестное {noun} конвертации: {names}. Допустимые: {allowed}")
    header = [name for name in fields if name in _CONVERSION_HEADER]
    if header:
        names = ", ".join(f"«{name}»" for name in header)
        raise RuleEditError(
            f"Поля заголовка правил {names} этим инструментом не правятся "
            "(версия формата, имена конфигураций, дата и остальные реквизиты конвертации). "
            "rule_update вида conversion меняет только тексты событий; "
            "пустая строка удаляет событие."
        )
    for name, value in fields.items():
        if not isinstance(value, str):
            raise ValueError(
                f"Текст события «{name}» должен быть строкой; пустая строка удаляет событие"
            )
        if value == "":
            root.values.pop(name, None)
        else:
            root.values[name] = value


# --- Поля --------------------------------------------------------------------------------------


def _tag(kind_name: str) -> str:
    if kind_name == "pro":
        raise RuleEditError(
            "Вид pro есть только у проекта правил регистрации и правками не меняется: "
            "правила регистрации строит registration_build, читает rules_get"
        )
    if kind_name in _TOP:
        return _TOP[kind_name][1]
    if kind_name in _NESTED_TAGS:
        return _NESTED_TAGS[kind_name]
    raise RuleEditError(f"Неизвестный вид правила «{kind_name}»")


def _require_key(kind_name: str, key: str) -> None:
    _tag(kind_name)
    if not key:
        raise RuleEditError("Пустой адрес правила")


def _set_identity(node: Node, kind_name: str, key: str) -> None:
    if kind_name in _ATTR_IDENTITY:
        node.attrs["Имя"] = key
    elif kind_name in _TOP:
        node.values["Код"] = key
    elif kind_name == "pkz":
        node.values[SOURCE] = key


def _reject_identity_mismatch(
    kind_name: str, key: str, fields: Mapping[str, FieldValue] | None
) -> None:
    if not fields:
        return
    if kind_name == "pkz":
        name: str | None = SOURCE
    elif kind_name in _ATTR_IDENTITY:
        name = "Имя"
    elif kind_name in _TOP:
        name = "Код"
    else:
        name = None
    if name is None or name not in fields or fields[name] == key:
        return
    raise RuleEditError(f"Поле «{name}» «{fields[name]}» не совпадает с адресом «{key}»")


def _apply_fields(node: Node, fields: Mapping[str, FieldValue]) -> None:
    for name, value in fields.items():
        if name in node.kind.children and node.kind.children[name].kind == "pks_side":
            _apply_side(node, name, value)
        elif name in node.kind.attr_map:
            node.attrs[name] = _coerce(node.kind.attr_map[name].type, value, node, name)
        elif name in node.kind.leaves:
            node.values[name] = _coerce(node.kind.leaves[name].type, value, node, name)
        else:
            title = "сторону ПКС" if node.kind.name == "pks_side" else node.kind.title
            raise UnknownFieldError(f"Поле «{name}» не входит в {title}")


def _apply_side(node: Node, name: str, value: FieldValue) -> None:
    if not isinstance(value, Mapping):
        raise RuleEditError(f"Поле «{name}» у {node.kind.title} задаётся атрибутами Имя, Вид и Тип")
    side = node.child(name)
    if side is None:
        side = Node.new("pks_side", name)
        node.children[name] = side
    _apply_fields(side, value)


def _coerce(value_type: ValueType, value: FieldValue, node: Node, name: str) -> Scalar:
    if isinstance(value, Mapping):
        raise RuleEditError(f"Поле «{name}» у {node.kind.title} — простое значение")
    if value_type is ValueType.STR:
        if isinstance(value, str):
            return value
        # `Код` ПКС в правилах КД — целое (`tests/data/exchange_rules.xml`); число не
        # подменяется строковым приведением и остаётся как передано.
        if (
            name == "Код"
            and node.kind.name in ("pks", "pks_group")
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            return value
        raise RuleEditError(f"Поле «{name}» у {node.kind.title}: ожидается строка")
    if value_type is ValueType.BOOL:
        if isinstance(value, bool):
            return value
        if value in ("true", "false"):
            return value == "true"
        raise RuleEditError(f"Поле «{name}» у {node.kind.title}: ожидается да или нет")
    if isinstance(value, str) and _is_int(value):
        return int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleEditError(f"Поле «{name}» у {node.kind.title}: ожидается целое число")
    return value


def _is_int(value: str) -> bool:
    digits = value[1:] if value.startswith("-") else value
    return bool(digits) and digits.isdigit()


def _save(node: Node) -> _Snap:
    return _Snap(
        dict(node.values),
        dict(node.attrs),
        {name: dict(child.attrs) for name, child in node.children.items()},
        set(node.children),
    )


def _restore(node: Node, snap: _Snap) -> None:
    node.values.clear()
    node.values.update(snap.values)
    node.attrs.clear()
    node.attrs.update(snap.attrs)
    for name in list(node.children):
        if name not in snap.child_names:
            del node.children[name]
    for name, attrs in snap.children.items():
        child = node.children.get(name)
        if child is not None:
            child.attrs.clear()
            child.attrs.update(attrs)


# --- Поиск и вставка ---------------------------------------------------------------------------


def _section_node(rules: ExchangeRules, section: str) -> Node | None:
    return rules.root.children.get(section)


def _walk_coded(rules: ExchangeRules, kind_name: str) -> list[Node]:
    root = _section_node(rules, _TOP[kind_name][0])
    return list(root.walk()) if root is not None else []


def _find_coded(rules: ExchangeRules, kind_name: str, key: str) -> tuple[Node, Node] | None:
    root = _section_node(rules, _TOP[kind_name][0])
    return _find_in(root, key) if root is not None else None


def _find_in(container: Node, key: str) -> tuple[Node, Node] | None:
    for item in container.items:
        if not item.is_group and item.code == rule_code(key):
            return container, item
        if item.is_group:
            found = _find_in(item, key)
            if found is not None:
                return found
    return None


def _find_pkz(container: Node, source_name: str) -> tuple[Node, Node] | None:
    for item in container.items:
        if item.is_group:
            found = _find_pkz(item, source_name)
            if found is not None:
                return found
        elif str(item.get(SOURCE)) == source_name:
            return container, item
    return None


def _find_pks(
    container: Node, path: str, prefix: str = "", owner: str = ""
) -> tuple[Node, Node] | None:
    """Контейнер и узел по пути ПКС. Голое имя при нескольких кандидатах — ошибка."""
    segment, _, rest = path.partition("/")
    found = pks_candidates(container, segment)
    if len(found) > 1:
        addresses = [
            pks_address(owner, f"{prefix}{built}") if owner else f"{prefix}{built}"
            for built, _item in found
        ]
        listed = ", ".join(addresses)
        raise AmbiguousAddressError(
            f"Адрес «{prefix}{segment}» подходит нескольким правилам: {listed}"
        )
    if not found:
        return None
    built, item = found[0]
    item_path = f"{prefix}{built}"
    if not rest:
        return container, item
    if not item.is_group:
        return None
    return _find_pks(item, rest, f"{item_path}/", owner)


def _require_pko(rules: ExchangeRules, owner: str) -> Node:
    if not owner:
        raise RuleEditError("Для ПКС и ПКЗ нужен код ПКО")
    found = _find_coded(rules, "pko", owner)
    if found is None:
        raise RuleNotFoundError(f"ПКО «{owner}» не найдено")
    return found[1]


def _child_list(owner_node: Node, tag: str, *, create: bool) -> Node | None:
    child = owner_node.child(tag)
    if child is None and create:
        child = Node.new(owner_node.kind.children[tag].kind, tag)
        owner_node.children[tag] = child
    return child


def _place_context(
    rules: ExchangeRules, kind_name: str, key: str, owner: str
) -> tuple[Node | None, Node | None]:
    """ПКО-владелец и контейнер для нового правила.

    Контейнер `None` у вложенного правила значит, что список (`Свойства` или `Значения`)
    ещё не создан и появится при вставке.
    """
    if kind_name in _TOP:
        return None, None
    pko = _require_pko(rules, owner)
    if kind_name == "pkz":
        return pko, _child_list(pko, "Значения", create=False)
    properties = pko.child("Свойства")
    parent_path, _, _last = key.rpartition("/")
    if not parent_path:
        return pko, properties
    if properties is None:
        raise RuleNotFoundError(f"Группа ПКС «{parent_path}» в ПКО «{pko.code}» не найдена")
    found = _find_pks(properties, parent_path, owner=pko.code)
    if found is None or not found[1].is_group:
        raise RuleNotFoundError(f"Группа ПКС «{parent_path}» в ПКО «{pko.code}» не найдена")
    return pko, found[1]


def _assign_pks_defaults(
    node: Node,
    pko: Node | None,
    parent: Node | None,
    fields: Mapping[str, FieldValue] | None,
) -> None:
    """Подставляет `Код`, `Порядок` и `Обязательное` ПКС, если этих полей нет в `fields`.

    `Порядок` — среди прямых ПКС и групп того же контейнера (`Свойства` или группа):
    максимум плюс `_ORDER_STEP`. Пустой контейнер и контейнер, где `Порядок` ни у кого
    не задан, получают 0. Так же начинает `_fill_properties` (`order = 0`, затем шаг),
    поэтому ПКС из `create_rule` и из автонастройки нумеруются одинаково: 0, 50, 100, ….
    `Код` — наибольшее целое среди всех ПКС ПКО (`walk_pks` раскрывает группы) плюс 1;
    нечисловые коды пропускаются; если целых нет — 1. В модель код пишется строкой,
    как поле схемы и как `<Код>1</Код>` в выгрузке.
    `Обязательное` — у ПКС (не у группы), если имя приёмника подобно `%ЭтоГруппа%`.
    """
    given = fields or {}
    if "Порядок" not in given:
        node.values["Порядок"] = _next_pks_order(parent)
    if "Код" not in given:
        node.values["Код"] = str(_next_pks_code(pko))
    if _MANDATORY not in given:
        _apply_group_mandatory(node)


def _apply_identifier_sync(
    node: Node, target_kind: str, fields: Mapping[str, FieldValue] | None
) -> bool:
    """Ставит синхронизацию по идентификатору, если поле не задано явно.

    Условие КД (`ОпределитьНужнаСинхронизацияПоИдентификатору`, 1907–1924): обе стороны —
    приложения 8 (для структур сервера это так), приёмник ссылочный и не перечисление
    (`глЕстьСсылка`, 350–370). Возвращает истину, только если умолчание подставлено.
    """
    if fields and _SYNC_BY_ID in fields:
        return False
    if target_kind not in _SYNC_REFERENCE_KINDS:
        return False
    node.values[_SYNC_BY_ID] = True
    return True


def _reference_kind(type_name: object) -> str:
    """Вид объекта по имени ссылочного типа: `СправочникСсылка.Валюты` → `Справочник`.

    Для `create_rule`, где приёмник задан именем типа в `fields`, а структуры может не быть.
    Нессылочный тип (`РегистрСведенийЗапись.Цены`, `Строка`) и пустое значение дают пустую строку.
    """
    if not isinstance(type_name, str):
        return ""
    kind, mark, _ = type_name.partition("Ссылка.")
    return kind if mark else ""


def _apply_group_mandatory(node: Node) -> bool:
    """Атрибут `Обязательное`, если имя приёмника ПОДОБНО «%ЭтоГруппа%» (ВК:1214–1216, 627).

    Группа ПКС атрибута не имеет: писатель выставляет его только у свойства (ВК:617–628).
    Сравнение без учёта регистра, как `ПОДОБНО` в запросе КД.
    """
    if node.kind.name != "pks":
        return False
    if not _is_group_property_name(side_name(node, TARGET)):
        return False
    node.attrs[_MANDATORY] = True
    return True


def _is_group_property_name(name: str) -> bool:
    """Имя свойства приёмника содержит «ЭтоГруппа» (`ПОДОБНО "%ЭтоГруппа%"`, ВК:1214–1216)."""
    return _GROUP_PROPERTY_MARK in name.casefold()


def _mandatory_default_applied(
    kind_name: str, node: Node, fields: Mapping[str, FieldValue] | None
) -> bool:
    """Умолчание `Обязательное` подставлено, а не передано в `fields`."""
    if kind_name != "pks" or (fields and _MANDATORY in fields):
        return False
    return node.attrs.get(_MANDATORY) is True


def _mandatory_default_note(path: str) -> str:
    """Заметка ответа: у созданной ПКС включено `Обязательное`."""
    return f"ПКС «{path}»: Обязательное включено по умолчанию"


def _next_pks_order(parent: Node | None) -> int:
    """Следующий `Порядок` прямых элементов контейнера; пустой контейнер — 0."""
    if parent is None:
        return 0
    numbers: list[int] = []
    for item in parent.items:
        number = _optional_int(item.values.get("Порядок"))
        if number is not None:
            numbers.append(number)
    if not numbers:
        return 0
    return max(numbers) + _ORDER_STEP


def _next_pks_code(pko: Node | None) -> int:
    """Следующий числовой `Код` ПКС этого ПКО; нечисловые не считаются."""
    properties = pko.child("Свойства") if pko is not None else None
    if properties is None:
        return 1
    numbers: list[int] = []
    for _path, item in walk_pks(properties):
        number = _optional_int(item.values.get("Код"))
        if number is not None:
            numbers.append(number)
    return max(numbers) + 1 if numbers else 1


def _optional_int(value: object) -> int | None:
    """Целое из поля правила; пустое и нечисловое — `None`."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _is_int(text):
            return int(text)
    return None


def _require(
    rules: ExchangeRules, kind_name: str, key: str, owner: str
) -> tuple[Node, Node, Node | None]:
    """Контейнер, узел и ПКО-владелец."""
    if kind_name in _TOP:
        found = _find_coded(rules, kind_name, key)
        if found is None:
            raise RuleNotFoundError(_missing(kind_name, key, owner))
        return found[0], found[1], None
    pko = _require_pko(rules, owner)
    if kind_name == "pkz":
        values = pko.child("Значения")
        found_pkz = _find_pkz(values, key) if values is not None else None
        if found_pkz is None:
            raise RuleNotFoundError(_missing(kind_name, key, owner))
        return found_pkz[0], found_pkz[1], pko
    properties = pko.child("Свойства")
    found_pks = _find_pks(properties, key, owner=pko.code) if properties is not None else None
    if found_pks is None:
        raise RuleNotFoundError(_missing(kind_name, key, owner))
    if found_pks[1].kind.name != kind_name:
        raise RuleNotFoundError(
            f"По пути «{key}» в ПКО «{pko.code}» находится {found_pks[1].kind.title}"
        )
    return found_pks[0], found_pks[1], pko


def _nested_keys(rules: ExchangeRules, kind_name: str, owner: str) -> list[str]:
    """Адреса вложенных правил вида `kind_name` у ПКО в порядке обхода документа.

    ПКС и группы ПКС — пути `walk_pks` (группы раскрываются). ПКЗ — имя значения
    источника, группы значений пропускаются.
    """
    pko = _require_pko(rules, owner)
    if kind_name == "pkz":
        values = pko.child("Значения")
        if values is None:
            return []
        return [str(node.get(SOURCE)) for node in values.walk()]
    properties = pko.child("Свойства")
    if properties is None:
        return []
    return [path for path, item in walk_pks(properties) if item.kind.name == kind_name]


def _select_batch(
    rules: ExchangeRules,
    kind_name: str,
    owner: str,
    keys: Sequence[str] | None,
    except_keys: Sequence[str],
) -> list[str]:
    """Адреса целей в порядке обхода. Неизвестный адрес — ошибка, документ не меняется.

    ПКС и группы сравниваются через те же звенья, что `walk_pks`: голое имя при нескольких
    кандидатах отклоняется, квалифицированное попадает ровно в одно правило.
    """
    available = _nested_keys(rules, kind_name, owner)
    if kind_name == "pkz":
        known = set(available)
        if keys is None:
            _reject_unknown(rules, kind_name, owner, except_keys, known)
            excluded = set(except_keys)
            return [key for key in available if key not in excluded]
        _reject_unknown(rules, kind_name, owner, keys, known)
        wanted = set(keys)
        return [key for key in available if key in wanted]
    if keys is None:
        excluded = {_canonical_pks_path(rules, kind_name, owner, key) for key in except_keys}
        return [key for key in available if key not in excluded]
    wanted = {_canonical_pks_path(rules, kind_name, owner, key) for key in keys}
    return [key for key in available if key in wanted]


def _canonical_pks_path(rules: ExchangeRules, kind_name: str, owner: str, key: str) -> str:
    """Построенный путь ПКС, который обозначает `key`."""
    _container, node, pko = _require(rules, kind_name, key, owner)
    properties = pko.child("Свойства") if pko is not None else None
    if properties is None:
        raise RuleNotFoundError(_missing(kind_name, key, owner))
    for path, item in walk_pks(properties):
        if item is node:
            return path
    raise RuleNotFoundError(_missing(kind_name, key, owner))


def _reject_unknown(
    rules: ExchangeRules,
    kind_name: str,
    owner: str,
    keys: Sequence[str],
    known: set[str],
) -> None:
    for key in keys:
        if key not in known:
            _require(rules, kind_name, key, owner)


def _missing(kind_name: str, key: str, owner: str) -> str:
    if kind_name == "pko":
        return f"ПКО «{key}» не найдено"
    if kind_name == "pvd":
        return f"ПВД «{key}» не найдено"
    if kind_name == "pod":
        return f"ПОД «{key}» не найдено"
    if kind_name == "algorithm":
        return f"Алгоритм «{key}» не найден"
    if kind_name == "query":
        return f"Запрос «{key}» не найден"
    if kind_name == "parameter":
        return f"Параметр «{key}» не найден"
    if kind_name == "pkz":
        return f"ПКО «{owner}» / ПКЗ {key} не найдено"
    return f"ПКО «{owner}» / ПКС {key} не найдено"


def _reject_duplicate(
    rules: ExchangeRules,
    kind_name: str,
    key: str,
    owner: str,
    parent: Node | None,
    node: Node,
) -> None:
    if kind_name in _TOP:
        if _find_coded(rules, kind_name, key) is not None:
            noun = "именем" if kind_name in _ATTR_IDENTITY else "кодом"
            title = node.kind.title
            raise DuplicateRuleError(f"{title} с {noun} «{key}» уже есть")
        return
    if kind_name == "pkz":
        if parent is not None and _find_pkz(parent, key) is not None:
            raise DuplicateRuleError(f"ПКЗ «{key}» в ПКО «{owner}» уже есть")
        return
    last = key.rpartition("/")[2]
    if parent is not None and _segment_taken(parent, last):
        raise DuplicateRuleError(f"ПКС «{key}» в ПКО «{owner}» уже есть")


def _segment_taken(container: Node, segment: str) -> bool:
    """Базовое имя приёмника уже занято. Квалификатор адреса при создании не учитывается."""
    return any(
        pks_base_segment(item, index) == segment for index, item in enumerate(container.items)
    )


def _reject_segment_mismatch(node: Node, last: str) -> None:
    segment = pks_base_segment(node, 0)
    if segment == last:
        return
    raise RuleEditError(
        f"Путь «{last}» не совпадает с именем свойства приёмника «{segment}»."
        " Адрес ПКС — путь группа/…/свойство-приёмник"
    )


def _list_group(rules: ExchangeRules, kind_name: str, group: str) -> Node | None:
    """Группа верхнего списка по пути кодов; пустой путь — корень списка (`None`).

    Ищется по цепочке `items`: узел `is_group` с `code`, равным сегменту пути.
    Группа не создаётся.
    """
    if not group:
        return None
    if kind_name not in _GROUPED:
        raise RuleEditError(f"Группа «{group}» задаётся только для ПКО, ПВД и ПОД")
    section = _TOP[kind_name][0]
    container = _section_node(rules, section)
    for segment in group.split("/"):
        found: Node | None = None
        if container is not None:
            for item in container.items:
                if item.is_group and item.code == segment:
                    found = item
                    break
        if found is None:
            raise RuleNotFoundError(f"Группа «{group}» в списке {section} не найдена")
        container = found
    return container


def _attach(
    rules: ExchangeRules,
    kind_name: str,
    node: Node,
    pko: Node | None,
    parent: Node | None,
    list_group: Node | None,
) -> _Inserted:
    """Вставляет узел. Пустые контейнеры, созданные ради него, запоминаются для отката."""
    created: list[tuple[Node, str]] = []
    if kind_name in _TOP:
        if list_group is not None:
            container = list_group
        else:
            section = _TOP[kind_name][0]
            if rules.root.children.get(section) is None:
                created.append((rules.root, section))
            container = rules.section(section)
        container.items.append(node)
        return _Inserted(container, created)
    if parent is None:
        if pko is None:
            raise RuleEditError("Некуда добавить правило")
        tag = "Значения" if kind_name == "pkz" else "Свойства"
        if pko.child(tag) is None:
            created.append((pko, tag))
        parent = _child_list(pko, tag, create=True)
    if parent is None:
        raise RuleEditError("Некуда добавить правило")
    parent.items.append(node)
    return _Inserted(parent, created)


def _detach(inserted: _Inserted, node: Node) -> None:
    """Снимает узел и убирает контейнеры, которые появились только для этой вставки."""
    inserted.container.items.remove(node)
    for owner, tag in reversed(inserted.created):
        child = owner.children.get(tag)
        if child is not None and not child.items:
            del owner.children[tag]


def _after_change(
    rules: ExchangeRules,
    kind_name: str,
    node: Node,
    container: Node,
    pko: Node | None,
    old_code: str,
    previous_target: str,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
    sides: dict[str, Structure | None] | None = None,
) -> list[str]:
    if kind_name == "pko" and node.code != old_code:
        refs = _referrers(rules, old_code, node)
        if refs:
            joined = ", ".join(refs)
            raise DanglingReferenceError(
                f"Код ПКО «{old_code}» изменить нельзя: на него ссылаются {joined}"
            )
    if kind_name in _TOP and node.code != old_code:
        for other in _walk_coded(rules, kind_name):
            if other is not node and other.code == node.code:
                noun = "именем" if kind_name in _ATTR_IDENTITY else "кодом"
                raise DuplicateRuleError(f"{node.kind.title} с {noun} «{node.code}» уже есть")
    if kind_name in ("pks", "pks_group"):
        _reject_receiver_clash(container, node, previous_target)
    if kind_name == "pkz" and pko is not None:
        _reject_pkz_clash(container, node)
    _check_dangling(rules, kind_name, node)
    return _apply_structure(rules, kind_name, node, source, target, sides)


def _reject_receiver_clash(container: Node, node: Node, previous: str) -> None:
    """Новое имя приёмника не должно совпасть с соседним.

    Неизменное имя не проверяется: поисковая и обычная ПКС с одним приёмником уже
    могут стоять в документе, и правка других полей такой пары законна.
    """
    name = side_name(node, TARGET)
    if not name or name == previous:
        return
    for item in container.items:
        if item is not node and side_name(item, TARGET) == name:
            raise DuplicateRuleError(f"ПКС «{name}» в этом списке уже есть")


def _reject_pkz_clash(container: Node, node: Node) -> None:
    source_name = str(node.get(SOURCE))
    for item in container.items:
        if item is not node and not item.is_group and str(item.get(SOURCE)) == source_name:
            raise DuplicateRuleError(f"ПКЗ «{source_name}» в этом ПКО уже есть")


def _reject_referenced(rules: ExchangeRules, node: Node) -> None:
    refs = _referrers(rules, node.code, node)
    if not refs:
        return
    raise DanglingReferenceError(
        f"ПКО «{node.code}» удалить нельзя: на него ссылаются {', '.join(refs)}"
    )


def _referrers(rules: ExchangeRules, code: str, skip: Node) -> list[str]:
    wanted = rule_code(code)
    if not wanted:
        return []
    found: list[str] = []
    for pko in rules.pko():
        if pko is skip:
            continue
        properties = pko.child("Свойства")
        if properties is None:
            continue
        for path, item in walk_pks(properties):
            if rule_code(item.get("КодПравилаКонвертации")) == wanted:
                found.append(pks_address(pko.code, path))
    for pvd in rules.pvd():
        if rule_code(pvd.get("КодПравилаКонвертации")) == wanted:
            found.append(rule_address(pvd))
    for item in _parameters(rules):
        if rule_code(item.attrs.get("ПравилоКонвертации", "")) == wanted:
            found.append(rule_address(item))
    return found


def _parameters(rules: ExchangeRules) -> list[Node]:
    root = _section_node(rules, "Параметры")
    return list(root.walk()) if root is not None else []


# --- Висячие ссылки и структуры ---------------------------------------------------------------


def _apply_structure(
    rules: ExchangeRules,
    kind_name: str,
    node: Node,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
    sides: dict[str, Structure | None] | None = None,
) -> list[str]:
    """Замечания `check_rule` по этому узлу. Ошибка отклоняет правку, предупреждение — нет.

    Текст `report.skip` в `skipped` не кладётся: там остаются фразы `_SOURCE_SKIPPED`
    и `_TARGET_SKIPPED`, их сравнивают тесты.
    """
    if kind_name not in _STRUCTURE_KINDS:
        return []
    return _structure_messages(check_rule(rules, node, source, target, sides=sides))


def _structure_messages(report: ValidationReport) -> list[str]:
    """Строки предупреждений. Ошибки — `DanglingReferenceError` с идентификатором проверки.

    `structure.pko_missing` при правке понижается до предупреждения: ПКО ссылочного типа
    агент часто создаёт следующим вызовом, а отказ ломал бы добавление реквизита в уже
    существующее ПКО. `rules_validate` по документу по-прежнему считает это ошибкой.
    """
    warnings: list[str] = []
    errors: list[str] = []
    for issue in report.issues:
        line = f"{issue.check}: {issue.message}"
        if issue.level is Level.WARNING or issue.check == "structure.pko_missing":
            warnings.append(line)
        else:
            errors.append(line)
    if errors:
        raise DanglingReferenceError("; ".join(errors))
    return warnings


def _check_dangling(rules: ExchangeRules, kind_name: str, node: Node) -> None:
    field_name = _REF_FIELDS.get(kind_name)
    if field_name is None:
        return
    ref = _stored(node, field_name)
    if not ref or any(item.code == rule_code(ref) for item in rules.pko()):
        return
    type_side, type_name = _ref_type(kind_name, node)
    listed = rules.pko()
    if type_name:
        listed = [item for item in listed if str(item.get(type_side)) == type_name]
    raise DanglingReferenceError(_dangling_message(field_name, ref, listed, type_name))


def _stored(node: Node, name: str) -> str:
    if name in node.kind.attr_map:
        return str(node.attrs.get(name, "")).strip()
    return str(node.values.get(name, "")).strip()


def _ref_type(kind_name: str, node: Node) -> tuple[str, str]:
    """Сторона ПКО и тип, по которому сужается перечень. Пустой тип — все ПКО."""
    if kind_name in ("pks", "pks_group"):
        side = node.child(TARGET)
        type_name = str(side.attrs.get("Тип", "")).strip() if side is not None else ""
        return TARGET, type_name
    if kind_name == "pvd":
        return SOURCE, str(node.get("ОбъектВыборки")).strip()
    return "", ""


def _dangling_message(field_name: str, ref: str, listed: list[Node], type_name: str) -> str:
    codes = ", ".join(f"«{item.code}»" for item in listed) or "нет"
    if type_name:
        return f"«{field_name}» «{ref}» не найден. ПКО для типа «{type_name}»: {codes}"
    return f"«{field_name}» «{ref}» не найден. Существующие ПКО: {codes}"


def _result(
    kind_name: str,
    node: Node,
    owner: str,
    pko: Node | None,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
) -> EditResult:
    return EditResult(
        _address(kind_name, node, owner, pko),
        skipped=_skipped(kind_name, source, target),
    )


def _skipped(
    kind_name: str, source: sqlite3.Connection | None, target: sqlite3.Connection | None
) -> list[str]:
    if kind_name not in _STRUCTURE_KINDS:
        return []
    skipped: list[str] = []
    if source is None:
        skipped.append(_SOURCE_SKIPPED)
    if target is None:
        skipped.append(_TARGET_SKIPPED)
    return skipped


def _address(kind_name: str, node: Node, owner: str, pko: Node | None) -> str:
    if kind_name in ("pks", "pks_group") and pko is not None:
        properties = pko.child("Свойства")
        if properties is not None:
            for path, item in walk_pks(properties):
                if item is node:
                    return pks_address(pko.code, path)
        return pks_address(owner, "")
    if kind_name == "pkz":
        return pkz_address(owner or (pko.code if pko is not None else ""), str(node.get(SOURCE)))
    return rule_address(node)


# --- Массовые ПКС ------------------------------------------------------------------------------


def _fill_properties(
    container: Node,
    candidates: list[Candidate],
    rules: ExchangeRules,
    prefix: str,
    result: EditResult,
) -> None:
    order = 0
    for candidate in candidates:
        target = candidate.target
        if target is None:
            continue
        path = f"{prefix}{target.name}"
        if candidate.source is not None and not candidate.auto:
            _collect_skipped(candidate, prefix, result)
            continue
        group = _is_group(candidate)
        node = _property_rule(candidate, group)
        node.values["Порядок"] = order
        order += _ORDER_STEP
        if not group and _apply_group_mandatory(node):
            result.warnings.append(_mandatory_default_note(path))
        if candidate.source is None:
            node.attrs["Отключить"] = True
            result.disabled.append(path)
        code = _conversion_code(rules, candidate.source, target, path, result)
        if code:
            node.values["КодПравилаКонвертации"] = code
        container.items.append(node)
        if group:
            _fill_properties(node, list(candidate.children), rules, f"{path}/", result)


def _is_group(candidate: Candidate) -> bool:
    kinds = {side.kind for side in (candidate.source, candidate.target) if side is not None}
    # Группа ПКС — табличная часть или строка с подчинёнными (СохранитьПравилаКС, 802–806).
    return "ТабличнаяЧасть" in kinds or bool(candidate.children)


def _property_rule(candidate: Candidate, group: bool) -> Node:
    node = Node.new("pks_group" if group else "pks", "Группа" if group else "Свойство")
    node.children[SOURCE] = _side_node(SOURCE, candidate.source, group=group)
    node.children[TARGET] = _side_node(TARGET, candidate.target, group=group)
    return node


def _side_node(tag: str, side: Side | None, *, group: bool) -> Node:
    """Сторона ПКС: имя, вид и тип единственного типа (ВК:643–672; у группы без типа, 714–726)."""
    node = Node.new("pks_side", tag)
    if side is None:
        return node
    node.attrs["Имя"] = side.name
    node.attrs["Вид"] = side.kind
    if not group and len(side.types) == 1:
        node.attrs["Тип"] = side.types[0]
    return node


def _conversion_code(
    rules: ExchangeRules,
    source: Side | None,
    target: Side,
    path: str,
    result: EditResult,
) -> str:
    """Код ПКО, если тип приёмника ссылочный и такая пара типов есть ровно у одного ПКО.

    КД ищет ПКО по обоим типам (`ОпределитьПоТипамНаличиеПКО`, 702–710) и берёт первое.
    Здесь при нуле или нескольких совпадениях код остаётся пустым.
    """
    if len(target.types) != 1 or not is_ref(target.types[0]):
        return ""
    target_type = target.types[0]
    source_type = source.types[0] if source is not None and len(source.types) == 1 else ""
    if not source_type:
        result.unresolved.append(
            f"{path}: ссылочный тип приёмника «{target_type}», тип источника не задан"
        )
        return ""
    matches = [
        item
        for item in rules.pko()
        if str(item.get(SOURCE)) == source_type and str(item.get(TARGET)) == target_type
    ]
    if len(matches) == 1:
        return matches[0].code
    codes = ", ".join(f"«{item.code}»" for item in matches) or "нет"
    result.unresolved.append(
        f"{path}: нет однозначного ПКО для «{source_type}» → «{target_type}»"
        f" (найдено {len(matches)}: {codes})"
    )
    return ""


def _collect_skipped(candidate: Candidate, prefix: str, result: EditResult) -> None:
    target = candidate.target
    if target is None:
        return
    path = f"{prefix}{target.name}"
    if candidate.source is not None and not candidate.auto:
        result.not_applied.append(_pair_label(candidate, path))
    for child in candidate.children:
        _collect_skipped(child, f"{path}/", result)


def _pair_label(candidate: Candidate, path: str) -> str:
    source_name = candidate.source.name if candidate.source is not None else ""
    target_name = candidate.target.name if candidate.target is not None else ""
    note = f": {candidate.note}" if candidate.note else ""
    return f"{path}: «{source_name}» → «{target_name}» ({candidate.confidence.value}){note}"
