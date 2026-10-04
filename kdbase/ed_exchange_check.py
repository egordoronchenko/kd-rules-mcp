"""Живая проверка обмена через универсальный формат между двумя песочницами.

`inspect` только читает версии, менеджеры, расширения, узлы, константы и объект.
`run` проводит случай из JSON: значение должно быть увидено в новом сообщении и
в приёмнике. `cleanup` удаляет только то, что этот прогон записал в журнал.
Установка расширения — другой скрипт; здесь проверяется уже установленное.

Базы — только с ролью «песочница», сервер данных как у `exchange_check`
(`bsp_load.server_for`). Код 1С — одна строка UTF-8. Сообщение передаётся
частями в base64. Откат не выполняется: журнал перечисляет созданное и
изменённое. Каталог прогона — `kdbase/run/ed-exchange-<время>/`.

Точки входа БСП — те же, что у обмена через план: `ОбменДаннымиСервер.
ВыполнитьВыгрузкуДляУзлаИнформационнойБазыВоВременноеХранилище` и
`ВыполнитьОбменДаннымиДляУзлаИнформационнойБазыЧерезФайлИлиСтроку`.
Повтор уже принятого номера сообщения приёмник отклоняет (XDTO:7835):
счётчики скрипт не обнуляет. Заполнитель правил — `ОбменДаннымиXDTOСервер.
КоллекцияПравилКонвертации` и `ЗаполнитьПравилаКонвертацииОбъектов`.
Результат загрузки — имя значения перечисления
(`ОбщегоНазначения.ИмяЗначенияПеречисления`, ОбщегоНазначения:2582), не синоним.
Менеджер, который БСП берёт для версии формата узла, —
`ОбменДаннымиXDTOСервер.МенеджерОбменаВерсииФормата` (XDTO:3566–3578).
Таблица изменений — у объекта (`Справочник.Имя.Изменения` и остальные виды
состава), не у плана обмена. `ВыбратьИзменения` не вызывается.
`ExchangePlan` заголовка сообщения сверяется с планом случая (XDTO:5959).
`cleanup` принимает только каталог внутри `kdbase/run/` с `protocol.txt` этого скрипта.
"""

import argparse
import base64
import contextlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bsp_load import DataServer, ExchangeCheckError, guarded, server_for, short_error
from lxml import etree

from kd2_rules_mcp.console import utf8_stdout
from kd2_rules_mcp.ed.schema import QName, load_schema, resolve_property
from kd2_rules_mcp.ed.schema.xdto import metadata
from kd2_rules_mcp.projects import ProjectConfigError

__all__ = ["CaseError", "bsl_expr", "load_case", "server_for"]

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "kdbase" / "run"
DONE = "КОНЕЦ"
ANSWER_END = "КОНЕЦОТВЕТА"
NODE_NAME = "ed exchange_check"
LEDGER = "created.json"
PRODUCER = "ed_exchange_check"
RAW_CHUNK = 48_000
FORMAT_PREFIX = "http://v8.1c.ru/edi/edi_stnd/EnterpriseData/"
MSG_NS = "http://www.1c.ru/SSL/Exchange/Message"
_IDENT = re.compile(r"^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*$")
_VERSION = re.compile(r"^\d+(?:\.\d+)+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TRANSPORT = (
    "пустой ответ",
    "сервер данных недоступен",
    "Internal error",
    "HTTP Error 500",
    "-32603",
    "не-JSON ответ",
    "обрыв HTTP",
)
_META = re.compile(r"^(Справочник|Документ)\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*$")
_TYPE_NAME = re.compile(
    r"^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)*$"
)
# Символы, которые `one_line` (str.splitlines) или разбор ответа (`→`, `⏎`) искажают.
_DISTORT = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "→", "⏎")
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


class CaseError(ExchangeCheckError):
    """Случай не разбирается: текст — для протокола."""


class StageFailed(ExchangeCheckError):
    """Стадия не пройдена: текст — строка протокола."""


@dataclass(frozen=True, slots=True)
class ObjectSpec:
    metadata: str
    create: bool
    name: str | None
    ref: str | None
    target_metadata: str
    format_name: str


@dataclass(frozen=True, slots=True)
class RuleSide:
    manager: str
    pko: str
    direction: str


@dataclass(frozen=True, slots=True)
class NodeSpec:
    mode: str
    source_code: str
    target_code: str
    variant: str | None


@dataclass(frozen=True, slots=True)
class Values:
    v1: str
    v2: str
    empty: str | None


@dataclass(frozen=True, slots=True)
class Case:
    plan: str
    format_version: str
    manager_interface: str
    format_property: str
    property_path: str
    source_attribute: str
    target_attribute: str
    absent: str
    absent_step: bool
    check_registration_on_write: bool
    obj: ObjectSpec
    values: Values
    nodes: NodeSpec
    source_rules: RuleSide
    target_rules: RuleSide
    source_dump: str | None
    target_dump: str | None


@dataclass(frozen=True, slots=True)
class NodeInfo:
    code: str
    version: str
    this: bool
    sent: int
    received: int
    ready: bool


@dataclass
class SideState:
    """Снимок одной базы. Поля пополняются разбором ответа, не конструктором целиком."""

    configuration: str = ""
    config_version: str = ""
    platform: str = ""
    constants: dict[str, str] = field(default_factory=dict)
    extensions: list[tuple[str, str, str]] = field(default_factory=list)
    nodes: list[NodeInfo] = field(default_factory=list)
    manager: str = ""
    manager_version: str = ""
    object_present: bool | None = None
    object_ref: str = ""
    object_value: str = ""
    object_name: str = ""
    name_count: int | None = None
    name_ref: str = ""

    def node(self, code: str) -> NodeInfo | None:
        return next((item for item in self.nodes if item.code == code), None)

    def this_node(self) -> NodeInfo | None:
        return next((item for item in self.nodes if item.this), None)


@dataclass
class Ledger:
    created: list[dict[str, str]] = field(default_factory=list)
    modified: list[dict[str, str]] = field(default_factory=list)

    def dump(self) -> dict[str, object]:
        return {"producer": PRODUCER, "created": self.created, "modified": self.modified}


def bsl_expr(value: str) -> str:
    """Выражение 1С: кавычки удваиваются, перевод строки — `Символы.ПС`, не литерал кода."""
    parts = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pieces = ['"' + part.replace('"', '""') + '"' for part in parts]
    joined: list[str] = []
    for index, piece in enumerate(pieces):
        if index:
            joined.append("Символы.ПС")
        joined.append(piece)
    return " + ".join(joined)


def _emit(stage: str, body: str) -> str:
    return guarded(f"СтадияПроверки = {bsl_expr(stage)};\n{body}")


def _show_bsl(expr: str) -> str:
    return f'СтрЗаменить(СтрЗаменить(Строка({expr}), Символы.ПС, "⏎"), Символы.Таб, "→")'


def _result(lines_expr: str) -> str:
    return f'Результат = "OK" + Символы.ПС + {lines_expr} + Символы.ПС + "{ANSWER_END}";'


def show(value: str) -> str:
    """Значение в одну строку протокола."""
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").replace("\n", "⏎")


def decode_cell(value: str) -> str:
    return value.replace("⏎", "\n").replace("→", "\t")


def format_uri(version: str) -> str:
    return f"{FORMAT_PREFIX}{version}"


def _rows(text: str) -> dict[str, list[list[str]]]:
    """Разбор ответа кода. Нет служебной строки конца — ответ обрезан, значениям верить нельзя."""
    raw_lines = text.splitlines()
    if ANSWER_END not in raw_lines:
        raise StageFailed("ответ обрезан")
    grouped: dict[str, list[list[str]]] = {}
    for raw in raw_lines[: raw_lines.index(ANSWER_END)]:
        if not raw.strip():
            continue
        parts = raw.split("\t")
        grouped.setdefault(parts[0], []).append(parts[1:])
    return grouped


def _cell(rows: Mapping[str, list[list[str]]], key: str, index: int = 0, cell: int = 0) -> str:
    group = rows.get(key) or []
    if index >= len(group) or cell >= len(group[index]):
        raise StageFailed(f"в ответе нет поля {key}")
    return group[index][cell]


def _number(value: str, where: str) -> int:
    if not value.isdecimal():
        raise StageFailed(f"{where}: не число")
    return int(value)


def _optional(rows: Mapping[str, list[list[str]]], key: str, index: int = 0, cell: int = 0) -> str:
    group = rows.get(key) or []
    if index >= len(group) or cell >= len(group[index]):
        return ""
    return group[index][cell]


def parse_state(text: str) -> SideState:
    """Ответ снимка базы. Пустые группы допустимы, обязательны конфигурация и менеджер."""
    rows = _rows(text)
    state = SideState()
    config = rows.get("КОНФИГУРАЦИЯ")
    if not config or len(config[0]) < 2:
        raise StageFailed("снимок без конфигурации")
    state.configuration, state.config_version = config[0][0], config[0][1]
    platform = rows.get("ПЛАТФОРМА")
    state.platform = platform[0][0] if platform and platform[0] else ""
    for item in rows.get("КОНСТ", []):
        if len(item) >= 2:
            state.constants[item[0]] = item[1]
    for item in rows.get("РАСШ", []):
        if len(item) >= 3:
            state.extensions.append((item[0], item[1], item[2]))
    for item in rows.get("УЗЕЛ", []):
        if len(item) < 6:
            raise StageFailed("строка узла оборвана")
        state.nodes.append(
            NodeInfo(
                code=item[0],
                version=item[1],
                this=item[2] == "1",
                sent=_number(item[3], "номер отправленного"),
                received=_number(item[4], "номер принятого"),
                ready=item[5] == "1",
            )
        )
    manager = rows.get("МЕНЕДЖЕР")
    if not manager or len(manager[0]) < 2:
        raise StageFailed("снимок без менеджера")
    state.manager, state.manager_version = manager[0][0], manager[0][1]
    obj = rows.get("ОБЪЕКТ")
    if obj:
        state.object_present = obj[0][0] == "есть"
        if state.object_present:
            if len(obj[0]) < 3:
                raise StageFailed("строка объекта оборвана")
            state.object_ref = obj[0][1]
            state.object_value = decode_cell(obj[0][2])
    object_name = rows.get("ИМЯОБЪЕКТА")
    if object_name and object_name[0]:
        state.object_name = decode_cell(object_name[0][0])
    names = rows.get("ИМЕНА")
    if names and names[0]:
        state.name_count = _number(names[0][0], "число имён")
    name_ref = rows.get("ИМЯУИД")
    if name_ref and name_ref[0]:
        state.name_ref = name_ref[0][0]
    return state


def _truth(value: str) -> bool:
    return value in ("Да", "Истина", "true", "True")


def _transport(error: ExchangeCheckError) -> bool:
    text = str(error)
    return any(mark in text for mark in _TRANSPORT)


def execute(server: DataServer, code: str) -> str:
    try:
        return server.run(code)
    except ExchangeCheckError as error:
        raise StageFailed(
            short_error(str(error)) if str(error).startswith("ОШИБКА") else str(error)
        ) from error


def mutate(
    server: DataServer,
    code: str,
    reread: str,
    happened: Callable[[str], bool],
) -> tuple[str, bool]:
    """Запись. Ошибка сервера данных — одно повторное чтение, без второй записи."""
    try:
        return server.run(code), False
    except ExchangeCheckError as error:
        if not _transport(error):
            raise StageFailed(str(error)) from error
        try:
            observed = server.run(reread)
        except ExchangeCheckError as second:
            raise StageFailed(f"{error}; повторное чтение: {second}") from second
        if happened(observed):
            return observed, True
        raise StageFailed(
            f"{error}; повторное чтение не подтвердило запись, повтор записи не выполнялся"
        ) from error


def state_code(
    plan: str,
    manager: str,
    metadata: str,
    attribute: str,
    ref: str | None,
    name: str | None,
) -> str:
    """Снимок без записи: конфигурация, константы, расширения, узлы, менеджер, объект."""
    object_part = ""
    if ref:
        object_part = f"""
        МенеджерОбъекта = ОбщегоНазначения.МенеджерОбъектаПоПолномуИмени({bsl_expr(metadata)});
        Ссылка = МенеджерОбъекта.ПолучитьСсылку(Новый УникальныйИдентификатор({bsl_expr(ref)}));
        ОбъектДанных = Ссылка.ПолучитьОбъект();
        Если ОбъектДанных = Неопределено Тогда
        Т.Добавить("ОБЪЕКТ" + Символы.Таб + "нет");
        Иначе
        Описание = Метаданные.НайтиПоПолномуИмени({bsl_expr(metadata)});
        ЕстьРеквизит = Ложь;
        Если Описание <> Неопределено Тогда
        Если Описание.Реквизиты.Найти({bsl_expr(attribute)}) <> Неопределено Тогда
        ЕстьРеквизит = Истина;
        Иначе
        Для Каждого СтандартныйРеквизит Из Описание.СтандартныеРеквизиты Цикл
        Если СтандартныйРеквизит.Имя = {bsl_expr(attribute)} Тогда
        ЕстьРеквизит = Истина;
        КонецЕсли;
        КонецЦикла;
        КонецЕсли;
        КонецЕсли;
        Если Не ЕстьРеквизит Тогда
        ЗначениеОбъекта = "НЕТ_РЕКВИЗИТА";
        Иначе
        ЗначениеОбъекта = {_show_bsl(f"ОбъектДанных[{bsl_expr(attribute)}]")};
        КонецЕсли;
        Т.Добавить("ОБЪЕКТ" + Символы.Таб + "есть" + Символы.Таб +
        Строка(Ссылка.УникальныйИдентификатор()) + Символы.Таб + ЗначениеОбъекта);
        Т.Добавить("ИМЯОБЪЕКТА" + Символы.Таб + {_show_bsl("ОбъектДанных.Наименование")});
        КонецЕсли;
        """
    name_part = ""
    if name is not None:
        name_part = f"""
        ЗапросИмени = Новый Запрос;
        ЗапросИмени.Текст = "ВЫБРАТЬ О.Ссылка КАК Ссылка ИЗ {metadata} КАК О ГДЕ О.Наименование =
        &Имя";
        ЗапросИмени.УстановитьПараметр("Имя", {bsl_expr(name)});
        ТаблицаИмени = ЗапросИмени.Выполнить().Выгрузить();
        Т.Добавить("ИМЕНА" + Символы.Таб + Формат(ТаблицаИмени.Количество(), "ЧН=0; ЧГ="));
        Если ТаблицаИмени.Количество() = 1 Тогда
        Т.Добавить("ИМЯУИД" + Символы.Таб +
        Строка(ТаблицаИмени[0].Ссылка.УникальныйИдентификатор()));
        КонецЕсли;
        """
    body = f"""
    Т = Новый Массив;
    Т.Добавить("КОНФИГУРАЦИЯ" + Символы.Таб + Метаданные.Имя + Символы.Таб + Метаданные.Версия);
    Сведения = Новый СистемнаяИнформация;
    Т.Добавить("ПЛАТФОРМА" + Символы.Таб + Сведения.ВерсияПриложения);
    ИменаКонстант = "ИспользоватьСинхронизациюДанных,"
    + "ИспользуетсяОбменДаннымиУниверсальныйФормат,"
    + "УчетЗарплатыИКадровВоВнешнейПрограмме";
    Для Каждого ИмяКонстанты Из СтрРазделить(ИменаКонстант, ",") Цикл
    Если Метаданные.Константы.Найти(ИмяКонстанты) <> Неопределено Тогда
    Т.Добавить("КОНСТ" + Символы.Таб + ИмяКонстанты + Символы.Таб +
    Строка(Константы[ИмяКонстанты].Получить()));
    КонецЕсли;
    КонецЦикла;
    Если Метаданные.ФункциональныеОпции.Найти("УчетЗарплатыИКадровСредствамиБухгалтерии") <>
    Неопределено Тогда
    Т.Добавить("КОНСТ" + Символы.Таб + "УчетЗарплатыИКадровСредствамиБухгалтерии" + Символы.Таб +
    Строка(ПолучитьФункциональнуюОпцию("УчетЗарплатыИКадровСредствамиБухгалтерии")));
    КонецЕсли;
    Для Каждого Расширение Из РасширенияКонфигурации.Получить() Цикл
    Т.Добавить("РАСШ" + Символы.Таб + Расширение.Имя + Символы.Таб + Строка(Расширение.Активно) +
    Символы.Таб + Строка(Расширение.БезопасныйРежим));
    КонецЦикла;
    ВыборкаУзлов = ПланыОбмена[{bsl_expr(plan)}].Выбрать();
    Пока ВыборкаУзлов.Следующий() Цикл
    ЭтоЭтот = ?(ВыборкаУзлов.Ссылка = ПланыОбмена[{bsl_expr(plan)}].ЭтотУзел(), "1", "0");
    Готов = ?(ОбменДаннымиСервер.НастройкаСинхронизацииЗавершена(ВыборкаУзлов.Ссылка), "1", "0");
    Т.Добавить("УЗЕЛ" + Символы.Таб + СокрЛП(ВыборкаУзлов.Код) + Символы.Таб +
    Строка(ВыборкаУзлов.ВерсияФорматаОбмена) + Символы.Таб + ЭтоЭтот + Символы.Таб +
    Формат(ВыборкаУзлов.НомерОтправленного, "ЧН=0; ЧГ=") + Символы.Таб +
    Формат(ВыборкаУзлов.НомерПринятого, "ЧН=0; ЧГ=") + Символы.Таб + Готов);
    КонецЦикла;
    Попытка
    Т.Добавить("МЕНЕДЖЕР" + Символы.Таб + {bsl_expr(manager)} + Символы.Таб +
    {manager}.ВерсияФорматаМенеджераОбмена());
    Исключение
    Т.Добавить("МЕНЕДЖЕР" + Символы.Таб + {bsl_expr(manager)} + Символы.Таб + "ОШИБКА");
    КонецПопытки;
    {object_part}
    {name_part}
    {_result("СтрСоединить(Т, Символы.ПС)")}
    """
    return _emit("state", body)


def create_node_code(
    plan: str, code: str, version: str, variant: str | None, enable_sync: bool
) -> str:
    """Узел корреспондента, если его ещё нет. Чужой существующий узел не меняется."""
    sync = ""
    if enable_sync:
        sync = """
        Если Не Константы.ИспользоватьСинхронизациюДанных.Получить() Тогда
        МенеджерКонстанты = Константы.ИспользоватьСинхронизациюДанных.СоздатьМенеджерЗначения();
        МенеджерКонстанты.Значение = Истина;
        МенеджерКонстанты.ОбменДанными.Загрузка = Истина;
        МенеджерКонстанты.Записать();
        КонецЕсли;
        """
    fill = ""
    if variant:
        fill = f"""
        ОбъектУзла.ВариантНастройки = {bsl_expr(variant)};
        ОбъектУзла.Заполнить(Неопределено);
        """
    body = f"""
    {sync}
    МенеджерПлана = ПланыОбмена[{bsl_expr(plan)}];
    СсылкаУзла = МенеджерПлана.НайтиПоКоду({bsl_expr(code)});
    Если Не СсылкаУзла.Пустая() Тогда
    {_result('"СОЗДАН" + Символы.Таб + "уже"')}
    Иначе
    ОбъектУзла = МенеджерПлана.СоздатьУзел();
    {fill}
    ОбъектУзла.Код = {bsl_expr(code)};
    ОбъектУзла.Наименование = {bsl_expr(NODE_NAME)};
    ОбъектУзла.ВерсияФорматаОбмена = {bsl_expr(version)};
    ОписаниеПлана = Метаданные.ПланыОбмена[{bsl_expr(plan)}];
    Если ОписаниеПлана.Реквизиты.Найти("ПравилаОтправкиДокументов") <> Неопределено Тогда
    ОбъектУзла["ПравилаОтправкиДокументов"] = "НеСинхронизировать";
    КонецЕсли;
    ОбъектУзла.Записать();
    РегистрыСведений.ОбщиеНастройкиУзловИнформационныхБаз.УстановитьПризнакНастройкаЗавершена(ОбъектУзла.Ссылка);
    {
        _result(
            '"СОЗДАН" + Символы.Таб + "да" + Символы.ПС + "КОД" + Символы.Таб + '
            "СокрЛП(ОбъектУзла.Код)"
        )
    }
    КонецЕсли;
    """
    return _emit("create-node", body)


def create_object_code(metadata: str, name: str) -> str:
    """Новый элемент справочника с точным наименованием. Уже существующее имя не занимается."""
    body = f"""
    МенеджерОбъекта = ОбщегоНазначения.МенеджерОбъектаПоПолномуИмени({bsl_expr(metadata)});
    ЗапросИмени = Новый Запрос;
    ЗапросИмени.Текст = "ВЫБРАТЬ О.Ссылка КАК Ссылка ИЗ {metadata} КАК О ГДЕ О.Наименование = &Имя";
    ЗапросИмени.УстановитьПараметр("Имя", {bsl_expr(name)});
    ТаблицаИмени = ЗапросИмени.Выполнить().Выгрузить();
    Если ТаблицаИмени.Количество() > 0 Тогда
    {
        _result(
            '"СОЗДАН" + Символы.Таб + "занято" + Символы.ПС + "СТРОК" + Символы.Таб + '
            'Формат(ТаблицаИмени.Количество(), "ЧН=0; ЧГ=")'
        )
    }
    Иначе
    ОбъектДанных = МенеджерОбъекта.СоздатьЭлемент();
    ОбъектДанных.Наименование = {bsl_expr(name)};
    ОбъектДанных.Записать();
    {
        _result(
            '"СОЗДАН" + Символы.Таб + "да" + Символы.ПС + "УИД" + Символы.Таб + '
            "Строка(ОбъектДанных.Ссылка.УникальныйИдентификатор())"
        )
    }
    КонецЕсли;
    """
    return _emit("create-object", body)


def hook_code(
    manager: str,
    interface: str,
    pko: str,
    attribute: str,
    prop: str,
    plan: str,
    node_code: str,
    format_version: str,
    direction: str,
) -> str:
    """Заполнитель обоих направлений и менеджер версии узла.

    В направлении случая пара ищется в названном ПКО. В противоположном — по всем
    ПКО: утечка той же пары в другое правило видна. Повтор строки с тем же именем
    ПКО отдаётся отдельным полем `ДУБЛЬ`. Менеджер версии —
    `ОбменДаннымиXDTOСервер.МенеджерОбменаВерсииФормата` (XDTO:3566–3578):
    функция возвращает общий модуль по версии формата и узлу.
    """
    body = f"""
    Т = Новый Массив;
    ВерсияМенеджера = {manager}.ВерсияФорматаМенеджераОбмена();
    Т.Добавить("ВЕРСИЯ" + Символы.Таб + ВерсияМенеджера);
    Если ВерсияМенеджера <> {bsl_expr(interface)} Тогда
    Т.Добавить("ИНТЕРФЕЙС" + Символы.Таб + "другой");
    Иначе
    Для Каждого Направление Из СтрРазделить("Отправка,Получение", ",") Цикл
    Правила = ОбменДаннымиXDTOСервер.КоллекцияПравилКонвертации(ВерсияМенеджера);
    {manager}.ЗаполнитьПравилаКонвертацииОбъектов(Направление, Правила);
    Пара = 0;
    Реквизит = 0;
    Есть = "нет";
    Если Направление = {bsl_expr(direction)} Тогда
    ЧислоПКО = 0;
    Для Каждого ПравилоОбхода Из Правила Цикл
    Если ПравилоОбхода.ИмяПКО = {bsl_expr(pko)} Тогда
    ЧислоПКО = ЧислоПКО + 1;
    КонецЕсли;
    КонецЦикла;
    Если ЧислоПКО > 1 Тогда
    Т.Добавить("ДУБЛЬ" + Символы.Таб + Формат(ЧислоПКО, "ЧН=0; ЧГ="));
    КонецЕсли;
    Правило = Правила.Найти({bsl_expr(pko)}, "ИмяПКО");
    Если Правило <> Неопределено Тогда
    Есть = "есть";
    Для Каждого Свойство Из Правило.Свойства Цикл
    Если Свойство.СвойствоКонфигурации = {bsl_expr(attribute)} И Свойство.СвойствоФормата =
    {bsl_expr(prop)} Тогда
    Пара = Пара + 1;
    КонецЕсли;
    Если Свойство.СвойствоКонфигурации = {bsl_expr(attribute)} Тогда
    Реквизит = Реквизит + 1;
    КонецЕсли;
    КонецЦикла;
    КонецЕсли;
    Иначе
    Для Каждого ПравилоОбхода Из Правила Цикл
    Для Каждого Свойство Из ПравилоОбхода.Свойства Цикл
    Если Свойство.СвойствоКонфигурации = {bsl_expr(attribute)} И Свойство.СвойствоФормата =
    {bsl_expr(prop)} Тогда
    Пара = Пара + 1;
    КонецЕсли;
    Если Свойство.СвойствоКонфигурации = {bsl_expr(attribute)} Тогда
    Реквизит = Реквизит + 1;
    КонецЕсли;
    КонецЦикла;
    КонецЦикла;
    Если Пара > 0 Тогда
    Есть = "есть";
    КонецЕсли;
    КонецЕсли;
    Т.Добавить("НАПРАВЛЕНИЕ" + Символы.Таб + Направление + Символы.Таб + Есть + Символы.Таб +
    Формат(Пара, "ЧН=0; ЧГ=") + Символы.Таб + Формат(Реквизит, "ЧН=0; ЧГ="));
    КонецЦикла;
    КонецЕсли;
    УзелМенеджера = ПланыОбмена[{bsl_expr(plan)}].НайтиПоКоду({bsl_expr(node_code)});
    Если УзелМенеджера.Пустая() Тогда
    Т.Добавить("МЕНЕДЖЕРВЕРСИИ" + Символы.Таб + "нет узла");
    Иначе
    МенеджерВерсии = ОбменДаннымиXDTOСервер.МенеджерОбменаВерсииФормата({bsl_expr(format_version)},
    УзелМенеджера);
    Если МенеджерВерсии <> {manager} Тогда
    Т.Добавить("МЕНЕДЖЕРВЕРСИИ" + Символы.Таб + "другой");
    Иначе
    Т.Добавить("МЕНЕДЖЕРВЕРСИИ" + Символы.Таб + "тот");
    КонецЕсли;
    КонецЕсли;
    {_result("СтрСоединить(Т, Символы.ПС)")}
    """
    return _emit("hook", body)


def write_code(metadata: str, ref: str, attribute: str, value: str) -> str:
    body = f"""
    МенеджерОбъекта = ОбщегоНазначения.МенеджерОбъектаПоПолномуИмени({bsl_expr(metadata)});
    Ссылка = МенеджерОбъекта.ПолучитьСсылку(Новый УникальныйИдентификатор({bsl_expr(ref)}));
    ОбъектДанных = Ссылка.ПолучитьОбъект();
    Если ОбъектДанных = Неопределено Тогда
    ВызватьИсключение "Нет объекта " + {bsl_expr(ref)};
    КонецЕсли;
    ОбъектДанных[{bsl_expr(attribute)}] = {bsl_expr(value)};
    ОбъектДанных.Записать();
    Прочитано = ОбщегоНазначения.ЗначениеРеквизитаОбъекта(Ссылка, {bsl_expr(attribute)});
    {_result('"ЗНАЧЕНИЕ" + Символы.Таб + ' + _show_bsl("Прочитано"))}
    """
    return _emit("write", body)


def register_code(plan: str, node_code: str, metadata: str, ref: str, clear: bool) -> str:
    clearing = "ПланыОбмена.УдалитьРегистрациюИзменений(Узел);" if clear else ""
    body = f"""
    Узел = ПланыОбмена[{bsl_expr(plan)}].НайтиПоКоду({bsl_expr(node_code)});
    Если Узел.Пустая() Тогда
    ВызватьИсключение "Нет узла " + {bsl_expr(node_code)};
    КонецЕсли;
    МенеджерОбъекта = ОбщегоНазначения.МенеджерОбъектаПоПолномуИмени({bsl_expr(metadata)});
    Ссылка = МенеджерОбъекта.ПолучитьСсылку(Новый УникальныйИдентификатор({bsl_expr(ref)}));
    {clearing}
    ПланыОбмена.ЗарегистрироватьИзменения(Узел, Ссылка);
    {_result('"РЕГИСТРАЦИЯ" + Символы.Таб + "да"')}
    """
    return _emit("register", body)


def unregister_code(plan: str, node_code: str, metadata: str, ref: str) -> str:
    """Снять регистрацию только своего объекта на узле и прочитать, что её не осталось.

    Нужно проверке регистрации при записи: регистрация, оставшаяся от прежнего обмена,
    иначе выглядит как сделанная этой записью.
    """
    body = f"""
    Узел = ПланыОбмена[{bsl_expr(plan)}].НайтиПоКоду({bsl_expr(node_code)});
    Если Узел.Пустая() Тогда
    ВызватьИсключение "Нет узла " + {bsl_expr(node_code)};
    КонецЕсли;
    МенеджерОбъекта = ОбщегоНазначения.МенеджерОбъектаПоПолномуИмени({bsl_expr(metadata)});
    Ссылка = МенеджерОбъекта.ПолучитьСсылку(Новый УникальныйИдентификатор({bsl_expr(ref)}));
    ПланыОбмена.УдалитьРегистрациюИзменений(Узел, Ссылка);
    ЗапросОстатка = Новый Запрос;
    ЗапросОстатка.Текст = "ВЫБРАТЬ ПЕРВЫЕ 1 1 КАК Признак ИЗ {metadata}.Изменения КАК
    Изменения ГДЕ Изменения.Узел = &Узел И Изменения.Ссылка = &Ссылка";
    ЗапросОстатка.УстановитьПараметр("Узел", Узел);
    ЗапросОстатка.УстановитьПараметр("Ссылка", Ссылка);
    Осталось = ?(ЗапросОстатка.Выполнить().Пустой(), 0, 1);
    {_result('"ОСТАЛОСЬ" + Символы.Таб + Формат(Осталось, "ЧН=0; ЧГ=")')}
    """
    return _emit("unregister", body)


def changes_code(plan: str, node_code: str, metadata: str, ref: str) -> str:
    """Регистрации узла чтением, без `ВыбратьИзменения`.

    Свой объект — таблица изменений его типа (`Справочник.Имя.Изменения`).
    Чужие регистрации — обход `Метаданные.ПланыОбмена[план].Состав`: у ссылочных
    видов в таблице есть `Ссылка`, у регистров и констант её нет, поэтому чужие
    строки читаются как `ВЫБРАТЬ 1`, без поля `Ссылка`. Таблицы изменений плана
    обмена платформа не имеет.
    """
    body = f"""
    Узел = ПланыОбмена[{bsl_expr(plan)}].НайтиПоКоду({bsl_expr(node_code)});
    Если Узел.Пустая() Тогда
    ВызватьИсключение "Нет узла " + {bsl_expr(node_code)};
    КонецЕсли;
    Если Узел = ПланыОбмена[{bsl_expr(plan)}].ЭтотУзел() Тогда
    ВызватьИсключение "узел этой базы";
    КонецЕсли;
    Найдено = 0;
    Чужие = 0;
    ЗапросСвоего = Новый Запрос;
    ЗапросСвоего.Текст = "ВЫБРАТЬ Изменения.Ссылка КАК Ссылка ИЗ {metadata}.Изменения КАК
    Изменения ГДЕ Изменения.Узел = &Узел";
    ЗапросСвоего.УстановитьПараметр("Узел", Узел);
    ВыборкаСвоего = ЗапросСвоего.Выполнить().Выбрать();
    Пока ВыборкаСвоего.Следующий() Цикл
    Если Строка(ВыборкаСвоего.Ссылка.УникальныйИдентификатор()) = {bsl_expr(ref)} Тогда
    Найдено = Найдено + 1;
    Иначе
    Чужие = Чужие + 1;
    КонецЕсли;
    КонецЦикла;
    Для Каждого ЭлементСостава Из Метаданные.ПланыОбмена[{bsl_expr(plan)}].Состав Цикл
    ПолноеИмя = ЭлементСостава.Метаданные.ПолноеИмя();
    Если ПолноеИмя = {bsl_expr(metadata)} Тогда
    Продолжить;
    КонецЕсли;
    Если СтрНайти(ПолноеИмя, Символ(34)) > 0 Или СтрНайти(ПолноеИмя, " ") > 0 Тогда
    Продолжить;
    КонецЕсли;
    ЗапросСостава = Новый Запрос;
    ЗапросСостава.Текст = "ВЫБРАТЬ 1 КАК Признак ИЗ " + ПолноеИмя + ".Изменения КАК Изменения
    ГДЕ Изменения.Узел = &Узел";
    ЗапросСостава.УстановитьПараметр("Узел", Узел);
    ВыборкаСостава = ЗапросСостава.Выполнить().Выбрать();
    Пока ВыборкаСостава.Следующий() Цикл
    Чужие = Чужие + 1;
    КонецЦикла;
    КонецЦикла;
    {
        _result(
            '"НАЙДЕНО" + Символы.Таб + Формат(Найдено, "ЧН=0; ЧГ=") + Символы.ПС + '
            '"ЧУЖИЕ" + Символы.Таб + Формат(Чужие, "ЧН=0; ЧГ=")'
        )
    }
    """
    return _emit("changes", body)


def export_code(plan: str, node_code: str) -> str:
    body = f"""
    Узел = ПланыОбмена[{bsl_expr(plan)}].НайтиПоКоду({bsl_expr(node_code)});
    Если Узел.Пустая() Тогда
    ВызватьИсключение "Нет узла " + {bsl_expr(node_code)};
    КонецЕсли;
    Адрес = "";
    ОбменДаннымиСервер.ВыполнитьВыгрузкуДляУзлаИнформационнойБазыВоВременноеХранилище(
    {bsl_expr(plan)}, {bsl_expr(node_code)}, Адрес);
    Данные = ПолучитьИзВременногоХранилища(Адрес);
    ИмяФайла = ПолучитьИмяВременногоФайла("xml");
    Данные.Записать(ИмяФайла);
    УзелПосле = ПланыОбмена[{bsl_expr(plan)}].НайтиПоКоду({bsl_expr(node_code)});
    {
        _result(
            '"ФАЙЛ" + Символы.Таб + ИмяФайла + Символы.ПС + "РАЗМЕР" + Символы.Таб + '
            'Формат(Данные.Размер(), "ЧН=0; ЧГ=") + Символы.ПС + "НОМЕР" + Символы.Таб + '
            'Формат(УзелПосле.НомерОтправленного, "ЧН=0; ЧГ=")'
        )
    }
    """
    return _emit("export", body)


def read_chunk_code(path: str, offset: int, length: int) -> str:
    body = f"""
    ИмяФайлаЧтения = {bsl_expr(path)};
    Смещение = {offset};
    ДлинаЧтения = {length};
    Данные = Новый ДвоичныеДанные(ИмяФайлаЧтения);
    Поток = Данные.ОткрытьПотокДляЧтения();
    Поток.Перейти(Смещение, ПозицияВПотоке.Начало);
    Буфер = Новый БуферДвоичныхДанных(ДлинаЧтения);
    Прочитано = Поток.Прочитать(Буфер, 0, ДлинаЧтения);
    Поток.Закрыть();
    Если Прочитано = 0 Тогда
    Кусок = "";
    Иначе
    Если Прочитано < ДлинаЧтения Тогда
    Буфер = Буфер.Прочитать(0, Прочитано);
    КонецЕсли;
    Кусок = Base64Строка(ПолучитьДвоичныеДанныеИзБуфераДвоичныхДанных(Буфер));
    Кусок = СтрЗаменить(СтрЗаменить(Кусок, Символы.ВК, ""), Символы.ПС, "");
    КонецЕсли;
    {
        _result(
            '"КУСОК" + Символы.Таб + Кусок + Символы.ПС + "ПРОЧИТАНО" + Символы.Таб + '
            'Формат(Прочитано, "ЧН=0; ЧГ=")'
        )
    }
    """
    return _emit("read-chunk", body)


def delete_file_code(path: str) -> str:
    body = f"""
    УдалитьФайлы({bsl_expr(path)});
    {_result('"УДАЛЕН" + Символы.Таб + "да"')}
    """
    return _emit("delete-file", body)


def begin_file_code() -> str:
    body = f"""
    ИмяФайла = ПолучитьИмяВременногоФайла("xml");
    Поток = Новый ФайловыйПоток(ИмяФайла, РежимОткрытияФайла.Создать);
    Поток.Закрыть();
    {_result('"ФАЙЛ" + Символы.Таб + ИмяФайла')}
    """
    return _emit("begin-file", body)


def append_chunk_code(path: str, payload: str) -> str:
    body = f"""
    Поток = Новый ФайловыйПоток({bsl_expr(path)}, РежимОткрытияФайла.Дописать);
    Запись = Новый ЗаписьДанных(Поток);
    Кусок = СтрЗаменить(СтрЗаменить({bsl_expr(payload)}, Символы.ВК, ""), Символы.ПС, "");
    Запись.Записать(Base64Значение(Кусок));
    Запись.Закрыть();
    Поток.Закрыть();
    {_result('"ДОПИСАН" + Символы.Таб + "да"')}
    """
    return _emit("append-chunk", body)


def file_size_code(path: str) -> str:
    """Размер файла на сервере после передачи частями."""
    body = f"""
    ДанныеФайла = Новый ДвоичныеДанные({bsl_expr(path)});
    {_result('"РАЗМЕР" + Символы.Таб + Формат(ДанныеФайла.Размер(), "ЧН=0; ЧГ=")')}
    """
    return _emit("file-size", body)


def import_code(plan: str, node_code: str, path: str) -> str:
    body = f"""
    ИмяФайла = {bsl_expr(path)};
    ПараметрыОбмена = ОбменДаннымиСервер.ПараметрыОбменаДаннымиЧерезФайлИлиСтроку();
    ПараметрыОбмена.ПолноеИмяФайлаСообщенияОбмена = ИмяФайла;
    ПараметрыОбмена.ДействиеПриОбмене = Перечисления.ДействияПриОбмене.ЗагрузкаДанных;
    ПараметрыОбмена.ИмяПланаОбмена = {bsl_expr(plan)};
    ПараметрыОбмена.КодУзлаИнформационнойБазы = {bsl_expr(node_code)};
    Попытка
    ОбменДаннымиСервер.ВыполнитьОбменДаннымиДляУзлаИнформационнойБазыЧерезФайлИлиСтроку(ПараметрыОбмена);
    Исключение
    УдалитьФайлы(ИмяФайла);
    ВызватьИсключение;
    КонецПопытки;
    УдалитьФайлы(ИмяФайла);
    Узел = ПланыОбмена[{bsl_expr(plan)}].НайтиПоКоду({bsl_expr(node_code)});
    ЗапросСостояния = Новый Запрос;
    ЗапросСостояния.Текст = "ВЫБРАТЬ ПЕРВЫЕ 1 Состояния.РезультатВыполненияОбмена КАК Результат ИЗ
    РегистрСведений.СостоянияОбменовДанными КАК Состояния ГДЕ Состояния.УзелИнформационнойБазы =
    &Узел И Состояния.ДействиеПриОбмене = ЗНАЧЕНИЕ(Перечисление.ДействияПриОбмене.ЗагрузкаДанных)";
    ЗапросСостояния.УстановитьПараметр("Узел", Узел);
    ВыборкаСостояния = ЗапросСостояния.Выполнить().Выбрать();
    ИтогЗагрузки = "";
    Если ВыборкаСостояния.Следующий() Тогда
    ИтогЗагрузки = ОбщегоНазначения.ИмяЗначенияПеречисления(ВыборкаСостояния.Результат);
    КонецЕсли;
    {
        _result(
            '"РЕЗУЛЬТАТ" + Символы.Таб + ИтогЗагрузки + Символы.ПС + "НОМЕР" + Символы.Таб + '
            'Формат(Узел.НомерПринятого, "ЧН=0; ЧГ=")'
        )
    }
    """
    return _emit("import", body)


def query_code(metadata: str, ref: str, attribute: str) -> str:
    body = f"""
    МенеджерОбъекта = ОбщегоНазначения.МенеджерОбъектаПоПолномуИмени({bsl_expr(metadata)});
    Ссылка = МенеджерОбъекта.ПолучитьСсылку(Новый УникальныйИдентификатор({bsl_expr(ref)}));
    ЗапросОбъекта = Новый Запрос;
    ЗапросОбъекта.Текст = "ВЫБРАТЬ О.Ссылка КАК Ссылка, О.{attribute} КАК Значение ИЗ {metadata}
    КАК О ГДЕ О.Ссылка = &Ссылка";
    ЗапросОбъекта.УстановитьПараметр("Ссылка", Ссылка);
    ТаблицаОбъекта = ЗапросОбъекта.Выполнить().Выгрузить();
    Если ТаблицаОбъекта.Количество() = 0 Тогда
    {_result('"СТРОК" + Символы.Таб + "0"')}
    Иначе
    {
        _result(
            '"СТРОК" + Символы.Таб + Формат(ТаблицаОбъекта.Количество(), "ЧН=0; ЧГ=") + '
            'Символы.ПС + "УИД" + Символы.Таб + '
            "Строка(ТаблицаОбъекта[0].Ссылка.УникальныйИдентификатор()) + "
            'Символы.ПС + "ЗНАЧЕНИЕ" + Символы.Таб + ' + _show_bsl("ТаблицаОбъекта[0].Значение")
        )
    }
    КонецЕсли;
    """
    return _emit("query", body)


def delete_object_code(metadata: str, ref: str, name: str) -> str:
    """Удаление только если наименование совпало с журналом. Иначе объект не трогается."""
    body = f"""
    МенеджерОбъекта = ОбщегоНазначения.МенеджерОбъектаПоПолномуИмени({bsl_expr(metadata)});
    Ссылка = МенеджерОбъекта.ПолучитьСсылку(Новый УникальныйИдентификатор({bsl_expr(ref)}));
    ОбъектДанных = Ссылка.ПолучитьОбъект();
    Если ОбъектДанных = Неопределено Тогда
    {_result('"УДАЛЕН" + Символы.Таб + "нет"')}
    ИначеЕсли ОбъектДанных.Наименование <> {bsl_expr(name)} Тогда
    {_result('"УДАЛЕН" + Символы.Таб + "чужое"')}
    Иначе
    ОбъектДанных.Удалить();
    {_result('"УДАЛЕН" + Символы.Таб + "да"')}
    КонецЕсли;
    """
    return _emit("delete-object", body)


def delete_node_code(plan: str, code: str) -> str:
    """Удаление узла только с наименованием этого скрипта. Чужое наименование не трогается."""
    body = f"""
    Узел = ПланыОбмена[{bsl_expr(plan)}].НайтиПоКоду({bsl_expr(code)});
    Если Узел.Пустая() Тогда
    {_result('"УДАЛЕН" + Символы.Таб + "нет"')}
    Иначе
    ОбъектУзла = Узел.ПолучитьОбъект();
    Если ОбъектУзла = Неопределено Тогда
    {_result('"УДАЛЕН" + Символы.Таб + "нет"')}
    ИначеЕсли ОбъектУзла.Наименование <> {bsl_expr(NODE_NAME)} Тогда
    {_result('"УДАЛЕН" + Символы.Таб + "чужое"')}
    Иначе
    ОбменДаннымиСервер.УдалитьНастройкуСинхронизации(Узел);
    {_result('"УДАЛЕН" + Символы.Таб + "да"')}
    КонецЕсли;
    КонецЕсли;
    """
    return _emit("delete-node", body)


def registrations_code(metadata: str, ref: str) -> str:
    """На каких узлах планов с этим типом в составе объект ещё зарегистрирован.

    Запрос только на чтение таблицы изменений объекта. `ВыбратьИзменения` не вызывается.
    """
    body = f"""
    Т = Новый Массив;
    ОписаниеОбъекта = Метаданные.НайтиПоПолномуИмени({bsl_expr(metadata)});
    МенеджерОбъекта = ОбщегоНазначения.МенеджерОбъектаПоПолномуИмени({bsl_expr(metadata)});
    Ссылка = МенеджерОбъекта.ПолучитьСсылку(Новый УникальныйИдентификатор({bsl_expr(ref)}));
    Если ОписаниеОбъекта <> Неопределено Тогда
    Для Каждого План Из Метаданные.ПланыОбмена Цикл
    ВСоставе = Ложь;
    Для Каждого ЭлементСостава Из План.Состав Цикл
    Если ЭлементСостава.Метаданные = ОписаниеОбъекта Тогда
    ВСоставе = Истина;
    КонецЕсли;
    КонецЦикла;
    Если Не ВСоставе Тогда
    Продолжить;
    КонецЕсли;
    ВыборкаУзлов = ПланыОбмена[План.Имя].Выбрать();
    Пока ВыборкаУзлов.Следующий() Цикл
    ЗапросРегистрации = Новый Запрос;
    ЗапросРегистрации.Текст = "ВЫБРАТЬ ПЕРВЫЕ 1 1 КАК Признак ИЗ {metadata}.Изменения КАК
    Изменения ГДЕ Изменения.Узел = &Узел И Изменения.Ссылка = &Ссылка";
    ЗапросРегистрации.УстановитьПараметр("Узел", ВыборкаУзлов.Ссылка);
    ЗапросРегистрации.УстановитьПараметр("Ссылка", Ссылка);
    Если Не ЗапросРегистрации.Выполнить().Пустой() Тогда
    Т.Добавить("РЕГ" + Символы.Таб + План.Имя + Символы.Таб + СокрЛП(ВыборкаУзлов.Код));
    КонецЕсли;
    КонецЦикла;
    КонецЦикла;
    КонецЕсли;
    {_result("СтрСоединить(Т, Символы.ПС)")}
    """
    return _emit("registrations", body)


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname


def _object_key(element: etree._Element) -> str:
    """`КлючевыеСвойства/Ссылка` прямого потомка объекта, не ссылка вложенного родителя."""
    for child in element:
        if _local(child) != "КлючевыеСвойства":
            continue
        for key in child:
            if _local(key) == "Ссылка":
                return (key.text or "").strip()
    return ""


def check_message(
    payload: bytes,
    *,
    version: str,
    number: int,
    sender: str,
    receiver: str,
    plan: str,
    metadata: str,
    ref: str,
    path: str,
    value: str | None,
) -> tuple[bool, str]:
    """Сообщение: URI версии, номер, узлы, план, объект по ключу и путь свойства.

    Объект — прямой потомок тела нужного типа, идентификатор только в его
    `КлючевыеСвойства/Ссылка`. Два таких элемента — провал. Ссылка вложенного
    родителя не считается этим объектом.

    Возвращает `(свойство есть, текст значения)`. `value is None` — шаг отсутствия:
    наличие свойства не ошибка само по себе, его фиксирует вызывающий.
    """
    try:
        root = etree.fromstring(payload, _PARSER)
    except etree.XMLSyntaxError as error:
        raise StageFailed(f"сообщение не XML: {error}") from error
    ns = {"msg": MSG_NS}
    fmt = (root.findtext("msg:Header/msg:Format", namespaces=ns) or "").strip()
    expected = format_uri(version)
    if fmt != expected:
        raise StageFailed(f"пространство имён формата {fmt or 'пусто'}, ожидалось {expected}")
    got_number = (
        root.findtext("msg:Header/msg:Confirmation/msg:MessageNo", namespaces=ns) or ""
    ).strip()
    if got_number != str(number):
        raise StageFailed(f"номер сообщения {got_number or 'пусто'}, ожидалось {number}")
    got_from = (root.findtext("msg:Header/msg:Confirmation/msg:From", namespaces=ns) or "").strip()
    got_to = (root.findtext("msg:Header/msg:Confirmation/msg:To", namespaces=ns) or "").strip()
    if got_from != sender or got_to != receiver:
        raise StageFailed(f"узлы сообщения From={got_from or 'пусто'} To={got_to or 'пусто'}")
    got_plan = (
        root.findtext("msg:Header/msg:Confirmation/msg:ExchangePlan", namespaces=ns) or ""
    ).strip()
    if got_plan != plan:
        raise StageFailed(f"план сообщения {got_plan or 'пусто'}, случай задаёт {plan}")
    body = next((child for child in root if _local(child) == "Body"), None)
    if body is None:
        raise StageFailed("в сообщении нет тела")
    body_ns = (body.nsmap.get(None) or "").strip()
    if body_ns != expected:
        raise StageFailed(f"пространство имён тела {body_ns or 'пусто'}")
    found = [
        element
        for element in body
        if _local(element) == metadata and _object_key(element).casefold() == ref.casefold()
    ]
    if not found:
        raise StageFailed(f"в сообщении нет объекта {ref}")
    if len(found) > 1:
        raise StageFailed(f"в сообщении несколько объектов {ref}")
    cursor: etree._Element | None = found[0]
    for part in path.split("/"):
        if cursor is None:
            break
        cursor = next((child for child in cursor if _local(child) == part), None)
    if cursor is None:
        if value is not None:
            raise StageFailed(f"в сообщении нет свойства {path}")
        return False, ""
    text = "".join(str(part) for part in cursor.itertext())
    if value is not None and text != value:
        raise StageFailed(f"свойство {path}={show(text)}, ожидалось {show(value)}")
    return True, text


def packages_dir(dump: Path) -> Path:
    nested = dump / "XDTOPackages"
    if nested.is_dir():
        return nested
    if dump.name == "XDTOPackages" and dump.is_dir():
        return dump
    raise StageFailed(f"в выгрузке нет каталога пакетов формата: {dump.name}")


def schema_property_path(dump: Path, version: str, owner: str, prop: str) -> str:
    """Физический путь свойства в пакете версии. Несколько пакетов одной версии — ошибка."""
    folder = packages_dir(dump)
    chosen: list[Path] = []
    catalog: dict[str, list[Path]] = {}
    expected = tuple(int(part) for part in version.split("."))
    for description in sorted(folder.glob("*.xml")):
        try:
            _name, uri, _revision, _source = metadata(description)
        except Exception as error:
            raise StageFailed(f"описание пакета не читается: {error}") from error
        catalog.setdefault(uri, []).append(description)
        match = re.search(r"/EnterpriseData/(\d+(?:\.\d+)*)/?$", uri)
        if match and tuple(int(part) for part in match.group(1).split(".")) == expected:
            chosen.append(description)
    if not chosen:
        raise StageFailed(f"в выгрузке нет пакета EnterpriseData/{version}")
    if len(chosen) > 1:
        raise StageFailed(f"несколько пакетов EnterpriseData/{version}")

    def locate(uri: str) -> Path | None:
        items = catalog.get(uri, [])
        if len(items) > 1:
            raise StageFailed(f"URI импорта {uri} указан в нескольких пакетах")
        return items[0] if items else None

    try:
        schema = load_schema(chosen[0], locate_import=locate)
    except Exception as error:
        raise StageFailed(f"схема версии {version} не загружена: {error}") from error
    resolved = resolve_property(schema, QName(schema.base_namespace, owner), prop)
    if resolved.status != "resolved" or len(resolved.physical_paths) != 1:
        raise StageFailed(
            f"свойство {prop} в схеме версии {version}: {resolved.status}"
            + (f" ({resolved.reason})" if resolved.reason else "")
        )
    return "/".join(part.local for part in resolved.physical_paths[0])


def resolved_property_path(
    case: Case, source_dump: Path | None, target_dump: Path | None
) -> tuple[str, str]:
    """Путь в сообщении и пометка проверки по схеме. Без выгрузки схема не читается."""
    dumps = [("источника", source_dump), ("приёмника", target_dump)]
    present = [(label, path) for label, path in dumps if path is not None]
    if not present:
        return case.property_path, "по схеме не проверено"
    found: list[tuple[str, str]] = []
    for label, path in present:
        physical = schema_property_path(
            path, case.format_version, case.obj.format_name, case.format_property
        )
        found.append((label, physical))
    paths = {item[1] for item in found}
    if len(paths) > 1:
        raise StageFailed("пути свойства в схемах источника и приёмника различаются")
    physical = found[0][1]
    if physical != case.property_path:
        raise StageFailed(f"путь схемы {physical} не совпадает с путём случая {case.property_path}")
    return physical, "по схеме проверено"


def _identifier(value: object, where: str) -> str:
    if not isinstance(value, str) or _IDENT.fullmatch(value) is None:
        raise CaseError(f"{where}: нужно имя 1С")
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise CaseError(f"{where}: ожидается строка")
    return value


def _flag(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise CaseError(f"{where}: ожидается да или нет")
    return value


def _mapping(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CaseError(f"{where}: ожидается объект")
    return value


def _exact_keys(data: Mapping[str, object], allowed: set[str], where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        extra = sorted(unknown)[0]
        place = f"{where}.{extra}" if where else extra
        raise CaseError(f"неизвестное поле случая: {place}")


def _need(data: Mapping[str, object], key: str, where: str) -> object:
    if key not in data:
        place = f"{where}.{key}" if where else key
        raise CaseError(f"нет поля случая: {place}")
    return data[key]


def _plain(value: str, where: str) -> str:
    if not value.strip() or any(ord(char) < 32 for char in value):
        raise CaseError(f"{where}: пусто или есть перевод строки")
    return value


def _readable(value: str, where: str) -> str:
    """Отказ, если символ режет строку кода (`one_line`) или подменяется при разборе ответа."""
    for char in _DISTORT:
        if char in value:
            raise CaseError(f"{where}: символ, который протокол искажает")
    return value


def _metadata_text(value: object, where: str) -> str:
    text = _text(value, where)
    if _META.fullmatch(text) is None:
        raise CaseError(f"{where}: нужно Справочник.Имя или Документ.Имя")
    return text


def _type_name(value: object, where: str) -> str:
    text = _text(value, where)
    if _TYPE_NAME.fullmatch(text) is None:
        raise CaseError(f"{where}: нужно имя типа")
    return text


def _rule(data: object, where: str) -> RuleSide:
    body = _mapping(data, where)
    _exact_keys(body, {"manager", "pko", "direction"}, where)
    direction = _text(_need(body, "direction", where), f"{where}.direction")
    if direction not in ("Отправка", "Получение"):
        raise CaseError(f"{where}.direction: нужно Отправка или Получение")
    return RuleSide(
        _identifier(_need(body, "manager", where), f"{where}.manager"),
        _identifier(_need(body, "pko", where), f"{where}.pko"),
        direction,
    )


def load_case(path: Path) -> Case:
    """JSON случая. Неизвестное или пропущенное поле — ошибка, кода 1С в случае нет."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CaseError(f"не прочитан файл случая {path.name}") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CaseError(f"случай не JSON: {error.msg}") from error
    data = _mapping(parsed, "случай")
    allowed = {
        "plan",
        "format_version",
        "manager_interface",
        "format_property",
        "property_path",
        "source_attribute",
        "target_attribute",
        "absent",
        "absent_step",
        "check_registration_on_write",
        "object",
        "values",
        "nodes",
        "rules",
        "source_dump",
        "target_dump",
    }
    _exact_keys(data, allowed, "")
    interface = _text(_need(data, "manager_interface", ""), "manager_interface")
    if interface not in ("1", "2"):
        raise CaseError(
            "manager_interface: интерфейс 3 этим скриптом не проверяется, "
            "заполнитель требует компоненты обмена"
        )
    version = _text(_need(data, "format_version", ""), "format_version")
    if _VERSION.fullmatch(version) is None:
        raise CaseError("format_version: нужна версия вида 1.20")
    path_text = _text(_need(data, "property_path", ""), "property_path")
    parts = path_text.split("/")
    if not parts or any(_IDENT.fullmatch(part) is None for part in parts):
        raise CaseError("property_path: нужен путь из имён через /")
    absent = _text(_need(data, "absent", ""), "absent")
    if absent not in ("keep", "clear", "unknown"):
        raise CaseError("absent: нужно keep, clear или unknown")
    absent_step = _flag(_need(data, "absent_step", ""), "absent_step")
    obj_body = _mapping(_need(data, "object", ""), "object")
    _exact_keys(
        obj_body,
        {"metadata", "create", "name", "ref", "target_metadata", "format_name"},
        "object",
    )
    metadata_name = _metadata_text(_need(obj_body, "metadata", "object"), "object.metadata")
    target_metadata = metadata_name
    if "target_metadata" in obj_body:
        target_metadata = _metadata_text(obj_body["target_metadata"], "object.target_metadata")
    format_name = metadata_name
    if "format_name" in obj_body:
        format_name = _type_name(obj_body["format_name"], "object.format_name")
    create = _flag(_need(obj_body, "create", "object"), "object.create")
    if create and not metadata_name.startswith("Справочник."):
        raise CaseError(
            "object.create: создание тестового объекта поддержано для справочника; "
            "документ задаётся уникальным идентификатором"
        )
    name = obj_body.get("name")
    ref = obj_body.get("ref")
    if create:
        if ref is not None:
            raise CaseError("object.ref: при создании идентификатор задаёт база, поле не нужно")
        if not isinstance(name, str) or not name.strip():
            raise CaseError("нет поля случая: object.name")
        object_name = _readable(name, "object.name")
        object_ref = None
    else:
        if name is not None:
            raise CaseError("object.name: имя нужно только при создании")
        if not isinstance(ref, str) or _UUID.fullmatch(ref) is None:
            raise CaseError("object.ref: нужен уникальный идентификатор")
        object_name = None
        object_ref = ref
    values_body = _mapping(_need(data, "values", ""), "values")
    value_keys = {"v1", "v2", "empty"}
    _exact_keys(values_body, value_keys, "values")
    if absent_step and "empty" not in values_body:
        raise CaseError("нет поля случая: values.empty")
    v1 = _readable(_text(_need(values_body, "v1", "values"), "values.v1"), "values.v1")
    v2 = _readable(_text(_need(values_body, "v2", "values"), "values.v2"), "values.v2")
    if v1 == v2:
        raise CaseError("values: v1 и v2 должны различаться")
    empty_text = (
        _readable(_text(values_body["empty"], "values.empty"), "values.empty")
        if "empty" in values_body
        else None
    )
    if empty_text is not None and empty_text in (v1, v2):
        raise CaseError("values: пустое значение должно отличаться от v1 и v2")
    nodes_body = _mapping(_need(data, "nodes", ""), "nodes")
    _exact_keys(nodes_body, {"mode", "source_code", "target_code", "variant"}, "nodes")
    mode = _text(_need(nodes_body, "mode", "nodes"), "nodes.mode")
    if mode not in ("use", "create"):
        raise CaseError("nodes.mode: нужно use или create")
    source_code = _plain(
        _text(_need(nodes_body, "source_code", "nodes"), "nodes.source_code"), "nodes.source_code"
    )
    target_code = _plain(
        _text(_need(nodes_body, "target_code", "nodes"), "nodes.target_code"), "nodes.target_code"
    )
    if source_code == target_code:
        raise CaseError("nodes: коды узлов источника и приёмника совпадают")
    variant = nodes_body.get("variant")
    if variant is not None:
        variant = _identifier(variant, "nodes.variant")
        if mode != "create":
            raise CaseError("nodes.variant: вариант задаётся только при создании узлов")
    rules_body = _mapping(_need(data, "rules", ""), "rules")
    _exact_keys(rules_body, {"source", "target"}, "rules")
    source_dump = data.get("source_dump")
    target_dump = data.get("target_dump")
    if source_dump is not None:
        source_dump = _plain(_text(source_dump, "source_dump"), "source_dump")
    if target_dump is not None:
        target_dump = _plain(_text(target_dump, "target_dump"), "target_dump")
    return Case(
        plan=_identifier(_need(data, "plan", ""), "plan"),
        format_version=version,
        manager_interface=interface,
        format_property=_identifier(_need(data, "format_property", ""), "format_property"),
        property_path=path_text,
        source_attribute=_identifier(_need(data, "source_attribute", ""), "source_attribute"),
        target_attribute=_identifier(_need(data, "target_attribute", ""), "target_attribute"),
        absent=absent,
        absent_step=absent_step,
        check_registration_on_write=_flag(
            _need(data, "check_registration_on_write", ""), "check_registration_on_write"
        ),
        obj=ObjectSpec(
            metadata_name, create, object_name, object_ref, target_metadata, format_name
        ),
        values=Values(v1, v2, empty_text),
        nodes=NodeSpec(
            mode, source_code, target_code, variant if isinstance(variant, str) else None
        ),
        source_rules=_rule(_need(rules_body, "source", "rules"), "rules.source"),
        target_rules=_rule(_need(rules_body, "target", "rules"), "rules.target"),
        source_dump=source_dump if isinstance(source_dump, str) else None,
        target_dump=target_dump if isinstance(target_dump, str) else None,
    )


def _read_state(
    server: DataServer,
    case: Case,
    rules: RuleSide,
    metadata_name: str,
    attribute: str,
    ref: str | None,
    name: str | None,
) -> SideState:
    text = execute(
        server, state_code(case.plan, rules.manager, metadata_name, attribute, ref, name)
    )
    return parse_state(text)


def _format_state(title: str, state: SideState, case: Case) -> list[str]:
    lines = [
        f"{title} {state.configuration} {state.config_version} платформа {state.platform}",
        f"{title} МЕНЕДЖЕР {state.manager} версия {state.manager_version}",
    ]
    for name, value in state.constants.items():
        lines.append(f"{title} КОНСТ {name}={value}")
    for name, active, safe in state.extensions:
        lines.append(f"{title} РАСШ {name} акт={active} безоп={safe}")
    for node in state.nodes:
        lines.append(
            f"{title} УЗЕЛ код={node.code} версия={node.version} "
            f"этот={'Да' if node.this else 'Нет'} "
            f"отпр={node.sent} прин={node.received} настройка={'Да' if node.ready else 'Нет'}"
        )
    if state.object_present is None:
        lines.append(f"{title} ОБЪЕКТ не запрашивался")
    elif not state.object_present:
        lines.append(f"{title} ОБЪЕКТ нет")
    else:
        lines.append(
            f"{title} ОБЪЕКТ есть уид={state.object_ref} значение={show(state.object_value)}"
        )
    if state.name_count is not None:
        lines.append(f"{title} ИМЕНА {state.name_count}")
    _ = case
    return lines


def _opposite(direction: str) -> str:
    return "Получение" if direction == "Отправка" else "Отправка"


def _hook_detail(text: str, rules: RuleSide, interface: str, safe_mode: bool) -> str:
    rows = _rows(text)
    version = _cell(rows, "ВЕРСИЯ")
    if version != interface or rows.get("ИНТЕРФЕЙС"):
        raise StageFailed(f"версия менеджера {version}, случай задаёт интерфейс {interface}")
    found: dict[str, tuple[str, int, int]] = {}
    for item in rows.get("НАПРАВЛЕНИЕ", []):
        if len(item) < 4:
            raise StageFailed("строка заполнителя оборвана")
        if not item[2].isdecimal() or not item[3].isdecimal():
            raise StageFailed("строка заполнителя оборвана")
        found[item[0]] = (item[1], int(item[2]), int(item[3]))
    need = found.get(rules.direction)
    other = found.get(_opposite(rules.direction))
    if need is None or other is None:
        raise StageFailed("заполнитель не вернул оба направления")
    hint = ""
    if safe_mode and need[1] != 1:
        hint = "; расширение в безопасном режиме, перехватчик может не действовать"
    if need[0] != "есть" or need[1] != 1 or need[2] != 1:
        raise StageFailed(
            f"направление {rules.direction}: ПКО {need[0]}, пар {need[1]}, реквизит {need[2]}{hint}"
        )
    if other[1] != 0 or other[2] != 0:
        raise StageFailed(
            f"направление {_opposite(rules.direction)}: пар {other[1]}, реквизит {other[2]}"
        )
    manager_mark = _optional(rows, "МЕНЕДЖЕРВЕРСИИ")
    if manager_mark == "другой":
        raise StageFailed(f"менеджер {rules.manager} не тот, что БСП берёт для версии формата узла")
    note = ""
    if manager_mark == "нет узла":
        note = " менеджер версии: узел ещё не создан"
    elif manager_mark == "тот":
        note = " менеджер версии: тот"
    extra = ""
    duplicates = rows.get("ДУБЛЬ")
    if duplicates and duplicates[0]:
        extra = f"\nПКО ДУБЛЬ {rules.pko} строк={duplicates[0][0]}"
    other = _opposite(rules.direction)
    return f"OK {rules.direction} ПКС=1 {other} ПКС=0 ПКО {rules.pko}{note}{extra}"


def _safe(state: SideState) -> bool:
    return any(safe == "Да" for _name, _active, safe in state.extensions)


def _delete_quiet(server: DataServer, path: str) -> None:
    try:
        server.run(delete_file_code(path))
    except ExchangeCheckError:
        return


def fetch_file(server: DataServer, path: str, size: int) -> bytes:
    """Файл сообщения с сервера частями. Временный файл удаляется в любом исходе."""
    try:
        if size < 0:
            raise StageFailed("размер сообщения отрицательный")
        offset = 0
        parts: list[bytes] = []
        while offset < size:
            length = min(RAW_CHUNK, size - offset)
            text = execute(server, read_chunk_code(path, offset, length))
            rows = _rows(text)
            chunk = base64.b64decode("".join(_cell(rows, "КУСОК").split()), validate=True)
            read = _number(_cell(rows, "ПРОЧИТАНО"), "прочитано")
            if read != len(chunk) or read == 0:
                raise StageFailed("кусок сообщения оборван")
            parts.append(chunk)
            offset += read
        payload = b"".join(parts)
        if len(payload) != size:
            raise StageFailed(f"прочитано {len(payload)} байт из {size}")
        return payload
    finally:
        _delete_quiet(server, path)


def push_file(server: DataServer, payload: bytes) -> str:
    """Файл на сервере приёмника, записанный частями. При ошибке файл удаляется."""
    text = execute(server, begin_file_code())
    path = _cell(_rows(text), "ФАЙЛ")
    try:
        chunks = [
            payload[index : index + RAW_CHUNK] for index in range(0, len(payload), RAW_CHUNK)
        ] or [b""]
        for chunk in chunks:
            piece = "".join(base64.b64encode(chunk).decode("ascii").split())
            execute(server, append_chunk_code(path, piece))
        size_text = execute(server, file_size_code(path))
        size = _number(_cell(_rows(size_text), "РАЗМЕР"), "размер файла")
        if size != len(payload):
            raise StageFailed(f"размер файла на приёмнике {size}, отправлено {len(payload)}")
        return path
    except Exception:
        _delete_quiet(server, path)
        raise


def _write_ledger(run_dir: Path, ledger: Ledger) -> None:
    (run_dir / LEDGER).write_text(
        json.dumps(ledger.dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _diff_lines(label: str, before: SideState, after: SideState) -> list[str]:
    lines: list[str] = []
    names = sorted(set(before.constants) | set(after.constants))
    for name in names:
        left = before.constants.get(name, "не было")
        right = after.constants.get(name, "нет")
        if left != right:
            lines.append(f"ИЗМЕНЕНО {label} константа {name}: {left} → {right}")
    before_nodes = {node.code: node for node in before.nodes}
    after_nodes = {node.code: node for node in after.nodes}
    for code in sorted(set(before_nodes) | set(after_nodes)):
        left = before_nodes.get(code)
        right = after_nodes.get(code)
        if right is None:
            lines.append(f"ИЗМЕНЕНО {label} узел {code}: был, в снимке после его нет")
            continue
        sent_before = "не было" if left is None else str(left.sent)
        recv_before = "не было" if left is None else str(left.received)
        if sent_before != str(right.sent):
            lines.append(
                f"ИЗМЕНЕНО {label} узел {code} номер отправленного: {sent_before} → {right.sent}"
            )
        if recv_before != str(right.received):
            lines.append(
                f"ИЗМЕНЕНО {label} узел {code} номер принятого: {recv_before} → {right.received}"
            )
    safe_before = {name: safe for name, _active, safe in before.extensions}
    safe_after = {name: safe for name, _active, safe in after.extensions}
    for name in sorted(set(safe_before) | set(safe_after)):
        left = safe_before.get(name, "не было")
        right = safe_after.get(name, "нет")
        if left != right:
            lines.append(f"ИЗМЕНЕНО {label} расширение {name} безопасный режим: {left} → {right}")
    if before.object_present and after.object_present and before.object_value != after.object_value:
        lines.append(
            f"ИЗМЕНЕНО {label} {before.object_ref} значение: "
            f"{show(before.object_value)} → {show(after.object_value)}"
        )
    return lines


def _as_lines(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


class Flow:
    """Стадии пишут строки сразу. Любое исключение стадии — её провал, не обрыв прогона."""

    def __init__(self, lines: list[str]) -> None:
        self.failed = False
        self.lines = lines

    def step(self, title: str, action: Callable[[], str], *, force: bool = False) -> None:
        if self.failed and not force:
            self.lines.append(f"{title} ПРОПУЩЕНА предыдущая стадия не пройдена")
            return
        try:
            detail = action()
        except StageFailed as error:
            self.failed = True
            self.lines.append(f"{title} ОШИБКА {error}")
            return
        except Exception as error:
            self.failed = True
            self.lines.append(f"{title} ОШИБКА {error}")
            return
        parts = detail.split("\n")
        self.lines.append(f"{title} {parts[0]}")
        self.lines.extend(parts[1:])


def inspect_command(case: Case, source: DataServer, target: DataServer, lines: list[str]) -> bool:
    lines.append(f"ПЛАН {case.plan}")
    lines.append(f"ВЕРСИЯ {case.format_version}")
    lines.append(f"ИСТОЧНИК {source.label} → ПРИЕМНИК {target.label}")
    ok = True
    for title, server, rules, attribute, metadata_name in (
        ("ИСТОЧНИК", source, case.source_rules, case.source_attribute, case.obj.metadata),
        (
            "ПРИЕМНИК",
            target,
            case.target_rules,
            case.target_attribute,
            case.obj.target_metadata,
        ),
    ):
        try:
            state = parse_state(
                execute(
                    server,
                    state_code(
                        case.plan,
                        rules.manager,
                        metadata_name,
                        attribute,
                        case.obj.ref,
                        case.obj.name,
                    ),
                )
            )
        except StageFailed as error:
            ok = False
            lines.append(f"{title} ОШИБКА {error}")
            continue
        lines.extend(_format_state(title, state, case))
    return ok


def run_command(
    case: Case,
    source: DataServer,
    target: DataServer,
    source_dump: Path | None,
    target_dump: Path | None,
    lines: list[str],
    found: list[Path],
) -> bool:
    run_dir = RUNS / f"ed-exchange-{datetime.now():%Y%m%d-%H%M%S-%f}"
    run_dir.mkdir(parents=True)
    found.append(run_dir)
    ledger = Ledger()
    try:
        return _run_steps(case, source, target, source_dump, target_dump, lines, run_dir, ledger)
    finally:
        _write_ledger(run_dir, ledger)


def _run_steps(
    case: Case,
    source: DataServer,
    target: DataServer,
    source_dump: Path | None,
    target_dump: Path | None,
    lines: list[str],
    run_dir: Path,
    ledger: Ledger,
) -> bool:
    flow = Flow(lines)
    lines.append(f"ПЛАН {case.plan}")
    lines.append(f"ВЕРСИЯ {case.format_version}")
    lines.append(f"ИСТОЧНИК {source.label} → ПРИЕМНИК {target.label}")
    held: dict[str, object] = {}

    def remember_before() -> str:
        source_state = _read_state(
            source,
            case,
            case.source_rules,
            case.obj.metadata,
            case.source_attribute,
            case.obj.ref,
            case.obj.name,
        )
        target_state = _read_state(
            target,
            case,
            case.target_rules,
            case.obj.target_metadata,
            case.target_attribute,
            case.obj.ref,
            case.obj.name,
        )
        held["source_before"] = source_state
        held["target_before"] = target_state
        held["before_lines"] = [
            *_format_state("ИСТОЧНИК", source_state, case),
            *_format_state("ПРИЕМНИК", target_state, case),
        ]
        return "OK"

    def ensure_nodes() -> str:
        source_state = held["source_before"]
        target_state = held["target_before"]
        assert isinstance(source_state, SideState)
        assert isinstance(target_state, SideState)
        source_this = source_state.this_node()
        target_this = target_state.this_node()
        if (
            source_this is None
            or target_this is None
            or not source_this.code
            or not target_this.code
        ):
            raise StageFailed(
                "код этого узла пуст или узел не найден; скрипт код этого узла не меняет"
            )
        if source_this.code != case.nodes.target_code or target_this.code != case.nodes.source_code:
            raise StageFailed(
                "коды этих узлов не зеркальны заданным кодам корреспондентов; "
                "этот узел не изменяется"
            )
        created: list[str] = []
        for label, server, state, code in (
            ("источник", source, source_state, case.nodes.source_code),
            ("приёмник", target, target_state, case.nodes.target_code),
        ):
            node = state.node(code)
            if node is None:
                if case.nodes.mode != "create":
                    raise StageFailed(f"в базе {label} нет узла {code}")
                sync = _truth(state.constants.get("ИспользоватьСинхронизациюДанных", "Да"))
                text, recovered = mutate(
                    server,
                    create_node_code(
                        case.plan,
                        code,
                        case.format_version,
                        case.nodes.variant,
                        not sync,
                    ),
                    state_code(
                        case.plan,
                        case.source_rules.manager
                        if server is source
                        else case.target_rules.manager,
                        case.obj.metadata,
                        case.source_attribute if server is source else case.target_attribute,
                        case.obj.ref,
                        case.obj.name,
                    ),
                    lambda observed, expected=code: (
                        parse_state(observed).node(expected) is not None
                    ),
                )
                if not recovered:
                    rows = _rows(text)
                    if _cell(rows, "СОЗДАН") != "да":
                        raise StageFailed(f"узел {code} не создан")
                ledger.created.append(
                    {"base": server.label, "kind": "node", "plan": case.plan, "code": code}
                )
                _write_ledger(run_dir, ledger)
                created.append(
                    f"{label} создан {code}" + (" повторным чтением" if recovered else "")
                )
                held["created_" + label] = True
            else:
                if node.version != case.format_version:
                    raise StageFailed(
                        f"узел {code} версии {node.version or 'пусто'}, "
                        f"случай задаёт {case.format_version}; существующий узел не изменяется"
                    )
                if not node.ready:
                    raise StageFailed(
                        f"узел {code}: настройка синхронизации не завершена; узел не изменяется"
                    )
                created.append(f"{label} существующий {code}")
                held["created_" + label] = False
        held["sent_before"] = 0
        existing = source_state.node(case.nodes.source_code)
        if existing is not None:
            held["sent_before"] = existing.sent
        return "OK " + "; ".join(created)

    def ensure_object() -> str:
        if not case.obj.create:
            source_state = held["source_before"]
            assert isinstance(source_state, SideState)
            if not source_state.object_present or not case.obj.ref:
                raise StageFailed("объекта источника нет")
            held["ref"] = case.obj.ref
            return f"OK существующий {case.obj.ref}"
        assert case.obj.name is not None
        source_state = held["source_before"]
        assert isinstance(source_state, SideState)
        target_state = held["target_before"]
        assert isinstance(target_state, SideState)
        if source_state.name_count:
            raise StageFailed("объект с таким именем уже есть, чужой объект не используется")
        if target_state.name_count:
            raise StageFailed("в приёмнике уже есть объект с таким именем, создание не выполнялось")

        def created(observed: str) -> bool:
            parsed = parse_state(observed)
            return parsed.name_count == 1 and bool(parsed.name_ref)

        text, recovered = mutate(
            source,
            create_object_code(case.obj.metadata, case.obj.name),
            state_code(
                case.plan,
                case.source_rules.manager,
                case.obj.metadata,
                case.source_attribute,
                None,
                case.obj.name,
            ),
            created,
        )
        if recovered:
            ref = parse_state(text).name_ref
            note = " повторным чтением"
        else:
            rows = _rows(text)
            if _cell(rows, "СОЗДАН") == "занято":
                raise StageFailed("объект с таким именем уже есть, чужой объект не используется")
            if _cell(rows, "СОЗДАН") != "да":
                raise StageFailed("объект не создан")
            ref = _cell(rows, "УИД")
            note = ""
        held["ref"] = ref
        ledger.created.append(
            {
                "base": source.label,
                "kind": "object",
                "metadata": case.obj.metadata,
                "ref": ref,
                "name": case.obj.name or "",
            }
        )
        _write_ledger(run_dir, ledger)
        return f"OK создан {ref}{note}"

    def hook(server: DataServer, rules: RuleSide, state_key: str) -> str:
        state = held[state_key]
        assert isinstance(state, SideState)
        text = execute(
            server,
            hook_code(
                rules.manager,
                case.manager_interface,
                rules.pko,
                case.source_attribute if rules is case.source_rules else case.target_attribute,
                case.format_property,
                case.plan,
                case.nodes.source_code if rules is case.source_rules else case.nodes.target_code,
                case.format_version,
                rules.direction,
            ),
        )
        detail = _hook_detail(text, rules, case.manager_interface, _safe(state))
        if "узел ещё не создан" in detail:
            held["manager_pending"] = True
        return detail

    def confirm_managers() -> str:
        if not held.get("manager_pending"):
            return "OK сверено при перехватчике"
        notes: list[str] = []
        for server, rules, node_code, attribute in (
            (source, case.source_rules, case.nodes.source_code, case.source_attribute),
            (target, case.target_rules, case.nodes.target_code, case.target_attribute),
        ):
            text = execute(
                server,
                hook_code(
                    rules.manager,
                    case.manager_interface,
                    rules.pko,
                    attribute,
                    case.format_property,
                    case.plan,
                    node_code,
                    case.format_version,
                    rules.direction,
                ),
            )
            mark = _cell(_rows(text), "МЕНЕДЖЕРВЕРСИИ")
            if mark != "тот":
                raise StageFailed(f"менеджер {rules.manager}: {mark or 'пусто'}")
            notes.append(rules.manager)
        return "OK " + " ".join(notes)

    def write_value(value: str) -> str:
        ref = str(held["ref"])
        if value == case.values.v1:
            target_before = held["target_before"]
            assert isinstance(target_before, SideState)
            if target_before.object_present and target_before.object_value == case.values.v1:
                raise StageFailed(
                    "в приёмнике до прогона уже значение v1; шаг ничего не докажет, "
                    "запись не выполнялась"
                )

        def applied(observed: str) -> bool:
            parsed = parse_state(observed)
            return parsed.object_present is True and parsed.object_value == value

        if case.check_registration_on_write:
            # Регистрация от прежнего обмена не должна сойти за сделанную этой записью.
            left = execute(
                source,
                unregister_code(case.plan, case.nodes.source_code, case.obj.metadata, ref),
            )
            if _number(_cell(_rows(left), "ОСТАЛОСЬ"), "осталось") != 0:
                raise StageFailed(
                    "прежняя регистрация объекта на узле не снята; запись не выполнялась"
                )

        text, recovered = mutate(
            source,
            write_code(case.obj.metadata, ref, case.source_attribute, value),
            state_code(
                case.plan,
                case.source_rules.manager,
                case.obj.metadata,
                case.source_attribute,
                ref,
                None,
            ),
            applied,
        )
        if recovered:
            return f"OK значение={show(value)} повторным чтением"
        got = decode_cell(_cell(_rows(text), "ЗНАЧЕНИЕ"))
        if got != value:
            raise StageFailed(f"после записи прочитано {show(got)}")
        return f"OK значение={show(got)}"

    def register() -> str:
        ref = str(held["ref"])
        own_node = bool(held.get("created_источник"))
        if not own_node or case.check_registration_on_write:
            text = execute(
                source, changes_code(case.plan, case.nodes.source_code, case.obj.metadata, ref)
            )
            rows = _rows(text)
            foreigners = _number(_cell(rows, "ЧУЖИЕ"), "чужие")
            if foreigners > 0 and not own_node:
                raise StageFailed("на узле есть чужие регистрации; регистрация не выполнялась")
            if case.check_registration_on_write:
                if _number(_cell(rows, "НАЙДЕНО"), "найдено") < 1:
                    raise StageFailed("после записи объект не зарегистрирован")
                return "OK при записи"
        text = execute(
            source,
            register_code(case.plan, case.nodes.source_code, case.obj.metadata, ref, own_node),
        )
        if _cell(_rows(text), "РЕГИСТРАЦИЯ") != "да":
            raise StageFailed("регистрация не подтверждена")
        return f"OK явная очищены={'да' if own_node else 'нет'}"

    def export_new(file_name: str) -> str:
        ref = str(held["ref"])
        if not held.get("created_источник"):
            text = execute(
                source, changes_code(case.plan, case.nodes.source_code, case.obj.metadata, ref)
            )
            if _number(_cell(_rows(text), "ЧУЖИЕ"), "чужие") > 0:
                raise StageFailed(
                    "на узле есть чужие регистрации; очистка разрешена только для узла, "
                    "созданного этим прогоном"
                )
        before_sent = held.get("sent", 0)
        before = before_sent if isinstance(before_sent, int) else 0
        try:
            text = source.run(export_code(case.plan, case.nodes.source_code))
        except ExchangeCheckError as error:
            if not _transport(error):
                raise StageFailed(str(error)) from error
            observed = execute(
                source,
                state_code(
                    case.plan,
                    case.source_rules.manager,
                    case.obj.metadata,
                    case.source_attribute,
                    ref,
                    None,
                ),
            )
            node = parse_state(observed).node(case.nodes.source_code)
            sent = node.sent if node else before
            if sent != before:
                raise StageFailed(
                    f"{error}; номер отправленного {before} → {sent}, файл сообщения не получен, "
                    "повтор выгрузки не выполнялся"
                ) from error
            raise StageFailed(
                f"{error}; номер не изменился, повтор выгрузки не выполнялся"
            ) from error
        rows = _rows(text)
        number = _number(_cell(rows, "НОМЕР"), "номер сообщения")
        size = _number(_cell(rows, "РАЗМЕР"), "размер сообщения")
        remote = _cell(rows, "ФАЙЛ")
        if number <= before:
            _delete_quiet(source, remote)
            raise StageFailed(f"сообщение не новое: номер {number}, предыдущий {before}")
        payload = fetch_file(source, remote, size)
        (run_dir / file_name).write_bytes(payload)
        held["payload"] = payload
        held["number"] = number
        held["sent"] = number
        return f"OK {file_name} {len(payload)} байт номер={number}"

    def check_payload(value: str | None) -> str:
        payload = held.get("payload")
        number = held.get("number")
        if not isinstance(payload, bytes) or not isinstance(number, int):
            raise StageFailed("сообщение не прочитано")
        path, note = resolved_property_path(case, source_dump, target_dump)
        present, text = check_message(
            payload,
            version=case.format_version,
            number=number,
            sender=case.nodes.target_code,
            receiver=case.nodes.source_code,
            plan=case.plan,
            metadata=case.obj.format_name,
            ref=str(held["ref"]),
            path=path,
            value=value,
        )
        if value is None:
            fact = "свойства нет" if not present else f"свойство есть значение={show(text)}"
            return f"ФАКТ {fact} номер={number} путь={path} {note}"
        return f"OK номер={number} путь={path} значение={show(text)} {note}"

    def _record_target(parsed: SideState) -> None:
        """Объект приёмника — в журнал сразу после загрузки, повторным чтением."""
        target_before = held["target_before"]
        if (
            parsed.object_present
            and isinstance(target_before, SideState)
            and not target_before.object_present
            and not held.get("target_object_recorded")
        ):
            ledger.created.append(
                {
                    "base": target.label,
                    "kind": "object",
                    "metadata": case.obj.target_metadata,
                    "ref": str(held["ref"]),
                    "name": parsed.object_name,
                }
            )
            held["target_object_recorded"] = True
            _write_ledger(run_dir, ledger)

    def load_payload() -> str:
        payload = held.get("payload")
        number = held.get("number")
        if not isinstance(payload, bytes) or not isinstance(number, int):
            raise StageFailed("сообщение не прочитано")
        before_state = parse_state(
            execute(
                target,
                state_code(
                    case.plan,
                    case.target_rules.manager,
                    case.obj.target_metadata,
                    case.target_attribute,
                    str(held["ref"]),
                    None,
                ),
            )
        )
        before_node = before_state.node(case.nodes.target_code)
        before_received = before_node.received if before_node is not None else 0
        if before_received >= number:
            raise StageFailed(
                f"номер принятого {before_received} уже не меньше номера сообщения {number}, "
                "загрузка не выполняется"
            )
        remote = push_file(target, payload)

        def received_number(observed: str) -> bool:
            node = parse_state(observed).node(case.nodes.target_code)
            return node is not None and node.received != before_received

        try:
            text, recovered = mutate(
                target,
                import_code(case.plan, case.nodes.target_code, remote),
                state_code(
                    case.plan,
                    case.target_rules.manager,
                    case.obj.target_metadata,
                    case.target_attribute,
                    str(held["ref"]),
                    None,
                ),
                received_number,
            )
        finally:
            _delete_quiet(target, remote)
        if recovered:
            got_node = parse_state(text).node(case.nodes.target_code)
            got = got_node.received if got_node is not None else before_received
            if got != number:
                raise StageFailed(f"номер принятого {got}, сообщение {number}")
            _record_target(parse_state(text))
            held["received"] = got
            return f"OK номер={got} повторным чтением"
        rows = _rows(text)
        result = _cell(rows, "РЕЗУЛЬТАТ")
        got = _number(_cell(rows, "НОМЕР"), "номер принятого")
        if result not in ("Выполнено", "ВыполненоСПредупреждениями"):
            raise StageFailed(f"результат загрузки {result or 'пусто'}")
        if got != number:
            raise StageFailed(f"номер принятого {got}, сообщение {number}")
        _record_target(
            parse_state(
                execute(
                    target,
                    state_code(
                        case.plan,
                        case.target_rules.manager,
                        case.obj.target_metadata,
                        case.target_attribute,
                        str(held["ref"]),
                        None,
                    ),
                )
            )
        )
        held["received"] = got
        return f"OK номер={got}"

    def query_receiver(expected: str | None, fact: bool) -> str:
        ref = str(held["ref"])
        text = execute(target, query_code(case.obj.target_metadata, ref, case.target_attribute))
        rows = _rows(text)
        count = _number(_cell(rows, "СТРОК"), "строк")
        if count != 1:
            raise StageFailed(f"строк={count}")
        uid = _cell(rows, "УИД")
        if uid.casefold() != ref.casefold():
            raise StageFailed(f"идентификатор {uid}")
        value = decode_cell(_cell(rows, "ЗНАЧЕНИЕ"))
        if fact:
            return f"ФАКТ строк=1 уид={uid} значение={show(value)}"
        if expected is None or value != expected:
            raise StageFailed(f"значение={show(value)}, ожидалось {show(expected or '')}")
        return f"OK строк=1 уид={uid} значение={show(value)}"

    def remember_after() -> str:
        ref = held.get("ref")
        ref_text = ref if isinstance(ref, str) else case.obj.ref
        source_after = _read_state(
            source,
            case,
            case.source_rules,
            case.obj.metadata,
            case.source_attribute,
            ref_text,
            case.obj.name,
        )
        target_after = _read_state(
            target,
            case,
            case.target_rules,
            case.obj.target_metadata,
            case.target_attribute,
            ref_text,
            case.obj.name,
        )
        reg_lines: list[str] = []
        if isinstance(ref_text, str):
            for title, server, metadata_name in (
                ("ИСТОЧНИК", source, case.obj.metadata),
                ("ПРИЕМНИК", target, case.obj.target_metadata),
            ):
                reg_text = execute(server, registrations_code(metadata_name, ref_text))
                reg_rows = _rows(reg_text).get("РЕГ") or []
                if not reg_rows:
                    reg_lines.append(f"{title} РЕГИСТРАЦИИ нет")
                    continue
                for item in reg_rows:
                    if len(item) >= 2:
                        reg_lines.append(f"{title} РЕГИСТРАЦИИ {item[0]} {item[1]}")
        held["reg_lines"] = reg_lines
        source_before = held.get("source_before")
        target_before = held.get("target_before")
        changes: list[str] = []
        if isinstance(source_before, SideState):
            changes.extend(_diff_lines(source.label, source_before, source_after))
        if isinstance(target_before, SideState):
            changes.extend(_diff_lines(target.label, target_before, target_after))
        notes: list[str] = []
        for item in changes:
            kind = "constant" if "константа" in item else "state"
            ledger.modified.append({"base": item.split()[1], "kind": kind, "text": item})
        _write_ledger(run_dir, ledger)
        notes.extend(changes or ["ИЗМЕНЕНО нет"])
        notes.extend(reg_lines)
        if ledger.created:
            for item in ledger.created:
                if item["kind"] == "node":
                    notes.append(f"СОЗДАНО {item['base']} узел {item['code']}")
                else:
                    notes.append(f"СОЗДАНО {item['base']} {item['metadata']} {item['ref']}")
        else:
            notes.append("СОЗДАНО нет")
        held["after_notes"] = notes
        return "OK"

    flow.step("СОСТОЯНИЕ ДО", remember_before)
    flow.lines.extend(_as_lines(held.get("before_lines")))
    flow.step("ПЕРЕХВАТЧИК ИСТОЧНИК", lambda: hook(source, case.source_rules, "source_before"))
    flow.step("ПЕРЕХВАТЧИК ПРИЕМНИК", lambda: hook(target, case.target_rules, "target_before"))
    flow.step("УЗЛЫ", ensure_nodes)
    flow.step("МЕНЕДЖЕР ВЕРСИИ", confirm_managers)
    flow.step("ОБЪЕКТ", ensure_object)
    sent_before = held.get("sent_before", 0)
    held["sent"] = sent_before if isinstance(sent_before, int) else 0

    def cycle(label: str, value: str, *, absent: bool = False) -> None:
        flow.step(f"ЗАПИСЬ {label}", lambda value=value: write_value(value))
        flow.step(f"РЕГИСТРАЦИЯ {label}", register)
        flow.step(f"ВЫГРУЗКА {label}", lambda label=label: export_new(f"message-{label}.xml"))
        if absent and case.absent == "unknown":
            flow.step(f"СООБЩЕНИЕ {label}", lambda: check_payload(None))
        elif absent:
            flow.step(f"СООБЩЕНИЕ {label}", lambda: _require_absent())
        else:
            flow.step(f"СООБЩЕНИЕ {label}", lambda value=value: check_payload(value))
        flow.step(f"ЗАГРУЗКА {label}", load_payload)
        if absent and case.absent == "unknown":
            flow.step(f"ПРИЕМНИК {label}", lambda: query_receiver(None, True))
        elif absent and case.absent == "keep":
            flow.step(f"ПРИЕМНИК {label}", lambda: query_receiver(case.values.v2, False))
        elif absent and case.absent == "clear":
            flow.step(f"ПРИЕМНИК {label}", lambda: query_receiver(case.values.empty or "", False))
        else:
            flow.step(f"ПРИЕМНИК {label}", lambda value=value: query_receiver(value, False))

    def _require_absent() -> str:
        detail = check_payload(None)
        if detail.startswith("ФАКТ свойство есть"):
            raise StageFailed(detail.removeprefix("ФАКТ "))
        return detail.replace("ФАКТ ", "OK ", 1)

    cycle("v1", case.values.v1)
    cycle("v2", case.values.v2)
    if case.absent_step:
        cycle("пусто", case.values.empty or "", absent=True)
    else:
        flow.step("ОТСУТСТВИЕ", lambda: "ПРОПУЩЕНА шаг не запрошен случаем")
    flow.step("СОСТОЯНИЕ ПОСЛЕ", remember_after, force=True)
    flow.lines.extend(_as_lines(held.get("after_notes")))
    _write_ledger(run_dir, ledger)
    return not flow.failed


def _own_run_dir(run_dir: Path) -> Path:
    """Только каталог прогона этого скрипта внутри `kdbase/run`."""
    resolved = run_dir.resolve()
    root = RUNS.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise CaseError("каталог очистки должен быть прогоном внутри kdbase/run")
    protocol = resolved / "protocol.txt"
    try:
        text = protocol.read_text(encoding="utf-8")
    except OSError as error:
        raise CaseError("нет protocol.txt этого скрипта") from error
    rows = [line for line in text.splitlines() if line.strip()]
    if not rows or not rows[0].startswith("ПЛАН ") or rows[-1] != DONE or "→ ПРИЕМНИК" not in text:
        raise CaseError("protocol.txt не от этого скрипта")
    return resolved


def _load_ledger(path: Path) -> dict[str, object]:
    try:
        data = json.loads((path / LEDGER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaseError(f"журнал прогона не читается: {path.name}") from error
    if not isinstance(data, dict) or data.get("producer") != PRODUCER:
        raise CaseError("журнал прогона не от этого скрипта")
    return data


def _journal_base(value: object) -> str:
    if not isinstance(value, str):
        raise StageFailed("в журнале нет базы")
    project, dot, base = value.partition(".")
    if dot != "." or _IDENT.fullmatch(project) is None or _IDENT.fullmatch(base) is None:
        raise StageFailed("в журнале база не в виде проект.база")
    return value


def _journal_code(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise StageFailed(f"{where}: код не принят")
    return value


def cleanup_command(run_dir: Path, lines: list[str]) -> bool:
    run_dir = _own_run_dir(run_dir)
    data = _load_ledger(run_dir)
    created = data.get("created")
    modified = data.get("modified")
    if not isinstance(created, list) or not isinstance(modified, list):
        raise CaseError("журнал прогона повреждён")
    ok = True
    servers: dict[str, DataServer] = {}

    def server(label: str) -> DataServer:
        if label not in servers:
            servers[label] = server_for(label, ROOT)
        return servers[label]

    for item in created:
        if not isinstance(item, dict):
            ok = False
            lines.append("ОЧИСТКА ОШИБКА запись журнала не объект")
            continue
        kind = item.get("kind")
        try:
            base = _journal_base(item.get("base"))
            if kind == "object":
                metadata_name = item.get("metadata")
                ref = item.get("ref")
                name = item.get("name")
                if not isinstance(metadata_name, str) or _META.fullmatch(metadata_name) is None:
                    raise StageFailed("запись объекта: тип не принят")
                if not isinstance(ref, str) or _UUID.fullmatch(ref) is None:
                    raise StageFailed("запись объекта: идентификатор не принят")
                if not isinstance(name, str):
                    raise StageFailed("запись объекта: нет наименования")
                text = execute(server(base), delete_object_code(metadata_name, ref, name))
                flag = _cell(_rows(text), "УДАЛЕН")
                if flag == "чужое":
                    ok = False
                    lines.append(
                        f"ОЧИСТКА ПРОПУСК {base} {metadata_name} {ref} "
                        "наименование не совпало с журналом"
                    )
                    continue
                if flag != "да":
                    ok = False
                lines.append(f"ОЧИСТКА {base} {metadata_name} {ref} {flag}")
            elif kind == "node":
                plan = item.get("plan")
                code = _journal_code(item.get("code"), "запись узла")
                if not isinstance(plan, str) or _IDENT.fullmatch(plan) is None:
                    raise StageFailed("запись узла: план не принят")
                text = execute(server(base), delete_node_code(plan, code))
                flag = _cell(_rows(text), "УДАЛЕН")
                if flag == "чужое":
                    ok = False
                    lines.append(f"ОЧИСТКА ПРОПУСК {base} узел {code} наименование не {NODE_NAME}")
                    continue
                if flag != "да":
                    ok = False
                lines.append(f"ОЧИСТКА {base} узел {code} {flag}")
            else:
                raise StageFailed(f"неизвестный вид {kind}")
        except (StageFailed, ExchangeCheckError, ProjectConfigError) as error:
            ok = False
            lines.append(f"ОЧИСТКА ОШИБКА {error}")
    if modified:
        for item in modified:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                lines.append(f"ОСТАВЛЕНО {item['text'].removeprefix('ИЗМЕНЕНО ')}")
            else:
                lines.append("ОСТАВЛЕНО запись изменения без текста")
    else:
        lines.append("ОСТАВЛЕНО нет")
    return ok


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Живая проверка обмена через универсальный формат")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("inspect", "чтение версий, менеджеров, расширений, узлов и объекта"),
        ("run", "сценарий случая: новое сообщение и проверка приёмника"),
    ):
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--source", required=True, help="база-источник <проект>.<база>")
        command.add_argument("--target", required=True, help="база-приёмник <проект>.<база>")
        command.add_argument("--case", required=True, type=Path, help="JSON случая")
        if name == "run":
            command.add_argument(
                "--source-dump", type=Path, help="выгрузка источника для проверки по схеме"
            )
            command.add_argument(
                "--target-dump", type=Path, help="выгрузка приёмника для проверки по схеме"
            )
    cleanup = commands.add_parser("cleanup", help="удалить созданное прогоном по журналу")
    cleanup.add_argument("--run-dir", required=True, type=Path, help="каталог прогона с журналом")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    utf8_stdout()
    ok = False
    lines: list[str] = []
    found: list[Path] = []
    command = ""
    try:
        args = parse_args(argv)
        command = args.command
        if command == "cleanup":
            ok = cleanup_command(args.run_dir, lines)
            found.append(args.run_dir.resolve())
        else:
            case = load_case(args.case)
            source, target = server_for(args.source, ROOT), server_for(args.target, ROOT)
            if command == "inspect":
                ok = inspect_command(case, source, target, lines)
            else:
                source_dump = args.source_dump or (
                    Path(case.source_dump) if case.source_dump else None
                )
                target_dump = args.target_dump or (
                    Path(case.target_dump) if case.target_dump else None
                )
                ok = run_command(case, source, target, source_dump, target_dump, lines, found)
    except Exception as error:
        lines.append(f"ОШИБКА {error}")
    text = "\n".join([*lines, f"ИТОГ {'OK' if ok else 'ОШИБКА'}", DONE])
    print(text)
    if found and command in ("run", "cleanup"):
        file_name = "protocol.txt" if command == "run" else "cleanup.txt"
        with contextlib.suppress(OSError):
            (found[-1] / file_name).write_text(text + "\n", encoding="utf-8", newline="\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
