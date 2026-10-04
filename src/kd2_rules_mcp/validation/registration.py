"""Проверка правил регистрации против структуры конфигурации-источника
(спецификация `rules-validation`, «Проверка правил регистрации»; design.md, Д9).

Читатель БСП — `DataProcessors/ЗагрузкаПравилРегистрацииОбъектов/Ext/ObjectModule.bsl`,
исполнитель — `CommonModules/ОбменДаннымиСобытия/Ext/Module.bsl`,
писатель КД — `reference/kd2-cfg/DataProcessors/ВыгрузкаРегистрации/Ext/ObjectModule.bsl`.

`registration.autoregistration` — предупреждение. При авторегистрации «Разрешить»
`ВыполнитьПравилаРегистрацииОбъектовДляПланаОбмена` при записи не вызывается
(ОбменДаннымиСобытия:1389-1414); признак — `АвтоРегистрацияРазрешена`
(ОбменДаннымиПовтИсп:851-859). Объект при этом регистрирует платформа, поэтому
это не ошибка файла правил.

`registration.no_pvd` — предупреждение. Выборка изменений берёт только метаданные
включённых ПВД (БСП:17931-17938, БСП:18210-18236; `Отключить` снимает `Включить`,
БСП:6768). Квитанция удаляет регистрацию по номеру сообщения (БСП:17439-17445)
и не затрагивает изменение, которое в выборку не попало.
"""

import os
import sqlite3
from dataclasses import dataclass

from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules
from kd2_rules_mcp.structures.queries import (
    exchange_plan_autoregistration,
    read_object_card,
)
from kd2_rules_mcp.structures.xmldump import KINDS
from kd2_rules_mcp.validation.address import rule_address
from kd2_rules_mcp.validation.report import ValidationReport

# Вид метаданных → префикс имени типа КД (`Документ` → `ДокументСсылка.`).
# Так писатель кладёт ОбъектНастройки (Наименование) и ОбъектМетаданныхИмя (Тип.Имя)
# одного объекта (ВыгрузкаРегистрации:212-217) и так тип пишется в структуру (xmldump.KINDS).
_TYPE_PREFIX = {info[1]: info[4] for info in KINDS.values()}

# Примитивы не разыменовываются (MD83Exp:2021-2054, имена как в xmlbuild.PRIMITIVES).
_PRIMITIVES = frozenset(
    {"Число", "Строка", "Дата", "Булево", "ХранилищеЗначения", "УникальныйИдентификатор"}
)

# Не поля шапки узла: состав плана обмена (xmlbuild.plan_content).
_NOT_HEADER = frozenset({"СоставПланаОбмена", "ЭлементСоставаПланаОбмена"})

# Проверки, которым нужна загруженная структура. Без неё — skip, итог с оговоркой.
_STRUCTURE_CHECKS = (
    "registration.exchange_plan",
    "registration.object",
    "registration.plan_membership",
    "registration.plan_property",
    "registration.object_property",
    "registration.unload_mode",
    "registration.plan_content",
    "registration.autoregistration",
    "registration.no_pvd",
)

_NO_STRUCTURE = "структура конфигурации-источника не загружена"
_NO_PLAN_CONTENT = "в менеджере регистрации нет состава плана обмена"
_NO_OBJECT_SETTINGS = "объект настройки менеджера регистрации недоступен"
_NO_EXCHANGE_RULES = "проект правил обмена не передан"


@dataclass(slots=True)
class _Prop:
    """Свойство объекта структуры: вид, группа ли, типы."""

    kind: str
    is_group: bool
    types: tuple[str, ...]
    unresolved: tuple[str, ...]


@dataclass(slots=True)
class _Obj:
    """Объект метаданных и его свойства по пути (`Реквизит`, `ТабличнаяЧасть.Реквизит`)."""

    full_name: str
    type_name: str
    kind: str
    properties: dict[str, _Prop]


@dataclass(slots=True)
class _Plan:
    """План обмена: свойства и состав (тип КД → авторегистрация)."""

    name: str
    obj: _Obj
    content: dict[str, bool]


# Один и тот же файл структуры читают десятки макетов. Ключ — путь, время и размер:
# правка файла даёт новый ключ. Соединение в памяти не делит кэш с другими.
_OBJECTS: dict[tuple[object, ...], _Obj | None] = {}
_PLANS: dict[tuple[object, ...], dict[str, bool]] = {}


def _database_stamp(connection: sqlite3.Connection) -> tuple[str, int, int] | None:
    """Файл соединения. Пустой путь — временная база, её между вызовами не запоминаем."""
    listed = connection.execute("PRAGMA database_list").fetchone()
    if listed is None:
        return None
    file = listed["file"] if isinstance(listed, sqlite3.Row) else listed[2]
    if not file:
        return None
    try:
        stat = os.stat(file)
    except OSError:
        return None
    return (file, stat.st_mtime_ns, stat.st_size)


class _Index:
    """Объекты структуры: в одном вызове имя читается один раз, между вызовами — по файлу."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._stamp = _database_stamp(connection)
        self._cache: dict[str, _Obj | None] = {}

    def get(self, name: str) -> _Obj | None:
        """Объект по полному имени `Вид.Имя` или по имени типа КД."""
        if name not in self._cache:
            key = None if self._stamp is None else (*self._stamp, name)
            if key is not None and key in _OBJECTS:
                loaded = _OBJECTS[key]
            else:
                loaded = self._load(name)
                self._remember(name, loaded)
            self._cache[name] = loaded
            if loaded is not None:
                self._cache.setdefault(loaded.full_name, loaded)
                self._cache.setdefault(loaded.type_name, loaded)
        return self._cache[name]

    def _remember(self, name: str, loaded: _Obj | None) -> None:
        if self._stamp is None:
            return
        names = [name]
        if loaded is not None:
            names.extend((loaded.full_name, loaded.type_name))
        for item in names:
            _OBJECTS.setdefault((*self._stamp, item), loaded)

    def _load(self, name: str) -> _Obj | None:
        card = read_object_card(self._connection, name)
        if card is None:
            return None
        properties: dict[str, _Prop] = {}
        for item in card.properties:
            # Повтор пути: как у страниц describe_object, остаётся более поздняя строка.
            properties[item.path] = _Prop(item.kind, item.is_group, item.types, item.unresolved)
        return _Obj(card.name, card.type_name, card.kind, properties)


def check_registration(
    rules: RegistrationRules,
    structure: sqlite3.Connection | None,
    *,
    has_plan_content: bool = True,
    has_object_settings: bool = True,
    exchange_rules: ExchangeRules | None = None,
) -> ValidationReport:
    """Проверяет правила регистрации против структуры источника.

    `structure` — соединение структуры конфигурации, где живёт план обмена.
    Без структуры проверки по структуре пропускаются и итог не говорит «ошибок нет»
    без оговорки (спецификация, «Отчёт проверки»).

    `has_plan_content` — в правилах есть достоверный блок `СоставПланаОбмена`.
    `has_object_settings` — `ОбъектНастройки` можно проверять. В менеджере регистрации
    объекта настройки нет, адаптер передаёт False. По умолчанию оба флага сохраняют
    проверку обычного файла `ПравилаРегистрации`.

    `exchange_rules` — правила обмена этого плана. Без них `registration.no_pvd`
    пропускается: не с чем сравнить состав.
    """
    report = ValidationReport()
    if structure is None:
        for check in _STRUCTURE_CHECKS:
            report.skip(check, _NO_STRUCTURE)
        if not has_object_settings:
            report.skip("registration.settings_type", _NO_OBJECT_SETTINGS)
            return report
        # Согласованность ОбъектНастройки с видом не требует базы: её задаёт писатель.
        for rule in rules.rules():
            if _loaded(rule):
                _check_settings(report, rule, None)
        return report

    index = _Index(structure)
    plan = _load_plan(report, rules.exchange_plan, index, structure)
    if plan is not None and has_plan_content:
        _check_plan_content(report, rules, plan)
    elif plan is not None:
        report.skip("registration.plan_content", _NO_PLAN_CONTENT)
    for rule in rules.rules():
        if not _loaded(rule):
            continue
        obj = _check_object(report, rule, index, plan)
        if has_object_settings:
            _check_settings(report, rule, obj)
        if plan is not None:
            _check_plan_filters(report, rule, plan, obj, index)
            _check_unload_mode(report, rule, plan)
            _check_autoregistration(report, rule, plan, obj)
        if obj is not None:
            _check_object_filters(report, rule, obj, index)
    if plan is not None:
        if exchange_rules is None:
            report.skip("registration.no_pvd", _NO_EXCHANGE_RULES)
        else:
            _check_no_pvd(report, rules, plan, exchange_rules)
    if not has_object_settings:
        report.skip("registration.settings_type", _NO_OBJECT_SETTINGS)
    return report


def _loaded(rule: Node) -> bool:
    """Правило попадёт в таблицу читателя.

    `Отключить=true` или `Валидное=false` — поддерево пропускается
    (ЗагрузкаПравилРегистрацииОбъектов:282-293). Пустой булев атрибут читается
    как Ложь (там же, :1077-1078), поэтому отсутствующее `Валидное` тоже отбрасывает
    правило. `Отключить` группы читатель не смотрит (:662-687) — вложенные правила
    проверяются.
    """
    if rule.attrs.get("Отключить") is True:
        return False
    return rule.attrs.get("Валидное", False) is True


def _title(rule: Node) -> str:
    """Имя правила для текста замечания: наименование, иначе код."""
    name = str(rule.get("Наименование"))
    return name or rule.code


def _load_plan(
    report: ValidationReport, name: str, index: _Index, connection: sqlite3.Connection
) -> _Plan | None:
    """План обмена `ПланОбмена/@Имя` (ЗагрузкаПравилРегистрацииОбъектов:181).

    Нет плана — остальные проверки по его составу не выполняются.
    """
    full_name = f"ПланОбмена.{name}" if name else ""
    obj = index.get(full_name) if full_name else None
    if obj is None or obj.kind != "ПланОбмена" or obj.full_name != full_name:
        shown = name or "не указан"
        report.error(
            "registration.exchange_plan",
            f"ПланОбмена «{shown}»",
            f"План обмена «{shown}» не найден в структуре",
        )
        reason = (
            f"план обмена «{shown}» не найден в структуре, проверки по его составу не выполнены"
        )
        for check in (
            "registration.plan_membership",
            "registration.plan_property",
            "registration.unload_mode",
            "registration.plan_content",
            "registration.autoregistration",
            "registration.no_pvd",
        ):
            report.skip(check, reason)
        return None
    return _Plan(name, obj, _plan_content(connection, full_name))


def _plan_content(connection: sqlite3.Connection, full_name: str) -> dict[str, bool]:
    """Тип элемента состава → авторегистрация. Первое вхождение типа побеждает."""
    stamp = _database_stamp(connection)
    key = None if stamp is None else (*stamp, full_name)
    if key is not None and key in _PLANS:
        return dict(_PLANS[key])
    result = exchange_plan_autoregistration(connection, full_name)
    if key is not None:
        _PLANS[key] = result
        return dict(result)
    return result


def _check_object(
    report: ValidationReport, rule: Node, index: _Index, plan: _Plan | None
) -> _Obj | None:
    """Объект ПРО есть в структуре и, если план найден, входит в его состав.

    Вид — префикс `ОбъектМетаданныхИмя` до точки: писатель собирает строку как
    `Тип.Имя` (ВыгрузкаРегистрации:217), исполнитель подставляет её в запрос как
    имя таблицы метаданных (ОбменДаннымиСобытия:2064-2069).
    """
    metadata_name = str(rule.get("ОбъектМетаданныхИмя"))
    obj = index.get(metadata_name) if metadata_name else None
    if obj is None or obj.full_name != metadata_name:
        report.error(
            "registration.object",
            rule_address(rule),
            f"Правило «{_title(rule)}»: объект «{metadata_name}» не найден в структуре",
        )
        return None
    if plan is not None and obj.type_name not in plan.content:
        report.error(
            "registration.plan_membership",
            rule_address(rule),
            f"Правило «{_title(rule)}»: объект «{metadata_name}» не входит в состав"
            f" плана обмена «{plan.name}»",
        )
    return obj


def _check_settings(report: ValidationReport, rule: Node, obj: _Obj | None) -> None:
    """`ОбъектНастройки` — тип ссылки или записи того же объекта, иначе предупреждение.

    Писатель выводит наименование типа и `Тип.Имя` одного объекта настройки
    (ВыгрузкаРегистрации:212-217). Есть объект в структуре — ожидается его `type_name`.
    """
    metadata_name = str(rule.get("ОбъектМетаданныхИмя"))
    expected = obj.type_name if obj is not None else _prefix_type(metadata_name)
    if expected is None:
        return
    actual = str(rule.get("ОбъектНастройки"))
    if actual == expected:
        return
    report.warning(
        "registration.settings_type",
        rule_address(rule),
        f"Правило «{_title(rule)}»: ОбъектНастройки «{actual}» не совпадает"
        f" с типом «{expected}» объекта «{metadata_name}»",
    )


def _prefix_type(metadata_name: str) -> str | None:
    """`Документ.Приход` → `ДокументСсылка.Приход` по таблице видов КД."""
    kind, dot, name = metadata_name.partition(".")
    prefix = _TYPE_PREFIX.get(kind)
    if not dot or not name or prefix is None:
        return None
    return f"{prefix}{name}"


def _check_plan_filters(
    report: ValidationReport, rule: Node, plan: _Plan, obj: _Obj | None, index: _Index
) -> None:
    """Отбор по свойствам плана обмена: реквизит узла и, если это не константа, свойство объекта.

    Читатель разбирает `[ТабличнаяЧасть].Реквизит` (ЗагрузкаПравилРегистрацииОбъектов:460-484)
    и подставляет поле в запрос к плану обмена (там же, :836-894).
    `ЭтоСтрокаКонстанты` — значение в `СвойствоОбъекта` литерал, а не путь (:448-458).
    """
    container = rule.child("ОтборПоСвойствамПланаОбмена")
    if container is None:
        return
    for item in container.walk():
        raw = str(item.get("СвойствоПланаОбмена"))
        if not _plan_field_exists(index, plan.obj, raw):
            report.error(
                "registration.plan_property",
                rule_address(rule),
                f"Правило «{_title(rule)}»: реквизит плана обмена «{raw}» не найден",
            )
        if obj is not None and item.get("ЭтоСтрокаКонстанты") is not True:
            _check_object_path(report, rule, obj, str(item.get("СвойствоОбъекта")), index)


def _check_object_filters(report: ValidationReport, rule: Node, obj: _Obj, index: _Index) -> None:
    """Отбор по свойствам объекта: `СвойствоОбъекта` есть у объекта ПРО.

    Читатель сохраняет строку как есть (ЗагрузкаПравилРегистрацииОбъектов:525-527),
    исполнитель идёт по ней через точку (ОбменДаннымиСобытия:2258-2267, :2416-2433).
    """
    container = rule.child("ОтборПоСвойствамОбъекта")
    if container is None:
        return
    for item in container.walk():
        _check_object_path(report, rule, obj, str(item.get("СвойствоОбъекта")), index)


def _check_object_path(
    report: ValidationReport, rule: Node, obj: _Obj, raw: str, index: _Index
) -> None:
    if not raw:
        report.error(
            "registration.object_property",
            rule_address(rule),
            f"Правило «{_title(rule)}»: свойство объекта «{obj.full_name}» не заполнено",
        )
        return
    missing = _missing_segment(index, obj.full_name, raw.split("."))
    if missing is None:
        return
    report.error(
        "registration.object_property",
        rule_address(rule),
        f"Правило «{_title(rule)}»: свойство «{raw}» объекта «{obj.full_name}»"
        f" не найдено (нет «{missing}»)",
    )


def _check_unload_mode(report: ValidationReport, rule: Node, plan: _Plan) -> None:
    """`РеквизитРежимаВыгрузки` — поле шапки плана обмена, если задан.

    Читатель кладёт его в `ИмяРеквизитаФлага` (ЗагрузкаПравилРегистрацииОбъектов:309-311).
    Запрос регистрации обращается к шапке узла `ШапкаПланаОбмена.<имя>`
    (ОбменДаннымиСобытия:1703-1716). Пустое значение в отбор не идёт (:1623).
    """
    name = str(rule.get("РеквизитРежимаВыгрузки"))
    if not name:
        return
    if _header_field(plan.obj, name) is None:
        report.error(
            "registration.unload_mode",
            rule_address(rule),
            f"Правило «{_title(rule)}»: реквизит режима выгрузки «{name}» не найден"
            f" у плана обмена «{plan.name}»",
        )


def _check_autoregistration(
    report: ValidationReport, rule: Node, plan: _Plan, obj: _Obj | None
) -> None:
    """ПРО объекта с авторегистрацией «Разрешить» при записи не исполняется.

    `АвтоРегистрацияРазрешена` истинна только для элемента состава с
    `АвтоРегистрация = Разрешить` (ОбменДаннымиПовтИсп:851-859). Тогда ветка
    правил регистрации при записи пропускается (ОбменДаннымиСобытия:1389-1414).
    Объекта нет в составе — это `registration.plan_membership`, здесь не повторяется:
    у отсутствующего элемента признак ложен (ОбменДаннымиПовтИсп:855-856).
    """
    if obj is None or plan.content.get(obj.type_name) is not True:
        return
    report.warning(
        "registration.autoregistration",
        rule_address(rule),
        f"Правило «{_title(rule)}»: у объекта «{obj.full_name}» в составе плана обмена "
        f"«{plan.name}» авторегистрация «Разрешить»: правила регистрации при записи "
        "не исполняются",
    )


def _check_no_pvd(
    report: ValidationReport, rules: RegistrationRules, plan: _Plan, exchange: ExchangeRules
) -> None:
    """Тип состава плана без включённого ПВД не выгружается и не снимается с регистрации.

    В фильтр `ВыбратьИзменения` попадают метаданные только включённых ПВД
    (БСП:17931-17938, БСП:18210-18236). Тип берётся из непустого `ОбъектВыборки`
    (БСП:6810-6812); пустой объект выборки тип не задаёт. `Отключить` выключает ПВД
    (БСП:6768).

    Предупреждение — только для типа, изменения которого регистрируются: у него есть
    загружаемое ПРО либо в составе плана стоит авторегистрация «Разрешить». Тип с запретом
    авторегистрации и без ПРО не регистрируется вовсе (так в составе типовых планов стоят
    объекты, которые эта сторона только получает) — зависать нечему.
    """
    covered = _enabled_pvd_types(exchange)
    with_rule = {
        str(rule.get("ОбъектНастройки")).strip() for rule in rules.rules() if _loaded(rule)
    }
    for type_name in sorted(plan.content):
        if type_name in covered:
            continue
        if type_name in with_rule:
            how = "изменения регистрирует правило регистрации"
        elif plan.content[type_name]:
            how = "изменения регистрирует платформа (авторегистрация «Разрешить»)"
        else:
            continue
        report.warning(
            "registration.no_pvd",
            _address_for_type(rules, type_name, plan.name),
            f"Объект «{type_name}» входит в состав плана обмена «{plan.name}», {how}, "
            "а включённого ПВД с таким ОбъектВыборки нет: зарегистрированное изменение "
            "не попадает в выборку выгрузки и регистрация не снимается",
        )


def _enabled_pvd_types(rules: ExchangeRules) -> set[str]:
    """Типы `ОбъектВыборки` включённых ПВД. Пустой объект выборки тип не покрывает."""
    covered: set[str] = set()
    for rule in rules.pvd():
        if rule.attrs.get("Отключить") is True:
            continue
        type_name = str(rule.get("ОбъектВыборки")).strip()
        if type_name:
            covered.add(type_name)
    return covered


def _address_for_type(rules: RegistrationRules, type_name: str, plan_name: str) -> str:
    """Адрес ПРО этого типа, если правило загружается; иначе адрес плана."""
    for rule in rules.rules():
        if _loaded(rule) and str(rule.get("ОбъектНастройки")) == type_name:
            return rule_address(rule)
    return f"ПланОбмена «{plan_name}»"


def _check_plan_content(report: ValidationReport, rules: RegistrationRules, plan: _Plan) -> None:
    """Блок `СоставПланаОбмена` против состава в структуре: расхождения — предупреждение.

    Читатель БСП блок пропускает (ЗагрузкаПравилРегистрацииОбъектов:222-226), писатель
    его выводит (ВыгрузкаРегистрации:103-116). Расхождение — признак устаревших правил
    (design.md, Д9). Элемент состава без правила регистрации замечанием не является.
    Константы MD83Exp в состав не пишет
    (`reference/kd2-dist-src/MD83Exp/ВыгрузкаМетаданных/Ext/ObjectModule.bsl:1335-1336`),
    сверять их не с чем.
    """
    declared = _declared_content(rules)
    address = f"ПланОбмена «{plan.name}»"
    # Расхождения сводятся в одно замечание на вид: у устаревших правил их тысячи.
    autoregistration = [
        f"{name} ({_flag(flag)} → {_flag(plan.content[name])})"
        for name, flag in sorted(declared.items())
        if name in plan.content and flag != plan.content[name]
    ]
    extra = [
        name for name in sorted(declared) if name not in plan.content and not _structure_omits(name)
    ]
    missing = sorted(set(plan.content) - set(declared))
    for items, text in (
        (autoregistration, "Авторегистрация в блоке СоставПланаОбмена отличается от структуры"),
        (extra, "В блоке СоставПланаОбмена есть типы, которых нет в составе плана обмена"),
        (missing, "В составе плана обмена есть типы, которых нет в блоке СоставПланаОбмена"),
    ):
        if items:
            report.warning(
                "registration.plan_content", address, f"{text} ({len(items)}): {_sample(items)}"
            )


def _sample(items: list[str], limit: int = 10) -> str:
    """Первые элементы списка и число остальных."""
    shown = ", ".join(items[:limit])
    return f"{shown} и ещё {len(items) - limit}" if len(items) > limit else shown


def _declared_content(rules: RegistrationRules) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for item in rules.plan_content():
        type_name = str(item.get("Тип"))
        if type_name and type_name not in result:
            result[type_name] = item.get("Авторегистрация") is True
    return result


def _structure_omits(type_name: str) -> bool:
    """Константы в выгрузку состава не попадают (MD83Exp:1335-1336)."""
    head, _, _ = type_name.partition(".")
    return head.startswith("Константа")


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _plan_field_exists(index: _Index, obj: _Obj, raw: str) -> bool:
    """Реквизит шапки или реквизит табличной части `[Имя].Реквизит`.

    Разбор скобок — как у читателя (ЗагрузкаПравилРегистрацииОбъектов:471-484):
    имя табличной части между `[` и `]`, реквизит — после `].`.
    Строка целиком становится полем запроса `Таблица.<строка>` (там же, :894),
    поэтому точки после первого реквизита — разыменование (`Сотрудник.Ссылка`).
    """
    tabular, attribute = _split_plan_property(raw)
    segments = attribute.split(".") if attribute else []
    if not segments:
        return False
    head, *rest = segments
    if tabular:
        section = obj.properties.get(tabular)
        if section is None or section.kind != "ТабличнаяЧасть":
            return False
        field = obj.properties.get(f"{tabular}.{head}")
        if field is None or field.is_group:
            return False
    else:
        field = _header_field(obj, head)
        if field is None:
            return False
    if not rest:
        return True
    return _walk_refs(index, field, rest) is None


def _split_plan_property(raw: str) -> tuple[str, str]:
    """`[Организации].Организация` → (`Организации`, `Организация`); без скобок — шапка."""
    open_at = raw.find("[")
    if open_at < 0:
        return "", raw
    close_at = raw.find("]", open_at + 1)
    if close_at < 0:
        return "", raw
    # Сред(строка, ПозСкобки2 + 2): пропускаются «]» и следующий символ (точка).
    return raw[open_at + 1 : close_at], raw[close_at + 2 :]


def _header_field(obj: _Obj, name: str) -> _Prop | None:
    """Поле шапки: реквизит или стандартное свойство, не состав и не табличная часть."""
    prop = obj.properties.get(name)
    if prop is None or prop.is_group or prop.kind in _NOT_HEADER:
        return None
    return prop


def _missing_segment(index: _Index, full_name: str, segments: list[str]) -> str | None:
    """Первое звено пути, которого нет; None — путь есть.

    Путь — строка `СвойствоОбъекта`, разделённая точкой (ОбменДаннымиСобытия:2262).
    Таблицу свойств читатель пропускает (ЗагрузкаПравилРегистрацииОбъектов:502-504),
    хотя писатель её выводит (ВыгрузкаРегистрации:339-340, :382-383): в строке уже
    полный путь (`Склад.ТипСклада`). Табличная часть — группа, следующее звено её реквизит.
    """
    return _walk(index, full_name, "", segments)


def _walk(index: _Index, full_name: str, prefix: str, segments: list[str]) -> str | None:
    if not segments:
        return None
    head, *rest = segments
    obj = index.get(full_name)
    if obj is None:
        return head
    path = f"{prefix}.{head}" if prefix else head
    prop = obj.properties.get(path)
    if prop is None:
        # `Ссылка` есть у ссылочного объекта, MD83Exp её в свойства не пишет
        # (цепочка ВыгрузитьОсновныеСвойства, MD83Exp:408-1150; исполнитель
        # обращается к любому имени через [], ОбменДаннымиСобытия:2266).
        if not prefix and head == "Ссылка" and _is_reference(obj.type_name):
            return _walk(index, full_name, "", rest)
        return head
    if not rest:
        return None
    if prop.is_group:
        return _walk(index, full_name, path, rest)
    return _walk_refs(index, prop, rest)


def _walk_refs(index: _Index, prop: _Prop, rest: list[str]) -> str | None:
    """Хвост пути по типу ссылки. Достаточно одного типа, где хвост есть.

    Неразрешённый тип проверить нельзя — это не ошибка свойства.
    """
    candidates = [name for name in prop.types if name not in _PRIMITIVES and "." in name]
    if not candidates:
        return None if prop.unresolved else rest[0]
    missing = rest[0]
    unresolved = False
    for type_name in candidates:
        target = index.get(type_name)
        if target is None or target.type_name != type_name:
            unresolved = True
            continue
        found = _walk(index, target.full_name, "", rest)
        if found is None:
            return None
        missing = found
    if unresolved:
        return None
    return missing


def _is_reference(type_name: str) -> bool:
    """`ДокументСсылка.Приход` — ссылка; `РегистрСведенийЗапись.Курсы` — нет."""
    head, dot, _ = type_name.partition(".")
    return bool(dot) and head.endswith("Ссылка")
