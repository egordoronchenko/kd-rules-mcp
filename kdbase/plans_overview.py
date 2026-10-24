"""Обзор всех планов обмена песочницы без обновления настроек и правил."""

import argparse
import sys

from bsp_load import DataServer, ExchangeCheckError, server_for, short_error
from live_checks import (
    read_live_json as read_json,
)
from live_checks import (
    rule_description,
    stage,
    text_line,
)

from kd_rules_mcp.console import utf8_stdout
from kd_rules_mcp.live.codegen.plans import overview_code, plans_code


def overview(server: DataServer, lines: list[str]) -> None:
    """Список → отдельное чтение каждого плана; сбой одного плана останавливает обзор."""
    plans = stage(lines, "СПИСОК ПЛАНОВ", lambda: read_json(server, plans_code())["plans"])
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
