"""Проверка правил обмена штатной загрузкой БСП без записи в базу (задача 8.2).

Правила из файла БСП принимает ZIP-архивом (БСП 3.1.12, модуль менеджера
`InformationRegisters/ПравилаДляОбменаДанными`):

- два файла `ExchangeRules.xml` и `CorrespondentExchangeRules.xml` — `ЗагрузитьПравила`, ветка
  `ЭтоАрхив` (419–630; форма «Правила конвертации объектов»);
- три файла, с `RegistrationRules.xml`, — `ЗагрузитьКомплектПравил` (632–822; форма «Загрузить
  правила синхронизации»).

Правила конвертации обеих частей разбирает обработка `КонвертацияОбъектовИнформационныхБаз` — для
выгрузки и для загрузки, правила регистрации — `ЗагрузкаПравилРегистрацииОбъектов`. Скрипт вызывает
метод через внешнее соединение с менеджерами записи в памяти и не вызывает `Записать`: регистр базы
не меняется. Успех — `ПравилаЗагружены = Истина` у всех записей.

Запуск: `uv run python kdbase/bsp_check.py <правила> <правила корреспондента> --plan <план обмена>`
(архив из двух файлов собирается сам) или `… --archive <ZIP> --plan …` (готовый архив, например из
`rules_pack`); база — `--project <проект> --base <база>` (только песочница из projects.yaml) или
`--connection <строка>`. Пользователь 1С базы с авторизацией — `logins` в личном projects.local.yaml
(`<проект>.<база>: {user, password}`), иначе `IB_USER` / `IB_PASSWORD` из .dev.env проекта
(`dev_env` базы в projects.yaml); для `--connection` — `KD2_BSP_USER` / `KD2_BSP_PASSWORD`.
"""

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from kd2_rules_mcp.console import utf8_stdout
from kd2_rules_mcp.projects import base_login, load_catalog, load_local, with_login

ROOT = Path(__file__).resolve().parents[1]
KINDS = ("ПравилаКонвертацииОбъектов", "ПравилаРегистрацииОбъектов")


def check(archive: Path, plan: str, connection_string: str) -> int:
    """Загружает архив правил в записи регистра в памяти; печатает итог и сообщения БСП."""
    import win32com.client

    user = os.environ.get("KD2_BSP_USER", "")
    if user and "Usr=" not in connection_string:
        login = (user, os.environ.get("KD2_BSP_PASSWORD", ""))
        connection_string = with_login(connection_string, login)
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
    full_set = len(names) == 3
    print(f"ПЛАН {plan}")
    print(f"АРХИВ {archive}: {', '.join(names)}")
    print("ЗАГРУЗКА " + ("ЗагрузитьКомплектПравил" if full_set else "ЗагрузитьПравила (архив)"))
    connector = win32com.client.Dispatch("V83.COMConnector")
    base = connector.Connect(connection_string)
    register = base.РегистрыСведений.ПравилаДляОбменаДанными
    records = [_record(base, register, plan, kind) for kind in KINDS[: 2 if full_set else 1]]
    address = base.ПоместитьВоВременноеХранилище(base.NewObject("ДвоичныеДанные", str(archive)))
    try:
        if full_set:
            data = base.NewObject("Структура")
            data.Вставить("ЗаписьПравилКонвертации", records[0])
            data.Вставить("ЗаписьПравилРегистрации", records[1])
            register.ЗагрузитьКомплектПравил(False, data, "", address, archive.name)
        else:
            register.ЗагрузитьПравила(False, records[0], address, archive.name, True)
    except Exception as error:  # исключение 1С (например, разбор XML) приходит через COM
        details = getattr(error, "excepinfo", None)
        text = details[2] if details and len(details) > 2 and details[2] else str(error)
        print("ОШИБКА " + str(text).replace("\n", " "))
    loaded = all(bool(record.ПравилаЗагружены) for record in records)
    for record in records:
        info = str(record.ИнформацияОПравилах or "").strip()
        if info:
            print("ИНФОРМАЦИЯ " + " | ".join(line for line in info.splitlines() if line.strip()))
    messages = base.ПолучитьСообщенияПользователю(True)
    for index in range(messages.Количество()):
        text = str(messages.Получить(index).Текст).replace("\n", " ")
        print(f"СООБЩЕНИЕ {text}")
    print("ИТОГ " + ("OK" if loaded else "ОШИБКА"))
    return 0 if loaded else 1


def _record(base: Any, register: Any, plan: str, kind: str) -> Any:
    """Менеджер записи регистра правил в памяти — как его заполняет форма загрузки."""
    record = register.СоздатьМенеджерЗаписи()
    record.ИмяПланаОбмена = plan
    record.ВидПравил = getattr(base.Перечисления.ВидыПравилДляОбменаДанными, kind)
    record.ИсточникПравил = base.Перечисления.ИсточникиПравилДляОбменаДанными.Файл
    return record


def connection_for(project_id: str, base_id: str) -> str:
    """Строка соединения базы из projects.yaml; не песочница — отказ (боевые базы не трогаем)."""
    catalog = load_catalog(ROOT / "projects.yaml")
    base = catalog.base(project_id, base_id)
    if not base.is_sandbox:
        raise SystemExit(
            f"База {project_id}.{base_id} — «{base.role}»: "
            "проверка подключается только к песочницам"
        )
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
    args = parser.parse_args()
    if args.connection:
        connection = args.connection
    elif args.project and args.base:
        connection = connection_for(args.project, args.base)
    else:
        parser.error("укажите --project и --base (песочница из projects.yaml) или --connection")
    if args.archive is not None:
        if args.rules is not None:
            parser.error("укажите либо --archive, либо пару файлов правил")
        sys.exit(check(args.archive, args.plan, connection))
    if args.rules is None or args.correspondent is None:
        parser.error("укажите правила и правила корреспондента или --archive")
    print(f"ПРАВИЛА {args.rules}")
    print(f"КОРРЕСПОНДЕНТ {args.correspondent}")
    with tempfile.TemporaryDirectory() as folder:
        archive = Path(folder) / "rules.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            package.write(args.rules, "ExchangeRules.xml")
            package.write(args.correspondent, "CorrespondentExchangeRules.xml")
        sys.exit(check(archive, args.plan, connection))


if __name__ == "__main__":
    main()
