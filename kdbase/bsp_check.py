"""Проверка правил обмена штатной загрузкой БСП без записи в базу (задача 8.2).

Правила из файла БСП принимает ZIP-архивом (БСП 3.1.12, модуль менеджера
`InformationRegisters/ПравилаДляОбменаДанными`):

- два файла `ExchangeRules.xml` и `CorrespondentExchangeRules.xml` — `ЗагрузитьПравила`, ветка
  `ЭтоАрхив` (419–630; форма «Правила конвертации объектов»);
- три файла, с `RegistrationRules.xml`, — `ЗагрузитьКомплектПравил` (632–822; форма «Загрузить
  правила синхронизации»).

Правила конвертации обеих частей разбирает обработка `КонвертацияОбъектовИнформационныхБаз` — для
выгрузки и для загрузки, правила регистрации — `ЗагрузкаПравилРегистрацииОбъектов`. Скрипт вызывает
метод с менеджерами записи в памяти и не вызывает `Записать`: регистр базы не меняется. Успех —
`ПравилаЗагружены = Истина` у всех записей.

Два транспорта. Без `--via` песочница с непустым `data_mcp` грузится через сервер данных базы
(HTTP, с любой машины); иначе — внешнее соединение (COM, `pywin32`). `--via data|com` выбирает
транспорт явно: `--via com` — всегда COM, `--via data` без `data_mcp` — отказ, `--connection` с
`--via data` — отказ (у строки соединения нет `data_mcp`). Первая строка протокола — `ТРАНСПОРТ COM`
или `ТРАНСПОРТ сервер данных`.

Запуск: `uv run python kdbase/bsp_check.py <правила> <правила корреспондента> --plan <план обмена>`
(архив из двух файлов собирается сам) или `… --archive <ZIP> --plan …` (готовый архив, например из
`rules_pack`); база — `--project <проект> --base <база>` (только песочница из projects.yaml) или
`--connection <строка>`. Пользователь 1С базы с авторизацией — `logins` в личном projects.local.yaml
(`<проект>.<база>: {user, password}`), иначе `IB_USER` / `IB_PASSWORD` из .dev.env проекта
(`dev_env` базы в projects.yaml); для `--connection` — `KD2_BSP_USER` / `KD2_BSP_PASSWORD`.
Клиент-серверная строка (`Srvr=`) без `Usr=` и без `KD2_BSP_USER` — отказ до подключения;
файловая (`File=`) подключается как раньше.
"""

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from bsp_load import (
    TOOL,
    DataServer,
    ExchangeCheckError,
    load_rules_code,
    parse_load_report,
    server_for,
    short_error,
)

from kd_rules_mcp.console import utf8_stdout
from kd_rules_mcp.projects import (
    ProjectConfigError,
    base_login,
    load_catalog,
    load_local,
    with_login,
)

ROOT = Path(__file__).resolve().parents[1]
KINDS = ("ПравилаКонвертацииОбъектов", "ПравилаРегистрацииОбъектов")


def require_login(connection: str) -> None:
    """Отказ до COM, если клиент-серверная база без пользователя 1С.

    Файловая база (`File=`) и строка с `Usr=` проходят. `KD2_BSP_USER` тоже считается логином:
    его подставит `check_com()` при подключении.
    """
    if "Srvr=" not in connection or "Usr=" in connection or os.environ.get("KD2_BSP_USER", ""):
        return
    raise SystemExit(
        f"База {connection}: логин не задан — укажите его в `projects.local.yaml` (`logins`) "
        "или `.dev.env` проекта (`dev_env` у базы в `projects.yaml`); "
        "без логина к клиент-серверной базе не подключаемся"
    )


def resolve_transport(project_id: str, base_id: str, via: str | None, root: Path = ROOT) -> str:
    """`data` или `com`. Читает только `projects.yaml`, в сеть и в COM не ходит.

    Не песочница — `SystemExit` с тем же текстом, что `connection_for`,
    раньше проверки `data_mcp`.
    via is None: непустой `Base.data_mcp` → `data`, иначе `com`.
    via == "com" → `com`.
    via == "data" и `data_mcp` пуст → `SystemExit` с именем базы и словом `data_mcp`.
    """
    catalog = load_catalog(root / "projects.yaml")
    base = catalog.base(project_id, base_id)
    if not base.is_sandbox:
        raise SystemExit(_sandbox_only(project_id, base_id, base.role))
    if via == "com":
        return "com"
    if via == "data":
        if not base.data_mcp:
            raise SystemExit(f"У базы {project_id}.{base_id} не задан data_mcp")
        return "data"
    if via is not None:
        raise ValueError(f"неизвестный транспорт {via}")
    return "data" if base.data_mcp else "com"


def report_header(
    transport: str, plan: str, archive: Path, names: list[str], full_set: bool
) -> list[str]:
    """Строки протокола до вызова загрузки: транспорт, план, архив, метод."""
    label = "ЗагрузитьКомплектПравил" if full_set else "ЗагрузитьПравила (архив)"
    return [
        f"ТРАНСПОРТ {transport}",
        f"ПЛАН {plan}",
        f"АРХИВ {archive}: {', '.join(names)}",
        f"ЗАГРУЗКА {label}",
    ]


def report_body(
    error: str | None, infos: list[str], messages: list[str], loaded: bool
) -> list[str]:
    """Строки после вызова: ошибка, непустые сведения, сообщения, итог."""
    lines: list[str] = []
    if error is not None:
        lines.append("ОШИБКА " + error.replace("\n", " "))
    for info in infos:
        text = info.strip()
        if not text:
            continue
        joined = " | ".join(line for line in text.splitlines() if line.strip())
        if joined:
            lines.append(f"ИНФОРМАЦИЯ {joined}")
    for message in messages:
        lines.append("СООБЩЕНИЕ " + message.replace("\n", " "))
    lines.append("ИТОГ " + ("OK" if loaded else "ОШИБКА"))
    return lines


def check_com(archive: Path, plan: str, connection_string: str) -> int:
    """Загружает архив через внешнее соединение; печатает тот же протокол, что `check_data`."""
    import win32com.client

    user = os.environ.get("KD2_BSP_USER", "")
    if user and "Usr=" not in connection_string:
        login = (user, os.environ.get("KD2_BSP_PASSWORD", ""))
        connection_string = with_login(connection_string, login)
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
    full_set = len(names) == 3
    _emit(report_header("COM", plan, archive, names, full_set))
    connector = win32com.client.Dispatch("V83.COMConnector")
    base = connector.Connect(connection_string)
    register = base.РегистрыСведений.ПравилаДляОбменаДанными
    records = [_record(base, register, plan, kind) for kind in KINDS[: 2 if full_set else 1]]
    address = base.ПоместитьВоВременноеХранилище(base.NewObject("ДвоичныеДанные", str(archive)))
    error: str | None = None
    try:
        if full_set:
            data = base.NewObject("Структура")
            data.Вставить("ЗаписьПравилКонвертации", records[0])
            data.Вставить("ЗаписьПравилРегистрации", records[1])
            register.ЗагрузитьКомплектПравил(False, data, "", address, archive.name)
        else:
            register.ЗагрузитьПравила(False, records[0], address, archive.name, True)
    except Exception as caught:  # исключение 1С (например, разбор XML) приходит через COM
        details = getattr(caught, "excepinfo", None)
        text = details[2] if details and len(details) > 2 and details[2] else str(caught)
        error = str(text)
    infos = [str(record.ИнформацияОПравилах or "") for record in records]
    flags = [bool(record.ПравилаЗагружены) for record in records]
    user_messages = base.ПолучитьСообщенияПользователю(True)
    messages = [
        str(user_messages.Получить(index).Текст) for index in range(user_messages.Количество())
    ]
    loaded = error is None and bool(flags) and all(flags)
    _emit(report_body(error, infos, messages, loaded))
    return 0 if loaded else 1


def check_data(archive: Path, plan: str, server: DataServer) -> int:
    """Печатает протокол. Код — `load_rules_code(..., write=False, full_set=...)`.

    Ответ берёт `server.call`, не `run`: `run` считает любую строку без префикса `OK`
    ошибкой шага, а ложный `ПравилаЗагружены` — успешный вызов с `ИТОГ ОШИБКА`.
    """
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
    full_set = len(names) == 3
    _emit(report_header("сервер данных", plan, archive, names, full_set))
    code = load_rules_code(plan, archive.read_bytes(), archive.name, write=False, full_set=full_set)
    try:
        text = server.call(code)
    except ExchangeCheckError as error:
        _emit(report_body(str(error), [], [], False))
        return 1
    if not text:
        _emit(
            report_body(
                f"{server.label}: пустой ответ сервера данных "
                f"(код не выполнился или нет инструмента {TOOL})",
                [],
                [],
                False,
            )
        )
        return 1
    if not text.startswith("OK"):
        detail = short_error(text) if text.startswith("ОШИБКА") else text
        _emit(report_body(detail, [], [], False))
        return 1
    report = parse_load_report(text)
    loaded = bool(report.flags) and all(report.flags)
    _emit(report_body(None, list(report.infos), list(report.messages), loaded))
    return 0 if loaded else 1


def _emit(lines: list[str]) -> None:
    for line in lines:
        print(line)


def _record(base: Any, register: Any, plan: str, kind: str) -> Any:
    """Менеджер записи регистра правил в памяти — как его заполняет форма загрузки."""
    record = register.СоздатьМенеджерЗаписи()
    record.ИмяПланаОбмена = plan
    record.ВидПравил = getattr(base.Перечисления.ВидыПравилДляОбменаДанными, kind)
    record.ИсточникПравил = base.Перечисления.ИсточникиПравилДляОбменаДанными.Файл
    return record


def _sandbox_only(project_id: str, base_id: str, role: str) -> str:
    return f"База {project_id}.{base_id} — «{role}»: проверка подключается только к песочницам"


def connection_for(project_id: str, base_id: str) -> str:
    """Строка соединения базы из projects.yaml; не песочница — отказ (боевые базы не трогаем)."""
    catalog = load_catalog(ROOT / "projects.yaml")
    base = catalog.base(project_id, base_id)
    if not base.is_sandbox:
        raise SystemExit(_sandbox_only(project_id, base_id, base.role))
    local_file = ROOT / "projects.local.yaml"
    login = base_login(load_local(local_file), project_id, base) if local_file.is_file() else None
    return with_login(base.connection, login)


def main() -> None:
    utf8_stdout()
    parser = argparse.ArgumentParser(description="Проверка правил штатной загрузкой БСП")
    parser.add_argument("rules", type=Path, nargs="?", help="ПравилаОбмена (текущая программа)")
    parser.add_argument("correspondent", type=Path, nargs="?", help="ПравилаОбмена корреспондента")
    parser.add_argument("--archive", type=Path, help="готовый ZIP правил (2 или 3 файла)")
    parser.add_argument("--plan", required=True, help="имя плана обмена")
    parser.add_argument("--project", help="проект из projects.yaml (с --base)")
    parser.add_argument("--base", help="база-песочница проекта из projects.yaml")
    parser.add_argument("--connection", help="строка соединения вместо --project/--base")
    parser.add_argument(
        "--via",
        choices=("data", "com"),
        help="транспорт: сервер данных (data) или внешнее соединение (com)",
    )
    args = parser.parse_args()
    connection, data_ref = _bind(parser, args)

    def run(archive: Path) -> int:
        if data_ref is None:
            return check_com(archive, args.plan, connection)
        try:
            server = server_for(data_ref, ROOT)
        except ProjectConfigError as error:
            raise SystemExit(str(error)) from error
        return check_data(archive, args.plan, server)

    if args.archive is not None:
        if args.rules is not None:
            parser.error("укажите либо --archive, либо пару файлов правил")
        sys.exit(run(args.archive))
    if args.rules is None or args.correspondent is None:
        parser.error("укажите правила и правила корреспондента или --archive")
    print(f"ПРАВИЛА {args.rules}")
    print(f"КОРРЕСПОНДЕНТ {args.correspondent}")
    with tempfile.TemporaryDirectory() as folder:
        archive = Path(folder) / "rules.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            package.write(args.rules, "ExchangeRules.xml")
            package.write(args.correspondent, "CorrespondentExchangeRules.xml")
        sys.exit(run(archive))


def _bind(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[str, str | None]:
    """Строка COM и ссылка сервера данных (`None` — транспорт COM).

    Отказ транспорта — до загрузки. `require_login` — только для COM.
    """
    if args.connection:
        if args.via == "data":
            raise SystemExit(
                "У строки --connection нет data_mcp: сервер данных выбирается по --project и --base"
            )
        require_login(args.connection)
        return args.connection, None
    if args.project and args.base:
        if resolve_transport(args.project, args.base, args.via, ROOT) == "data":
            return "", f"{args.project}.{args.base}"
        connection = connection_for(args.project, args.base)
        require_login(connection)
        return connection, None
    parser.error("укажите --project и --base (песочница из projects.yaml) или --connection")


if __name__ == "__main__":
    main()
