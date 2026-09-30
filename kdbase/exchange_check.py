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
  `--source-rules` / `--target-rules` (ZIP комплекта из трёх файлов; без них — правила баз),
  выгрузка, загрузка, `--query <запрос к приёмнику>` и `--expect-rows N`. Протокол — в stdout,
  сообщение обмена — в `kdbase\\run\\exchange-<время>\\`. Код выхода 0 — «ИТОГ OK», 1 — ошибка.

Точки входа БСП (3.1.12): `ОбменДаннымиСервер` —
`ВыполнитьВыгрузкуДляУзлаИнформационнойБазыВоВременноеХранилище`,
`ВыполнитьОбменДаннымиДляУзлаИнформационнойБазыЧерезФайлИлиСтроку` (`ЗагрузкаДанных`);
`РегистрыСведений.ПравилаДляОбменаДанными.ЗагрузитьКомплектПравил`;
`РегистрыСведений.ОбщиеНастройкиУзловИнформационныхБаз.УстановитьПризнакНастройкаЗавершена`.
Порядок и грабли — скилл `kd2-exchange-pitfalls`, «Как проверить исправление живым обменом».
"""

import argparse
import base64
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kd2_rules_mcp.projects import data_endpoint, load_catalog, load_local

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "kdbase" / "run"
TOOL = "vcexecutecode"
DONE = "КОНЕЦ"
NODE_NAME = "kd2 exchange_check"
TIMEOUT_S = 900


class ExchangeCheckError(Exception):
    """Ошибка шага проверки: текст — для протокола."""


def bsl(value: str) -> str:
    """Строковый литерал 1С."""
    return '"' + value.replace('"', '""') + '"'


def one_line(code: str) -> str:
    """Код одной строкой: многострочный код сервер данных не выполняет (ответ пустой)."""
    return " ".join(line.strip() for line in code.splitlines() if line.strip())


def guarded(body: str) -> str:
    """Тело в `Попытка`: результат «OK …» или «ОШИБКА <подробное представление>»."""
    return one_line(
        f"""Попытка
        {body}
        Исключение
        Результат = "ОШИБКА " + ПодробноеПредставлениеОшибки(ИнформацияОбОшибке());
        КонецПопытки;"""
    )


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
    payload = base64.b64encode(archive).decode("ascii")
    kinds = "ПравилаКонвертацииОбъектов,ПравилаРегистрацииОбъектов"
    return guarded(
        f"""Рег = РегистрыСведений.ПравилаДляОбменаДанными; Записи = Новый Массив;
        Для Каждого Вид Из СтрРазделить({bsl(kinds)}, ",") Цикл
        З = Рег.СоздатьМенеджерЗаписи(); З.ИмяПланаОбмена = {bsl(plan)};
        З.ВидПравил = Перечисления.ВидыПравилДляОбменаДанными[Вид];
        З.ИсточникПравил = Перечисления.ИсточникиПравилДляОбменаДанными.Файл;
        Записи.Добавить(З);
        КонецЦикла;
        Данные = Новый Структура("ЗаписьПравилКонвертации, ЗаписьПравилРегистрации",
        Записи[0], Записи[1]);
        Адрес = ПоместитьВоВременноеХранилище(Base64Значение({bsl(payload)}));
        Отказ = Ложь; Описание = "";
        Рег.ЗагрузитьКомплектПравил(Отказ, Данные, Описание, Адрес, {bsl(file_name)});
        Если Отказ Или Не Записи[0].ПравилаЗагружены Или Не Записи[1].ПравилаЗагружены Тогда
        Тексты = Новый Массив; Тексты.Добавить(Описание);
        Для Каждого С Из ПолучитьСообщенияПользователю(Истина) Цикл
        Тексты.Добавить(С.Текст);
        КонецЦикла;
        ВызватьИсключение "Правила не загружены: " + СтрСоединить(Тексты, "; ");
        КонецЕсли;
        Записи[0].Записать(); Записи[1].Записать();
        Результат = "OK";"""
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


def short_error(text: str) -> str:
    """Ошибка 1С без эха нашего кода и стека HTTP-сервиса: до первой строки стека «{…}»."""
    text = re.sub(r"[A-Za-z0-9+/=]{200,}", "<base64>", text)
    kept: list[str] = []
    for line in text.removeprefix("ОШИБКА ").splitlines():
        if kept and line.lstrip().startswith("{"):
            break
        kept.append(line.strip())
    return " ".join(part for part in kept if part)


@dataclass(frozen=True, slots=True)
class DataServer:
    """Сервер данных базы-песочницы: `vcexecutecode` по HTTP (JSON-RPC MCP)."""

    label: str
    url: str
    headers: dict[str, str]

    def call(self, code: str) -> str:
        """Текст ответа инструмента; ошибка HTTP или JSON-RPC — `ExchangeCheckError`."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": TOOL, "arguments": {"bslcode": code}},
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                **self.headers,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                raw = response.read().decode("utf-8")
        except OSError as error:
            raise ExchangeCheckError(f"{self.label}: сервер данных недоступен: {error}") from error
        answer = json.loads(_sse_data(raw))
        if "error" in answer:
            raise ExchangeCheckError(f"{self.label}: {answer['error']}")
        result = answer.get("result", {})
        return "".join(part.get("text", "") for part in result.get("content", []))

    def run(self, code: str) -> str:
        """Код из `guarded`: текст после «OK»; «ОШИБКА …» или пустой ответ — ошибка шага."""
        text = self.call(code)
        if text.startswith("OK"):
            return text[2:].removeprefix(" ").removeprefix("\n")
        if not text:
            raise ExchangeCheckError(
                f"{self.label}: пустой ответ сервера данных "
                f"(код не выполнился или нет инструмента {TOOL})"
            )
        raise ExchangeCheckError(f"{self.label}: {short_error(text)}")


def _sse_data(raw: str) -> str:
    """Тело JSON-RPC: ответ в формате SSE (`data: …`) или обычный JSON."""
    for line in raw.splitlines():
        if line.startswith("data:"):
            return line[5:].strip()
    return raw


def server_for(ref: str, root: Path = ROOT) -> DataServer:
    """Сервер данных базы `<проект>.<база>` из projects.yaml (только песочница)."""
    project_id, _, base_id = ref.partition(".")
    if not base_id:
        raise ExchangeCheckError(f"База задаётся как <проект>.<база>, получено «{ref}»")
    catalog = load_catalog(root / "projects.yaml")
    local = load_local(root / "projects.local.yaml")
    url, headers = data_endpoint(catalog, local, project_id, base_id)
    return DataServer(ref, url, headers)


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
    """Правила, выгрузка, загрузка, запрос; строки протокола — в `lines`, результат — успех."""
    lines.append(f"ПЛАН {args.plan}")
    source_code = source.run(this_node_code(args.plan, args.source_code)).strip()
    target_code = target.run(this_node_code(args.plan, args.target_code)).strip()
    lines.append(
        f"ИСТОЧНИК {source.label} ({source_code}) → ПРИЕМНИК {target.label} ({target_code})"
    )
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
    for name, help_text in (("setup", "узлы плана обмена"), ("run", "выгрузка и загрузка объекта")):
        command = commands.add_parser(name, help=help_text)
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
