"""Обзор всех планов обмена песочницы без обновления настроек и правил."""

import argparse
import sys

from bsp_load import DataServer, ExchangeCheckError, server_for, short_error
from live_checks import (
    json_code,
    literal,
    read_json,
    rule_description,
    rule_info_body,
    rule_record_body,
    stage,
    text_line,
)

from kd_rules_mcp.console import utf8_stdout


def plans_code() -> str:
    return json_code(
        """Данные = Новый Массив;
        Для Каждого П Из Метаданные.ПланыОбмена Цикл Данные.Добавить(П.Имя); КонецЦикла;"""
    )


def overview_code(plan: str) -> str:
    # БСП: ОбменДаннымиПовтИсп:226–228, 1136–1151 — ПланыОбменаБСП,
    # список подключённых к подсистеме планов (через ПолучитьПланыОбмена).
    # БСП: ОбменДаннымиПовтИсп:430–436 — ЭтоПланОбменаXDTO;
    # 406–408 — ЭтоПланОбменаРаспределеннойИнформационнойБазы;
    # 717–731 — УзлыПланаОбмена, без этого узла; 392–394 — ЕстьМакетПланаОбмена.
    # БСП: ОбменДаннымиСервер:11963–11974, 11991–12015 —
    # ПравилаОбмена, ПравилаОбменаКорреспондента, ПравилаРегистрации.
    # БСП: ПравилаДляОбменаДанными/ManagerModule:908–918 — ПолучитьМакет / Записать.
    records = ""
    for kind in ("exchange", "registration"):
        records += (
            rule_record_body(plan, kind)
            + rule_info_body(kind)
            + f' Данные.Вставить("kind", {literal(kind)}); Правила.Добавить(Данные);'
        )
    return json_code(
        f"""Имя = {literal(plan)};
        Подключен = ОбменДаннымиПовтИсп.ПланыОбменаБСП().Найти(Имя) <> Неопределено;
        Технология = "не определена (план вне БСП)";
        Если Подключен Тогда
        Технология = ?(ОбменДаннымиПовтИсп.ЭтоПланОбменаXDTO(Имя), "универсальный формат", "XML");
        КонецЕсли;
        РИБ = ОбменДаннымиПовтИсп.ЭтоПланОбменаРаспределеннойИнформационнойБазы(Имя);
        Узлы = ОбменДаннымиПовтИсп.УзлыПланаОбмена(Имя);
        Макеты = Новый Массив;
        Для Каждого Название Из СтрРазделить(
        "ПравилаОбмена,ПравилаОбменаКорреспондента,ПравилаРегистрации", ",") Цикл
        Есть = ОбменДаннымиПовтИсп.ЕстьМакетПланаОбмена(Имя, Название);
        М = Новый Структура("name,present", Название, Есть);
        Если Есть Тогда
        Файл = ПолучитьИмяВременногоФайла("xml");
        Попытка
        Макет = ПланыОбмена[Имя].ПолучитьМакет(Название); Макет.Записать(Файл);
        Байты = Новый ДвоичныеДанные(Файл); М.Вставить("size", Байты.Размер());
        Исключение УдалитьФайлы(Файл); ВызватьИсключение; КонецПопытки;
        УдалитьФайлы(Файл); КонецЕсли; Макеты.Добавить(М); КонецЦикла;
        Правила = Новый Массив; {records}
        Данные = Новый Структура("bsp,technology,rib,nodes,templates,rules",
        Подключен, Технология, РИБ, Узлы.Количество(), Макеты, Правила);"""
    )


def overview(server: DataServer, lines: list[str]) -> None:
    """Список → отдельное чтение каждого плана; сбой одного плана останавливает обзор."""
    plans = stage(lines, "СПИСОК ПЛАНОВ", lambda: read_json(server, plans_code()))
    if not isinstance(plans, list) or any(not isinstance(p, str) for p in plans):
        raise ExchangeCheckError("некорректный список планов")
    if not plans:
        lines.append("ПЛАНЫ отсутствуют")
    for plan in plans:
        info = stage(
            lines,
            f"ЧТЕНИЕ ПЛАНА {text_line(plan)}",
            lambda p=plan: read_json(server, overview_code(p)),
        )
        lines.append(
            f"ПЛАН {text_line(plan)}: БСП={'да' if info['bsp'] else 'нет'}, "
            f"технология={text_line(info['technology'])}, РИБ={'да' if info['rib'] else 'нет'}, "
            f"узлов кроме этого={info['nodes']}"
        )
        for template in info["templates"]:
            size = template.get("size", 0)
            status = ("пустой" if size == 0 else f"{size} байт") if template["present"] else "нет"
            lines.append(f"МАКЕТ {text_line(template['name'])}: {status}")
        for rule in info["rules"]:
            lines.append(f"ЗАПИСЬ {text_line(rule['kind'])}: {rule_description(rule)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Обзор планов обмена песочницы (только чтение)")
    parser.add_argument("--base", required=True, help="<проект>.<база>")
    return parser.parse_args(argv)


def main() -> None:
    utf8_stdout()
    args = parse_args()
    lines = [f"БАЗА {text_line(args.base)}"]
    ok = False
    try:
        server = stage(lines, "ПЕСОЧНИЦА", lambda: server_for(args.base))
        overview(server, lines)
        ok = True
    except Exception as error:
        lines.append(f"ОШИБКА {short_error(str(error))}")
    print("\n".join([*lines, f"ИТОГ {'OK' if ok else 'ОШИБКА'}", "КОНЕЦ"]))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
