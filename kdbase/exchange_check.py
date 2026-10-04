"""Живая проверка правил обменом между двумя базами-песочницами (задача #8).

`rules_validate`, `kd_check` и `bsp_check` подтверждают, что правила читаются, но не то, что поиск
в приёмнике находит нужный объект. Здесь — штатный обмен БСП через сообщение: один объект источника
выгружается по узлу плана обмена и загружается в приёмник, затем контрольный запрос в приёмнике.

Всё выполняется внутри баз через HTTP-сервис сервера данных базы (`data_mcp` в projects.yaml,
инструмент `vcexecutecode`): по внешнему соединению (COM) запись в базах с расширениями падает на
подписках, у модулей которых нет флага «Внешнее соединение». Адрес — из .mcp.json проекта, логин —
как у `setup_local.py` (`logins` или .dev.env проекта). Базы — только с ролью «песочница».

Команды (`uv run python kdbase/exchange_check.py …`, базы — `<проект>.<база>`):

- `setup --plan <план> --source <база> --target <база>` — константа
  `ИспользоватьСинхронизациюДанных`, код «этого узла» (пустой — `--source-code` / `--target-code`;
  заданный не меняется), узел корреспондента в каждой базе, признак «настройка завершена», сверка
  идентификаторов узлов.
- `run --plan … --source … --target … --object <Документ.Имя> --ref <уникальный идентификатор>` —
  до загрузки правил проверяется состав плана обмена (успех — строка `СОСТАВ` в протоколе);
  `--source-rules` / `--target-rules` (ZIP комплекта из трёх файлов; без них — правила баз),
  снятие регистраций узла и регистрация объекта (строка `РЕГИСТРАЦИЯ`), выгрузка, загрузка,
  `--query <запрос к приёмнику>` и `--expect-rows N`. Протокол — в stdout,
  сообщение обмена — в `kdbase\\run\\exchange-<время>\\`. Код выхода 0 — «ИТОГ OK», 1 — ошибка.

Точки входа БСП (3.1.12): `ОбменДаннымиСервер` —
`ВыполнитьВыгрузкуДляУзлаИнформационнойБазыВоВременноеХранилище`,
`ВыполнитьОбменДаннымиДляУзлаИнформационнойБазыЧерезФайлИлиСтроку` (`ЗагрузкаДанных`);
`РегистрыСведений.ПравилаДляОбменаДанными.ЗагрузитьКомплектПравил`;
`РегистрыСведений.ОбщиеНастройкиУзловИнформационныхБаз.УстановитьПризнакНастройкаЗавершена`.
Код загрузки комплекта — `bsp_load.load_rules_code(..., write=True)`.
Порядок и грабли — скилл `kd2-exchange-pitfalls`, «Как проверить исправление живым обменом».
"""

import argparse
import base64
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from bsp_load import (
    DataServer,
    ExchangeCheckError,
    bsl,
    guarded,
    load_rules_code,
    server_for,
    short_error,
)
from live_checks import (
    json_code,
    literal,
    object_body,
    read_json,
    require_ok,
    stage,
    text_line,
)
from lxml import etree

from kd2_rules_mcp.console import utf8_stdout

__all__ = ["DataServer", "ExchangeCheckError", "server_for", "short_error"]

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "kdbase" / "run"
DONE = "КОНЕЦ"
NODE_NAME = "kd2 exchange_check"


def this_node_code(plan: str, default: str) -> str:
    """Константа синхронизации и код «этого узла» (пустой — `default`); «OK <код>»."""
    # Константа пишется в режиме загрузки: штатная запись включает связанную функциональность
    # (лицензии тарифа, НСИ) и в песочнице без неё падает; обмену нужен только сам флаг.
    return guarded(
        f"""Если Не ПолучитьФункциональнуюОпцию("ИспользоватьСинхронизациюДанных") Тогда
        М = Константы.ИспользоватьСинхронизациюДанных.СоздатьМенеджерЗначения();
        М.Значение = Истина; М.ОбменДанными.Загрузка = Истина; М.Записать();
        КонецЕсли;
        Этот = ПланыОбмена[{bsl(plan)}].ЭтотУзел().ПолучитьОбъект();
        Если ПустаяСтрока(СокрЛП(Этот.Код)) Тогда
        Этот.Код = {bsl(default)}; Этот.Наименование = {bsl(default)}; Этот.Записать();
        КонецЕсли;
        Результат = "OK " + СокрЛП(Этот.Код);"""
    )


def correspondent_node(plan: str, code: str) -> str:
    """Узел корреспондента `code` и признак «настройка завершена»; «OK <идентификатор узла>»."""
    return guarded(
        f"""Менеджер = ПланыОбмена[{bsl(plan)}];
        Узел = Менеджер.НайтиПоКоду({bsl(code)});
        Если Узел.Пустая() Тогда
        О = Менеджер.СоздатьУзел(); О.Код = {bsl(code)};
        О.Наименование = {bsl(NODE_NAME)}; О.Записать(); Узел = О.Ссылка;
        КонецЕсли;
        РегистрыСведений.ОбщиеНастройкиУзловИнформационныхБаз
        .УстановитьПризнакНастройкаЗавершена(Узел);
        Результат = "OK " + ОбменДаннымиСервер.ИдентификаторЭтогоУзлаДляОбмена(Узел);"""
    )


def load_rules(plan: str, archive: bytes, file_name: str) -> str:
    """Комплект правил (ZIP из трёх файлов) в `ПравилаДляОбменаДанными`, как форма загрузки."""
    return load_rules_code(plan, archive, file_name, write=True)


def plan_content_check(plan: str, full_name: str) -> str:
    """«OK» если объект `full_name` входит в состав плана `plan`, иначе «НЕТ <имя>»."""
    return guarded(
        f"""Объект = Метаданные.НайтиПоПолномуИмени({bsl(full_name)});
        Если Объект = Неопределено Тогда
        Результат = "НЕТ объект не найден";
        ИначеЕсли Метаданные.ПланыОбмена[{bsl(plan)}].Состав.Содержит(Объект) Тогда
        Результат = "OK";
        Иначе
        Результат = "НЕТ " + {bsl(full_name)};
        КонецЕсли;"""
    )


def export_object(plan: str, node_code: str, full_name: str, ref: str) -> str:
    """Снятие регистраций узла, регистрация одного объекта и выгрузка; «OK <сообщение в base64>»."""
    return guarded(
        f"""Узел = ПланыОбмена[{bsl(plan)}].НайтиПоКоду({bsl(node_code)});
        Если Узел.Пустая() Тогда
        ВызватьИсключение "Нет узла " + {bsl(node_code)} + ": выполните setup";
        КонецЕсли;
        Ссылка = ОбщегоНазначения.МенеджерОбъектаПоПолномуИмени({bsl(full_name)})
        .ПолучитьСсылку(Новый УникальныйИдентификатор({bsl(ref)}));
        Если Ссылка.ПолучитьОбъект() = Неопределено Тогда
        ВызватьИсключение "Нет объекта " + {bsl(full_name)} + " " + {bsl(ref)};
        КонецЕсли;
        ПланыОбмена.УдалитьРегистрациюИзменений(Узел);
        ПланыОбмена.ЗарегистрироватьИзменения(Узел, Ссылка);
        Адрес = "";
        ОбменДаннымиСервер.ВыполнитьВыгрузкуДляУзлаИнформационнойБазыВоВременноеХранилище(
        {bsl(plan)}, {bsl(node_code)}, Адрес);
        Результат = "OK " + Base64Строка(ПолучитьИзВременногоХранилища(Адрес));"""
    )


def import_message(plan: str, node_code: str, message: bytes) -> str:
    """Загрузка сообщения в приёмник по узлу отправителя; «OK» и сообщения пользователю."""
    payload = base64.b64encode(message).decode("ascii")
    return guarded(
        f"""ИмяФайла = ПолучитьИмяВременногоФайла("xml");
        Base64Значение({bsl(payload)}).Записать(ИмяФайла);
        П = ОбменДаннымиСервер.ПараметрыОбменаДаннымиЧерезФайлИлиСтроку();
        П.ПолноеИмяФайлаСообщенияОбмена = ИмяФайла;
        П.ДействиеПриОбмене = Перечисления.ДействияПриОбмене.ЗагрузкаДанных;
        П.ИмяПланаОбмена = {bsl(plan)}; П.КодУзлаИнформационнойБазы = {bsl(node_code)};
        Попытка
        ОбменДаннымиСервер.ВыполнитьОбменДаннымиДляУзлаИнформационнойБазыЧерезФайлИлиСтроку(П);
        Исключение
        УдалитьФайлы(ИмяФайла); ВызватьИсключение;
        КонецПопытки;
        УдалитьФайлы(ИмяФайла);
        Тексты = Новый Массив; Тексты.Добавить("OK");
        Для Каждого С Из ПолучитьСообщенияПользователю(Истина) Цикл
        Тексты.Добавить("СООБЩЕНИЕ " + С.Текст);
        КонецЦикла;
        Результат = СтрСоединить(Тексты, Символы.ПС);"""
    )


def query_rows(text: str) -> str:
    """Запрос к базе; «OK <число строк>», затем шапка и строки через « | »."""
    return guarded(
        f"""З = Новый Запрос({bsl(" ".join(text.split()))}); Т = З.Выполнить().Выгрузить();
        Строки = Новый Массив; Имена = Новый Массив;
        Для Каждого К Из Т.Колонки Цикл Имена.Добавить(К.Имя); КонецЦикла;
        Строки.Добавить(СтрСоединить(Имена, " | "));
        Для Каждого С Из Т Цикл
        Значения = Новый Массив;
        Для Каждого К Из Т.Колонки Цикл Значения.Добавить(Строка(С[К.Имя])); КонецЦикла;
        Строки.Добавить(СтрСоединить(Значения, " | "));
        КонецЦикла;
        Результат = "OK " + Формат(Т.Количество(), "ЧН=0; ЧГ=") + Символы.ПС
        + СтрСоединить(Строки, Символы.ПС);"""
    )


def setup(plan: str, source: DataServer, target: DataServer, codes: tuple[str, str]) -> list[str]:
    """Коды этих узлов, узлы корреспондентов и сверка идентификаторов; строки протокола."""
    source_code = source.run(this_node_code(plan, codes[0])).strip()
    target_code = target.run(this_node_code(plan, codes[1])).strip()
    if source_code == target_code:
        raise ExchangeCheckError(f"Коды этих узлов совпадают ({source_code}): нужны разные")
    source_id = source.run(correspondent_node(plan, target_code)).strip()
    target_id = target.run(correspondent_node(plan, source_code)).strip()
    if (source_id, target_id) != (source_code, target_code):
        # Код «этого узла» до двух символов заменяется префиксом (ИдентификаторЭтогоУзлаДляОбмена).
        raise ExchangeCheckError(
            f"Идентификаторы узлов в сообщениях ({source_id}, {target_id}) не совпадают с кодами "
            f"({source_code}, {target_code}): задайте коды длиннее двух символов"
        )
    return [
        f"ИСТОЧНИК {source.label}: этот узел {source_code}, узел приёмника {target_code}",
        f"ПРИЕМНИК {target.label}: этот узел {target_code}, узел источника {source_code}",
    ]


def run(args: argparse.Namespace, source: DataServer, target: DataServer, lines: list[str]) -> bool:
    """Состав плана, правила, выгрузка, загрузка, запрос; строки — в `lines`, результат — успех."""
    lines.append(f"ПЛАН {args.plan}")
    source_code = source.run(this_node_code(args.plan, args.source_code)).strip()
    target_code = target.run(this_node_code(args.plan, args.target_code)).strip()
    lines.append(
        f"ИСТОЧНИК {source.label} ({source_code}) → ПРИЕМНИК {target.label} ({target_code})"
    )
    # `call`, не `run`: ответ «НЕТ …» — не ошибка транспорта, а отказ до записи правил.
    # Всё остальное, кроме «OK» (ошибка 1С, пустой ответ), — сбой шага, как у `run`.
    answer = source.call(plan_content_check(args.plan, args.object)).strip()
    if answer.startswith("НЕТ"):
        lines.append(
            f"ОШИБКА объект {args.object} не входит в состав плана обмена {args.plan} в "
            f"{source.label}: правила не загружались, базы не менялись"
        )
        return False
    if answer != "OK":
        detail = short_error(answer) if answer else "пустой ответ сервера данных"
        raise ExchangeCheckError(f"{source.label}: проверка состава плана: {detail}")
    lines.append(f"СОСТАВ {args.object} входит в план обмена {args.plan} в {source.label}")
    for label, server, archive in (
        ("ИСТОЧНИКА", source, args.source_rules),
        ("ПРИЕМНИКА", target, args.target_rules),
    ):
        if archive is not None:
            server.run(load_rules(args.plan, archive.read_bytes(), archive.name))
            lines.append(f"ПРАВИЛА {label} {archive}: загружены")
    lines.append(f"ОБЪЕКТ {args.object} {args.ref}")
    exported = source.run(export_object(args.plan, target_code, args.object, args.ref))
    lines.append(
        f"РЕГИСТРАЦИЯ узла {target_code} в источнике очищена, зарегистрирован {args.object}"
    )
    message = base64.b64decode(exported)
    run_dir = RUNS / f"exchange-{datetime.now():%Y%m%d-%H%M%S-%f}"
    run_dir.mkdir(parents=True)
    message_file = run_dir / "message.xml"
    message_file.write_bytes(message)
    lines.append(f"ВЫГРУЗКА {message_file} {len(message)} байт")
    ok = True
    try:
        answer = target.run(import_message(args.plan, source_code, message))
        lines.append("ЗАГРУЗКА OK")
        lines += [line for line in answer.splitlines() if line.startswith("СООБЩЕНИЕ")]
    except ExchangeCheckError as error:
        ok = False
        lines.append(f"ЗАГРУЗКА ОШИБКА {error}")
    if args.query:
        count_text, _, table = target.run(query_rows(args.query)).partition("\n")
        count = int(count_text.strip() or 0)
        lines.append(f"ЗАПРОС {count} строк")
        lines += [f"  {row}" for row in table.splitlines()]
        if args.expect_rows is not None and count != args.expect_rows:
            ok = False
            lines.append(f"ОШИБКА ожидалось строк: {args.expect_rows}, получено: {count}")
    return ok


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Живая проверка правил обменом в песочницах")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("setup", "узлы плана обмена"),
        ("run", "выгрузка и загрузка объекта; состав плана проверяется до загрузки правил"),
        ("delete", "пометка или удаление одного объекта с проверкой приёмника"),
    ):
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--plan", required=True, help="имя плана обмена")
        command.add_argument("--source", required=True, help="база-источник <проект>.<база>")
        command.add_argument("--target", required=True, help="база-приёмник <проект>.<база>")
        command.add_argument("--source-code", default="KD2S", help="код этого узла, если пуст")
        command.add_argument("--target-code", default="KD2T", help="код этого узла, если пуст")
        if name in ("run", "delete"):
            command.add_argument("--object", required=True, help="полное имя: Документ.Заказ")
            command.add_argument("--ref", required=True, help="уникальный идентификатор объекта")
            command.add_argument("--source-rules", type=Path, help="ZIP правил для источника")
            command.add_argument("--target-rules", type=Path, help="ZIP правил для приёмника")
            command.add_argument("--query", help="контрольный запрос к приёмнику")
            command.add_argument("--expect-rows", type=int, help="ожидаемое число строк")
        if name == "delete":
            command.add_argument("--mode", required=True, choices=("mark", "delete"))
            command.add_argument("--expect", choices=("marked", "deleted", "kept"))
            command.add_argument(
                "--confirm", action="store_true", help="подтвердить изменение источника"
            )
            command.add_argument(
                "--target-object", help="тип объекта приёмника, по умолчанию --object"
            )
            command.add_argument("--target-ref", help="UUID объекта приёмника, по умолчанию --ref")
    command = commands.add_parser("register", help="проверка регистрации обычной записью")
    command.add_argument("--base", required=True, help="<проект>.<база>")
    command.add_argument("--object", required=True)
    command.add_argument("--ref", required=True)
    command.add_argument("--plan", help="ограничить наблюдение одним планом")
    command.add_argument(
        "--restore", action="store_true", help="снять только добавленные регистрации"
    )
    return parser.parse_args(argv)


def main() -> None:
    utf8_stdout()
    args = parse_args()
    ok = False
    lines: list[str] = []
    if args.command == "delete" and not args.confirm:
        print(
            f"ПРЕДПРОСМОТР {text_line(args.mode)} {text_line(args.object)} {text_line(args.ref)} "
            f"в {text_line(args.source)}; выгрузка в {text_line(args.target)}, "
            f"ожидание {args.expect or ('marked' if args.mode == 'mark' else 'не задано')}; "
            "для выполнения требуется --confirm\nИТОГ НЕ ВЫПОЛНЕНО\nКОНЕЦ"
        )
        sys.exit(2)
    try:
        if args.command == "register":
            server = stage(lines, "ПЕСОЧНИЦА", lambda: server_for(args.base))
            ok = register(args, server, lines)
        elif args.command == "delete":
            source = stage(lines, "ПЕСОЧНИЦА ИСТОЧНИКА", lambda: server_for(args.source))
            target = stage(lines, "ПЕСОЧНИЦА ПРИЕМНИКА", lambda: server_for(args.target))
            ok = delete(args, source, target, lines)
        else:
            source, target = server_for(args.source), server_for(args.target)
            if args.command == "setup":
                codes = (args.source_code, args.target_code)
                lines += [f"ПЛАН {args.plan}", *setup(args.plan, source, target, codes)]
                ok = True
            else:
                ok = run(args, source, target, lines)
    except Exception as error:  # ошибка шага или настроек — в протокол, а не трассировкой
        lines.append(f"ОШИБКА {error}")
    print("\n".join([*lines, f"ИТОГ {'OK' if ok else 'ОШИБКА'}", DONE]))
    sys.exit(0 if ok else 1)


def registration_code(full_name: str, ref: str, plan: str | None) -> str:
    """Снимок без выбора/нумерации сообщения и без явной регистрации объекта."""
    # БСП: ОбменДаннымиСобытия:901–918, 928–942 — таблица <тип>.Изменения,
    # поля Ссылка, Узел; имя таблицы берётся только из найденных метаданных.
    selected = literal(plan) if plan is not None else '""'
    return json_code(
        object_body(full_name, ref)
        + f"""Отбор = {selected}; Данные = Новый Структура;
        Если Отбор <> "" И Метаданные.ПланыОбмена.Найти(Отбор) = Неопределено Тогда
        ВызватьИсключение "Нет плана обмена"; КонецЕсли;
        Для Каждого П Из Метаданные.ПланыОбмена Цикл
        Если Отбор <> "" И П.Имя <> Отбор Тогда Продолжить; КонецЕсли;
        Узлы = Новый Массив;
        Если П.Состав.Содержит(МД) Тогда
        Запрос = Новый Запрос("ВЫБРАТЬ Узел ИЗ " + МД.ПолноеИмя()
        + ".Изменения ГДЕ Ссылка = &Ссылка И Узел ССЫЛКА ПланОбмена." + П.Имя);
        Запрос.УстановитьПараметр("Ссылка", Ссылка); В = Запрос.Выполнить().Выбрать();
        Пока В.Следующий() Цикл
        Узлы.Добавить(Новый Структура("ref,code", Строка(В.Узел.УникальныйИдентификатор()),
        СокрЛП(В.Узел.Код))); КонецЦикла; КонецЕсли;
        Данные.Вставить(П.Имя, Узлы); КонецЦикла;"""
    )


def write_object_code(full_name: str, ref: str) -> str:
    # БСП: КонвертацияОбъектовИнформационныхБаз:1769–1770 — Записать;
    # ОбменДаннымиСобытия:140–157 — подписка удаления; запись наблюдается без её вызова.
    return guarded(object_body(full_name, ref) + ' О.Записать(); Результат = "OK";')


def restore_code(full_name: str, ref: str, added: dict[str, list[dict[str, str]]]) -> str:
    """Снимает только пару узел/ссылка из разницы двух снимков, не весь узел."""
    # БСП: ОбменДаннымиСобытия:723, 728 —
    # УдалитьРегистрациюИзменений с конкретным объектом (платформенный метод).
    body = object_body(full_name, ref)
    for plan, nodes in added.items():
        for node in nodes:
            body += f"""Узел = ПланыОбмена[{literal(plan)}].ПолучитьСсылку(
                Новый УникальныйИдентификатор({literal(node["ref"])}));
                ПланыОбмена.УдалитьРегистрациюИзменений(Узел, Ссылка);"""
    return guarded(body + ' Результат = "OK";')


def _snapshot(server: DataServer, args: argparse.Namespace) -> dict[str, list[dict[str, str]]]:
    data = read_json(server, registration_code(args.object, args.ref, args.plan))
    if not isinstance(data, dict):
        raise ExchangeCheckError("неверный снимок регистрации")
    for nodes in data.values():
        if not isinstance(nodes, list) or any(
            not isinstance(n, dict)
            or not isinstance(n.get("ref"), str)
            or not isinstance(n.get("code"), str)
            for n in nodes
        ):
            raise ExchangeCheckError("неверный список узлов регистрации")
    return data


def register(args: argparse.Namespace, server: DataServer, lines: list[str]) -> bool:
    """До → обычная запись → после → разница; restore не компенсирует изменения подписок.

    В песочнице не должно идти параллельной регистрации того же объекта: разница снимков
    отражает интервал записи, а не доказанную причинность при одновременной работе сеансов.
    """
    lines.append(f"БАЗА {text_line(server.label)}")
    lines.append(f"ОБЪЕКТ {text_line(args.object)} {text_line(args.ref)}")
    before = stage(lines, "РЕГИСТРАЦИЯ ДО", lambda: _snapshot(server, args))
    stage(lines, "ЗАПИСЬ", lambda: require_ok(server, write_object_code(args.object, args.ref)))
    after = stage(lines, "РЕГИСТРАЦИЯ ПОСЛЕ", lambda: _snapshot(server, args))
    if before.keys() != after.keys():
        raise ExchangeCheckError("состав планов изменился между снимками")
    added = {
        p: [n for n in after[p] if n["ref"] not in {old["ref"] for old in before[p]}]
        for p in before
    }
    quiet = [plan for plan in before if not before[plan] and not after[plan]]
    for plan in before:
        if plan in quiet and len(before) > 1:
            continue
        for label, nodes in (
            ("было", before[plan]),
            ("стало", after[plan]),
            ("добавлено", added[plan]),
        ):
            names = ", ".join(f"{text_line(n['code'])} ({text_line(n['ref'])})" for n in nodes)
            lines.append(f"РЕГИСТРАЦИЯ {text_line(plan)} {label}: {names or '—'}")
    if quiet and len(before) > 1:
        lines.append(f"РЕГИСТРАЦИЯ планов без регистрации объекта до и после: {len(quiet)}")
    if args.restore:
        stage(
            lines,
            "ВОССТАНОВЛЕНИЕ",
            lambda: require_ok(server, restore_code(args.object, args.ref, added)),
        )

        def check_restored() -> None:
            restored = _snapshot(server, args)
            for plan, nodes in added.items():
                refs = {n["ref"] for n in restored[plan]}
                if any(n["ref"] in refs for n in nodes):
                    raise ExchangeCheckError("добавленная регистрация не снята")

        stage(lines, "ПРОВЕРКА ВОССТАНОВЛЕНИЯ", check_restored)
    return True


def state_code(full_name: str, ref: str) -> str:
    return guarded(
        object_body(full_name, ref, required=False)
        + """Если О = Неопределено Тогда Результат = "OK deleted";
        Иначе Результат = "OK " + ?(О.ПометкаУдаления, "marked", "kept"); КонецЕсли;"""
    )


def mutation_code(plan: str, node_code: str, full_name: str, ref: str, mode: str) -> str:
    """Регистрация конкретной ссылки перед удалением, в одной транзакции с изменением."""
    if mode not in ("mark", "delete"):
        raise ValueError("неверный режим удаления")
    # БСП: ОбменДаннымиСобытия:227 — ЗарегистрироватьИзменения;
    # КонвертацияОбъектовИнформационныхБаз:7146 — Удалить;
    # 1876–1889 — УстановитьПометкуУдаления; ОбменДаннымиСобытия:176 — Получатели;
    # ОбменДаннымиСобытия:140–157 —
    # обработка ПередУдалением, без режима ОбменДанными.Загрузка.
    change = "О.Удалить();"
    if mode == "mark":
        change = """Если Метаданные.Справочники.Найти(МД.Имя) = МД
            Или Метаданные.ПланыВидовХарактеристик.Найти(МД.Имя) = МД
            Или Метаданные.ПланыСчетов.Найти(МД.Имя) = МД Тогда
            О.УстановитьПометкуУдаления(Истина, Ложь);
            Иначе О.УстановитьПометкуУдаления(Истина); КонецЕсли;"""
    return guarded(
        object_body(full_name, ref)
        + f"""Узел = ПланыОбмена[{literal(plan)}].НайтиПоКоду({literal(node_code)});
        Если Узел.Пустая() Тогда ВызватьИсключение "Нет узла: выполните setup"; КонецЕсли;
        НачатьТранзакцию(); Попытка
        ПланыОбмена.ЗарегистрироватьИзменения(Узел, Ссылка);
        О.ОбменДанными.Получатели.Добавить(Узел); {change}
        ЗафиксироватьТранзакцию();
        Исключение ОтменитьТранзакцию(); ВызватьИсключение; КонецПопытки;
        Результат = "OK";"""
    )


def export_registered_code(plan: str, node_code: str) -> str:
    # БСП: ОбменДаннымиСервер:5061–5075 — штатная выгрузка; регистрация уже сделана.
    return guarded(
        f"""Адрес = "";
        ОбменДаннымиСервер.ВыполнитьВыгрузкуДляУзлаИнформационнойБазыВоВременноеХранилище(
        {literal(plan)}, {literal(node_code)}, Адрес);
        Результат = "OK " + Base64Строка(ПолучитьИзВременногоХранилища(Адрес));"""
    )


def _safe_code(builder: Callable[..., str], *values: str) -> str:
    """Соседние генераторы сохраняют прежнее поведение; новые аргументы экранируются."""
    markers = tuple(f"KD2_ARGUMENT_{i}" for i in range(len(values)))
    code = builder(*markers)
    replacements = {bsl(m): literal(v) for m, v in zip(markers, values, strict=True)}
    return re.sub(r'"KD2_ARGUMENT_\d+"', lambda m: replacements[m[0]], code)


def delete(
    args: argparse.Namespace, source: DataServer, target: DataServer, lines: list[str]
) -> bool:
    """Удаление ровно --ref; без confirm не выполняется даже чтение баз.

    БСП: КонвертацияОбъектовИнформационныхБаз:16658–16671 — поиск удаления по UUID
    с заменой по соответствиям; 16595–16654 — непосредственное удаление найденного объекта,
    отмена при ЗагрузкаЗапрещена или Отказ в ПриПолученииИнформацииОбУдалении.
    БСП: КонвертацияОбъектовИнформационныхБаз:7124–7150 — предопределённый элемент
    сохраняется; непосредственная ветка вызывает Удалить, другая — пометку.
    Обработчик может поставить пометку и отказаться от непосредственного удаления.
    БСП: КонвертацияОбъектовИнформационныхБаз:8104–8109 — пометка обычной загрузки
    передаётся как свойство ПКО; её наличие в правилах не предполагается сценарием.
    """
    if not args.confirm:
        raise ExchangeCheckError("требуется --confirm")
    if args.mode == "delete" and args.expect is None:
        raise ExchangeCheckError("для --mode delete требуется --expect marked|deleted|kept")
    if args.mode == "mark" and args.expect not in (None, "marked"):
        raise ExchangeCheckError("для --mode mark ожидание — marked")
    if source.label == target.label:
        raise ExchangeCheckError("источник и приёмник должны быть разными песочницами")
    lines.append(f"ПЛАН {text_line(args.plan)}")
    lines.append(f"ИСТОЧНИК {text_line(source.label)} → ПРИЕМНИК {text_line(target.label)}")
    lines.append(
        f"ОБЪЕКТ ИСТОЧНИКА {text_line(args.object)} {text_line(args.ref)}, режим={args.mode}"
    )
    target_object, target_ref = args.target_object or args.object, args.target_ref or args.ref
    # Как run: синхронизация включается, пустой код этого узла получает значение аргумента.
    # БСП: ОбменДаннымиСервер:1775, 1785, 2249 — ИспользоватьСинхронизациюДанных.
    # БСП: FunctionalOptions/ИспользоватьСинхронизациюДанных.xml:5 — одноимённая опция.
    source_code = stage(
        lines,
        "УЗЕЛ ИСТОЧНИКА",
        lambda: source.run(_safe_code(this_node_code, args.plan, args.source_code)).strip(),
    )
    target_code = stage(
        lines,
        "УЗЕЛ ПРИЕМНИКА",
        lambda: target.run(_safe_code(this_node_code, args.plan, args.target_code)).strip(),
    )
    if not source_code or not target_code or source_code == target_code:
        raise ExchangeCheckError("нужны разные непустые коды узлов")
    stage(
        lines,
        "СОСТАВ",
        lambda: require_ok(source, _safe_code(plan_content_check, args.plan, args.object)),
    )
    before = stage(
        lines, "ПРИЕМНИК ДО", lambda: target.run(state_code(target_object, target_ref)).strip()
    )
    if before not in ("kept", "marked"):
        raise ExchangeCheckError(
            "объект приёмника не найден; проверьте --target-object / --target-ref"
        )
    lines.append(f"ОБЪЕКТ ПРИЕМНИКА {text_line(target_object)} {text_line(target_ref)}: {before}")
    # БСП: ПравилаДляОбменаДанными/ManagerModule:632–822 — ЗагрузитьКомплектПравил;
    # 208–221, 276–285 — виды правил, ИсточникиПравилДляОбменаДанными.Файл и флаг загрузки.
    for label, server, archive in (
        ("ИСТОЧНИКА", source, args.source_rules),
        ("ПРИЕМНИКА", target, args.target_rules),
    ):
        if archive is not None:
            stage(
                lines,
                f"ПРАВИЛА {label}",
                lambda s=server, a=archive: require_ok(
                    s, _safe_code(lambda p, n: load_rules(p, a.read_bytes(), n), args.plan, a.name)
                ),
            )
    # БСП: ОбменДаннымиСервер:1203 — УдалитьРегистрациюИзменений всего узла.
    # Как run: накопленные регистрации этого узла теряются. Удаляется только регистрация,
    # не объект; единственная мутация объекта — ниже, по явно заданному --ref.
    cleanup = guarded(
        f"""Узел = ПланыОбмена[{literal(args.plan)}].НайтиПоКоду({literal(target_code)});
        Если Узел.Пустая() Тогда ВызватьИсключение "Нет узла: выполните setup"; КонецЕсли;
        ПланыОбмена.УдалитьРегистрациюИзменений(Узел); Результат = "OK";"""
    )
    stage(lines, "ОЧИСТКА РЕГИСТРАЦИИ", lambda: require_ok(source, cleanup))
    stage(
        lines,
        "ИЗМЕНЕНИЕ ИСТОЧНИКА",
        lambda: require_ok(
            source, mutation_code(args.plan, target_code, args.object, args.ref, args.mode)
        ),
    )

    def export() -> bytes:
        encoded = source.run(export_registered_code(args.plan, target_code))
        data = base64.b64decode("".join(encoded.split()), validate=True)
        etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
        out = RUNS / f"delete-{datetime.now():%Y%m%d-%H%M%S-%f}"
        out.mkdir(parents=True)
        (out / "message.xml").write_bytes(data)
        return data

    message = stage(lines, "ВЫГРУЗКА", export)
    # БСП: ОбменДаннымиСервер:5081, 5097 — функции import_message;
    # 5085–5089 — ПолноеИмяФайлаСообщенияОбмена, ДействиеПриОбмене,
    # ИмяПланаОбмена, КодУзлаИнформационнойБазы; 5529 — ДействияПриОбмене.ЗагрузкаДанных.
    answer = stage(
        lines,
        "ЗАГРУЗКА",
        lambda: target.run(
            _safe_code(lambda p, n: import_message(p, n, message), args.plan, source_code)
        ),
    )
    lines.extend(text_line(line) for line in answer.splitlines() if line.startswith("СООБЩЕНИЕ"))
    actual = stage(
        lines, "ПРИЕМНИК ПОСЛЕ", lambda: target.run(state_code(target_object, target_ref)).strip()
    )
    expected = args.expect or "marked"
    lines.append(f"УДАЛЕНИЕ факт={text_line(actual)}, ожидалось={expected}")
    if actual != expected:
        lines.append("СРАВНЕНИЕ ОШИБКА")
        return False
    lines.append("СРАВНЕНИЕ OK")
    if args.query:
        result = stage(lines, "ЗАПРОС", lambda: target.run(query_rows(args.query)))
        count = int(result.partition("\n")[0])
        if args.expect_rows is not None and count != args.expect_rows:
            lines.append(f"ЗАПРОС ОШИБКА ожидалось {args.expect_rows}, получено {count}")
            return False
    return True


if __name__ == "__main__":
    main()
