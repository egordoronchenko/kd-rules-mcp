"""Долговечные подсказки открытия входов менеджера при отказе после перезапуска."""

from functools import wraps
from inspect import signature
from pathlib import PurePosixPath
from typing import Any, cast

from kd_rules_mcp.errors import (
    EdAuthoringPreconditionError,
    EdSchemaNotFoundError,
    StructureNotFoundError,
)


def reopen_details(arguments, metadata, *, schema=False, structure=False):
    calls, missing = [], []
    if schema:
        params = {"format_version": arguments.get("format_version")}
        packages = metadata.get("schema_packages", [])
        sources = metadata.get("schema_sources", [])
        if arguments.get("project"):
            # Старые снимки хранят имя лишь в пути выгрузки; путь хоста бывает Windows.
            package = packages[0].get("name") if packages else None
            source = packages[0]["path"] if packages else sources[0][0] if sources else None
            source_path = PurePosixPath(source.replace("\\", "/")) if source else None
            if not package and source_path and source_path.suffix.casefold() == ".xml":
                package = source_path.stem
            elif (
                not package
                and source_path
                and source_path.name.casefold() == "package.bin"
                and source_path.parent.name.casefold() == "ext"
            ):
                package = source_path.parent.parent.name
            params.update(
                project=arguments["project"],
                configuration=arguments.get("configuration", "full"),
                package=package,
            )
        else:
            if packages:
                params.update(
                    path=packages[0]["path"],
                    imports={
                        p["namespace"]: p["path"] for p in packages[1:] if p["role"] == "dependency"
                    },
                    extensions=[p["path"] for p in packages if p["role"] == "extension"],
                )
            elif sources:
                params["path"] = sources[0][0]
        absent = []
        if not params["format_version"]:
            absent.append("format_version")
        if "project" in params and not params["package"]:
            absent.append("package")
        if "project" not in params and "path" not in params:
            absent.append("project/package or path")
        if absent:
            missing.append({"tool": "ed_schema_open", "fields": absent})
        else:
            calls.append({"tool": "ed_schema_open", "arguments": params})
    if structure:
        params = {"structure_id": arguments.get("structure_id")}
        if arguments.get("project"):
            params.update(
                project_id=arguments["project"],
                configuration_id=arguments.get("configuration", "full"),
            )
            tool = "structure_load_project"
        else:
            params.update(
                configuration_path=arguments.get("configuration_path"),
                extension_paths=arguments.get("extensions", []),
            )
            tool = "structure_load_xml"
        if not params["structure_id"] or (
            tool == "structure_load_xml" and not params.get("configuration_path")
        ):
            missing.append(
                {"tool": tool, "fields": ["structure_id", "project/configuration or path"]}
            )
        else:
            calls.append({"tool": tool, "arguments": params})
    return {"reopen_calls": calls, **({"missing_reopen_parameters": missing} if missing else {})}


def with_reopen_hints(method):
    """Дополняет существующий отказ, не меняя код, сигнатуру и порядок работы инструмента."""
    contract = signature(method)

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (
            EdSchemaNotFoundError,
            StructureNotFoundError,
            EdAuthoringPreconditionError,
        ) as error:
            details = getattr(error, "details", {})
            schema = isinstance(error, EdSchemaNotFoundError) or any(
                str(f.get("id", "")).endswith("schema_snapshot_unavailable")
                for f in details.get("failures", [])
                if isinstance(f, dict)
            )
            structure = isinstance(error, StructureNotFoundError)
            if not schema and not structure:
                raise
            if details.get("reopen_calls"):
                raise
            bound = contract.bind(self, *args, **kwargs).arguments
            project_id = bound.get("project_id") or (bound.get("target") or {}).get("project_id")
            project = self._ed_projects.get(project_id)
            if project and project.manager_project_id:
                project_id = project.manager_project_id
            metadata = {}
            if project_id in self.manager_workspace.ids():
                metadata = self._manager_metadata(project_id)
            params = dict(metadata.get("arguments", {}))
            params.setdefault("structure_id", bound.get("structure_id"))
            schema = schema or bool(
                params.get("schema_id") and params["schema_id"] not in self._ed_schemas
            )
            structure = structure or bool(
                params.get("structure_id") and not self.store.exists(params["structure_id"])
            )
            details.update(reopen_details(params, metadata, schema=schema, structure=structure))
            if details.get("missing_reopen_parameters"):
                error.args = (
                    str(error)
                    + "; отсутствуют параметры повторного открытия: "
                    + str(details["missing_reopen_parameters"]),
                )
            cast(Any, error).reopen_details = details
            raise

    return wrapped
