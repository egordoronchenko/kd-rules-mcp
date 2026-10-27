"""`live_format_support`: что песочница знает об объектах формата корреспондента."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from kd_rules_mcp.errors import LiveParseError
from kd_rules_mcp.live.codegen.format_support import format_support_code
from kd_rules_mcp.live.runs import execute
from kd_rules_mcp.service.base import ServiceBase
from kd_rules_mcp.service.views import slice_rows

if TYPE_CHECKING:
    from kd_rules_mcp.service.live import LiveMixin

NOTE = (
    "Таблица — ОбменДаннымиXDTOСервер.ПоддерживаемыеОбъектыФорматаКорреспондента "
    "(XDTO:2971–3013). settings_received=false: корреспондент своих настроек не присылал, "
    "база подставляет свой состав зеркально (отправка ↔ получение), пересечение полное; "
    "при незавершённой настройке таблица пуста. Список корреспондента обновляется, только "
    "когда от него приходит сообщение (XDTO:8194–8199)."
)


def _rows(node: dict) -> list[dict[str, Any]]:
    rows = node.get("objects")
    if not isinstance(rows, list):
        raise LiveParseError("неверный ответ: objects")
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("version"), str)
            or not isinstance(row.get("object"), str)
            or not isinstance(row.get("send"), bool)
            or not isinstance(row.get("receive"), bool)
        ):
            raise LiveParseError("неверная строка таблицы объектов формата")
    return rows


class LiveFormatMixin(ServiceBase):
    """Только чтение песочницы; база задаётся ссылкой `<проект>.<база>`."""

    def live_format_support(
        self,
        base: str,
        plan: str,
        node: str | None = None,
        format_object: str | None = None,
        version: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not isinstance(plan, str) or not plan.strip():
            raise ValueError("Нужно имя плана обмена")
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("offset ≥ 0, limit 1…200")
        live = cast("LiveMixin", self)
        with self._lock:
            access = live._live_access(base)
            run = live._live_run("live_format_support")
            payload = run.stage(
                "ОБЪЕКТЫ ФОРМАТА",
                lambda: execute(access, format_support_code(plan.strip(), node)),
            )
            if payload is None:
                return run.result()
            if (
                not isinstance(payload.get("exists"), bool)
                or not isinstance(payload.get("xdto"), bool)
                or not isinstance(payload.get("nodes"), list)
            ):
                raise LiveParseError("неверный ответ: exists, xdto, nodes")
            nodes = []
            for item in payload["nodes"]:
                if not isinstance(item, dict) or not isinstance(item.get("code"), str):
                    raise LiveParseError("неверный узел")
                rows = _rows(item)
                total = len(rows)
                if format_object is not None:
                    rows = [r for r in rows if r["object"] == format_object.strip()]
                selected = version or item.get("format_version") or None
                if version is not None:
                    rows = [r for r in rows if r["version"] == version.strip()]
                versions = sorted({r["version"] for r in _rows(item)})
                nodes.append(
                    {
                        "code": item["code"],
                        "name": item.get("name"),
                        "format_version": item.get("format_version") or None,
                        "settings_received": item.get("settings_received"),
                        "setup_complete": item.get("setup_complete"),
                        "versions": versions,
                        "total": total,
                        "node_version_rows": sum(r["version"] == selected for r in rows)
                        if selected
                        else None,
                        "objects": slice_rows(rows, offset, limit),
                    }
                )
            extra = {"node_found": bool(nodes)} if node is not None else {}
            return run.result(
                base=base,
                plan=plan.strip(),
                exists=payload["exists"],
                xdto=payload["xdto"],
                **extra,
                nodes=nodes,
                note=NOTE,
            )
