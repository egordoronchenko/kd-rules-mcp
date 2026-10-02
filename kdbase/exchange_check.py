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
  до загрузки правил проверяется состав плана обмена;
  `--source-rules` / `--target-rules` (ZIP комплекта из трёх файлов; без них — правила баз),
  выгрузка, загрузка, `--query <запрос к приёмнику>` и `--expect-rows N`. Протокол — в stdout,
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
import sys
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
    """Регистрация одного объекта на узле и выгрузка; «OK <сообщение в base64>»."""
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
    for label, server, archive in (
        ("ИСТОЧНИКА", source, args.source_rules),
        ("ПРИЕМНИКА", target, args.target_rules),
    ):
        if archive is not None:
            server.run(load_rules(args.plan, archive.read_bytes(), archive.name))
            lines.append(f"ПРАВИЛА {label} {archive}: загружены")
    lines.append(f"ОБЪЕКТ {args.object} {args.ref}")
    exported = source.run(export_object(args.plan, target_code, args.object, args.ref))
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
    ):
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--plan", required=True, help="имя плана обмена")
        command.add_argument("--source", required=True, help="база-источник <проект>.<база>")
        command.add_argument("--target", required=True, help="база-приёмник <проект>.<база>")
        command.add_argument("--source-code", default="KD2S", help="код этого узла, если пуст")
        command.add_argument("--target-code", default="KD2T", help="код этого узла, если пуст")
        if name == "run":
            command.add_argument("--object", required=True, help="полное имя: Документ.Заказ")
            command.add_argument("--ref", required=True, help="уникальный идентификатор объекта")
            command.add_argument("--source-rules", type=Path, help="ZIP правил для источника")
            command.add_argument("--target-rules", type=Path, help="ZIP правил для приёмника")
            command.add_argument("--query", help="контрольный запрос к приёмнику")
            command.add_argument("--expect-rows", type=int, help="ожидаемое число строк")
    return parser.parse_args(argv)


def main() -> None:
    utf8_stdout()
    args = parse_args()
    ok = False
    lines: list[str] = []
    try:
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


if __name__ == "__main__":
    main()
