"""Авторинг ED: прочитанные снимки, ограниченный кэш входов и запись только в workspace."""

import hashlib
import os
import shutil
import tempfile
from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kd2_rules_mcp.authoring.ed.artifacts import artifact_name, combine_artifacts, previous_artifact
from kd2_rules_mcp.authoring.ed.candidates import candidates, target_objects
from kd2_rules_mcp.authoring.ed.canonical import canonical_property
from kd2_rules_mcp.authoring.ed.manifest import ArtifactManifest, sha256
from kd2_rules_mcp.authoring.ed.model import (
    AddHeaderProperty,
    AttributeDraft,
    AuthoringInputs,
    AuthoringPreconditionError,
    AuthoringTarget,
    ExtensionIdentity,
    Failure,
    SourceSet,
    digest,
)
from kd2_rules_mcp.authoring.ed.render import render_authoring
from kd2_rules_mcp.authoring.ed.xml_dump import M, parse_xml, read_description
from kd2_rules_mcp.ed.address import AmbiguousAddressError, EntityNotFoundError
from kd2_rules_mcp.ed.schema import EdSchema
from kd2_rules_mcp.errors import (
    EdAuthoringAckRequiredError,
    EdAuthoringIoError,
    EdAuthoringPathError,
    EdAuthoringPreconditionError,
    EdAuthoringResourceLimitError,
    EdAuthoringStaleError,
)
from kd2_rules_mcp.projects import resolve
from kd2_rules_mcp.service import ed_authoring_views as views
from kd2_rules_mcp.service.ed import EdMixin
from kd2_rules_mcp.service.ed_routes import EdRoutesMixin, _is_stale, _load_for_uri, _RouteSnapshot
from kd2_rules_mcp.service.ed_schema import EdSchemaMixin
from kd2_rules_mcp.service.ed_views import validate_page
from kd2_rules_mcp.service.paths import Settings
from kd2_rules_mcp.structures.store import dump_fingerprint
from kd2_rules_mcp.validation.ed_authoring import prepare_authoring
from kd2_rules_mcp.validation.ed_routes import _format_uri
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key

MAX_OPERATIONS = 100
MAX_MANAGERS = 16
MAX_PROFILES = 64
MAX_FILES = 1000
MAX_BYTES = 32 * 1024 * 1024
MAX_INPUTS = 32
MAX_INPUT_BYTES = 128 * 1024 * 1024


@dataclass(slots=True)
class _Inputs:
    value: AuthoringInputs
    files: dict[Path, str]
    route: _RouteSnapshot
    roots: tuple[Path, ...]
    structure_id: str
    structure_fingerprint: str
    stored_bytes: int
    descriptions: dict[str, str]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: нужна непустая строка")
    return value


def _mapping(value: object, name: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"{name}: неверный набор полей")
    return value


def _failure(error: AuthoringPreconditionError) -> EdAuthoringPreconditionError:
    return EdAuthoringPreconditionError(
        "Предусловия авторинга не выполнены",
        {
            "failures": [
                {
                    "id": f.id,
                    "address": f.address,
                    "message": f.message,
                    "source": {"file": f.file, "line": f.line},
                }
                for f in error.failures
            ],
            "summary": {"failures": len(error.failures)},
        },
    )


class EdAuthoringMixin(EdRoutesMixin, EdSchemaMixin, EdMixin):
    """Входы кэшируются, проекта авторинга и draft_id в хранилище нет."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._authoring_inputs: OrderedDict[tuple, _Inputs] = OrderedDict()
        self._authoring_input_bytes = 0

    def _authoring_target(
        self, raw: object, project: str, configuration: str
    ) -> tuple[AuthoringTarget, tuple[str, str, str]]:
        value = _mapping(
            raw,
            "target",
            {
                "plan",
                "variant",
                "format_version",
                "direction",
                "pko_address",
                "project_id",
                "schema_id",
                "structure_id",
            },
        )
        try:
            refs = tuple(_text(value[k], k) for k in ("project_id", "schema_id", "structure_id"))
            variant = value.get("variant")
            if variant is not None:
                variant = _text(variant, "variant")
            direction = _text(value["direction"], "direction")
            if direction not in ("send", "receive"):
                raise ValueError("direction: send или receive")
            target = AuthoringTarget(
                project,
                configuration,
                _text(value["plan"], "plan"),
                variant,
                _text(value["format_version"], "format_version"),
                direction,
                _text(value["pko_address"], "pko_address"),
            )
        except KeyError as error:
            raise ValueError(f"Отсутствует поле target: {error.args[0]}") from error
        return target, (refs[0], refs[1], refs[2])

    def _authoring_operation(
        self, raw: object, project: str, configuration: str
    ) -> tuple[AddHeaderProperty, tuple[str, str, str]]:
        value = _mapping(
            raw,
            "operation",
            {"target", "configuration_attribute", "format_property", "new_attribute"},
        )
        try:
            target, refs = self._authoring_target(value["target"], project, configuration)
            draft = None
            if value.get("new_attribute") is not None:
                data = _mapping(
                    value["new_attribute"],
                    "new_attribute",
                    {"name", "synonym", "primitive", "qualifiers"},
                )
                if not isinstance(data.get("qualifiers"), dict):
                    raise ValueError("qualifiers: нужен словарь")
                primitive = _text(data["primitive"], "primitive")
                if primitive not in ("string", "boolean", "number", "date"):
                    raise ValueError("primitive: string, boolean, number или date")
                draft = AttributeDraft(
                    _text(data["name"], "name"),
                    _text(data["synonym"], "synonym"),
                    primitive,
                    data["qualifiers"],
                )
            return AddHeaderProperty(
                target,
                _text(value["configuration_attribute"], "configuration_attribute"),
                _text(value["format_property"], "format_property"),
                draft,
            ), refs
        except KeyError as error:
            raise ValueError(f"Отсутствует поле операции: {error.args[0]}") from error

    @staticmethod
    def _hash_file(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _verify_inputs(self, entry: _Inputs) -> None:
        changed = []
        try:
            changed = [self._host(p) for p, h in entry.files.items() if self._hash_file(p) != h]
            for extension in entry.roots[1:]:
                expected_files = {p for p in entry.files if p.is_relative_to(extension)}
                actual_files = {
                    p
                    for p in extension.rglob("*")
                    if p.is_file() and p.suffix.lower() in (".bsl", ".xml")
                }
                if actual_files != expected_files:
                    changed.append(self._host(extension))
            if _is_stale(entry.route):
                changed.append(self._host(entry.route.root))
            meta = self.store.meta(entry.structure_id)
            if (
                meta.get("input_hash") != entry.structure_fingerprint
                or dump_fingerprint(entry.roots) != entry.structure_fingerprint
            ):
                changed.append("structure:" + entry.structure_id)
        except OSError as error:
            raise EdAuthoringStaleError(
                "Входные файлы недоступны; перечитайте снимки", {}
            ) from error
        if changed:
            raise EdAuthoringStaleError(
                "Входные файлы изменились; перечитайте снимки", {"changed": changed}
            )

    def _inputs_for(self, project: str, configuration: str, refs: tuple[str, str, str]) -> _Inputs:
        # Открытые идентификаторы проверяются даже при попадании в кэш.
        doc_project = self._ed_project(refs[0])
        schema_project = self._schema_project(refs[1])
        self._require_structure(refs[2])
        key = (project, configuration, *refs)
        cached = self._authoring_inputs.get(key)
        if cached is not None:
            self._verify_inputs(cached)
            self._authoring_inputs.move_to_end(key)
            return cached
        root = self._project_root(project, configuration)
        route, _, stale = self._open_snapshot(root, project, configuration, False)
        if stale:
            raise EdAuthoringStaleError(
                "Снимок маршрутов изменился; повторите ed_routes с force", {}
            )
        config = self._catalog().configuration(project, configuration)
        folder = self.settings.project_dirs[project]
        roots = (root, *(resolve(folder, p).resolve() for p in config.extensions))
        meta = self.store.meta(refs[2])
        expected = meta.get("input_hash", "")
        if (
            meta.get("source") != "xml"
            or Path(meta.get("source_path", "")).resolve() != root
            or expected != dump_fingerprint(roots)
        ):
            raise _failure(
                AuthoringPreconditionError(
                    (
                        Failure(
                            "ed.author.snapshot_mismatch",
                            "",
                            "Структура не соответствует выбранной выгрузке и расширениям",
                        ),
                    )
                )
            )
        document = doc_project.document
        selected = schema_project.schema
        if not all(
            Path(s.path).resolve().is_relative_to(root / "XDTOPackages")
            for s in selected.packages[0].sources
        ):
            raise AuthoringPreconditionError(
                (
                    Failure(
                        "ed.author.snapshot_mismatch",
                        "",
                        "Выбранная схема прочитана из другой выгрузки",
                    ),
                )
            )
        paths: dict[Path, str] = {doc_project.path: document.files[0].sha256}
        paths[self.settings.projects_file.resolve()] = self._hash_file(self.settings.projects_file)
        schemas: dict[str, EdSchema | str] = {schema_project.format_version: selected}
        manager = next(
            (
                m
                for m in route.profile.managers
                if m.path and (root / m.path).resolve() == doc_project.path
            ),
            None,
        )
        versions = {
            e.key
            for e in (
                *(e for p in route.profile.plans for e in p.entries),
                *route.profile.without_node_entries,
            )
            if manager and e.manager_name and e.manager_name.casefold() == manager.name.casefold()
        }
        self._limit(len(versions), MAX_PROFILES, "профили версий")
        for version in sorted(versions - {schema_project.format_version}):
            uris = {
                uri
                for plan in route.profile.plans
                if any(
                    e.key == version and manager and e.manager_name == manager.name
                    for e in plan.entries
                )
                if (uri := _format_uri(plan.base_namespace, version)) is not None
            }
            if len(uris) == 1:
                loaded = _load_for_uri(route, next(iter(uris)))
                schemas[version] = loaded if isinstance(loaded, EdSchema) else loaded.reason
            else:
                schemas[version] = "Пакет версии отсутствует или неоднозначен"
        for schema in schemas.values():
            if isinstance(schema, EdSchema):
                paths.update({Path(s.path): s.sha256 for p in schema.packages for s in p.sources})
        extension_sources: dict[str, str] = {}
        for number, extension in enumerate(roots[1:]):
            for path in sorted(extension.rglob("*")):
                if path.is_file() and path.suffix.lower() in (".bsl", ".xml"):
                    text = path.read_text("utf-8-sig")
                    extension_sources[
                        f"extensions/{number}/" + path.relative_to(extension).as_posix()
                    ] = text
                    paths[path] = self._hash_file(path)
        structure, _ = self._ed_structure_snapshot(refs[2])
        source = SourceSet(
            project,
            configuration,
            digest(
                (
                    document.parser_version,
                    document.manager_version,
                    tuple((f.path, f.sha256) for f in document.files),
                )
            ),
            digest(
                tuple(
                    (v, s.schema_id if isinstance(s, EdSchema) else s)
                    for v, s in sorted(schemas.items())
                )
            ),
            expected,
            route.profile.sources_fingerprint,
            tuple(config.extensions),
            digest(extension_sources),
        )
        value = AuthoringInputs(
            document, schemas, structure, route.profile, source, extension_sources
        )
        size = (
            sum(len(s.text.encode("utf-8")) for s in document.files)
            + sum(
                s.bytes
                for schema in schemas.values()
                if isinstance(schema, EdSchema)
                for p in schema.packages
                for s in p.sources
            )
            + sum(len(t.encode("utf-8")) for t in extension_sources.values())
        )
        self._limit(size, MAX_INPUT_BYTES, "байты входов")
        entry = _Inputs(value, paths, route, roots, refs[2], expected, size, {})
        self._verify_inputs(entry)
        while self._authoring_inputs and (
            len(self._authoring_inputs) >= MAX_INPUTS
            or self._authoring_input_bytes + size > MAX_INPUT_BYTES
        ):
            _, old = self._authoring_inputs.popitem(last=False)
            self._authoring_input_bytes -= old.stored_bytes
        self._authoring_inputs[key] = entry
        self._authoring_input_bytes += size
        return entry

    @staticmethod
    def _limit(count: int, maximum: int, name: str) -> None:
        if count > maximum:
            raise EdAuthoringResourceLimitError(
                f"Превышен предел: {name} ({maximum})",
                {"resource": name, "limit": maximum, "actual": count},
            )

    def ed_authoring_candidates(
        self,
        target: dict,
        kind: str,
        text: str = "",
        offset: int = 0,
        limit: int = 50,
        configuration_attribute: str | None = None,
        format_property: str | None = None,
    ) -> dict[str, Any]:
        validate_page(offset, limit)
        if kind not in ("format", "configuration") or not isinstance(text, str):
            raise ValueError("kind: format или configuration; text: строка")
        value = dict(
            _mapping(
                target,
                "target",
                {
                    "project",
                    "configuration",
                    "plan",
                    "variant",
                    "format_version",
                    "direction",
                    "pko_address",
                    "project_id",
                    "schema_id",
                    "structure_id",
                },
            )
        )
        project = _text(value.pop("project", None), "project")
        configuration = _text(value.pop("configuration", None), "configuration")
        resolved, refs = self._authoring_target(value, project, configuration)
        for name, item in (
            ("configuration_attribute", configuration_attribute),
            ("format_property", format_property),
        ):
            if item is not None:
                _text(item, name)
        with self._lock:
            try:
                entry = self._inputs_for(project, configuration, refs)
                self._require_selected_schema(resolved, refs, entry.value)
                try:
                    rule, profile, applicable, typ, owner = target_objects(entry.value, resolved)
                except (EntityNotFoundError, AmbiguousAddressError) as error:
                    identifier = (
                        "pko_ambiguous"
                        if isinstance(error, AmbiguousAddressError)
                        else "pko_missing"
                    )
                    raise AuthoringPreconditionError(
                        (
                            Failure(
                                "ed.author." + identifier,
                                resolved.pko_address,
                                str(error),
                                self._host(self._ed_project(refs[0]).path),
                                1,
                            ),
                        )
                    ) from error
                skipped = []
                state = applicable.evaluate(rule, resolved.direction)
                if typ is None or owner is None or state is not True:
                    skipped.append(
                        {
                            "check": "ed.author.candidates",
                            "reason": "Тип, владелец или применимость ПКО не подтверждены",
                        }
                    )
                if owner and configuration_attribute:
                    actual = owner.property(configuration_attribute.strip(" "))
                    if len(actual) == 1:
                        configuration_attribute = actual[0].path
                if typ and format_property:
                    format_property = (
                        canonical_property(profile, typ, format_property) or format_property
                    )
                canonical_target = dict(target)
                from kd2_rules_mcp.ed.address import build_addresses

                canonical_target["pko_address"] = build_addresses(entry.value.document).by_id[
                    rule.entity_id
                ][0]
                canonical_target["plan"] = next(
                    (
                        p.plan_name
                        for p in entry.value.routes.plans
                        if p.plan_name.casefold() == resolved.plan.casefold()
                    ),
                    resolved.plan,
                )
                found = candidates(
                    entry.value,
                    resolved,
                    kind,
                    configuration_attribute=configuration_attribute,
                    format_property=format_property,
                    text=text,
                )
                rows = [
                    {
                        "name": c.name,
                        "path": c.path,
                        "type": list(c.types),
                        "qualifiers": dict(c.qualifiers),
                        "occupied": False,
                        "compatible": c.compatible,
                        "reason": c.reason,
                        "source": c.source,
                        "auto": c.auto,
                    }
                    for c in found
                ]
                return {
                    "target": canonical_target,
                    "scope": {"version_scope": "manager"},
                    "skipped": skipped,
                    **views.page(rows, offset, limit),
                }
            except AuthoringPreconditionError as error:
                raise _failure(error) from error
            except (OSError, UnicodeError) as error:
                raise EdAuthoringIoError(
                    "Ошибка чтения входов авторинга",
                    {"path": self._host_text(str(getattr(error, "filename", "") or ""))},
                ) from error

    def _descriptions(
        self, entry: _Inputs, operations: tuple[AddHeaderProperty, ...]
    ) -> dict[str, str]:
        root = entry.route.root
        needed = {"Configuration.xml"}
        module = (
            entry.value.document.files[0]
            .path.replace("\\", "/")
            .split("CommonModules/")[-1]
            .split("/")[0]
        )
        needed.add("CommonModules/" + module + ".xml")
        configuration_text = entry.descriptions.get("Configuration.xml") or (
            root / "Configuration.xml"
        ).read_text("utf-8-sig")
        language = parse_xml("Configuration.xml", configuration_text)[0].findtext(
            f"{{{M}}}Properties/{{{M}}}DefaultLanguage", ""
        )
        needed.add("Languages/" + language.removeprefix("Language.") + ".xml")
        from kd2_rules_mcp.ed.address import build_addresses

        index = build_addresses(entry.value.document)
        for op in operations:
            if op.new_attribute:
                rule = index.find(op.target.pko_address)
                from kd2_rules_mcp.ed.model import ObjectRule

                assert isinstance(rule, ObjectRule)
                key, _ = metadata_key(rule.configuration_object.value)
                if key:
                    owner = entry.value.structure.objects.get(key)
                    if owner:
                        kind = "Catalogs" if key[0] == "справочник" else "Documents"
                        needed.add(kind + "/" + owner.name + ".xml")
        descriptions = {}
        for name in sorted(needed):
            path = root / name
            actual = self._hash_file(path)
            if path in entry.files and entry.files[path] != actual:
                raise EdAuthoringStaleError(
                    "Описание метаданных изменилось", {"changed": [self._host(path)]}
                )
            descriptions[name] = entry.descriptions.get(name) or path.read_text("utf-8-sig")
            entry.descriptions[name] = descriptions[name]
            entry.files[path] = actual
        return descriptions

    def _require_selected_schema(
        self, target: AuthoringTarget, refs: tuple[str, str, str], inputs: AuthoringInputs
    ) -> None:
        selected = self._schema_project(refs[1])
        plans = [p for p in inputs.routes.plans if p.plan_name.casefold() == target.plan.casefold()]
        expected_uri = (
            _format_uri(plans[0].base_namespace, target.format_version) if len(plans) == 1 else None
        )
        if selected.format_version != target.format_version or (
            expected_uri is not None and selected.schema.base_namespace != expected_uri
        ):
            raise AuthoringPreconditionError(
                (
                    Failure(
                        "ed.author.snapshot_mismatch",
                        target.pko_address,
                        "schema_id не соответствует выбранной версии и URI маршрута",
                    ),
                )
            )

    def _destination(self, name: str, output_dir: str | None) -> Path:
        root = self.workspace.root.absolute()
        target = root / "ed-authoring" / name
        if output_dir is not None:
            _text(output_dir, "output_dir")
            supplied = self._local(output_dir)
            if supplied is None:
                raise EdAuthoringPathError("Каталог комплекта вне workspace", {})
            supplied = supplied if supplied.is_absolute() else root / supplied
            if supplied.absolute() != target.absolute():
                raise EdAuthoringPathError("Разрешён только вычисленный каталог комплекта", {})
        self._safe_path(target)
        return target

    def _safe_path(self, target: Path) -> None:
        root = self.workspace.root.absolute()
        if not target.absolute().is_relative_to(root):
            raise EdAuthoringPathError("Каталог комплекта вне workspace", {})
        for item in (target, *target.parents):
            if item.is_symlink() or item.is_junction():
                raise EdAuthoringPathError("Символические ссылки и junction запрещены", {})
        if not target.resolve().is_relative_to(root.resolve()):
            raise EdAuthoringPathError("Каталог комплекта вне workspace", {})

    def _previous(self, destination: Path) -> tuple[ArtifactManifest | None, dict[str, bytes]]:
        self._safe_path(destination)
        if not destination.exists():
            return None, {}
        if not destination.is_dir():
            raise EdAuthoringPathError("Каталог комплекта занят чужим файлом", {})
        files = {}
        for path in destination.rglob("*"):
            self._safe_path(path)
            if path.is_file():
                files[path.relative_to(destination).as_posix()] = path.read_bytes()
                self._limit(len(files), MAX_FILES, "файлы прежнего комплекта")
                self._limit(
                    sum(len(b) for b in files.values()), MAX_BYTES, "байты прежнего комплекта"
                )
        if files and "manifest.json" not in files:
            raise EdAuthoringPathError("Каталог содержит чужие файлы", {})
        if "manifest.json" in files:
            manifest = ArtifactManifest.from_bytes(files["manifest.json"])
            if set(files) - {*manifest.file_hashes, "manifest.json"}:
                raise EdAuthoringPathError("Каталог содержит чужие файлы", {})
            allowed_dirs = {
                parent.as_posix()
                for name in manifest.file_hashes
                for parent in Path(name).parents
                if str(parent) != "."
            }
            if any(
                p.relative_to(destination).as_posix() not in allowed_dirs
                for p in destination.rglob("*")
                if p.is_dir()
            ):
                raise EdAuthoringPathError("Каталог содержит чужие подкаталоги", {})
        elif any(destination.iterdir()):
            raise EdAuthoringPathError("Каталог содержит чужие подкаталоги", {})
        return previous_artifact(files), files

    def _write_artifact(
        self,
        destination: Path,
        files: Mapping[str, bytes],
        previous: Mapping[str, bytes],
        entries: list[_Inputs],
    ) -> str:
        self._safe_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=destination.parent))
        backup = staging.with_name(staging.name + "-previous")
        moved = False
        try:
            for name, content in files.items():
                path = staging / name
                self._safe_path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            for entry in entries:
                self._verify_inputs(entry)
            try:
                _, current = self._previous(destination)
            except (AuthoringPreconditionError, EdAuthoringPathError) as error:
                raise EdAuthoringStaleError(
                    "Прежний комплект изменился во время записи", {}
                ) from error
            if current != dict(previous):
                raise EdAuthoringStaleError("Прежний комплект изменился во время записи", {})
            if dict(files) == current:
                return "unchanged"
            self._safe_path(destination)
            if destination.exists():
                os.replace(destination, backup)
                moved = True
            try:
                if moved:
                    try:
                        _, saved = self._previous(backup)
                        if saved != dict(previous):
                            raise EdAuthoringStaleError("Комплект изменился при переименовании", {})
                    except (AuthoringPreconditionError, EdAuthoringPathError) as error:
                        raise EdAuthoringStaleError(
                            "Комплект изменился при переименовании", {}
                        ) from error
                os.replace(staging, destination)
            except (OSError, EdAuthoringStaleError):
                if moved:
                    os.replace(backup, destination)
                    moved = False
                raise
            if moved:
                # Публикация завершена; недоступный для удаления backup не отменяет её.
                with suppress(OSError):
                    shutil.rmtree(backup)
            return "written"
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _previous_refs(
        self, operation: AddHeaderProperty, entries: dict[tuple[str, str, str], _Inputs]
    ) -> tuple[str, str, str]:
        """После перезапуска прежние решения восстанавливаются по карте, без хранимого draft."""
        entry = next(iter(entries.values()))
        target = operation.target
        plans = [
            p for p in entry.value.routes.plans if p.plan_name.casefold() == target.plan.casefold()
        ]
        names = {
            e.manager_name
            for p in plans
            for e in p.entries
            if e.key == target.format_version and e.state == "effective" and e.manager_name
        }
        managers = [m for m in entry.value.routes.managers if m.name in names and m.path]
        uris = {_format_uri(p.base_namespace, target.format_version) for p in plans}
        packages = [p for p in entry.value.routes.packages if p.namespace in uris]
        if len(managers) != 1 or len(packages) != 1:
            raise AuthoringPreconditionError(
                (
                    Failure(
                        "ed.author.route_unresolved",
                        target.pko_address,
                        "Прежний менеджер или выбранная схема не разрешены однозначно",
                    ),
                )
            )
        opened = self.ed_open(self._host(entry.route.root / (managers[0].path or "")))
        if opened["source_changed"]:
            raise EdAuthoringStaleError("Прежний менеджер изменился; перечитайте снимок", {})
        schema = self.ed_schema_open(
            target.format_version,
            project=target.project,
            configuration=target.configuration,
            package=packages[0].metadata_name,
        )
        if schema["source_changed"]:
            raise EdAuthoringStaleError("Прежняя схема изменилась; перечитайте снимок", {})
        return opened["project_id"], schema["schema_id"], entry.structure_id

    def ed_authoring_build(
        self,
        project: str,
        configuration: str,
        extension: dict,
        operations: list[dict],
        version_scope: str | None = None,
        mode: str = "preview",
        delivery: str = "extension",
        output_dir: str | None = None,
        expected_preview_hash: str | None = None,
        acknowledged_notices: list[str] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        validate_page(offset, limit)
        _text(project, "project")
        _text(configuration, "configuration")
        if mode not in ("preview", "write"):
            raise ValueError("mode: preview или write")
        if delivery == "extension_with_load":
            raise ValueError("extension_with_load появится вместе со скриптом загрузки")
        if delivery not in ("extension", "manual"):
            raise ValueError("delivery: extension или manual")
        if version_scope not in (None, "manager"):
            raise ValueError("version_scope: manager или null")
        if not isinstance(operations, list) or not operations:
            raise ValueError("Нужен непустой список операций")
        self._limit(len(operations), MAX_OPERATIONS, "операции")
        identity_data = _mapping(
            extension, "extension", {"name", "prefix", "synonym", "version", "compatibility_mode"}
        )
        try:
            identity = ExtensionIdentity(**identity_data)
            for key in ("name", "prefix", "version"):
                _text(getattr(identity, key), key)
            if not isinstance(identity.synonym, str) or (
                identity.compatibility_mode is not None
                and not isinstance(identity.compatibility_mode, str)
            ):
                raise ValueError("Неверные поля extension")
        except TypeError as error:
            raise ValueError("Неверные поля extension") from error
        if acknowledged_notices is None:
            acknowledged_notices = []
        if not isinstance(acknowledged_notices, list) or any(
            not isinstance(n, str) for n in acknowledged_notices
        ):
            raise ValueError("acknowledged_notices: нужен список строк")
        if expected_preview_hash is not None:
            _text(expected_preview_hash, "expected_preview_hash")
        parsed = [self._authoring_operation(o, project, configuration) for o in operations]
        groups: dict[tuple[str, str, str], list[AddHeaderProperty]] = defaultdict(list)
        for operation, refs in parsed:
            groups[refs].append(operation)
        self._limit(len({r[0] for r in groups}), MAX_MANAGERS, "менеджеры")
        with self._lock:
            try:
                entries = {refs: self._inputs_for(project, configuration, refs) for refs in groups}
                manager_versions: dict[str, str] = {}
                for op, refs in parsed:
                    manager_path = entries[refs].value.document.files[0].path
                    if (
                        manager_path in manager_versions
                        and manager_versions[manager_path] != op.target.format_version
                    ):
                        raise AuthoringPreconditionError(
                            (
                                Failure(
                                    "ed.author.scope_required",
                                    op.target.pko_address,
                                    "операции одного менеджера проверяются "
                                    "по одной выбранной версии; "
                                    "остальные версии показываются как другие",
                                ),
                            )
                        )
                    manager_versions[manager_path] = op.target.format_version
                root = next(iter(entries.values())).route.root
                first_entry = next(iter(entries.values()))
                config_description = read_description(
                    "Configuration.xml",
                    first_entry.descriptions.get("Configuration.xml")
                    or (root / "Configuration.xml").read_text("utf-8-sig"),
                    "Configuration",
                )
                destination = self._destination(
                    artifact_name(identity.name, config_description.uuid), output_dir
                )
                previous, previous_files = self._previous(destination)
                if previous:
                    if previous.identity != identity:
                        raise _failure(
                            AuthoringPreconditionError(
                                (
                                    Failure(
                                        "ed.author.owned_content_changed",
                                        "",
                                        "Идентичность прежнего комплекта отличается",
                                    ),
                                )
                            )
                        )
                    for op in previous.operations:
                        matches = [
                            refs
                            for refs, entry in entries.items()
                            if any(
                                p.plan_name == op.target.plan
                                and any(
                                    e.key == op.target.format_version
                                    and e.manager_name
                                    and entry.value.document.files[0]
                                    .path.replace("\\", "/")
                                    .endswith("CommonModules/" + e.manager_name + "/Ext/Module.bsl")
                                    for e in p.entries
                                )
                                for p in entry.value.routes.plans
                            )
                        ]
                        if not matches:
                            refs = self._previous_refs(op, entries)
                            entries[refs] = self._inputs_for(project, configuration, refs)
                            matches = [refs]
                        if len(matches) != 1:
                            raise ValueError("Несколько снимков прежнего менеджера в одном вызове")
                        groups[matches[0]].append(op)
                bundles = []
                preparations = []
                self._limit(len({r[0] for r in entries}), MAX_MANAGERS, "менеджеры")
                for refs, ops in groups.items():
                    entry = entries[refs]
                    for op in ops:
                        self._require_selected_schema(op.target, refs, entry.value)
                    prepared = prepare_authoring(
                        entry.value, tuple(ops), identity, version_scope=version_scope
                    )
                    preparations.append(prepared)
                    bundles.append(
                        render_authoring(
                            prepared,
                            self._descriptions(entry, prepared.operations),
                            delivery=delivery,
                        )
                    )
                self._limit(
                    sum(len(p.selected_profiles) + len(p.other_profiles) for p in preparations),
                    MAX_PROFILES,
                    "профили версий",
                )
                bundle = combine_artifacts(bundles)
                if previous and (
                    previous.identity_map.artifact_uuid
                    != bundle.manifest.identity_map.artifact_uuid
                    or any(
                        bundle.manifest.identity_map.objects.get(p) != v
                        for p, v in previous.identity_map.objects.items()
                    )
                    or any(
                        bundle.manifest.identity_map.borrowed.get(p) != v
                        for p, v in previous.identity_map.borrowed.items()
                    )
                ):
                    raise AuthoringPreconditionError(
                        (
                            Failure(
                                "ed.author.owned_content_changed",
                                "",
                                "Карта UUID прежнего комплекта отличается "
                                "от порождённой идентичности",
                            ),
                        )
                    )
                if previous and (
                    any(
                        getattr(previous.source_set, field)
                        != getattr(bundle.manifest.source_set, field)
                        for field in (
                            "project",
                            "configuration",
                            "structure_hash",
                            "routes_hash",
                            "extensions",
                            "extensions_hash",
                        )
                    )
                    or any(
                        bundle.manifest.source_hashes.get(p) != h
                        for p, h in previous.source_hashes.items()
                    )
                ):
                    raise EdAuthoringStaleError("Входы прежнего комплекта изменились", {})
                self._limit(len(bundle.files), MAX_FILES, "файлы комплекта")
                self._limit(
                    sum(len(b) for b in bundle.files.values()), MAX_BYTES, "байты комплекта"
                )
                build_hash = sha256(bundle.manifest.to_bytes())
                for entry in entries.values():
                    self._verify_inputs(entry)
                status = "ready"
                if mode == "write":
                    if expected_preview_hash != build_hash:
                        raise EdAuthoringStaleError(
                            "Нужен expected_preview_hash текущего preview",
                            {"preview_hash": build_hash},
                        )
                    missing = sorted(set(bundle.manifest.notices) - set(acknowledged_notices))
                    if missing:
                        raise EdAuthoringAckRequiredError(
                            "Замечания требуют явного подтверждения",
                            {"required_acknowledgements": missing, "preview_hash": build_hash},
                        )
                    status = self._write_artifact(
                        destination, bundle.files, previous_files, list(entries.values())
                    )
                result = views.build_view(
                    bundle,
                    preparations,
                    build_hash=build_hash,
                    status=status,
                    output_path=self._host(destination),
                    written=mode == "write",
                    offset=offset,
                    limit=limit,
                    inputs=[entries[refs].value for refs in groups],
                )
                for scope in result["scopes"]:
                    scope["manager_path"] = self._host_text(scope["manager_path"])
                return result
            except AuthoringPreconditionError as error:
                mapped = _failure(error)
                for failure in mapped.details["failures"]:
                    failure["source"]["file"] = self._host_text(failure["source"]["file"])
                raise mapped from error
            except (OSError, UnicodeError) as error:
                raise EdAuthoringIoError(
                    "Ошибка чтения или атомарной записи комплекта",
                    {"path": self._host_text(str(getattr(error, "filename", "") or ""))},
                ) from error
