"""Профили обменов: пути и готовые шаги без исполнения и обращения к базам."""

from pathlib import PurePosixPath
from typing import Any

from kd_rules_mcp.live.codegen.rules import FILES
from kd_rules_mcp.projects import Catalog, Exchange, ExchangeSide, resolve
from kd_rules_mcp.service.base import ServiceBase
from kd_rules_mcp.service.views import project_structure_id

# Аргументы, которые ed_route_compare кладёт в profile.selected.<сторона>.ed_schema_open.
_ED_SCHEMA_ARGUMENTS = (
    "format_version",
    "path",
    "project",
    "configuration",
    "package",
    "imports",
)
_ED_VERSION_NOTE = (
    "Версия формата узла по выгрузкам неизвестна; сравнение маршрутов показывает "
    "статическую совместимость, версию выбирает агент по ответу, живой обмен — отдельно."
)


def _ed_schema_note(side: str) -> str:
    """Поле ответа сравнения с готовыми аргументами ed_schema_open для одной стороны."""
    return (
        f"Готовые аргументы — profile.selected.{side}.ed_schema_open ответа шага сравнения; "
        f"если null, причину называет profile.selected.{side}.ed_schema_reason."
    )


class ExchangesMixin(ServiceBase):
    """Представление общего каталога обменов для агента."""

    def project_exchanges(self, project: str | None = None, exchange: str | None = None) -> dict:
        catalog = self._catalog()
        if project is not None:
            catalog.project(project)
        selected = (catalog.exchange(exchange),) if exchange is not None else catalog.exchanges
        return {
            "exchanges": [
                self._exchange_view(catalog, item)
                for item in selected
                if project is None or project in item.projects
            ]
        }

    def _exchange_side(self, catalog: Catalog, side: ExchangeSide) -> dict[str, Any]:
        project = catalog.project(side.project)
        base = project.bases[side.base] if side.base else None
        configuration = (
            base.configuration
            if base
            else (
                "full" if "full" in project.configurations else next(iter(project.configurations))
            )
        )
        structure_id = project_structure_id(side.project, configuration)
        row: dict[str, Any] = {
            "project": side.project,
            "base": f"{side.project}.{base.id}" if base else None,
            "role": base.role if base else None,
            "plan": side.plan,
            "configuration": configuration,
            "structure_id": structure_id,
            "structure_loaded": structure_id in self.store.ids(),
        }
        if side.rules:
            folder = self.settings.project_dirs.get(side.project)
            available = folder is not None and folder.is_dir()
            rules_folder = resolve(folder, side.rules) if available and folder else None
            row["rules"] = {
                "folder": self._host(rules_folder) if rules_folder else side.rules,
                # rules_dir — папка не задана в профиле, взята `<rules_dir>/<план стороны>`.
                "source": "rules_dir" if side.rules_default else "profile",
                "files": {
                    kind: {
                        "path": self._host(rules_folder / name)
                        if rules_folder
                        else str(PurePosixPath(side.rules.replace("\\", "/")) / name),
                        "exists": (rules_folder / name).is_file() if rules_folder else None,
                    }
                    for kind, name in FILES.items()
                },
            }
        if side.module:
            row["module"] = side.module
        return row

    def _exchange_view(self, catalog: Catalog, item: Exchange) -> dict[str, Any]:
        sides = [
            self._exchange_side(catalog, s)
            for s in (item.sides or tuple(ExchangeSide(p, plan=item.plan) for p in item.projects))
        ]
        by_project = {s["project"]: s for s in sides}
        steps: list[dict[str, Any]] = []
        notes = ["live_registration_check и live_exchange_run требуют объект и ссылку."]

        def step(
            stage: str,
            tool: str,
            arguments: dict,
            fill: dict | None = None,
            note: str | None = None,
        ) -> int:
            n = len(steps) + 1
            row = {"n": n, "stage": stage, "tool": tool, "arguments": arguments}
            if fill:
                row["fill"] = fill
            if note:
                row["note"] = note
            steps.append(row)
            return n

        # Правила обмена и регистрации стороны работают, только когда она источник направления.
        sources = {source for source, _ in item.directions}
        for side in sides:
            if "rules" in side and side["rules"]["files"]["exchange"]["exists"] is None:
                notes.append(
                    f"Папка проекта {side['project']} не видна серверу: шаги с файлами её "
                    "комплекта пропущены, пути — от папки проекта."
                )
        opened: dict[tuple[str, str], int] = {}
        routed: dict[str, int] = {}
        for side in sides:
            step(
                "разбор",
                "structure_load_project",
                {"project": side["project"], "configuration": side["configuration"]},
            )
            if item.technology == "kd2" and "rules" in side and side["project"] in sources:
                for kind in ("exchange", "registration"):
                    file = side["rules"]["files"][kind]
                    if file["exists"] is True:
                        opened[side["project"], kind] = step(
                            "разбор", "rules_open", {"path": file["path"]}
                        )
            elif item.technology == "ed" and side.get("module"):
                opened[side["project"], "ed"] = step(
                    "разбор",
                    "ed_open",
                    {
                        "project": side["project"],
                        "module": side["module"],
                        "configuration": side["configuration"],
                    },
                )
                route_arguments = {
                    "project": side["project"],
                    "configuration": side["configuration"],
                }
                if side["plan"]:
                    route_arguments["plan"] = side["plan"]
                routed[side["project"]] = step("разбор", "ed_routes", route_arguments)

        directions = []
        for source, target in item.directions:
            left, right = by_project[source], by_project[target]
            direction = {"source": source, "target": target}
            for field, side, kind in (
                ("export", left, "exchange"),
                ("import", right, "correspondent"),
                ("registration", left, "registration"),
            ):
                direction[field] = side.get("rules", {}).get("files", {}).get(kind, {}).get("path")
            directions.append(direction)
            if item.technology == "kd2":
                n = opened.get((source, "exchange"))
                if n:
                    # Правила регистрации стороны дают pko_ref_only режим выгрузки узла.
                    paired = opened.get((source, "registration"))
                    step(
                        "проверка",
                        "rules_validate",
                        {
                            "source_structure": left["structure_id"],
                            "target_structure": right["structure_id"],
                        },
                        {"project_id": n, **({"exchange_project_id": paired} if paired else {})},
                    )
                if all(
                    s.get("rules", {}).get("files", {}).get(k, {}).get("exists") is True
                    for s, k in ((left, "exchange"), (right, "correspondent"))
                ):
                    step(
                        "проверка",
                        "rules_diff",
                        {"left": direction["export"], "right": direction["import"]},
                    )
        # Сравнение — на направление; схему открываем один раз на сторону из первого сравнения,
        # где она участвует: ответ несёт готовые аргументы и слева, и справа.
        schema_steps: dict[str, int] = {}
        for source, target in item.directions:
            if source not in routed or target not in routed:
                continue
            left, right = by_project[source], by_project[target]
            compare_arguments = {}
            if left["plan"]:
                compare_arguments["left_plan"] = left["plan"]
            if right["plan"]:
                compare_arguments["right_plan"] = right["plan"]
            compared = step(
                "разбор",
                "ed_route_compare",
                compare_arguments,
                {"left_profile_id": routed[source], "right_profile_id": routed[target]},
            )
            for project, side_name in ((source, "left"), (target, "right")):
                if project in schema_steps:
                    continue
                schema_steps[project] = step(
                    "разбор",
                    "ed_schema_open",
                    {},
                    {key: compared for key in _ED_SCHEMA_ARGUMENTS},
                    _ed_schema_note(side_name),
                )
        for side in sides:
            for kind, tool in (("registration", "rules_validate"), ("ed", "ed_validate")):
                n = opened.get((side["project"], kind))
                if n:
                    key = "structure_id" if kind == "ed" else "source_structure"
                    fill = {"project_id": n}
                    if kind == "ed" and side["project"] in schema_steps:
                        fill["schema_id"] = schema_steps[side["project"]]
                    step("проверка", tool, {key: side["structure_id"]}, fill)
        if item.technology is None:
            notes.append("Технологию покажет проверка базы.")
        elif item.technology == "ed":
            notes.append(_ED_VERSION_NOTE)
        for side in sides:
            if not side["base"]:
                continue
            base = catalog.project(side["project"]).bases[side["base"].split(".", 1)[1]]
            if not base.is_sandbox or not base.data_mcp:
                notes.append(
                    f"{side['base']}: живая проверка недоступна "
                    f"(роль «{base.role}», нужен data_mcp песочницы)."
                )
                continue
            args = {"base": side["base"], "plan": side["plan"]}
            step(
                "живая проверка",
                "live_base_check",
                {**args, "exchange": item.id},
                note="Технологию покажет проверка базы." if item.technology is None else None,
            )
            if item.technology is None:
                continue
            step("живая проверка", "live_status", args)
            if item.technology == "kd2":
                step("живая проверка", "live_rules_dump", args)
                if "rules" in side and side["rules"]["files"]["exchange"]["exists"] is not None:
                    step(
                        "живая проверка",
                        "live_rules_check",
                        {**args, "files": side["rules"]["folder"]},
                    )
        return {
            "id": item.id,
            "plan": item.plan,
            "technology": item.technology,
            "projects": list(item.projects),
            "sides": sides,
            "directions": directions,
            "steps": steps,
            "notes": notes,
        }
