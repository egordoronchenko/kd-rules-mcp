"""Проверка правил обмена штатной загрузкой БСП без записи в базу (задача 8.2).

Правила конвертации из файла БСП принимает ZIP-архивом из `ExchangeRules.xml` и
`CorrespondentExchangeRules.xml` (`РегистрыСведений.ПравилаДляОбменаДанными.ЗагрузитьПравила`,
ветка `ЭтоАрхив`; БСП 3.1.12, `InformationRegisters/ПравилаДляОбменаДанными`, модуль
менеджера, 419–630): обе части разбирает обработка `КонвертацияОбъектовИнформационныхБаз` —
для выгрузки и для загрузки. Скрипт вызывает этот метод через внешнее соединение с менеджером
записи в памяти и не вызывает `Записать`: регистр базы не меняется.
Успех — `ПравилаЗагружены = Истина`.

Запуск: `uv run python kdbase/bsp_check.py <правила> <правила корреспондента> --plan <план обмена>`
`--project <проект> --base <база>` (только песочница из projects.yaml) или `--connection <строка>`.
Пользователь 1С базы с авторизацией — `logins` в личном projects.local.yaml
(`<проект>.<база>: {user, password}`), иначе `IB_USER` / `IB_PASSWORD` из .dev.env проекта
(`dev_env` базы в projects.yaml); для `--connection` — `KD2_BSP_USER` / `KD2_BSP_PASSWORD`.
"""

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from kd2_rules_mcp.projects import base_login, load_catalog, load_local, with_login

ROOT = Path(__file__).resolve().parents[1]


def check(rules: Path, correspondent: Path, plan: str, connection_string: str) -> int:
    """Загружает пару правил в запись регистра в памяти; печатает итог и сообщения БСП."""
    import win32com.client

    user = os.environ.get("KD2_BSP_USER", "")
    if user and "Usr=" not in connection_string:
        login = (user, os.environ.get("KD2_BSP_PASSWORD", ""))
        connection_string = with_login(connection_string, login)
    with tempfile.TemporaryDirectory() as folder:
        archive = Path(folder) / "rules.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            package.write(rules, "ExchangeRules.xml")
            package.write(correspondent, "CorrespondentExchangeRules.xml")
        connector = win32com.client.Dispatch("V83.COMConnector")
        base = connector.Connect(connection_string)
        record = base.РегистрыСведений.ПравилаДляОбменаДанными.СоздатьМенеджерЗаписи()
        record.ИмяПланаОбмена = plan
        record.ВидПравил = base.Перечисления.ВидыПравилДляОбменаДанными.ПравилаКонвертацииОбъектов
        record.ИсточникПравил = base.Перечисления.ИсточникиПравилДляОбменаДанными.Файл
        data = base.NewObject("ДвоичныеДанные", str(archive))
        address = base.ПоместитьВоВременноеХранилище(data)
        print(f"ПЛАН {plan}")
        print(f"ПРАВИЛА {rules}")
        print(f"КОРРЕСПОНДЕНТ {correspondent}")
        try:
            base.РегистрыСведений.ПравилаДляОбменаДанными.ЗагрузитьПравила(
                False, record, address, archive.name, True
            )
        except Exception as error:  # исключение 1С (например, разбор XML) приходит через COM
            details = getattr(error, "excepinfo", None)
            text = details[2] if details and len(details) > 2 and details[2] else str(error)
            print("ОШИБКА " + str(text).replace("\n", " "))
        loaded = bool(record.ПравилаЗагружены)
        info = str(record.ИнформацияОПравилах or "").strip()
        if info:
            print("ИНФОРМАЦИЯ " + " | ".join(line for line in info.splitlines() if line.strip()))
        messages = base.ПолучитьСообщенияПользователю(True)
        for index in range(messages.Количество()):
            text = str(messages.Получить(index).Текст).replace("\n", " ")
            print(f"СООБЩЕНИЕ {text}")
        print("ИТОГ " + ("OK" if loaded else "ОШИБКА"))
    return 0 if loaded else 1


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
    parser = argparse.ArgumentParser(description="Проверка правил штатной загрузкой БСП")
    parser.add_argument("rules", type=Path, help="ПравилаОбмена (текущая программа)")
    parser.add_argument("correspondent", type=Path, help="ПравилаОбмена корреспондента")
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
    sys.exit(check(args.rules, args.correspondent, args.plan, connection))


if __name__ == "__main__":
    main()
